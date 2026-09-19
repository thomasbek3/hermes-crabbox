"""Trusted Hermes coordinator, with separately fenced tool containers.

The coordinator has host Docker authority. This is deliberately not a sandbox for
model code: terminal/file execution belongs in the labelled tool containers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .runtime import Runtime, RuntimeError, LABEL, _id, _path, _IMAGE

HERMES_ARGV = ['/opt/hermes/venv/bin/python', '-m',
               'cloudworkbench.hermes_job_entrypoint', '/run/task/task.json']
PREFIX = 'io.cloudworkbench.'
MARKER = PREFIX + 'hermes-coordinator'
CACHE_PATHS = ('cache/documents','cache/images','cache/audio','cache/videos',
               'cache/screenshots','cache/web','cache/delegation','cache/spillover',
               'images','attachments','skills')


def tool_labels(owner, attempt_id, session_id, generation, image):
    return {LABEL: 'true', PREFIX+'owner': owner, PREFIX+'attempt': attempt_id,
            PREFIX+'session': session_id, PREFIX+'generation': str(generation),
            PREFIX+'image': image, PREFIX+'role': 'hermes-tool'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _regular(path, limit, *, private=False):
    path = _path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > limit or (private and info.st_mode & 0o027)):
            raise RuntimeError('unsafe Hermes runtime file')
        return fd
    except BaseException:
        os.close(fd)
        raise


class HermesCoordinatorRuntime(Runtime):
    """Runtime facade; ordinary launches retain the base Runtime implementation."""

    def __init__(self, config):
        super().__init__(config)
        self.hermes_source_root = _path(config['hermes_source_root'])
        self.hermes_auth = _path(config['hermes_grok_auth'])
        self.hermes_journal_root = _path(config['hermes_journal_root'])
        self.docker_socket = _path(config.get('docker_socket', '/run/docker.sock'))
        for name, default in [('coordinator_uid', 959), ('coordinator_gid', 960),
                              ('docker_gid', 966), ('tool_gid', 1000), ('tool_shared_gid', 959)]:
            value = config.get(name, default)
            if type(value) is not int or not 1 <= value <= 2**31-1:
                raise RuntimeError('invalid Hermes identity configuration')
            setattr(self, name, value)
        if config.get('coordinator_network', 'bridge') != 'bridge':
            raise RuntimeError('unsupported Hermes coordinator network')
        from .hermes_job_entrypoint import IMAGE
        self.tool_network = config.get('tool_network', 'none')
        self.tool_image_allowlist = config.get('tool_image_allowlist', [IMAGE])
        if (type(self.tool_network) is not str or self.tool_network not in ('none', 'bridge')
                or type(self.tool_image_allowlist) is not list or not self.tool_image_allowlist
                or len(self.tool_image_allowlist) > 16
                or any(type(image) is not str or not _IMAGE.fullmatch(image) for image in self.tool_image_allowlist)
                or len(set(self.tool_image_allowlist)) != len(self.tool_image_allowlist)):
            raise RuntimeError('invalid Hermes tool policy')

    def readiness_reason(self):
        from .hermes_job_entrypoint import read_auth, JobError
        try:
            read_auth(self.hermes_auth)
            return None
        except JobError:
            return 'hermes_auth_invalid'
        except OSError:
            return 'hermes_auth_unavailable'

    def with_image(self, image):
        return type(self)({**self.config, 'image': image})

    def _journal_path(self, attempt_id, generation):
        _id(attempt_id)
        if type(generation) is not int or generation < 1:
            raise RuntimeError('invalid fencing generation')
        return self.hermes_journal_root / f'{attempt_id}.{generation}.json'

    def _journal_directory(self, *, create=False):
        root = self.hermes_journal_root
        if create:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not root.exists():
            return False
        info = root.lstat()
        if (root != root.resolve() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise RuntimeError('unsafe Hermes runtime journal')
        return True

    def _record(self, attempt_id, generation):
        path = self._journal_path(attempt_id, generation)
        if not self._journal_directory() or not path.exists():
            return None
        fd = _regular(path, 65536, private=True)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise RuntimeError('unsafe Hermes runtime journal')
            raw = stream.read(65537)
        try:
            record = json.loads(raw)
            fields = {'version','owner','attempt','generation','session','image','workspace','native','inputs','mounts','user','groups','cpus','memory_mib','pids'}
            if (type(record.get('version')) is not int or record['version'] not in (1, 2)
                    or set(record) != (fields if record['version'] == 1 else fields | {'tool_network','tool_memory_mib','tool_pids'})
                    or _canonical(record) != raw
                    or record['owner'] != self.owner or record['attempt'] != attempt_id
                    or type(record['generation']) is not int or record['generation'] != generation
                    or not _IMAGE.fullmatch(record['image'])
                    or record['workspace'] != str(self.root / _id(record['session']) / 'work')
                    or record['native'] != str(self.root / record['session'] / 'native')):
                raise ValueError
            if record['version'] == 2:
                from .hermes_job_entrypoint import tool_policy
                if type(record['tool_network']) is not str or record['tool_network'] not in ('none', 'bridge'):
                    raise ValueError
                policy = tool_policy(record)
                if (type(record['tool_memory_mib']) is not int or type(record['tool_pids']) is not int
                        or record['tool_memory_mib'] != policy['memory_mib'] or record['tool_pids'] != policy['pids']):
                    raise ValueError
            return record
        except (ValueError, TypeError, KeyError):
            raise RuntimeError('invalid Hermes runtime journal') from None

    def _save(self, record):
        self._journal_directory(create=True)
        destination = self._journal_path(record['attempt'], record['generation'])
        fd, name = tempfile.mkstemp(prefix='.pending-', dir=self.hermes_journal_root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(_canonical(record)); stream.flush(); os.fsync(stream.fileno())
            # No overwrite or automatic replay after an uncertain create/start.
            os.link(name, destination)
        except FileExistsError:
            raise RuntimeError('Hermes launch already attempted; reconcile without relaunch') from None
        finally:
            os.unlink(name)
        fd = os.open(self.hermes_journal_root, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def launch(self, attempt_id, session_id, argv, env, mounts=None, *, generation=1):
        if argv != HERMES_ARGV:
            return super().launch(attempt_id, session_id, argv, env, mounts, generation=generation)
        _id(attempt_id); _id(session_id)
        self._journal_path(attempt_id, generation)
        if env:
            raise RuntimeError('Hermes coordinator environment is fixed')
        workspace = self.make_workspace(session_id)
        native = self.native_state(session_id)
        self._mounts(mounts)  # Retain the existing approved source boundary.
        mount_map = {m['target']: m for m in mounts or []}
        if (set(mount_map) - {'/run/task','/state','/inputs'} or '/run/task' not in mount_map
                or mount_map['/run/task'].get('readonly', True) is not True
                or '/state' not in mount_map or mount_map['/state'].get('readonly', True) is not False
                or _path(mount_map['/state']['source']) != native
                or ('/inputs' in mount_map and mount_map['/inputs'].get('readonly', True) is not True)):
            raise RuntimeError('invalid Hermes launch mounts')
        taskdir = _path(mount_map['/run/task']['source'])
        fd = _regular(taskdir/'task.json', 262144)
        with os.fdopen(fd, 'rb') as stream:
            try: task = json.load(stream)
            except (ValueError, UnicodeError): raise RuntimeError('invalid Hermes task') from None
        from .hermes_job_entrypoint import validate_task, tool_policy, JobError
        try: validate_task(task)
        except JobError: raise RuntimeError('invalid Hermes task contract') from None
        if task['schema_version'] == 2 and (task['tool_network'] != self.tool_network
                or task['tool_image'] not in self.tool_image_allowlist):
            raise RuntimeError('Hermes tool policy is not configured')
        expected = {'attempt_id':attempt_id, 'generation':generation, 'session_id':session_id,
                    'runtime_owner':self.owner, 'workspace_host':str(workspace),
                    'state_host':str(native), 'tool_image':self.image}
        if not isinstance(task, dict) or any(task.get(k) != v or type(task.get(k)) is not type(v) for k,v in expected.items()):
            raise RuntimeError('Hermes task identity mismatch')
        inputs = task.get('inputs', [])
        if not isinstance(inputs, list) or len(inputs) > 64:
            raise RuntimeError('invalid Hermes input mounts')
        input_mounts = []
        for item in inputs:
            if (not isinstance(item, dict) or set(item) != {'id','host_path'}
                    or not isinstance(item['id'], str) or not re.fullmatch(r'[a-f0-9-]{36}', item['id'])
                    or '/inputs' not in mount_map):
                raise RuntimeError('invalid Hermes input mounts')
            source = _path(item['host_path'])
            if source != _path(mount_map['/inputs']['source']) / item['id']:
                raise RuntimeError('Hermes input mount identity mismatch')
            fd = _regular(source, 1024**3); os.close(fd)
            input_mounts.append({'source':str(source), 'target':'/inputs/'+item['id'], 'readonly':True})
        if len({v['target'] for v in input_mounts}) != len(input_mounts):
            raise RuntimeError('duplicate Hermes input mount')
        fd = _regular(self.hermes_auth, 65536, private=True); os.close(fd)
        if not stat.S_ISSOCK(self.docker_socket.stat().st_mode):
            raise RuntimeError('configured Docker socket is not a socket')
        if not self.hermes_source_root.is_dir():
            raise RuntimeError('Hermes source directory missing')
        bind = [dict(source=str(workspace),target='/workspace',readonly=False),
                dict(source=str(workspace),target=str(workspace),readonly=False),
                dict(source=str(native),target='/state',readonly=False),
                dict(source=str(native),target=str(native),readonly=False),
                dict(source=str(taskdir),target='/run/task',readonly=True),
                dict(source=str(self.hermes_source_root),target='/opt/cloudworkbench',readonly=True),
                dict(source=str(self.hermes_auth),target='/run/secrets/grok-auth.json',readonly=True),
                dict(source=self.docker,target='/usr/bin/docker',readonly=True),
                dict(source=str(self.docker_socket),target='/var/run/docker.sock',readonly=False)]
        bind.extend(dict(source=i['source'],target=i['source'],readonly=True) for i in input_mounts)
        record = dict(version=1,owner=self.owner,attempt=attempt_id,generation=generation,session=session_id,
                      image=self.image,workspace=str(workspace),native=str(native),inputs=input_mounts,mounts=bind,
                      user=f'{self.coordinator_uid}:{self.coordinator_gid}',groups=sorted({str(self.docker_gid),str(self.tool_gid),str(self.tool_shared_gid)}),
                      cpus=self.cpus,memory_mib=self.memory_mib,pids=self.pids)
        if task['schema_version'] == 2:
            policy = tool_policy(task)
            record.update(version=2,tool_network=policy['network'],
                          tool_memory_mib=policy['memory_mib'],tool_pids=policy['pids'])
        self._save(record)
        labels = tool_labels(self.owner,attempt_id,session_id,generation,self.image)
        labels[PREFIX+'role'] = 'job'; labels[MARKER] = '1'
        labels[PREFIX+'hermes-record'] = hashlib.sha256(_canonical(record)).hexdigest()
        args = ['create','--name',f'cwb2-{self.owner}-{attempt_id}']
        for key,value in labels.items(): args += ['--label',key+'='+value]
        for group in record['groups']: args += ['--group-add',group]
        args += ['--user',record['user'],
                 '--init','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                 '--network','bridge','--ipc','private','--pids-limit',str(self.pids),'--cpus',str(self.cpus),
                 '--memory',f'{self.memory_mib}m','--memory-swap',f'{self.memory_mib}m',
                 '--ulimit','nofile=1024:1024','--log-driver','local','--log-opt','max-size=10m','--log-opt','max-file=3',
                 '--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777','--workdir','/workspace']
        for item in bind:
            args += ['--mount',f"type=bind,src={item['source']},dst={item['target']}"+(',readonly' if item['readonly'] else '')]
        args += ['--env','PYTHONPATH=/opt','--env','PYTHONDONTWRITEBYTECODE=1',
                 '--env','HOME=/tmp/coordinator-home','--env','TMPDIR=/tmp',
                 '--entrypoint',argv[0],self.image,*argv[1:]]
        rid = self._run(args)
        if not re.fullmatch(r'[a-f0-9]{64}',rid):
            raise RuntimeError('unexpected Docker create response; reconcile by attempt label')
        if self._coordinator(rid,generation) != record:
            raise RuntimeError('created coordinator identity mismatch')
        self._run(['start',rid])
        return rid

    def _inspect(self, rid):
        if not re.fullmatch(r'[a-f0-9]{64}',rid):
            raise RuntimeError('complete runtime identity required')
        raw = self._run(['inspect',rid])
        try:
            value = json.loads(raw)
            if len(raw) > 262144 or not isinstance(value,list) or len(value)!=1 or value[0]['Id'] != rid:
                raise ValueError
            return value[0]
        except (ValueError,TypeError,KeyError):
            raise RuntimeError('invalid Docker identity inspection') from None

    def _coordinator(self,rid,generation):
        data = self._inspect(rid)
        labels = data.get('Config',{}).get('Labels') or {}
        if labels.get(MARKER) != '1':
            attempt = labels.get(PREFIX+'attempt')
            gen = labels.get(PREFIX+'generation')
            if (isinstance(attempt,str) and isinstance(gen,str) and gen.isdigit()
                    and self._record(attempt,int(gen)) is not None):
                raise RuntimeError('Hermes coordinator marker missing')
            return None
        try:
            record = self._record(labels[PREFIX+'attempt'], int(labels[PREFIX+'generation']))
            if record is None or (generation is not None and record['generation'] != generation): raise ValueError
            expected = tool_labels(self.owner,record['attempt'],record['session'],record['generation'],record['image'])
            expected.update({PREFIX+'role':'job',MARKER:'1',PREFIX+'hermes-record':hashlib.sha256(_canonical(record)).hexdigest()})
            host = data['HostConfig']
            mounts = sorted((m['Source'],m['Destination'],not m['RW']) for m in data['Mounts'] if m['Type']=='bind')
            expected_mounts = sorted((m['source'],m['target'],m['readonly']) for m in record['mounts'])
            if (any(labels.get(k)!=v for k,v in expected.items()) or data['Image'] != record['image']
                    or data['Name'] != '/cwb2-'+self.owner+'-'+record['attempt']
                    or data['Config']['User']!=record['user'] or host['NetworkMode']!='bridge'
                    or host['Privileged'] or not host['ReadonlyRootfs'] or host.get('PidMode') not in ('',None)
                    or host.get('IpcMode')!='private' or host.get('CapAdd') or set(host['CapDrop'])!={'ALL'}
                    or not {'no-new-privileges','no-new-privileges:true'} & set(host['SecurityOpt'])
                    or sorted(host.get('GroupAdd',[]))!=sorted(record['groups'])
                    or host['Memory']!=record['memory_mib']*1024**2 or host['MemorySwap']!=host['Memory']
                    or host['NanoCpus']!=int(record['cpus']*10**9) or host['PidsLimit']!=record['pids']
                    or mounts!=expected_mounts or any(m['Type'] not in ('bind','tmpfs') for m in data['Mounts'])):
                raise ValueError
            return record
        except (ValueError,TypeError,KeyError):
            raise RuntimeError('Hermes coordinator identity or isolation mismatch') from None

    def _tool_ids(self, record):
        return self._resource_ids(self._run(['container','ls','--all','--no-trunc',
            *self._attempt_filters(record['attempt'],record['generation']),
            '--filter','label='+PREFIX+'role=hermes-tool','--format','{{.ID}}']))

    def _cache_mount(self, record, source, destination, readonly):
        profile = Path(record['native']) / 'hermes-job' / 'hermes'
        for relative in CACHE_PATHS:
            expected = profile / relative
            if source != str(expected) or destination != '/root/.hermes/' + relative:
                continue
            if not readonly:
                return False
            # Every traversed private child must still be a real coordinator-owned
            # directory. No profile-wide mount, credential, or arbitrary cache alias.
            path = Path(record['native'])
            for part in ('hermes-job', 'hermes', *Path(relative).parts):
                path = path / part
                info = path.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != int(record['user'].split(':')[0])
                        or info.st_mode & 0o022 or path != path.resolve()):
                    return False
            return True
        return False

    def _clean_tools(self, record):
        # The caller first proves no live coordinator can create new siblings.
        for rid in self._tool_ids(record):
            data = self._inspect(rid)
            labels = data.get('Config',{}).get('Labels') or {}
            expected = tool_labels(self.owner,record['attempt'],record['session'],record['generation'],record['image'])
            if any(labels.get(k)!=v for k,v in expected.items()):
                raise RuntimeError('Hermes tool ownership mismatch')
            # Fence even a misconfigured *owned* writer; refuse a clean claim afterward.
            if data['State'].get('Running'):
                self._run(['stop','--time','10',rid],timeout=20)
                data = self._inspect(rid)
                if data['State'].get('Running'): raise RuntimeError('Hermes tool did not stop')
            host = data['HostConfig']
            binds = sorted((m['Source'],m['Destination'],not m['RW']) for m in data['Mounts'] if m['Type']=='bind')
            binds = [m for m in binds if not self._cache_mount(record,*m)]
            expected_binds = sorted([(record['workspace'],'/workspace',False)]+
                                   [(i['source'],i['target'],True) for i in record['inputs']])
            if (data['Image']!=record['image'] or host['NetworkMode']!=record.get('tool_network','none') or host['Privileged']
                    or data['Config'].get('User') != '1000:1000'
                    or set(host.get('GroupAdd') or []) != {str(self.tool_shared_gid)}
                    or host.get('PidsLimit') != record.get('tool_pids',128) or host.get('NanoCpus') != 10**9
                    or host.get('Memory') != record.get('tool_memory_mib',1536)*1024**2
                    or host.get('MemorySwap') != record.get('tool_memory_mib',1536)*1024**2
                    or set(host.get('CapDrop') or []) != {'ALL'}
                    or not {cap.removeprefix('CAP_') for cap in (host.get('CapAdd') or [])
                            if isinstance(cap,str)} <= {'DAC_OVERRIDE','CHOWN','FOWNER','SETUID','SETGID','SYS_CHROOT'}
                    or any(not isinstance(cap,str) for cap in (host.get('CapAdd') or []))
                    or not {'no-new-privileges','no-new-privileges:true'} & set(host.get('SecurityOpt') or [])
                    or host.get('PidMode') not in ('',None) or host.get('IpcMode') not in ('private','',None)
                    or host.get('Devices') or host.get('DeviceRequests')
                    or binds!=expected_binds or any(m['Type'] not in ('bind','tmpfs') for m in data['Mounts'])):
                raise RuntimeError('Hermes tool isolation mismatch; stopped for reconciliation')
            self._run(['rm',rid])
        if self._tool_ids(record): raise RuntimeError('Hermes tools remain after cleanup')

    def status(self,runtime_id,expected_generation=None):
        status = super().status(runtime_id,expected_generation)
        if status['state']=='missing': return status
        record = self._coordinator(runtime_id,expected_generation)
        if record and status['state']=='exited':
            self.cleanup_infrastructure(record['attempt'],expected_generation=record['generation'])
        return status

    def stop(self,runtime_id,*,expected_generation):
        state = self._owned(runtime_id,expected_generation)
        if state is None: return
        record = self._coordinator(runtime_id,expected_generation)
        super().stop(runtime_id,expected_generation=expected_generation)
        if record:
            self.cleanup_infrastructure(record['attempt'],expected_generation=expected_generation)

    def cleanup(self,runtime_id,*,expected_generation):
        state = self._owned(runtime_id,expected_generation)
        if state is None: return
        record = self._coordinator(runtime_id,expected_generation)
        if record:
            if state.get('Running'): raise RuntimeError('refusing cleanup of live runtime')
            self.cleanup_infrastructure(record['attempt'],expected_generation=expected_generation)
        super().cleanup(runtime_id,expected_generation=expected_generation)

    def cleanup_infrastructure(self,attempt_id,*,expected_generation):
        record = self._record(attempt_id,expected_generation)
        if record:
            jobs = self._resource_ids(self._run(['container','ls','--all','--no-trunc',
                *self._attempt_filters(attempt_id,expected_generation),
                '--filter','label='+PREFIX+'role=job','--format','{{.ID}}']))
            if len(jobs)>1: raise RuntimeError('multiple Hermes coordinators require reconciliation')
            for rid in jobs:
                if self._coordinator(rid,expected_generation) != record:
                    raise RuntimeError('Hermes coordinator ownership mismatch')
                state = self._owned(rid,expected_generation)
                if state is None or state.get('Running'):
                    raise RuntimeError('cannot clean tools while coordinator state is uncertain or live')
            self._clean_tools(record)
        super().cleanup_infrastructure(attempt_id,expected_generation=expected_generation)
