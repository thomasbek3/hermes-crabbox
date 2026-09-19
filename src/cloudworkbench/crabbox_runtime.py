"""Crabbox owns new Hermes environments; legacy attempts keep their original runtime."""
from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid

from .hermes_coordinator_runtime import HermesCoordinatorRuntime, HERMES_ARGV, PREFIX, _regular
from .hermes_job_entrypoint import read_auth, validate_task, RUN_BUDGET_SECONDS
from .crabbox_capture import read_additional_secrets
from .runtime import RuntimeError, _id, _path, _IMAGE


def save(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def private_json(path):
    fd = _regular(path, 1024**2, private=True)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError('unsafe Crabbox journal')
        return json.load(stream)


def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            try: os.waitpid(pid, os.WNOHANG)
            except ChildProcessError: pass
            return None
        return fields[19]
    except FileNotFoundError:
        return None


def _pstack_secret(path):
    try:
        fd = _regular(path, 16384, private=True)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if before.st_uid not in (0, os.geteuid()):
                raise ValueError()
            raw = stream.read(16385)
            after = os.fstat(stream.fileno())
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after) or len(raw) != before.st_size:
            raise ValueError()
        value = raw.decode('ascii').strip()
        if not 16 <= len(value) <= 16384 or not all(33 <= ord(char) <= 126 for char in value):
            raise ValueError()
        return value
    except Exception:
        raise RuntimeError('Crabbox pstack credential unavailable') from None


