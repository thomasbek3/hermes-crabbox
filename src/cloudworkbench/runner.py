"""Single fenced scheduler/supervisor. This is the only service with Docker authority."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import time
import tempfile

from .adapters import capabilities, read_event_spool, EventSpoolError, redact_public, read_credential, usage_status, FAILURE_CODES, _open_directory_nofollow
from .artifacts import export_workspace, adopt_export, ExportError, DEPENDENCY_EXCLUSIONS
from .runtime import Runtime, RuntimeError as DockerRuntimeError, RUNTIME_ERROR_CODES, DOCKER_OPERATIONS
from .credential_state import CredentialState, CredentialStateError
from .repositories import snapshot as repository_snapshot, save_baseline, load_baseline, initialize_workspace, text_patch
from .store import Store, StoreError
from .models import TERMINAL
from .resource_monitor import ResourceMonitor, Binding
from types import SimpleNamespace
from .environments import EnvironmentRegistry, EnvironmentError, validate_manifest


def write_json(path: Path, value: dict, mode: int = 0o640):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.new')
    with open(tmp, 'w') as out:
        os.chmod(tmp, mode)
        json.dump(value, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


class PolicyRejected(ValueError):
    pass


class Runner:
    def __init__(self, store: Store, config: dict, runtime=None):
        self.store, self.config = store, config
        self.root = Path(config['state_root'])
        if runtime is None and config.get('hermes_enabled') is True:
            from .hermes_coordinator_runtime import HermesCoordinatorRuntime
            hermes_config = config.get('hermes_runtime')
            if not isinstance(hermes_config, dict):
                raise PolicyRejected('Qualified Hermes runtime configuration required')
            runtime_class = HermesCoordinatorRuntime
            if hermes_config.get('crabbox_enabled') is True:
                from .crabbox_runtime import CrabboxRuntime
                runtime_class = CrabboxRuntime
            self.runtime = runtime_class({**config['runtime'], **hermes_config,
                'hermes_journal_root': str(self.root / 'hermes-runtime')})
        else:
            self.runtime = runtime or Runtime(config['runtime'])
        verify_config = {**config['runtime'], 'network_enabled': False, 'workspace_readonly': True}
        self.verifier = runtime or Runtime(verify_config)
        self.token_path = Path(config['claude_token']) if config.get('claude_token') else None
        self.secret, self.credential_error = read_credential(self.token_path)
        self.credential_state_path = self.root / 'credential-state' / 'claude.json'
        self.credential_state = CredentialState(self.credential_state_path,self.secret)
        self.forbidden = (self.secret,) if self.secret else ()
        self._credential_collection_failed = False
        self._terminal_cleanup_scans = {}
        for child in ('tasks', 'artifacts', 'results', 'logs', 'baselines'):
            (self.root / child).mkdir(exist_ok=True)
        self.started = time.monotonic()
        self.resources = None
        self._resource_cursor = None
        self._resource_health = {'status':'disabled','reason':'configuration_disabled','drops':{}}
        enabled = config.get('resource_sampling_enabled',True)
        if type(enabled) is not bool:
            self._resource_health['reason'] = 'invalid_configuration'
        elif enabled:
            try:
                self.resources = ResourceMonitor(store.resource_attempt, store.append_resource_sample,
                    cadence_seconds=config.get('resource_sampling_cadence_seconds',10))
                self._resource_health.update(status='idle',reason=None)
            except (ValueError,TypeError):
                self._resource_health['reason'] = 'invalid_configuration'

    def change(self, a, state, **fields):
        return self.store.transition(a['id'], state, expected_generation=a['generation'], **fields)

    def event(self, a, kind, payload, dedupe_key=None):
        self.store.append_event(a['id'], kind, payload, expected_generation=a['generation'], dedupe_key=dedupe_key)

    @staticmethod
    def error_diagnostic(exc):
        code = getattr(exc,'code',None)
        operation = getattr(exc,'operation',None)
        spool_codes = {
            'event_spool_invalid_name','event_spool_invalid_record','event_spool_line_limit',
            'event_spool_total_limit','event_spool_write_failed','event_spool_missing',
            'event_spool_unsafe_path','event_spool_unsafe_file','event_spool_replaced',
            'event_spool_truncated','event_spool_prefix_changed','event_spool_incomplete_line',
            'event_spool_record_limit','provider_output_total_limit','provider_output_line_limit',
        }
        if isinstance(exc,ExportError):
            return {'code':exc.code,'operation':'artifact_export'}
        if isinstance(exc,EventSpoolError) and code in spool_codes:
            return {'code':code,'operation':'event_capture'}
        if code not in RUNTIME_ERROR_CODES:
            code = ('timeout' if isinstance(exc,(TimeoutError,subprocess.TimeoutExpired)) else
                    'policy_rejected' if isinstance(exc,(PolicyRejected,EnvironmentError)) else
                    'io_error' if isinstance(exc,OSError) else
                    'value_error' if isinstance(exc,ValueError) else 'internal_error')
        return {'code':code,'operation':operation if operation in DOCKER_OPERATIONS else None}

    def record_error(self, a, stage, exc):
        diagnostic = self.error_diagnostic(exc)
        payload = {'stage':stage,**diagnostic}
        key = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        try:
            self.store.append_event(a['id'],'error',payload,expected_generation=a['generation'],dedupe_key='runner-error:'+key)
        except Exception:
            pass

    @staticmethod
    def runtime_identity(a):
        return a['id'] + ('-verify' if a['state'] == 'verifying' else '')

    def end(self, a, state, **fields):
        """Retire owned runtime infrastructure before releasing its reservation."""
        status = self.runtime.status(a['runtime_id'], a['generation']) if a.get('runtime_id') else {'state': 'missing'}
        if status['state'] == 'created':
            self.runtime.stop(a['runtime_id'], expected_generation=a['generation'])
            status = self.runtime.status(a['runtime_id'], a['generation'])
        if status['state'] not in ('missing', 'created', 'exited'):
            raise RuntimeError('Refusing terminal transition while runtime may be live')
        if status['state'] != 'missing':
            self.runtime.cleanup(a['runtime_id'], expected_generation=a['generation'])
        else:
            self.runtime.cleanup_infrastructure(self.runtime_identity(a), expected_generation=a['generation'])
        if a['agent'] == 'claude':
            if a['state'] != 'preparing' and read_credential(self.staged_credential_path(a))[1]:
                self._credential_collection_failed = True
            if self._credential_collection_failed:
                # If persistence fails, retain the live DB reservation for recovery.
                self.credential_state.block_collection()
                if state in ('failed','interrupted'):
                    state = 'failed'
                    fields['reason'] = (a.get('result') or {}).get('failure_code') or 'attempt_credential_unavailable'
        committed = self.change(a, state, **fields)
        try:
            self.cleanup_terminal_material(committed)
        except (OSError, ValueError, DockerRuntimeError) as exc:
            self.record_error(committed, 'terminal_material_cleanup', exc)
        return committed

    def capacity(self) -> int:
        # Fail closed if legacy inventory or host pressure cannot be measured.
        ps = subprocess.run(['/usr/bin/docker', 'ps', '--filter', 'label=cloudd=1', '--format', '{{.ID}}'],
                            capture_output=True, text=True, check=True, timeout=10)
        legacy = len(ps.stdout.splitlines())
        memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        available = int(memory['MemAvailable'].split()[0]) * 1024
        reserve = int(self.config.get('host_memory_reserve_mib', 8192)) * 1024**2
        needed = max(int(self.config['runtime'].get('memory_mib', 4096)),
                     int(self.config.get('admission_memory_mib', 0))) * 1024**2
        disk = shutil.disk_usage(self.root).free
        workspace_mib = max(int(self.config['runtime'].get('workspace_mib', 20480)),
                            int(self.config.get('admission_workspace_mib', 0)))
        if available < reserve + needed or disk < (20 * 1024**3 + workspace_mib * 1024**2):
            return int(self.config.get('capacity', 2))
        return legacy

    def blocked(self):
        result = {agent: value['readiness_reason'] for agent, value in capabilities(self.token_path, self.config.get('claude_enabled', False)).items() if not value['enabled']}
        if self.config.get('hermes_enabled') is True:
            result.pop('hermes', None)
            if hasattr(self.runtime, 'readiness_reason'):
                reason = self.runtime.readiness_reason()
                if reason:
                    result['hermes'] = reason
        if self.config.get('claude_enabled'):
            reason = ('attempt_credential_unavailable' if self._credential_collection_failed else self.credential_state.blocked_reason(self.token_path))
            if reason:
                result['claude'] = reason
        if not self.config.get('test_mode'):
            result['fixture'] = 'Synthetic adapter is available only in test mode'
        return result

    def staged_credential_path(self, a):
        return self.root / 'tasks' / '.credentials' / (a['id'] + '.' + str(a['generation']) + '.token')

    def stage_credential(self, a):
        reason = self.credential_state.blocked_reason(self.token_path)
        if reason:
            raise PolicyRejected(reason)
        path = self.staged_credential_path(a)
        path.parent.mkdir(mode=0o700,exist_ok=True)
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in (0o700, 0o2700):
            raise CredentialStateError('Private credential staging directory required')
        try:
            fd = os.open(path,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o640)
        except FileExistsError:
            token,error = read_credential(path)
            if error or token != self.secret:
                raise CredentialStateError('Staged credential identity mismatch')
        else:
            with os.fdopen(fd,'wb') as output:
                os.fchown(output.fileno(),-1,int(self.config['runtime'].get('gid',1000)))
                os.fchmod(output.fileno(),0o640)
                output.write(self.secret)
                output.flush()
                os.fsync(output.fileno())
            parent = os.open(path.parent,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(parent)
            finally: os.close(parent)
        return path

    def attempt_secret(self, a):
        token,error = read_credential(self.staged_credential_path(a))
        if error:
            self._credential_collection_failed = True
            try:
                self.credential_state.block_collection()
            except (OSError, ValueError):
                raise CredentialStateError('Collection block could not be persisted') from None
            raise CredentialStateError('Attempt credential unavailable for safe result collection')
        return token

    def forbidden_for(self, a):
        if a['agent'] == 'claude':
            return (self.attempt_secret(a),)
        extra = self.runtime.forbidden_for_attempt(a) if hasattr(self.runtime, 'forbidden_for_attempt') else ()
        return self.forbidden + extra

    def cleanup_terminal_material(self, a):
        """Unlink secrets only after the matching durable terminal commit."""
        current = self.store.get_attempt(a['id'])
        if current['state'] not in TERMINAL or current['generation'] != a['generation']:
            raise CredentialStateError('Terminal credential cleanup requires matching committed generation')
        for rid in {current.get('runtime_id'), (current.get('result') or {}).get('execution_runtime_id')} - {None}:
            if self.runtime.status(rid, current['generation'])['state'] not in ('missing','exited','created'):
                raise CredentialStateError('Terminal credential cleanup refused for live runtime')
        if hasattr(self.runtime, 'cleanup_terminal_secrets'):
            self.runtime.cleanup_terminal_secrets(current)
        paths = [self.staged_credential_path(current)] if current['agent'] == 'claude' else []
        if current['state'] != 'completed':
            paths.append(self.root / 'results' / (current['id'] + '.json'))
        for path in paths:
            try:
                directory = _open_directory_nofollow(path.parent)
            except FileNotFoundError:
                continue
            try:
                parent = os.fstat(directory)
                private = path.parent.name == '.credentials'
                if (parent.st_uid != os.geteuid() or parent.st_mode & 0o002
                        or (private and stat.S_IMODE(parent.st_mode) not in (0o700, 0o2700))
                        or (parent.st_mode & 0o020 and parent.st_gid != os.getgid())):
                    raise CredentialStateError('Unsafe terminal cleanup directory')
                try: info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError: continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                    raise CredentialStateError('Unsafe terminal cleanup file')
                os.unlink(path.name, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)

    def cleanup_terminal_credentials(self, limit=200):
        """Rotating bounded recovery sweep; no deletion without terminal DB proof."""
        seen = set()
        for folder, pattern in ((self.root / 'tasks' / '.credentials', r'([a-f0-9-]{36})\.([1-9][0-9]*)\.token'),
                                (self.root / 'results', r'([a-f0-9-]{36})\.json')):
            if not folder.is_dir() or folder.is_symlink():
                continue
            entries = self._terminal_cleanup_scans.get(folder)
            if entries is None:
                entries = self._terminal_cleanup_scans[folder] = os.scandir(folder)
            for _ in range(limit):
                try:
                    entry = next(entries)
                except StopIteration:
                    entries.close()
                    self._terminal_cleanup_scans.pop(folder, None)
                    break
                match = re.fullmatch(pattern, entry.name)
                if not match or match[1] in seen:
                    continue
                seen.add(match[1])
                try:
                    a = self.store.get_attempt(match[1])
                    if folder.name == 'results' and a['state'] == 'completed':
                        continue
                    if a['state'] in TERMINAL and (len(match.groups()) == 1 or a['generation'] == int(match[2])):
                        self.cleanup_terminal_material(a)
                except (StoreError, OSError, ValueError, DockerRuntimeError):
                    # Never infer a terminal generation from an orphan filename.
                    continue

    def validate_request(self, request):
        project = self.config['projects'].get(request['project_id'])
        if not project or request['agent'] not in project.get('allowed_agents', []):
            raise PolicyRejected('Project/agent not allowed by worker policy')
        if request.get('environment_version') not in project.get('environment_versions', []):
            raise PolicyRejected('Explicit ready environment version required')
        model = request.get('model')
        models = project.get('models', {}).get(request['agent'], [])
        if model and model not in models:
            raise PolicyRejected('Requested model not allowed; no substitution')
        if request['agent'] == 'hermes' and model != 'grok-4.6':
            raise PolicyRejected('Hermes requires its qualified model')
        if request['agent'] in self.blocked():
            raise PolicyRejected('Adapter not qualified')
        return project

    def task_dir(self, a):
        path = self.root / 'tasks' / a['id']
        path.mkdir(exist_ok=True, mode=0o750)
        job_gid = int(self.config['runtime'].get('gid', 1000))
        os.chown(path, -1, job_gid)
        return path

    def environment_project(self, a, *, pin=False):
        project = dict(self.config['projects'][a['request']['project_id']])
        snapshot = (a.get('result') or {}).get('environment')
        if snapshot is None:
            snapshot = self.store.get_environment_snapshot(a['session_id'],a['generation'])
        if snapshot is None and self.config.get('environment_registry'):
            snapshot = EnvironmentRegistry(self.config['environment_registry'], read_only=True).resolve(
                a['request']['project_id'], a['request']['environment_version'],
                operator_approved_sha256=project.get('operator_approved_environments', {}).get(a['request']['environment_version']))
        if snapshot is None:
            return a, project  # Explicit legacy compatibility: no registry configured.
        manifest, digest = validate_manifest(snapshot['manifest'])
        operator_approved = (snapshot.get('operator_approved') is True
                             and project.get('operator_approved_environments', {}).get(manifest['version']) == digest)
        if (digest != snapshot['manifest_sha256'] or manifest['project_id'] != a['request']['project_id']
                or manifest['version'] != a['request']['environment_version']
                or not (snapshot['qualified'] or operator_approved)):
            raise PolicyRejected('Pinned environment identity mismatch')
        resources = manifest['resources']
        defaults = {'cpus':2,'memory_mib':4096,'pids':512,'workspace_mib':20480}
        self.require_qualified_image(manifest['image_digest'])
        expected_resources = project.get('environment_resources', {}).get(manifest['version'],
            {key:self.config['runtime'].get(key, default) for key,default in defaults.items()})
        if any(
                resources[key] != expected_resources.get(key) for key in defaults):
            raise PolicyRejected('Environment resources require a matching worker deployment')
        if a['request']['agent'] == 'hermes':
            policy = self.config.get('hermes_runtime')
            if not isinstance(policy, dict):
                raise PolicyRejected('Explicit Hermes environment policy required')
            network = policy.get('environment_network_profile')
            networks = policy.get('environment_network_profiles', [network])
            secret_refs = policy.get('environment_secret_refs')
            identifier = r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}'
            if (not isinstance(network, str) or not re.fullmatch(identifier, network)
                    or not isinstance(networks, list) or not networks or len(networks) > 8
                    or any(not isinstance(value,str) or not re.fullmatch(identifier,value) for value in networks)
                    or not isinstance(secret_refs, list) or len(secret_refs) > 16
                    or any(not isinstance(ref, str) or not re.fullmatch(identifier, ref) for ref in secret_refs)
                    or len(set(secret_refs)) != len(secret_refs)):
                raise PolicyRejected('Explicit valid Hermes network/secret policy required')
            image_refs = policy.get('environment_secret_refs_by_image', {})
            if (type(image_refs) is not dict or len(image_refs) > 16
                    or any(type(image) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', image)
                           or type(refs) is not list or len(refs) > 16
                           or any(type(ref) is not str or not re.fullmatch(identifier, ref) for ref in refs)
                           or len(set(refs)) != len(refs)
                           for image, refs in image_refs.items())):
                raise PolicyRejected('Explicit valid Hermes image secret policy required')
            secret_refs = image_refs.get(manifest['image_digest'], secret_refs)
            if manifest['network_profile'] not in networks:
                raise PolicyRejected('Hermes environment network profile is not allowed')
            network = manifest['network_profile']
        else:
            network = self.config.get('environment_network_profile', 'legacy-configured' if self.config['runtime'].get('network_enabled') else 'none')
            secret_refs = self.config.get('environment_secret_refs', [])
        if manifest['network_profile'] != network or manifest['secret_refs'] != secret_refs:
            raise PolicyRejected('Environment network/secret profile does not match worker')
        if manifest['startup_commands']:
            raise PolicyRejected('Environment startup execution is not yet supported')
        if len(manifest['repositories']) > 1 or any(repo['destination'] != '.' for repo in manifest['repositories']):
            raise PolicyRejected('Current repository profile supports one root repository')
        if manifest['repositories']:
            project['repository'] = manifest['repositories'][0]
            project.pop('template', None)
        if manifest['legacy_template_id'] not in (None, a['request']['project_id']):
            raise PolicyRejected('Legacy template is not registered for this project')
        checks = []
        configured = {check['id']:check for check in project.get('checks', [])}
        for check in manifest['checks']:
            value = {'id':check['id'], 'description':check['description'], 'argv':list(check['argv']),
                     'timeout':check['timeout_seconds']}
            if check['script_id']:
                source = configured.get(check['script_id'], {})
                if not source.get('script_source') or not check.get('script_name'):
                    raise PolicyRejected('Protected script reference is unavailable')
                value.update(script_source=source['script_source'], script_name=check['script_name'],
                             script_sha256=check['script_sha256'])
            checks.append(value)
        project['checks'] = checks
        if pin and not (a.get('result') or {}).get('environment'):
            a = self.change(a, a['state'], result={**(a.get('result') or {}), 'environment':snapshot})
        return a, project

    def require_qualified_image(self, image):
        allowed = self.config.get('qualified_images')
        if allowed is None:
            allowed = [self.runtime.image]
        elif (not isinstance(allowed, list) or not 1 <= len(allowed) <= 32
              or any(not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value)
                     for value in allowed)):
            raise PolicyRejected('Worker qualified_images must contain immutable local image IDs')
        if image not in allowed:
            approved = self.config.get('operator_approved_images', [])
            if (not isinstance(approved, list) or len(approved) > 16
                    or any(not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in approved)
                    or image not in approved):
                raise PolicyRejected('Pinned environment image is not allowed by this worker')
        return image

    def attempt_image(self, a):
        snapshot = (a.get('result') or {}).get('environment')
        image = snapshot['manifest']['image_digest'] if snapshot else self.runtime.image
        return self.require_qualified_image(image)

    def runtime_for(self, a, *, verifier=False):
        image = self.attempt_image(a)
        base = self.verifier if verifier else self.runtime
        snapshot = (a.get('result') or {}).get('environment')
        manifest = snapshot['manifest'] if snapshot else None
        resources = manifest['resources'] if manifest else {}
        changed_resources = any(value != base.config.get(key) for key,value in resources.items())
        network = None
        if not verifier and a['agent'] == 'hermes' and manifest:
            network = self.config.get('hermes_runtime', {}).get('tool_network_by_profile', {}).get(manifest['network_profile'], 'none')
            if network not in ('none','bridge'):
                raise PolicyRejected('Unsupported Hermes tool network')
        if image == base.image and not changed_resources and network in (None, base.config.get('tool_network', 'none')):
            return base
        # Preserve host mount/UID policy and apply the pinned environment resources.
        config = dict(base.config)
        config.update(image=image, egress_image=base.egress_image)
        config.update(resources)
        if network is not None:
            config['tool_network'] = network
        if not verifier and hasattr(base, 'with_image'):
            return type(base)(config)
        return Runtime(config)

    def prepare_repository(self, a, definition, workspace):
        if definition.get('destination', '.') != '.':
            raise PolicyRejected('Repository must use the workspace root')
        registration = self.config.get('repositories', {}).get(definition['repository_id'])
        if not registration or definition['commit'] not in registration.get('allowed_commits', []):
            raise PolicyRejected('Repository revision is not registered')
        baseline_path = self.root / 'baselines' / (a['session_id'] + '.json')
        ready_path = baseline_path.with_suffix('.ready.json')
        if baseline_path.exists():
            baseline = load_baseline(baseline_path)
            if baseline['commit'] != definition['commit']:
                raise PolicyRejected('Session repository revision is immutable')
        else:
            baseline = repository_snapshot(Path(registration['path']), definition['commit'])
            save_baseline(baseline_path, baseline)
        if not ready_path.exists():
            initialize_workspace(workspace, baseline)
            write_json(ready_path, {'commit':baseline['commit']}, mode=0o600)
        return self.change(a,a['state'],result={**(a.get('result') or {}),'repository':{
            'repository_id':definition['repository_id'],'base_commit':baseline['commit'],
            'baseline_sha256':hashlib.sha256(baseline_path.read_bytes()).hexdigest()}})

    def export_delivery(self, a, workspace_records):
        baseline_path = self.root / 'baselines' / (a['session_id'] + '.json')
        baseline = load_baseline(baseline_path)
        expected = (a.get('result') or {})['repository']
        if (baseline['commit'] != expected['base_commit'] or
                hashlib.sha256(baseline_path.read_bytes()).hexdigest() != expected['baseline_sha256']):
            raise PolicyRejected('Repository baseline changed')
        delivered = {}
        for item in workspace_records:
            if item['path'].startswith('@delivery/'):
                raise PolicyRejected('Reserved delivery namespace')
            content = Path(item['storage_path']).read_bytes()
            if len(content) != item['bytes'] or hashlib.sha256(content).hexdigest() != item['sha256']:
                raise PolicyRejected('Exported file changed before patch creation')
            delivered[item['path']] = content
        delta = text_patch(baseline, delivered,
                           delivered_modes={item['path']:item.get('executable') for item in workspace_records},
                           excluded_names=DEPENDENCY_EXCLUSIONS)
        destination = self.root/'artifacts'/(a['id']+'-delivery')
        receipt_path = self.root/'logs'/(a['id']+'.delivery-receipt.json')
        identity = {key:a[key] for key in ('id','generation','session_id')}
        if destination.exists():
            metadata, _ = adopt_export(destination,receipt_path,expected_context=identity)
        else:
            with tempfile.TemporaryDirectory(prefix='.delivery-',dir=self.root/'logs') as temporary:
                stage = Path(temporary)
                (stage/'changes.patch').write_bytes(delta['patch'])
                (stage/'manifest.json').write_text(json.dumps({k:v for k,v in delta.items() if k!='patch'},indent=2)+'\n')
                metadata = export_workspace(stage,destination,shared_group=True,forbidden_values=self.forbidden_for(a),
                                            receipt_path=receipt_path,receipt_context=identity)
        public = []
        for item in metadata:
            item = {**item,'path':'@delivery/'+item['path']}
            self.store.register_artifact(a['id'],item,expected_generation=a['generation'])
            public.append({k:v for k,v in item.items() if k!='storage_path'})
        return {k:v for k,v in delta.items() if k!='patch'}, public

    def prepare(self, a):
        request = a['request']
        self.validate_request(request)
        a, project = self.environment_project(a, pin=True)
        workspace = self.runtime_for(a).make_workspace(a['session_id'])
        taskdir = self.task_dir(a)
        if project.get('repository') and project.get('template'):
            raise PolicyRejected('Project cannot combine a repository and a template')
        if project.get('repository'):
            a = self.prepare_repository(a, project['repository'], workspace)
        # Templates are operator-owned configuration, never arbitrary request paths.
        if not any(workspace.iterdir()) and project.get('template'):
            template = Path(project['template'])
            for source in template.rglob('*'):
                if source.is_symlink():
                    raise ValueError('Template contains symlink')
                child = workspace / source.relative_to(template)
                if source.is_dir():
                    child.mkdir(mode=0o2770, exist_ok=True)
                elif source.is_file():
                    shutil.copyfile(source, child)
                    child.chmod(0o660)
                else:
                    raise ValueError('Template contains special file')
        inputs = self.store.get_attempt_inputs(a['id'], a['generation'])
        if inputs:
            target = taskdir / 'inputs'
            target.mkdir(exist_ok=True, mode=0o750)
            os.chown(target, -1, int(self.config['runtime'].get('gid', 1000)))
            for item in inputs:
                source = Path(item['storage_path'])
                if not source.resolve().is_relative_to(Path(self.config['input_root']).resolve()) or source.is_symlink():
                    raise ValueError('Input storage path not authorized')
                content = source.read_bytes()
                if len(content) != item['bytes'] or hashlib.sha256(content).hexdigest() != item['sha256']:
                    raise ValueError('Input integrity failed')
                destination = target / item['id']
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != content:
                        raise ValueError('Existing input has changed')
                else:
                    with open(destination, 'xb') as out:
                        out.write(content)
                    destination.chmod(0o440)
                    os.chown(destination, -1, int(self.config['runtime'].get('gid', 1000)))
        task = {'adapter': request['agent'], 'prompt': request['goal'], 'model': request.get('model'),
                'continuation_summary': json.dumps(self.store.get_context(a['id']), ensure_ascii=False),
                'event_spool': f"{a['id']}.{a['generation']}.jsonl",
                'fixture_operation': project.get('fixture_operation'), 'inputs': [{'id':i['id'],'path':'/inputs/'+i['id']} for i in inputs]}
        argv = ['python3', '/opt/cloudworkbench/entrypoint.py', '/run/task/task.json']
        if request['agent'] == 'hermes':
            task = {'schema_version': 1, 'attempt_id': a['id'], 'generation': a['generation'],
                    'session_id': a['session_id'], 'prompt': request['goal'],
                    'runtime_owner': self.config['runtime'].get('owner', 'primary'),
                    'workspace_host': str(workspace),
                    'state_host': str(self.runtime_for(a).native_state(a['session_id'])),
                    'resume_session_id': self.store.get_native_resume_session(a['id']),
                    'tool_image': self.attempt_image(a),
                    'event_spool': task['event_spool'],
                    'continuation_summary': task['continuation_summary'],
                    'inputs': [{'id':i['id'], 'host_path':str(taskdir / 'inputs' / i['id'])} for i in inputs]}
            if self.runtime_for(a).config.get('tool_network') == 'bridge':
                task.update(schema_version=2, tool_network='bridge')
            argv = ['/opt/hermes/venv/bin/python', '-m', 'cloudworkbench.hermes_job_entrypoint', '/run/task/task.json']
        write_json(taskdir / 'task.json', task)
        os.chown(taskdir / 'task.json', -1, int(self.config['runtime'].get('gid', 1000)))
        mounts = [{'source': str(taskdir), 'target': '/run/task', 'readonly': True},
                  {'source': str(self.runtime_for(a).native_state(a['session_id'])), 'target': '/state', 'readonly': False}]
        if inputs:
            mounts.append({'source': str(taskdir / 'inputs'), 'target': '/inputs', 'readonly': True})
        if request['agent'] == 'claude':
            mounts.append({'source': str(self.stage_credential(a)), 'target': '/run/secrets/claude-token', 'readonly': True})
        fresh = self.store.get_attempt(a['id'])
        if fresh['cancel_requested']:
            return self.change(a, 'cancelled', reason='cancelled_before_launch')
        runtime_id = self.runtime_for(a).launch(a['id'], a['session_id'], argv, {}, mounts, generation=a['generation'])
        self.change(a, 'running', runtime_id=runtime_id)

    def reconcile_attempt(self, a, runtimes=None, absent_reason=None):
        if a.get('runtime_id'):
            return a
        # An inventory failure is uncertainty, never evidence that launch failed.
        runtimes = self.runtime.list_owned() if runtimes is None else runtimes
        identity = self.runtime_identity(a)
        matches = [runtime for runtime in runtimes
                   if runtime.get('labels', {}).get('io.cloudworkbench.attempt') == identity
                   and runtime.get('labels', {}).get('io.cloudworkbench.generation') == str(a['generation'])]
        if len(matches) > 1:
            raise RuntimeError('Multiple runtimes match one fenced attempt')
        if matches:
            rid = matches[0].get('id') or matches[0].get('runtime_id')
            status = self.runtime.status(rid, a['generation'])
            if status['state'] in ('running', 'exited'):
                return self.change(a, 'running' if a['state'] == 'preparing' else a['state'], runtime_id=rid)
            if status['state'] == 'created':
                a = self.change(a, a['state'], runtime_id=rid)
            elif status['state'] != 'missing':
                raise RuntimeError('Uncertain runtime state during reconciliation')
        if a['state'] == 'verifying' and not matches and (a.get('result') or {}).get('verification_started_at') and absent_reason is None:
            return self.start_verification(a)
        if a['state'] == 'verifying':
            result = {**(a.get('result') or {}), 'verification_error':absent_reason or 'runtime_missing'}
            return self.end(a, 'failed', reason='verifier_infrastructure', result=result)
        if a['state'] == 'preparing':
            return self.end(a, 'failed', reason=absent_reason or 'interrupted_during_preparation')
        return self.end(a, 'interrupted', reason='runtime_missing')

    def reconcile(self):
        self.cleanup_terminal_credentials()
        runtimes = self.runtime.list_owned()
        for a in self.store.active_attempts():
            if not a.get('runtime_id'):
                try:
                    self.reconcile_attempt(a, runtimes)
                except Exception as exc:
                    self.record_error(a, 'reconciliation', exc)

    def export(self, a, report=None):
        destination = self.root / 'artifacts' / a['id']
        report = {} if report is None else report
        receipt_path = self.root / 'logs' / (a['id'] + '.export-receipt.json')
        receipt_context = {key:a[key] for key in ('id','generation','session_id')}
        if destination.exists() or destination.is_symlink():
            metadata, saved_report = adopt_export(destination, receipt_path, expected_context=receipt_context)
            report.update(saved_report)
        else:
            metadata = export_workspace(self.runtime_for(a).make_workspace(a['session_id']), destination,
                                        max_bytes=int(self.config.get('artifact_max_bytes', 100 * 1024**2)), forbidden_values=self.forbidden_for(a), shared_group=True, excluded_names=DEPENDENCY_EXCLUSIONS, export_report=report, receipt_path=receipt_path, receipt_context=receipt_context)
        for item in metadata:
            self.store.register_artifact(a['id'], item, expected_generation=a['generation'])
        return metadata

    def capture_events(self, a, final=False):
        """Publish complete public records before advancing the durable reader cursor."""
        path = self.runtime_for(a).native_state(a['session_id']) / 'events' / f"{a['id']}.{a['generation']}.jsonl"
        cursor_path = self.root / 'logs' / (a['id'] + '.events.cursor.json')
        saved = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
        while True:
            batch = read_event_spool(path, saved.get('cursor'), forbidden=self.forbidden_for(a), final=final)
            if batch['cursor'] is None:
                return saved.get('provider_result')
            next_saved = {**saved, 'cursor':batch['cursor'], 'source':'native_public_event_spool'}
            count = saved.get('event_count',0)
            for record in batch['events']:
                event = record['event']
                count += 1
                if count > 10000:
                    raise EventSpoolError('event_spool_record_limit')
                self.store.append_event(a['id'],event['type'],event['payload'],expected_generation=a['generation'],dedupe_key='spool:' + str(record['offset']))
                if event['type'] == 'adapter.result':
                    next_saved['provider_result'] = event['payload']
            next_saved['event_count'] = count
            next_saved['complete'] = bool(final and batch['complete'])
            write_json(cursor_path,next_saved)
            saved = next_saved
            if not final or batch['complete']:
                return saved.get('provider_result')

    def stop_capture_failure(self, a, exc):
        self.record_error(a,'event_capture',exc)
        status = self.runtime.status(a['runtime_id'],a['generation'])
        if status['state'] == 'running':
            self.runtime.stop(a['runtime_id'],expected_generation=a['generation'])
            status = self.runtime.status(a['runtime_id'],a['generation'])
        if status['state'] not in ('exited','created','missing'):
            raise RuntimeError('Event capture failed but runtime may remain live')
        return self.end(a,'failed',reason='event_capture_failed',result={**(a.get('result') or {}),'capture_error':exc.code})

    def partial_result(self, a):
        result = dict(a.get('result') or {})
        result.update(partial=True, outcome='unverified')
        if a['state'] != 'verifying':
            try:
                self.capture_events(a, final=True)
                result['event_capture'] = {'complete':True,'source':'native_public_event_spool'}
            except (EventSpoolError, OSError, ValueError) as exc:
                self.record_error(a,'partial_event_capture',exc)
                result['event_capture'] = {'complete':False,'error':self.error_diagnostic(exc)['code']}
            try:
                report = {}
                result['artifacts'] = [{k:v for k,v in item.items() if k != 'storage_path'} for item in self.export(a,report)]
                result['export_report'] = report
            except (ExportError, OSError, ValueError) as exc:
                self.record_error(a,'partial_artifact_export',exc)
                result['artifact_error'] = self.error_diagnostic(exc)['code']
        return result

    def read_logs(self, a):
        forbidden = self.forbidden_for(a)
        raw = self.runtime.logs(a['runtime_id'], max_bytes=1024**2)
        for value in forbidden:
            raw = raw.replace(value, b'[REDACTED]')
        sanitized = []
        for line in raw.splitlines():
            try:
                sanitized.append(json.dumps(redact_public(json.loads(line),forbidden),ensure_ascii=False).encode())
            except (ValueError,UnicodeError,RecursionError):
                sanitized.append(line)
        raw = b'\n'.join(sanitized)
        path = self.root / 'logs' / (a['id'] + ('-verify' if a['state'] == 'verifying' else '') + '.log')
        path.write_bytes(raw)
        path.chmod(0o640)
        return raw

    def begin_verification(self, a, provider_result):
        a, project = self.environment_project(a)
        checks = project.get('checks', [])
        result = {**(a.get('result') or {}), 'schema_version': 1, 'session_id': a['session_id'], 'attempt_id': a['id'],
                  'agent': a['agent'], 'continuation': 'native_session' if a['agent'] == 'hermes' else 'reconstructed_workspace',
                  'environment_version': a['request']['environment_version'], 'image_digest': self.attempt_image(a),
                  'provider_result': provider_result, 'summary': provider_result.get('summary', ''),
                  'event_capture': {'source':'native_public_event_spool','complete':True},
                  'usage': provider_result.get('usage'), 'usage_status':provider_result.get('usage_status',usage_status(provider_result.get('usage'))), 'checks': []}
        # Export after container exit; verifier receives the same workspace read-only.
        report = {}
        metadata = self.export(a, report)
        result['artifacts'] = [{k:v for k,v in item.items() if k != 'storage_path'} for item in metadata]
        if result.get('repository'):
            result['delivery'], delivery_artifacts = self.export_delivery(a, metadata)
            result['artifacts'].extend(delivery_artifacts)
        result['export_report'] = report
        self.event(a, 'artifact.exported', report, dedupe_key='export-report')
        result['execution_runtime_id'] = a['runtime_id']
        result['verification_started_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        a = self.change(a, 'verifying', runtime_id=None, result=result)
        return self.start_verification(a)

    def start_verification(self, a):
        result = a['result']
        prior = result.get('execution_runtime_id')
        if prior:
            status = self.runtime.status(prior, a['generation'])
            if status['state'] not in ('exited','missing'):
                raise RuntimeError('Execution runtime must be stopped before verification')
            if status['state'] != 'missing':
                self.runtime.cleanup(prior, expected_generation=a['generation'])
        a, project = self.environment_project(a)
        checks = project.get('checks', [])
        if not checks:
            return self.finish(a, result, 'unverified')
        try:
            taskdir = self.task_dir(a)
            for check in checks:
                if check.get('script_source'):
                    if check.get('script_sha256'):
                        source = Path(check['script_source'])
                        fd = os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                        with os.fdopen(fd,'rb') as stream:
                            info = os.fstat(stream.fileno())
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                                raise PolicyRejected('Protected script must be a regular single-link file')
                            content = stream.read(1024*1024+1)
                        if len(content)>1024*1024 or hashlib.sha256(content).hexdigest()!=check['script_sha256']:
                            raise PolicyRejected('Protected script digest changed')
                        (taskdir / check['script_name']).write_bytes(content)
                    else:
                        shutil.copyfile(check['script_source'], taskdir / check['script_name'])
                    os.chown(taskdir / check['script_name'], -1, int(self.config['runtime'].get('gid', 1000)))
                    (taskdir / check['script_name']).chmod(0o640)
            write_json(taskdir / 'verify.json', {'adapter':'verify','checks':checks})
            os.chown(taskdir / 'verify.json', -1, int(self.config['runtime'].get('gid', 1000)))
            rid = self.runtime_for(a, verifier=True).launch(a['id'] + '-verify', a['session_id'], ['python3', '/opt/cloudworkbench/entrypoint.py', '/run/task/verify.json'], {},
                                       [{'source':str(taskdir),'target':'/run/task','readonly':True}], generation=a['generation'])
            self.change(a, 'verifying', runtime_id=rid)
        except Exception as exc:
            self.record_error(a, 'verifier_launch', exc)
            # Docker may have accepted create/start before its response was lost.
            return self.reconcile_attempt(self.store.get_attempt(a['id']), absent_reason='launch_failed')

    def finish(self, a, result, outcome):
        result['outcome'] = outcome
        # Only protected configured criteria can be automated; user-only criteria stay open.
        criteria = [item for item in a['request'].get('acceptance', []) if item.get('mandatory', True)]
        requested = {item['id'] for item in criteria}
        _, project = self.environment_project(a)
        definitions = {c['id']:c.get('description') for c in project.get('checks', [])}
        passed = {item['id'] for item in result['checks'] if item.get('result') == 'passed'}
        proven = {item['id'] for item in criteria if item['id'] in passed and definitions.get(item['id']) == item.get('description') and definitions.get(item['id'])}
        if outcome == 'verified' and requested - proven:
            result['outcome'] = 'needs_review'
            result['remaining_criteria'] = sorted(requested - proven)
        result['completion_intent'] = {'schema_version':1,'attempt_id':a['id'],
                                       'session_id':a['session_id'],'generation':a['generation'],
                                       'outcome':result['outcome']}
        a = self.change(a, 'verifying', result=result)
        return self.finalize_completion(a)

    @staticmethod
    def completion_result(a):
        result = a.get('result') or {}
        expected = {'schema_version':1,'attempt_id':a['id'],'session_id':a['session_id'],
                    'generation':a['generation'],'outcome':result.get('outcome')}
        if (a['state'] != 'verifying' or result.get('completion_intent') != expected
                or result.get('outcome') not in {'verified','needs_review','unverified','rejected'}):
            raise PolicyRejected('Completion intent identity mismatch')
        return result

    def finalize_completion(self, a):
        result = self.completion_result(a)
        write_json(self.root / 'results' / (a['id'] + '.json'), result)
        return self.end(a, 'completed', exit_code=0, outcome=result['outcome'], result=result)

    def monitor(self, a):
        if (a.get('result') or {}).get('completion_intent'):
            return self.finalize_completion(a)
        if not a['runtime_id']:
            return self.reconcile_attempt(a)
        status = self.runtime.status(a['runtime_id'], a['generation'])
        if status['state'] in ('missing', 'created'):
            state = 'failed' if a['state'] in ('preparing', 'verifying') else 'interrupted'
            reason = 'verifier_infrastructure' if a['state'] == 'verifying' else 'runtime_not_running_or_finished'
            fields = {'reason':reason}
            if a['state']=='verifying':
                fields['result'] = {**(a.get('result') or {}),'verification_error':'runtime_'+status['state']}
            return self.end(a, state, **fields)
        if a['cancel_requested']:
            self.runtime.stop(a['runtime_id'], expected_generation=a['generation'])
            if self.runtime.status(a['runtime_id'], a['generation'])['state'] != 'exited':
                raise RuntimeError('Cancellation has not stopped runtime')
            return self.end(a, 'cancelled', reason='owner_cancelled', result=self.partial_result(a))
        verifying = a['state'] == 'verifying'
        started_at = (a.get('result') or {}).get('verification_started_at') if verifying else a.get('started_at')
        started = dt.datetime.fromisoformat((started_at or a['updated_at']).replace('Z', '+00:00'))
        budget = self.config.get('verification_seconds', 600) if verifying else self.config.get('execution_seconds', 3600)
        if not verifying and a['agent'] == 'hermes':
            budget = self.config.get('hermes_execution_seconds', 7500)
        if (dt.datetime.now(dt.timezone.utc) - started).total_seconds() > budget:
            self.runtime.stop(a['runtime_id'], expected_generation=a['generation'])
            if self.runtime.status(a['runtime_id'], a['generation'])['state'] != 'exited':
                raise RuntimeError('Timeout has not stopped runtime')
            fields = {'reason': 'verifier_infrastructure' if verifying else 'timeout'}
            if verifying:
                fields['result'] = {**(a.get('result') or {}), 'verification_error': 'timeout'}
            else:
                fields['result'] = self.partial_result(a)
            return self.end(a, 'failed', **fields)
        if not verifying:
            try:
                provider_result = self.capture_events(a,final=status['state']=='exited')
            except CredentialStateError:
                self._credential_collection_failed = True
                if status['state'] == 'running':
                    self.runtime.stop(a['runtime_id'], expected_generation=a['generation'])
                self.credential_state.block_collection()
                return self.end(a,'failed',reason='attempt_credential_unavailable')
            except EventSpoolError as exc:
                if exc.code == 'event_spool_missing' and status['state']=='exited' and (status['exit_code'] or status['oom']):
                    provider_result = None
                else:
                    return self.stop_capture_failure(a,exc)
        if status['state'] == 'running':
            return
        if not verifying and not status['oom'] and provider_result and provider_result.get('is_error') and provider_result.get('failure_code') in FAILURE_CODES:
            code = provider_result['failure_code']
            result = {**(a.get('result') or {}),'provider_result':provider_result,
                      'usage':provider_result.get('usage'),
                      'usage_status':provider_result.get('usage_status',usage_status(provider_result.get('usage'))),
                      'failure_code':code}
            a = self.change(a,a['state'],result=result)
            try:
                if code == 'provider_auth_rejected':
                    CredentialState(self.credential_state_path,self.attempt_secret(a)).quarantine(code)
                self.read_logs(a)
            except (CredentialStateError, OSError):
                self._credential_collection_failed = True
                self.credential_state.block_collection()
            return self.end(a,'failed',reason=code,exit_code=status['exit_code'],result=result)
        if status['oom'] or status['exit_code']:
            self.read_logs(a)
            reason = 'verifier_infrastructure' if verifying else ('OOM' if status['oom'] else 'provider_or_execution_error')
            fields = {'exit_code': status['exit_code'], 'reason': reason}
            if verifying:
                fields['result'] = {**(a.get('result') or {}), 'verification_error': 'OOM' if status['oom'] else 'nonzero_exit'}
            return self.end(a, 'failed', **fields)
        raw = self.read_logs(a)
        if a['state'] == 'verifying':
            records = [json.loads(line) for line in raw.splitlines() if line.startswith(b'{')]
            proof = [item for item in records if item.get('type') == 'verification']
            if len(proof) != 1 or not isinstance(proof[0].get('checks'),list) or any(not isinstance(c,dict) or c.get('result') not in {'passed','failed','infrastructure_error'} for c in proof[0]['checks']):
                return self.end(a, 'failed', reason='verifier_infrastructure', result={**(a.get('result') or {}),'verification_error':'invalid_proof'})
            if any(c['result']=='infrastructure_error' for c in proof[0]['checks']):
                return self.end(a, 'failed', reason='verifier_infrastructure', result={**(a.get('result') or {}),'verification_error':'check_infrastructure'})
            _, project = self.environment_project(a)
            expected = [c['id'] for c in project.get('checks', [])]
            actual = [c.get('id') for c in proof[0]['checks']]
            if not expected or any(not isinstance(value,str) for value in actual) or sorted(actual) != sorted(expected) or len(set(actual)) != len(actual):
                return self.end(a, 'failed', reason='verifier_infrastructure', result={**(a.get('result') or {}),'verification_error':'check_set_mismatch'})
            result = a['result']
            result['checks'] = proof[0]['checks']
            self.event(a, 'verification.result', {'checks': [{k:v for k,v in c.items() if k not in ('stdout','stderr')} for c in result['checks']]}, dedupe_key='verification-result')
            return self.finish(a, result, 'verified' if all(c['result']=='passed' for c in result['checks']) else 'rejected')
        if provider_result and provider_result.get('permission_denials'):
            return self.end(a, 'failed', reason='permission_unsupported')
        if not provider_result or provider_result['is_error']:
            return self.end(a, 'failed', reason='provider_result_missing_or_error')
        self.begin_verification(a, provider_result)

    def sampling_runtime(self, attempt, *, verifier=False):
        # Reuse trusted fields without repeating Runtime filesystem validation.
        base = self.verifier if verifier else self.runtime
        fields = {key:getattr(base,key) for key in ('docker','owner','uid','gid','root')}
        return SimpleNamespace(**fields,image=self.attempt_image(attempt))

    def record_resource_health(self, status, reason=None):
        # Callers supply only fixed codes, never exception text or runtime data.
        self._resource_health.update(status=status,reason=reason)
        if reason:
            counts=self._resource_health['drops']
            counts[reason]=min(counts.get(reason,0)+1,2**31-1)

    def offer_resource_sample(self):
        if self.resources is None:
            return
        if self.resources.readiness() != 'ready':
            return
        candidates = self.store.resource_candidates()
        if self._resource_cursor is not None:
            candidates = [a for a in candidates if a['id'] > self._resource_cursor] + [a for a in candidates if a['id'] <= self._resource_cursor]
        for attempt in candidates:
            if (attempt.get('runtime_id') or '').startswith('cbx_'):
                continue
            try:
                binding = Binding(attempt['session_id'],attempt['id'],attempt['generation'],
                                  attempt.get('runtime_id'),'verifier' if attempt['state']=='verifying' else 'execution')
                if not binding.matches(attempt):
                    continue
                status = self.resources.offer(binding,self.sampling_runtime(attempt,verifier=binding.role=='verifier'))
                if status == 'submitted':
                    self.record_resource_health('sampling')
                    self._resource_cursor = attempt['id']
                    return
                if status in {'busy','cadence','closed'}:
                    return
            except Exception:
                self._resource_cursor = attempt['id']
                self.record_resource_health('unavailable','candidate_unavailable')
                continue

    def close(self):
        if self.resources is not None:
            try:
                self.resources.close()
            except Exception:
                self.record_resource_health('unavailable','close_unavailable')

    def tick(self):
        if self.resources is not None:
            try:
                result=self.resources.poll()
                self.record_resource_health(result['status'],result.get('reason'))
            except Exception:
                self.record_resource_health('unavailable','poll_unavailable')
        self.cleanup_terminal_credentials()
        errors = []
        for a in self.store.active_attempts():
            try:
                self.monitor(a)
            except Exception as exc:
                errors.append({'attempt_id': a['id'], 'error': type(exc).__name__})
                try:
                    current = self.store.get_attempt(a['id'])
                    self.record_error(current, 'result_collection', exc)
                    if (current.get('result') or {}).get('completion_intent'):
                        try:
                            self.completion_result(current)
                        except PolicyRejected:
                            pass
                        else:
                            continue
                    if not current.get('runtime_id'):
                        self.reconcile_attempt(current)
                        continue
                    status = self.runtime.status(current['runtime_id'], current['generation'])
                    if (current['state'] == 'verifying' and status['state'] == 'exited'
                            and status.get('exit_code') == 0
                            and not (current.get('result') or {}).get('completion_intent')):
                        result = dict(current.get('result') or {})
                        retries = result.get('verification_collection_retries', 0)
                        if type(retries) is int and 0 <= retries < 3:
                            result['verification_collection_retries'] = retries + 1
                            self.change(current, 'verifying', result=result)
                            continue
                    if status['state'] in ('exited', 'created', 'missing'):
                        reason = 'verifier_infrastructure' if current['state'] == 'verifying' else 'result_collection_failed'
                        fields = {'reason':reason}
                        if current['state']=='verifying':
                            fields['result'] = {**(current.get('result') or {}),'verification_error':'result_collection_failed'}
                        self.end(current, 'failed', **fields)
                except Exception:
                    # Uncertain discovery, stop or cleanup retains every reservation.
                    pass
        try:
            external = self.capacity()
            a = self.store.claim_next(capacity=self.config.get('capacity', 2), blocked_agents=self.blocked(),
                                     external_running=external, hermes_capacity=self.config.get('hermes_capacity', 1))
            if a:
                try:
                    self.prepare(a)
                except Exception as exc:
                    # Uncertain Docker creation must be reconciled, not retried.
                    self.record_error(a, 'preparation', exc)
                    self.reconcile_attempt(self.store.get_attempt(a['id']), absent_reason='policy_rejected' if isinstance(exc, (PolicyRejected, EnvironmentError)) else None)
        except Exception as exc:
            errors.append({'stage': 'admission', 'error': type(exc).__name__})
        try:
            self.offer_resource_sample()
        except Exception:
            self.record_resource_health('unavailable','offer_unavailable')
        blocked_agents = self.blocked()
        write_json(self.root / 'heartbeat.json', {'timestamp': time.time(), 'errors': errors, 'pid': os.getpid(),
                   'resource_observer':self._resource_health,
                   'blocked_agents':blocked_agents,
                   'available_agents':[agent for agent in self.config.get('agents',[]) if agent not in blocked_agents]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    os.umask(0o007)
    config = json.loads(Path(args.config).read_text())
    root = Path(config['state_root'])
    with open(root / 'runner.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = Runner(Store(Path(config['database']), shared_group=True), config)
        runner.reconcile()
        try:
            while True:
                runner.tick()
                time.sleep(1)
        finally:
            runner.close()


if __name__ == '__main__':
    main()
