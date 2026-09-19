"""Exact per-check Docker lifecycle. Results are exit observations, never gates."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
import threading

from .runtime import Runtime, RuntimeError, _path
from .routed_runtime import RoutedRuntime, _serialized
from .routed_export import _bounded_run
from .routed_observation_store import validate_forbidden_values

_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')
_HEX=re.compile(r'[0-9a-f]{64}\Z')
_IMAGE=re.compile(r'sha256:[0-9a-f]{64}\Z')
_INTERPRETERS={'/opt/hermes/venv/bin/python':{'-I','-B','-S','-u'},
    '/usr/bin/python3':{'-I','-B','-S','-u'},'/usr/local/bin/python3':{'-I','-B','-S','-u'},
    '/bin/sh':{'-e','-u'},'/bin/bash':{'-e','-u','--noprofile','--norc'},
    '/usr/bin/node':set(),'/usr/local/bin/node':set()}
_LOG_CAP=1024**2
_TMPFS={'/tmp':'rw,nosuid,nodev,noexec,size=64m,mode=1777'}


class VerifierRuntimeError(RuntimeError):
    pass


def _json(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def _sha(body):return hashlib.sha256(body).hexdigest()


def _posix_access(info,uid,gid,required=0o5):
    """Check mode bits for the fixed container identity, with no extra groups."""
    shift=6 if info.st_uid==uid else 3 if info.st_gid==gid else 0
    return ((stat.S_IMODE(info.st_mode)>>shift)&required)==required


def _script(argv):
    if (type(argv) is not tuple or not 1<=len(argv)<=64
        or any(type(a) is not str or not a or len(a.encode())>4096 or any(ord(c)<32 for c in a) for a in argv)
        or sum(len(a.encode()) for a in argv)>32768):raise VerifierRuntimeError('verifier_argv_invalid')
    index=0
    if argv[0] in _INTERPRETERS:
        index=1
        while index<len(argv) and argv[index] in _INTERPRETERS[argv[0]]:index+=1
    if index>=len(argv) or not re.fullmatch(r'/run/task/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',argv[index]):
        raise VerifierRuntimeError('verifier_protected_script_required')
    if index==0 and not argv[0].startswith('/run/task/'):raise VerifierRuntimeError('verifier_protected_script_required')
    return argv[index].rsplit('/',1)[-1]


@dataclass(frozen=True)
class CheckRuntimeSpec:
    owner: str
    attempt_id: str
    generation: int
    root_id: str
    root_generation: int
    plan_sha256: str
    check_id: str
    image: str
    candidate_path: Path
    task_path: Path
    candidate_identity: tuple[int,int]
    task_identity: tuple[int,int]
    argv: tuple[str,...]
    timeout_seconds: int
    uid: int=1000
    gid: int=1000
    cpus: float=2
    memory_mib: int=4096
    pids: int=512
    max_log_bytes: int=65536

    def __post_init__(self):
        if any(type(v) is not str or not _ID.fullmatch(v) for v in (self.owner,self.attempt_id,self.root_id)):
            raise VerifierRuntimeError('verifier_identity_invalid')
        if type(self.check_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',self.check_id):
            raise VerifierRuntimeError('verifier_identity_invalid')
        if any(type(v) is not int or not 1<=v<=2**31-1 for v in (self.generation,self.root_generation)):
            raise VerifierRuntimeError('verifier_generation_invalid')
        if (type(self.plan_sha256) is not str or not _HEX.fullmatch(self.plan_sha256)
            or type(self.image) is not str or not _IMAGE.fullmatch(self.image)):
            raise VerifierRuntimeError('verifier_digest_invalid')
        for path in (self.candidate_path,self.task_path):
            if not isinstance(path,Path) or '..' in path.parts or any(ord(c)<32 for c in str(path)):
                raise VerifierRuntimeError('verifier_path_invalid')
            if not path.is_absolute() or any(c in str(path) for c in (',','\x00')):
                raise VerifierRuntimeError('verifier_path_invalid')
        if (self.candidate_path==self.task_path or self.candidate_path in self.task_path.parents
            or self.task_path in self.candidate_path.parents):raise VerifierRuntimeError('verifier_mount_overlap')
        for identity in (self.candidate_identity,self.task_identity):
            if type(identity) is not tuple or len(identity)!=2 or any(type(v) is not int or v<=0 for v in identity):
                raise VerifierRuntimeError('verifier_directory_identity_invalid')
        if (type(self.timeout_seconds) is not int or not 1<=self.timeout_seconds<=600
            or type(self.uid) is not int or self.uid!=1000 or type(self.gid) is not int or self.gid!=1000
            or type(self.cpus) not in (int,float) or not math.isfinite(self.cpus) or not 1<=self.cpus<=2
            or type(self.memory_mib) is not int or not 128<=self.memory_mib<=4096
            or type(self.pids) is not int or not 16<=self.pids<=512
            or type(self.max_log_bytes) is not int or not 0<=self.max_log_bytes<=_LOG_CAP):raise VerifierRuntimeError('verifier_limits_invalid')
        _script(self.argv)

    @property
    def document(self):
        result=asdict(self);result['candidate_path']=str(self.candidate_path);result['task_path']=str(self.task_path)
        return result
    @property
    def digest(self):return _sha(_json(self.document))
    @property
    def name(self):return 'cwb2-verifier-'+_sha(_json([self.owner,self.attempt_id,self.generation,self.check_id]))[:32]


@dataclass(frozen=True)
class CheckResult:
    spec_sha256: str
    runtime_id: str
    exit_code: int
    oom: bool
    timed_out: bool
    cleanup_confirmed: bool
    logs_sha256: str
    logs: bytes=field(repr=False)


class VerifierRuntime:
    def __init__(self,base,*,journal_root):
        if not isinstance(base,Runtime):raise VerifierRuntimeError('verifier_runtime_required')
        self.base=base;self.root=_path(journal_root)
        self.root.mkdir(mode=0o700,parents=False,exist_ok=True);RoutedRuntime._private(self.root,directory=True)
        self._local=threading.local()

    @property
    def authorize(self):return getattr(self._local,"authorize",None)
    @authorize.setter
    def authorize(self,value):self._local.authorize=value
    @property
    def cleanup_deadline(self):return getattr(self._local,"cleanup_deadline",None)
    @cleanup_deadline.setter
    def cleanup_deadline(self,value):self._local.cleanup_deadline=value

    def _validate_spec(self,spec):
        if type(spec) is not CheckRuntimeSpec or spec.owner!=self.base.owner or spec.image!=self.base.image:
            raise VerifierRuntimeError('verifier_runtime_binding')
        spec.__post_init__()
        if any(path==self.root or path in self.root.parents or self.root in path.parents
            for path in (spec.candidate_path,spec.task_path)):
            raise VerifierRuntimeError('verifier_journal_mount_overlap')

    def _auth(self,action):
        if not callable(self.authorize) or self.authorize(action) is not True:
            raise VerifierRuntimeError('verifier_authority_unavailable')

    def _folder(self,spec):return self.root/spec.name

    def _save(self,spec,value):
        root=self._folder(spec);RoutedRuntime._private(root,directory=True)
        body=_json(value)
        if len(body)>65536:raise VerifierRuntimeError('verifier_journal_bound')
        fd,name=tempfile.mkstemp(prefix='.journal-',dir=root)
        try:
            with os.fdopen(fd,'wb') as output:output.write(body);output.flush();os.fsync(output.fileno())
            os.replace(name,root/'journal.json')
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:
            if os.path.exists(name):os.unlink(name)

    def _load(self,spec):
        root=self._folder(spec);RoutedRuntime._private(root,directory=True)
        path=root/'journal.json';RoutedRuntime._private(path)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            if os.fstat(fd).st_size>65536:raise VerifierRuntimeError('verifier_journal_bound')
            body=os.read(fd,65537)
        finally:os.close(fd)
        try:value=json.loads(body)
        except (ValueError,UnicodeError):raise VerifierRuntimeError('verifier_journal_invalid') from None
        if (type(value) is not dict or value.get('version')!=1 or value.get('spec')!=json.loads(_json(spec.document))
            or value.get('spec_sha256')!=spec.digest or value.get('state') not in ('create_intent','created','start_intent','started','observed','remove_intent','removal_confirmed','cleaned')
            or value.get('runtime_id') is not None and (type(value['runtime_id']) is not str or not _HEX.fullmatch(value['runtime_id']))):
            raise VerifierRuntimeError('verifier_journal_invalid')
        if (type(value.get('timed_out')) is not bool
            or type(value.get('script_sha256')) is not str or not _HEX.fullmatch(value['script_sha256'])
            or value.get('deadline_at') is not None and
                (type(value['deadline_at']) not in (int,float) or not math.isfinite(value['deadline_at']) or value['deadline_at']<=0)
            or value.get('aborted') not in (None,'interrupted','logs_rejected','diagnostics_unavailable','check_not_completed','completion_timing_unproven')):
            raise VerifierRuntimeError('verifier_journal_invalid')
        observation=value.get('observation')
        if observation is not None:
            if (type(observation) is not dict or set(observation)!={'exit_code','oom','timed_out','logs_sha256','logs_size','logs_identity'}
                or type(observation['exit_code']) is not int or not 0<=observation['exit_code']<=255
                or type(observation['oom']) is not bool or type(observation['timed_out']) is not bool
                or type(observation['logs_sha256']) is not str or not _HEX.fullmatch(observation['logs_sha256'])
                or type(observation['logs_size']) is not int or not 0<=observation['logs_size']<=spec.max_log_bytes
                or type(observation['logs_identity']) is not list or len(observation['logs_identity'])!=2
                or any(type(v) is not int or v<=0 for v in observation['logs_identity'])):
                raise VerifierRuntimeError('verifier_journal_invalid')
        return value

    def _paths(self,spec):
        for path,identity in ((spec.candidate_path,spec.candidate_identity),(spec.task_path,spec.task_identity)):
            _path(path);info=path.lstat()
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev,info.st_ino)!=identity:
                raise VerifierRuntimeError('verifier_directory_changed')
            if not _posix_access(info,spec.uid,spec.gid):
                raise VerifierRuntimeError('verifier_access_unavailable')
            if not any(path.is_relative_to(root) for root in self.base.allowed_roots):
                raise VerifierRuntimeError('verifier_mount_unapproved')
        info=spec.task_path.lstat()
        if info.st_uid not in (0,os.geteuid()) or info.st_mode&0o022:
            raise VerifierRuntimeError('verifier_task_untrusted')
        script=spec.task_path/_script(spec.argv)
        fd=os.open(script,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid not in (0,os.geteuid())
                or info.st_mode&0o022 or not 0<info.st_size<=1024**2):raise VerifierRuntimeError('verifier_script_untrusted')
            if not _posix_access(info,spec.uid,spec.gid):
                raise VerifierRuntimeError('verifier_access_unavailable')
            body=os.read(fd,1024**2+1);after=os.fstat(fd)
            if (len(body)!=info.st_size or (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)):
                raise VerifierRuntimeError('verifier_script_changed')
            return _sha(body)
        finally:os.close(fd)

    def _labels(self,spec):
        return {'io.cloudworkbench.managed':'true','io.cloudworkbench.owner':spec.owner,
            'io.cloudworkbench.attempt':spec.attempt_id,'io.cloudworkbench.generation':str(spec.generation),
            'io.cloudworkbench.root':spec.root_id,'io.cloudworkbench.root-generation':str(spec.root_generation),
            'io.cloudworkbench.role':'routed-verifier','io.cloudworkbench.check':spec.check_id,
            'io.cloudworkbench.verifier-spec':spec.digest,'io.cloudworkbench.plan':spec.plan_sha256}

    @staticmethod
    def _command(spec):return ['-i','PATH=/usr/local/bin:/usr/bin:/bin','HOME=/tmp','TMPDIR=/tmp','LANG=C.UTF-8',*spec.argv]

    def _rpc(self,action,args,*,timeout=5,preflight=None):
        self._auth(action)
        if preflight is not None:preflight()
        if self.cleanup_deadline is not None:
            timeout=min(timeout,self.cleanup_deadline-time.monotonic())
            if timeout<=0:raise VerifierRuntimeError('verifier_cleanup_deadline')
        return _bounded_run(self.base,args,timeout=timeout,cap=256*1024).decode('utf-8').strip()

    def _ids(self,identity):
        ids=self._rpc('cleanup_inspect',['container','ls','--all','--no-trunc','--filter','id='+identity,'--format','{{.ID}}']).splitlines()
        if ids not in ([],[identity]):raise VerifierRuntimeError('verifier_identity_unresolved')
        return ids

    def _inspect(self,spec,identity,*,cleanup=False):
        objects=json.loads(self._rpc('cleanup_inspect' if cleanup else 'inspect',['inspect',identity]))
        if type(objects) is not list or len(objects)!=1:raise VerifierRuntimeError('verifier_inspection_unresolved')
        obj=objects[0];host=obj.get('HostConfig',{});config=obj.get('Config',{});labels=config.get('Labels') or {}
        expected=self._labels(spec);inert={'io.cloudworkbench.build-owner','io.cloudworkbench.candidate'}
        if (obj.get('Id')!=identity or obj.get('Name')!='/'+spec.name or obj.get('Image')!=spec.image
            or any(labels.get(k)!=v for k,v in expected.items())
            or any(k.startswith('io.cloudworkbench.') and k not in expected and k not in inert for k in labels)
            or config.get('Entrypoint')!=['/usr/bin/env'] or config.get('Cmd')!=self._command(spec)
            or config.get('User')!=f'{spec.uid}:{spec.gid}' or config.get('WorkingDir')!='/workspace'
            or config.get('Tty') or host.get('Privileged') or host.get('ReadonlyRootfs') is not True
            or host.get('NetworkMode')!='none' or host.get('Dns')!=['127.0.0.1']
            or set(obj.get('NetworkSettings',{}).get('Networks',{}))!={'none'}
            or host.get('CapDrop')!=['ALL'] or host.get('CapAdd') or host.get('IpcMode')!='private'
            or host.get('PidMode') not in ('',None) or host.get('UTSMode') not in ('',None)
            or set(host.get('SecurityOpt',[])) not in ({'no-new-privileges'},{'no-new-privileges:true'})
            or any(host.get(k) for k in ('Devices','DeviceRequests','VolumesFrom','ExtraHosts','PublishAllPorts','Binds','PortBindings','AutoRemove','GroupAdd'))
            or host.get('RestartPolicy')!={'Name':'no','MaximumRetryCount':0}
            or host.get('Memory')!=spec.memory_mib*1024**2 or host.get('MemorySwap')!=spec.memory_mib*1024**2
            or host.get('NanoCpus')!=round(spec.cpus*1e9) or host.get('PidsLimit')!=spec.pids
            or host.get('LogConfig')!={'Type':'local','Config':{'max-size':'1m','max-file':'2'}}
            or host.get('Tmpfs')!=_TMPFS):raise VerifierRuntimeError('verifier_container_policy')
        expected_mounts={'/workspace':str(spec.candidate_path),'/run/task':str(spec.task_path)}
        all_mounts=obj.get('Mounts',[])
        tmpfs=[m for m in all_mounts if m.get('Type')=='tmpfs']
        if len(tmpfs)>1 or any(m.get('Destination')!='/tmp' or m.get('RW') is not True for m in tmpfs):
            raise VerifierRuntimeError('verifier_mount_policy')
        mounts=[m for m in all_mounts if m.get('Type')!='tmpfs']
        if (len(mounts)!=2 or {m.get('Destination') for m in mounts}!=set(expected_mounts)
            or any(m.get('Type')!='bind' or m.get('RW') is not False or m.get('Propagation')!='rprivate' or expected_mounts.get(m.get('Destination'))!=m.get('Source') for m in mounts)):
            raise VerifierRuntimeError('verifier_mount_policy')
        return obj['State']

    def _logs(self,spec,identity):
        self._auth('cleanup_logs')
        timeout=5 if self.cleanup_deadline is None else min(5,self.cleanup_deadline-time.monotonic())
        if timeout<=0:raise VerifierRuntimeError('verifier_cleanup_deadline')
        return _log_bytes(self.base,identity,timeout,spec.max_log_bytes)

    def _observe(self,spec,value,state,forbidden):
        if value.get('observation') is not None or value.get('aborted'):return
        if (state.get('Status')!='exited' or state.get('Running') is not False
            or type(state.get('ExitCode')) is not int or not 0<=state['ExitCode']<=255
            or type(state.get('OOMKilled')) is not bool):raise VerifierRuntimeError('verifier_exit_unconfirmed')
        logs=self._logs(spec,value['runtime_id'])
        try:_scan_logs(logs,forbidden)
        except VerifierRuntimeError:
            value['aborted']='logs_rejected';self._save(spec,value)
            raise
        path=self._folder(spec)/'logs.bin'
        try:fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            RoutedRuntime._private(path)
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                info=os.fstat(fd)
                if info.st_size!=len(logs) or os.read(fd,spec.max_log_bytes+1)!=logs:
                    raise VerifierRuntimeError('verifier_logs_changed')
                os.fsync(fd)
            finally:os.close(fd)
        else:
            try:
                with os.fdopen(fd,'wb',closefd=False) as output:output.write(logs);output.flush();os.fsync(fd)
                info=os.fstat(fd)
            finally:os.close(fd)
        value['observation']={'exit_code':state['ExitCode'],'oom':state['OOMKilled'],
            'timed_out':value['timed_out'] or time.time()>value['deadline_at'],
            'logs_sha256':_sha(logs),'logs_size':len(logs),'logs_identity':[info.st_dev,info.st_ino]}
        value['state']='observed';self._save(spec,value)

    def _cleanup(self,spec,value,*,forbidden,recovery):
        identity=value['runtime_id'];collection_error=None
        if self._ids(identity):
            if value['state'] in ('removal_confirmed','cleaned'):raise VerifierRuntimeError('verifier_reappeared')
            state=self._inspect(spec,identity,cleanup=True)
            if state.get('Running'):
                if recovery and not value['timed_out']:
                    value['aborted']='interrupted';self._save(spec,value)
                self._rpc('stop',['stop','--time','1',identity]);state=self._inspect(spec,identity,cleanup=True)
            if state.get('Running') is not False or state.get('Status') not in ('created','exited','dead'):
                raise VerifierRuntimeError('verifier_stop_unconfirmed')
            if recovery and not value.get('aborted') and value.get('observation') is None and state.get('Status')=='exited':
                # A previous process's wall deadline cannot prove elapsed time
                # across clock changes. Do not synthesize a usable observation.
                value['aborted']='completion_timing_unproven';self._save(spec,value)
            if not value.get('aborted') and value.get('observation') is None and state.get('Status')=='exited':
                try:self._observe(spec,value,state,forbidden)
                except Exception as error:
                    collection_error=error
                    value.setdefault('aborted','diagnostics_unavailable');self._save(spec,value)
            if value.get('observation') is None:
                value.setdefault('aborted','check_not_completed')
            value['state']='remove_intent';self._save(spec,value)
            self._rpc('remove',['rm',identity])
            value['state']='removal_confirmed';self._save(spec,value)
        elif value['state'] not in ('removal_confirmed','cleaned'):
            raise VerifierRuntimeError('verifier_absence_not_cleanup')
        if self._ids(identity):raise VerifierRuntimeError('verifier_removal_unconfirmed')
        value['state']='cleaned';self._save(spec,value)
        if collection_error is not None and not recovery:raise collection_error

    def _reconcile_locked(self,spec,forbidden):
        previous=self.cleanup_deadline;self.cleanup_deadline=time.monotonic()+15
        try:
            self._auth('reconcile')
            if not os.path.lexists(self._folder(spec)):
                return {'state':'not_started','spec_sha256':spec.digest,'runtime_id':None,
                    'cleanup_confirmed':True,'result_available':False,'aborted':None}
            value=self._load(spec)
            if value['runtime_id'] is None:
                args=['container','ls','--all','--no-trunc']
                for key,label in self._labels(spec).items():args+=['--filter','label='+key+'='+label]
                ids=self._rpc('cleanup_inspect',[*args,'--format','{{.ID}}']).splitlines()
                if len(ids)!=1 or not _HEX.fullmatch(ids[0]):raise VerifierRuntimeError('verifier_create_unresolved')
                state=self._inspect(spec,ids[0],cleanup=True)
                if state.get('Status')!='created' or state.get('Running') is not False:raise VerifierRuntimeError('verifier_unacknowledged_start')
                value['runtime_id']=ids[0];self._save(spec,value)
            self._cleanup(spec,value,forbidden=forbidden,recovery=True)
            return {'state':'cleaned','spec_sha256':spec.digest,'runtime_id':value['runtime_id'],'cleanup_confirmed':True,
                'result_available':value.get('observation') is not None and not value.get('aborted'),
                'aborted':value.get('aborted')}
        finally:self.cleanup_deadline=previous

    @staticmethod
    def _forbidden(values):
        try:return validate_forbidden_values(values)
        except ValueError:raise VerifierRuntimeError('verifier_secret_policy') from None

    @_serialized
    def run(self,spec,*,authorize,forbidden_values=()):
        self.authorize=authorize;forbidden=self._forbidden(forbidden_values);owned=False
        try:
            self._auth('prepare')
            if os.path.lexists(self._folder(spec)):return self._result(spec,forbidden)
            script_sha=self._paths(spec)
            try:self._folder(spec).mkdir(mode=0o700)
            except FileExistsError:raise VerifierRuntimeError('verifier_already_attempted') from None
            owned=True
            parent_fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent_fd)
            finally:os.close(parent_fd)
            value={'version':1,'spec':spec.document,'spec_sha256':spec.digest,'state':'create_intent',
                'runtime_id':None,'script_sha256':script_sha,'timed_out':False,'deadline_at':None}
            self._save(spec,value)
            args=['create','--name',spec.name,'--network','none','--dns','127.0.0.1','--read-only',
                '--user',f'{spec.uid}:{spec.gid}','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--ipc','private','--restart','no','--cpus',str(spec.cpus),'--memory',f'{spec.memory_mib}m',
                '--memory-swap',f'{spec.memory_mib}m','--pids-limit',str(spec.pids),
                '--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2','--workdir','/workspace']
            for k,v in self._labels(spec).items():args+=['--label',k+'='+v]
            args+=['--tmpfs','/tmp:'+_TMPFS['/tmp']]
            for source,target in ((spec.candidate_path,'/workspace'),(spec.task_path,'/run/task')):
                args+=['--mount',f'type=bind,src={source},dst={target},readonly']
            args+=['--entrypoint','/usr/bin/env',spec.image,*self._command(spec)]
            def preflight():
                if self._paths(spec)!=script_sha:raise VerifierRuntimeError('verifier_script_changed')
            identity=self._rpc('create',args,preflight=preflight)
            if not _HEX.fullmatch(identity):raise VerifierRuntimeError('verifier_create_unconfirmed')
            value['runtime_id']=identity;self._save(spec,value)
            state=self._inspect(spec,identity)
            if state.get('Status')!='created' or state.get('Running') is not False:raise VerifierRuntimeError('verifier_created_state')
            value['state']='created';self._save(spec,value)
            value['state']='start_intent';value['deadline_at']=time.time()+spec.timeout_seconds;self._save(spec,value)
            execution_deadline=time.monotonic()+spec.timeout_seconds
            self._rpc('start',['start',identity],preflight=preflight);value['state']='started';self._save(spec,value)
            while True:
                state=self._inspect(spec,identity)
                if time.monotonic()>=execution_deadline:
                    value['timed_out']=True;self._save(spec,value);break
                if state.get('Status')=='exited' and state.get('Running') is False:break
                time.sleep(min(.05,max(0,execution_deadline-time.monotonic())))
            previous=self.cleanup_deadline;self.cleanup_deadline=time.monotonic()+15
            try:self._cleanup(spec,value,forbidden=forbidden,recovery=False)
            finally:self.cleanup_deadline=previous
            return self._result(spec,forbidden)
        except Exception as error:
            if owned:
                try:
                    proof=self._reconcile_locked(spec,forbidden)
                    error.cleanup_status='confirmed';error.result_available=proof['result_available']
                except Exception:
                    error.cleanup_status='unresolved';error.add_note('Exact verifier cleanup remains unresolved; reconcile its private journal.')
            raise

    @_serialized
    def reconcile(self,spec,*,authorize,forbidden_values=()):
        self.authorize=authorize
        return self._reconcile_locked(spec,self._forbidden(forbidden_values))

    def _result(self,spec,forbidden):
        self._auth('publish');value=self._load(spec);observation=value.get('observation')
        if value['state']!='cleaned' or value.get('aborted') or type(observation) is not dict:
            raise VerifierRuntimeError('verifier_result_unavailable')
        if self._ids(value['runtime_id']):raise VerifierRuntimeError('verifier_reappeared')
        path=self._folder(spec)/'logs.bin';RoutedRuntime._private(path)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(fd)
            if before.st_size>spec.max_log_bytes or [before.st_dev,before.st_ino]!=observation['logs_identity']:
                raise VerifierRuntimeError('verifier_logs_changed')
            logs=os.read(fd,spec.max_log_bytes+1);after=os.fstat(fd)
            if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
                or len(logs)!=observation['logs_size'] or _sha(logs)!=observation['logs_sha256']):
                raise VerifierRuntimeError('verifier_logs_changed')
        finally:os.close(fd)
        _scan_logs(logs,forbidden)
        if (type(observation['exit_code']) is not int or not 0<=observation['exit_code']<=255
            or type(observation['oom']) is not bool or type(observation['timed_out']) is not bool):
            raise VerifierRuntimeError('verifier_result_invalid')
        self._auth('publish')
        return CheckResult(spec.digest,value['runtime_id'],observation['exit_code'],observation['oom'],
            observation['timed_out'],True,observation['logs_sha256'],logs)

    @_serialized
    def load(self,spec,*,authorize,forbidden_values=()):
        self.authorize=authorize;self._auth('load')
        return self._result(spec,self._forbidden(forbidden_values))


def _log_bytes(base,identity,timeout,cap):
    """Bounded merged Docker-retained logs, with no diagnostics persisted here."""
    try:process=subprocess.Popen([base.docker,'logs',identity],stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},start_new_session=True)
    except OSError:raise VerifierRuntimeError('verifier_logs_unavailable') from None
    output=bytearray();reaped=False;deadline=time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(),False);selector.register(process.stdout,selectors.EVENT_READ)
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:raise VerifierRuntimeError('verifier_logs_timeout')
                for key,_ in selector.select(min(remaining,.1)):
                    body=os.read(key.fd,65536)
                    if not body:selector.unregister(key.fileobj);continue
                    if len(output)+len(body)>cap:raise VerifierRuntimeError('verifier_logs_bound')
                    output.extend(body)
            remaining=deadline-time.monotonic()
            if remaining<=0:raise VerifierRuntimeError('verifier_logs_timeout')
            try:code=process.wait(timeout=remaining);reaped=True
            except subprocess.TimeoutExpired:raise VerifierRuntimeError('verifier_logs_timeout') from None
            if code:raise VerifierRuntimeError('verifier_logs_unavailable')
            return bytes(output)
    finally:
        if not reaped:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:pass
        process.stdout.close()


def _scan_logs(raw,forbidden):
    if not forbidden:return
    def scan(value):
        if any(secret in value for secret in forbidden):raise VerifierRuntimeError('verifier_logs_rejected')
    scan(raw)
    # Diagnostic text is not required to be JSON. Also scan decoded strings in
    # complete JSON documents/JSONL so common escaping cannot evade the policy.
    for body in [raw,*raw.splitlines()]:
        try:parsed=json.loads(body)
        except (ValueError,UnicodeError):continue
        except RecursionError:raise VerifierRuntimeError('verifier_logs_structure_bound') from None
        pending=[parsed]
        while pending:
            item=pending.pop()
            if isinstance(item,str):
                try:scan(item.encode())
                except UnicodeError:raise VerifierRuntimeError('verifier_logs_structure_invalid') from None
            elif isinstance(item,dict):pending.extend(item.keys());pending.extend(item.values())
            elif isinstance(item,list):pending.extend(item)