def _codex_access(path):
    from .crabbox_capture import pairs, invalid_constant
    try:
        fd = _regular(path, 65536, private=True)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if before.st_uid not in (0, os.geteuid()):
                raise ValueError()
            raw = stream.read(65537)
            after = os.fstat(stream.fileno())
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after) or len(raw) != before.st_size:
            raise ValueError()
        decode = lambda raw: json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
        value = decode(raw)
        if (type(value) is not dict or set(value)-{'auth_mode','OPENAI_API_KEY','tokens','last_refresh'}
                or value.get('auth_mode') != 'chatgpt' or value.get('OPENAI_API_KEY') is not None):
            raise ValueError()
        tokens = value.get('tokens')
        if type(tokens) is not dict or set(tokens)-{'access_token','refresh_token','id_token','account_id'}:
            raise ValueError()
        access = tokens.get('access_token')
        if (type(access) is not str or not 16 <= len(access) <= 16384
                or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', access)):
            raise ValueError()
        claims_part = access.split('.')[1]
        claims = decode(base64.urlsafe_b64decode(claims_part + '=' * (-len(claims_part) % 4)))
        if type(claims) is not dict or type(claims.get('https://api.openai.com/auth')) is not dict:
            raise ValueError()
        account = claims['https://api.openai.com/auth'].get('chatgpt_account_id')
        if (type(account) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', account)
                or tokens.get('account_id', account) != account):
            raise ValueError()
        if type(claims.get('exp')) not in (int, float) or not 0 < claims['exp'] < 1e12:
            raise ValueError()
        if claims['exp'] <= time.time() + RUN_BUDGET_SECONDS + 120:
            raise RuntimeError('Crabbox pstack Codex credential expired for job budget')
        return access
    except RuntimeError as exc:
        if str(exc) == 'Crabbox pstack Codex credential expired for job budget':
            raise
        raise RuntimeError('Crabbox pstack Codex credential unavailable') from None
    except Exception:
        raise RuntimeError('Crabbox pstack Codex credential unavailable') from None


class CrabboxRuntime(HermesCoordinatorRuntime):
    def __init__(self, config):
        super().__init__(config)
        self.crabbox = str(_path(config['crabbox_binary']))
        self.crabbox_image = config['crabbox_image']
        if not _IMAGE.fullmatch(self.crabbox_image):
            raise RuntimeError('invalid Crabbox image')
        self.crabbox_images = config.get('crabbox_images', [self.crabbox_image])
        self.crabbox_desktop_images = config.get('crabbox_desktop_images', [])
        self.crabbox_pstack_images = config.get('crabbox_pstack_images', [])
        if (type(self.crabbox_images) is not list or type(self.crabbox_desktop_images) is not list
                or type(self.crabbox_pstack_images) is not list
                or not self.crabbox_images or len(self.crabbox_images) > 16
                or any(type(value) is not str or not _IMAGE.fullmatch(value) for value in self.crabbox_images)
                or any(value not in self.crabbox_images for value in self.crabbox_desktop_images)
                or any(value not in self.crabbox_images for value in self.crabbox_pstack_images)):
            raise RuntimeError('invalid Crabbox image capability policy')
        credential_fields = {'anthropic': 'crabbox_pstack_claude_token',
                             'openai-codex': 'crabbox_pstack_codex_auth',
                             'jev': 'crabbox_pstack_jev_key'}
        if self.crabbox_pstack_images and any(not config.get(field) for field in credential_fields.values()):
            raise RuntimeError('pstack images require explicit Claude, Codex and Jev credential paths')
        self.pstack_credential_paths = {name: _path(config[field])
                                        for name, field in credential_fields.items() if config.get(field)}
        self.crabbox_root = _path(self.hermes_journal_root.parent / 'crabbox-runtime')
        self.crabbox_root.mkdir(mode=0o700, exist_ok=True)
        info = self.crabbox_root.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError('unsafe Crabbox state directory')

    def pstack_credentials(self):
        if len(self.pstack_credential_paths) != 3:
            raise RuntimeError('Crabbox pstack credential paths are not configured')
        return {'anthropic': _pstack_secret(self.pstack_credential_paths['anthropic']),
                'openai-codex': _codex_access(self.pstack_credential_paths['openai-codex']),
                'jev': _pstack_secret(self.pstack_credential_paths['jev'])}

    def cb_id(self, attempt, generation):
        _id(attempt)
        if type(generation) is not int or generation < 1:
            raise RuntimeError('invalid Crabbox generation')
        digest = hashlib.sha256(f'{self.owner}:{attempt}:{generation}'.encode()).hexdigest()[:12]
        return 'cbx_' + digest

    def cb_record(self, rid, generation=None):
        if not re.fullmatch(r'cbx_[0-9a-f]{12}', rid):
            raise RuntimeError('invalid Crabbox runtime identity')
        folder = _path(self.crabbox_root / rid)
        if not folder.exists():
            return None
        info = folder.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError('unsafe Crabbox attempt directory')
        if not (folder / 'record.json').exists():
            return None
        record = private_json(folder / 'record.json')
        if (record['owner'] != self.owner or record['id'] != rid
                or self.cb_id(record['attempt'], record['generation']) != rid
                or generation is not None and record['generation'] != generation):
            raise RuntimeError('Crabbox ownership or generation mismatch')
        return record

    def cb_save(self, record):
        save(self.crabbox_root / record['id'] / 'record.json', record)

    def cb_env(self, rid):
        home = self.crabbox_root / rid / 'controller'
        return {'PATH':'/usr/local/bin:/usr/bin:/bin', 'HOME':str(home), 'LANG':'C.UTF-8',
                'CRABBOX_CONFIG':str(home / 'config.json'),
                'XDG_STATE_HOME':str(home / 'state'), 'XDG_CACHE_HOME':str(home / 'cache'),
                'XDG_CONFIG_HOME':str(home / 'config')}

    def cb_run(self, record, args, timeout=120):
        env = self.cb_env(record['id'])
        try:
            result = subprocess.run([self.crabbox, *args], env=env, cwd=env['HOME'],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError('Crabbox operation incomplete; reconcile the same lease') from None
        if result.returncode:
            raise RuntimeError('Crabbox operation failed; retained exact lease for reconciliation')
        return result.stdout

    def cb_allocate(self, record):
        self.cb_run(record, record['create'], timeout=240)
        observed = json.loads(self.cb_run(record, ['inspect','--provider','local-container',
                                                 '--id',record['id'],'--json']))
        if observed.get('id') != record['id']:
            raise RuntimeError('Crabbox returned a different lease')
        container = observed.get('serverId')
        if type(container) is not str or not re.fullmatch(r'[0-9a-f]{64}', container):
            raise RuntimeError('Crabbox container identity unavailable')
        # Crabbox manages the lifecycle; Docker applies the existing per-job PID ceiling.
        self._run(['update','--pids-limit',str(record['pids']),container])
        record.update(phase='allocated', container=container)
        self.cb_save(record)

    def launch(self, attempt_id, session_id, argv, env, mounts=None, *, generation=1):
        if argv != HERMES_ARGV or self.image not in self.crabbox_images:
            return super().launch(attempt_id, session_id, argv, env, mounts, generation=generation)
        if env or self.tool_network != 'bridge':
            raise RuntimeError('invalid Crabbox launch policy')
        workspace = self.make_workspace(session_id)
        native = self.native_state(session_id)
        self._mounts(mounts)
        mapping = {item['target']:item for item in mounts or []}
        if (set(mapping) - {'/run/task','/state','/inputs'} or '/run/task' not in mapping
                or '/state' not in mapping or mapping['/run/task'].get('readonly') is not True
                or mapping['/state'].get('readonly') is not False
                or _path(mapping['/state']['source']) != native
                or '/inputs' in mapping and mapping['/inputs'].get('readonly') is not True):
            raise RuntimeError('invalid Crabbox task mounts')
        taskdir = _path(mapping['/run/task']['source'])
        fd = _regular(taskdir/'task.json', 524288)
        with os.fdopen(fd) as stream: task = validate_task(json.load(stream))
        expected = {'attempt_id':attempt_id,'session_id':session_id,'generation':generation,
                    'runtime_owner':self.owner,'workspace_host':str(workspace),
                    'state_host':str(native),'tool_image':self.image}
        if any(task.get(key) != value for key,value in expected.items()):
            raise RuntimeError('Crabbox task identity mismatch')
        inputs = task['inputs']
        if len(inputs) > 32:
            raise RuntimeError('too many Crabbox inputs')
        for item in inputs:
            if ('/inputs' not in mapping or _path(item['host_path']) !=
                    _path(mapping['/inputs']['source']) / item['id']):
                raise RuntimeError('Crabbox input identity mismatch')
        pstack = self.image in self.crabbox_pstack_images
        credentials = self.pstack_credentials() if pstack else None
        rid = self.cb_id(attempt_id, generation)
        folder = self.crabbox_root / rid
        try: folder.mkdir(mode=0o700)
        except FileExistsError:
            raise RuntimeError('Crabbox launch already attempted; reconcile without replaying the model') from None
        home, payload = folder/'controller', folder/'payload'
        home.mkdir(mode=0o700); payload.mkdir(mode=0o750)
        os.chown(payload, -1, self.gid)
        for sub in ('state','cache','config'): (home/sub).mkdir(mode=0o700)
        profile = native / 'crabbox-profile'
        if not profile.exists(): profile.mkdir(mode=0o700)
        _path(profile)
        if not stat.S_ISDIR(profile.stat().st_mode) or profile.stat().st_mode & 0o022:
            raise RuntimeError('unsafe Crabbox profile')
        key, _ = read_auth(self.hermes_auth)
        with open(folder/'model-key', 'xb') as stream:
            os.fchmod(stream.fileno(),0o600); stream.write(key.encode()); stream.flush(); os.fsync(stream.fileno())
        prompt = task['prompt']
        if task.get('continuation_summary') and not task['resume_session_id']:
            combined = prompt + '\n\nPrior task context (data):\n' + task['continuation_summary']
            if len(combined.encode()) <= 262144:
                prompt = combined
        request = {'prompt':prompt, 'resume_session_id':task['resume_session_id'],
                   'api_key':key, 'inputs':[item['id'] for item in inputs]}
        if pstack:
            request['pstack'] = {'credentials': credentials}
            save(folder/'pstack-secrets.json', list(dict.fromkeys(credentials.values())))
        save(payload/'request.json', request)
        os.chown(payload/'request.json', -1, self.gid); (payload/'request.json').chmod(0o640)
        launcher = ('#!/bin/sh\nset -eu\n'
                    f'sudo -n chown {self.uid}:{self.gid} /agent-state\n'
                    'cd /workspace\n'
                    'exec /opt/hermes/venv/bin/python /opt/cloudworkbench/crabbox_guest.py < /job-launcher/request.json\n')
        (payload/'start.sh').write_text(launcher)
        os.chown(payload/'start.sh', -1, self.gid); (payload/'start.sh').chmod(0o640)
        save(home/'config.json', {'provider':'local-container','ttl':'3h','idleTimeout':'3h',
            'localContainer':{'runtime':self.docker,'image':self.image,'user':'crabbox',
            'workRoot':'/work/crabbox','cpus':int(self.cpus),'memory':f'{self.memory_mib}m',
            'network':'bridge','dockerSocket':False}})
        desktop = self.image in self.crabbox_desktop_images
        create = ['warmup','--provider','local-container','--lease-id',rid,
                  '--keep','--ttl','3h','--idle-timeout','3h']
        if desktop: create += ['--desktop','--browser','--desktop-env','xfce']
        for source,target in [(workspace,'/workspace'),(profile,'/agent-state'),(payload,'/job-launcher:ro')]:
            create += ['--local-container-volume', f'{source}:{target}']
        if inputs: create += ['--local-container-volume', mapping['/inputs']['source']+':/inputs:ro']
        record = {'id':rid,'owner':self.owner,'attempt':attempt_id,'session':session_id,
                  'generation':generation,'image':self.image,'pids':self.pids,'desktop':desktop,'pstack':pstack,
                  'phase':'creating','create':create,'pid':None,'pid_start':None}
        self.cb_save(record)
        self.cb_allocate(record)
        # Require a full job budget of token lifetime after environment provisioning.
        current_key, _ = read_auth(self.hermes_auth)
        request_changed = False
        if current_key != key:
            key_path = folder / 'model-key'
            fd = os.open(key_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(current_key.encode()); stream.flush(); os.fsync(stream.fileno())
            request['api_key'] = current_key
            request_changed = True
        if pstack:
            current_credentials = self.pstack_credentials()
            save(folder/'pstack-secrets.json', list(dict.fromkeys(
                [key, *credentials.values(), *current_credentials.values()])))
            request['pstack'] = {'credentials': current_credentials}
            request_changed = True
        if request_changed:
            save(payload/'request.json', request)
            os.chown(payload/'request.json', -1, self.gid); (payload/'request.json').chmod(0o640)
        events = native / 'events'
        events.mkdir(mode=0o700, exist_ok=True)
        _path(events)
        if events.stat().st_uid != os.geteuid() or events.stat().st_mode & 0o077:
            raise RuntimeError('unsafe Crabbox event directory')
        driver = {'command':[self.crabbox,'run','--provider','local-container','--id',rid,
                             '--keep','--stop-after','never','--no-sync','--no-hydrate',
                             *(['--desktop','--browser','--desktop-env','xfce'] if desktop else []),
                             '--','/bin/sh','/job-launcher/start.sh'],
                  'env':self.cb_env(rid), 'event_spool':str(native/'events'/task['event_spool']),
                  'secret_file':str(folder/'model-key'), 'resume_session_id':task['resume_session_id'],
                  'receipt':str(folder/'exit.json')}
        if pstack:
            driver['additional_secret_file'] = str(folder/'pstack-secrets.json')
        save(folder/'capture.json',driver)
        child = subprocess.Popen([sys.executable,'-m','cloudworkbench.crabbox_capture',str(folder/'capture.json')],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(self.hermes_source_root.parent)},
            cwd=str(folder), start_new_session=True)
        record.update(phase='executing',pid=child.pid,pid_start=process_identity(child.pid))
        self.cb_save(record)
        return rid

    def cb_alive(self, record):
        return (type(record.get('pid')) is int and record.get('pid_start') is not None
                and process_identity(record['pid']) == record['pid_start'])

    def cb_stop(self, record):
        if record['phase'] == 'stopped': return
        if record['phase'] == 'creating':
            self.cb_allocate(record)
        self.cb_run(record,['stop','--provider','local-container','--id',record['id']])
        if self.cb_alive(record):
            os.killpg(record['pid'],signal.SIGTERM)
            deadline = time.monotonic()+6
            while self.cb_alive(record) and time.monotonic()<deadline: time.sleep(.05)
            if self.cb_alive(record): os.killpg(record['pid'],signal.SIGKILL)
        record['phase']='stopped'
        self.cb_save(record)

    def status(self, runtime_id, expected_generation=None):
        if not runtime_id.startswith('cbx_'): return super().status(runtime_id,expected_generation)
        record = self.cb_record(runtime_id,expected_generation)
        if record is None: return {'state':'missing','exit_code':None,'oom':False}
        receipt = self.crabbox_root/runtime_id/'exit.json'
        if receipt.exists():
            result = private_json(receipt)
            if type(result.get('exit_code')) is not int: raise RuntimeError('invalid Crabbox exit receipt')
            self.cb_stop(record)
            return {'state':'exited','exit_code':result['exit_code'],'oom':False}
        if record['phase'] == 'stopped': return {'state':'exited','exit_code':137,'oom':False}
        if self.cb_alive(record): return {'state':'running','exit_code':None,'oom':False}
        if record['phase'] in ('creating','allocated'): return {'state':'created','exit_code':None,'oom':False}
        self.cb_stop(record)
        return {'state':'exited','exit_code':137,'oom':False}

    def stop(self,runtime_id,*,expected_generation):
        if not runtime_id.startswith('cbx_'): return super().stop(runtime_id,expected_generation=expected_generation)
        record=self.cb_record(runtime_id,expected_generation)
        if record: self.cb_stop(record)

    def cleanup(self,runtime_id,*,expected_generation):
        if not runtime_id.startswith('cbx_'): return super().cleanup(runtime_id,expected_generation=expected_generation)
        record=self.cb_record(runtime_id,expected_generation)
        if record:
            if self.cb_alive(record): raise RuntimeError('cannot clean live Crabbox capture')
            self.cb_stop(record)

    def cleanup_infrastructure(self,attempt_id,*,expected_generation):
        rid=self.cb_id(attempt_id,expected_generation)
        record=self.cb_record(rid,expected_generation)
        if record: return self.cleanup(rid,expected_generation=expected_generation)
        return super().cleanup_infrastructure(attempt_id,expected_generation=expected_generation)

    def list_owned(self):
        values=super().list_owned()
        for path in self.crabbox_root.glob('cbx_*'):
            if not (path/'record.json').exists(): continue
            record=self.cb_record(path.name)
            if record is None: continue
            if record['phase']=='stopped': continue
            values.append({'runtime_id':path.name,'labels':{
                PREFIX+'attempt':record['attempt'],PREFIX+'generation':str(record['generation']),
                PREFIX+'session':record['session'],PREFIX+'owner':self.owner}, **self.status(path.name)})
        return values

    def logs(self,runtime_id,max_bytes=65536):
        if runtime_id.startswith('cbx_'):
            self.cb_record(runtime_id)
            return b''  # The redacted event spool is authoritative for this lane.
        return super().logs(runtime_id,max_bytes)

    def forbidden_for_attempt(self, attempt):
        rid=self.cb_id(attempt['id'],attempt['generation'])
        record=self.cb_record(rid,attempt['generation'])
        additional=self.crabbox_root/rid/'pstack-secrets.json'
        forbidden = ()
        if additional.exists():
            try: forbidden = read_additional_secrets(additional)
            except Exception: raise RuntimeError('Crabbox credentials unavailable for safe collection') from None
        elif record and record.get('pstack') and attempt['state'] not in ('completed','failed','cancelled','interrupted'):
            raise RuntimeError('Crabbox credentials unavailable for safe collection')
        path=self.crabbox_root/rid/'model-key'
        if not path.exists():
            if (self.crabbox_root/rid/'record.json').exists() and attempt['state'] not in ('completed','failed','cancelled','interrupted'):
                raise RuntimeError('Crabbox credential unavailable for safe collection')
            return forbidden
        fd=_regular(path,16384,private=True)
        with os.fdopen(fd,'rb') as stream: return (stream.read(), *forbidden)

    def cleanup_terminal_secrets(self,attempt):
        rid=self.cb_id(attempt['id'],attempt['generation'])
        record=self.cb_record(rid,attempt['generation'])
        if record and record['phase']!='stopped': raise RuntimeError('Crabbox secret cleanup before stop')
        for path in (self.crabbox_root/rid/'model-key',self.crabbox_root/rid/'pstack-secrets.json',
                     self.crabbox_root/rid/'payload/request.json'):
            path.unlink(missing_ok=True)
