"""Controller-only routed caller Docker lifecycle, composed with the existing Runtime.

Creates no provider credentials or account authority. Unknown RPC outcomes stay held.
"""
from dataclasses import asdict, dataclass
import fcntl
from functools import wraps
import time
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .hermes_adapter import RoutedLaunchPlan
from .runtime import RuntimeError, _id, _path, LABEL

_INIT = "import signal,time,sys;signal.signal(signal.SIGTERM,lambda *a:sys.exit(0));exec('while True: time.sleep(1)')"
_PYTHON = '/opt/hermes/venv/bin/python'
_SOURCE_FILES = frozenset('__init__.py routed_caller.py hermes_adapter.py pstack_routing.py inference_relay.py inference_service.py adapters.py'.split())
_BOOTSTRAP_ENV = {'PYTHONPATH':'/run/task/source','PYTHONDONTWRITEBYTECODE':'1'}
_TMPFS = {
    '/tmp':'rw,nosuid,nodev,noexec,size=64m,mode=1777',
    '/run/relay':'rw,nosuid,nodev,noexec,size=32m,uid=1001,gid=1001,mode=0700',
    '/run/tool':'rw,nosuid,nodev,noexec,size=128m,uid=1000,gid=1000,mode=0700',
    '/run/tool/hermes':'rw,nosuid,nodev,noexec,size=32m,uid=1000,gid=1000,mode=0700',
}

def _json(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def _hash(value):return hashlib.sha256(_json(value).encode()).hexdigest()


def _serialized(method):
    @wraps(method)
    def wrapped(self,spec,*args,**kwargs):
        self._validate_spec(spec)
        path=self.root/(spec.attempt_id+'.'+str(spec.generation)+'.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if info.st_uid!=os.geteuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1:raise RuntimeError('unsafe routed lock')
            deadline=time.monotonic()+5
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise RuntimeError('routed lifecycle busy') from None
                    time.sleep(.01)
            return method(self,spec,*args,**kwargs)
        finally:os.close(fd)
    return wrapped


@dataclass(frozen=True)
class CallerSpec:
    attempt_id: str
    session_id: str
    generation: int
    image: str
    owner: str
    workspace: str
    scratch: str
    task_dir: str
    worker_socket_dir: str
    workspace_readonly: bool
    task_files: tuple[tuple[str,str], ...]
    limits: tuple[float,int,int]

    @property
    def digest(self):return _hash(asdict(self))
    @property
    def name(self):return f'cwb2-{self.owner}-{self.attempt_id}-routed'


class RoutedRuntime:
    def __init__(self,runtime,*,journal_root):
        self.runtime=runtime
        self.root=_path(journal_root)
        self.root.mkdir(mode=0o700,parents=False,exist_ok=True)
        self._private(self.root,directory=True)

    @staticmethod
    def _private(path,*,directory=False):
        info=path.lstat()
        if ((not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
                or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o777!=(0o700 if directory else 0o600)
                or (not directory and info.st_nlink!=1)):
            raise RuntimeError('unsafe routed journal')

    def _folder(self,spec):
        return self.root/(spec.attempt_id+'.'+str(spec.generation))

    def _save(self,spec,journal):
        folder=self._folder(spec);self._private(folder,directory=True)
        body=_json(journal).encode()
        if len(body)>16384:raise RuntimeError('routed journal too large')
        # Never remove a pre-existing temporary file: an interrupted write is inspectable.
        fd,path=tempfile.mkstemp(prefix='journal-',dir=folder)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(body);stream.flush();os.fsync(stream.fileno())
            os.replace(path,folder/'journal.json')
            directory=os.open(folder,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(path):os.unlink(path)

    def _load(self,spec):
        self._private(self._folder(spec),directory=True)
        path=self._folder(spec)/'journal.json';self._private(path)
        if path.stat().st_size>16384:raise RuntimeError('routed journal too large')
        try:
            value=json.loads(path.read_bytes())
            if (type(value) is not dict or set(value) not in ({'version','spec_digest','runtime_id','create','start','processes'},{'version','spec_digest','runtime_id','create','start','processes','cleanup'}) or value['version']!=1
                or value.get('cleanup') not in (None,'stop_intent','stopped','remove_intent','removed')
                or value['create'] not in ('intent','confirmed') or value['start'] not in (None,'intent','confirmed')
                or type(value['processes']) is not dict or set(value['processes'])-{'relay','hermes'}
                or any(v not in ('intent','launched') for v in value['processes'].values())
                or (value['runtime_id'] is not None and (type(value['runtime_id']) is not str or not re.fullmatch('[0-9a-f]{64}',value['runtime_id'])))):raise ValueError
        except (ValueError,TypeError,KeyError):raise RuntimeError('invalid routed journal') from None
        if value.get('spec_digest')!=spec.digest:raise RuntimeError('routed caller binding changed')
        value.setdefault('cleanup',None)
        return value

    def _validate_spec(self,spec):
        if type(spec) is not CallerSpec or spec.owner!=self.runtime.owner or spec.image!=self.runtime.image or spec.limits!=(self.runtime.cpus,self.runtime.memory_mib,self.runtime.pids):raise RuntimeError('routed configuration changed')
        _id(spec.attempt_id);_id(spec.session_id)
        if type(spec.generation) is not int or spec.generation<1:raise RuntimeError('invalid routed generation')

    @staticmethod
    def _identity_access(info,*,uid,gid,mask):
        permissions=stat.S_IMODE(info.st_mode)
        bits=(permissions>>6) if info.st_uid==uid else ((permissions>>3) if info.st_gid==gid else permissions)
        return bits&mask==mask

    @staticmethod
    def _tool_access(info,*,directory=False):
        return RoutedRuntime._identity_access(info,uid=1000,gid=1000,mask=5 if directory else 4)

    @staticmethod
    def _relay_access(info,*,directory=False,traverse_only=False):
        return RoutedRuntime._identity_access(info,uid=1001,gid=1001,mask=1 if traverse_only else (5 if directory else 4))

    def _task_hashes(self,task_dir):
        entries=[]
        root_info=task_dir.lstat()
        if root_info.st_uid not in (0,os.geteuid()) or root_info.st_mode&0o022:raise RuntimeError('untrusted routed task directory')
        if not self._tool_access(root_info,directory=True):raise RuntimeError('task directory unreadable by caller')
        if not self._relay_access(root_info,traverse_only=True):raise RuntimeError('task directory not traversable by relay')
        expected={'prompt.txt','config.yaml','launch.json','http-capability'} | {'source/cloudworkbench/'+name for name in _SOURCE_FILES}
        seen=set()
        for path in sorted(task_dir.rglob('*')):
            info=path.lstat()
            if info.st_uid not in (0,os.geteuid()) or info.st_mode&0o022 or path.is_symlink():raise RuntimeError('untrusted routed task material')
            if not self._tool_access(info,directory=stat.S_ISDIR(info.st_mode)):raise RuntimeError('task material unreadable by caller')
            relative=path.relative_to(task_dir).as_posix()
            if not stat.S_ISDIR(info.st_mode) and relative not in expected:raise RuntimeError('unexpected routed task material')
            if relative=='source' or relative.startswith('source/'):
                if not self._relay_access(info,directory=stat.S_ISDIR(info.st_mode)):raise RuntimeError('bootstrap source unreadable by relay')
            elif not stat.S_ISDIR(info.st_mode) and self._relay_access(info):raise RuntimeError('private task material readable by relay')
            if stat.S_ISDIR(info.st_mode):continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>1024*1024:raise RuntimeError('invalid routed task file')
            relative=path.relative_to(task_dir).as_posix()
            if relative not in expected:raise RuntimeError('unexpected routed task material')
            seen.add(relative)
            # This is an attempt HTTP capability, never provider/control credentials.
            # Its bytes are not read or hashed by this runtime or included in receipts.
            if relative=='http-capability':
                if info.st_size!=64:raise RuntimeError('invalid HTTP capability metadata')
                continue
            entries.append((relative,hashlib.sha256(path.read_bytes()).hexdigest()))
            if len(entries)>32:raise RuntimeError('routed task file limit')
        if seen!=expected:raise RuntimeError('incomplete routed task material')
        return tuple(entries)

    def prepare_caller(self,attempt_id,session_id,*,generation,plan,workspace,scratch,task_dir,worker_socket_dir):
        if type(plan) is not RoutedLaunchPlan:raise RuntimeError('routed launch plan required')
        environment = json.loads(plan.receipt_json).get('qualified_environment')
        if environment is not None:
            configured = {name:getattr(self.runtime,name) for name in ('cpus','memory_mib','pids','workspace_mib')}
            if (type(environment) is not dict or environment.get('image_digest') != self.runtime.image
                    or environment.get('resources') != configured):
                raise RuntimeError('routed qualified environment runtime mismatch')
        paths=[_path(p) for p in (workspace,scratch,task_dir,worker_socket_dir)]
        if len(set(paths))!=4 or any(not p.is_dir() for p in paths):raise RuntimeError('distinct caller directories required')
        workspace,scratch,task_dir,worker_socket_dir=paths
        if any(a.is_relative_to(b) for a in paths for b in paths if a!=b):raise RuntimeError('caller directories overlap')
        for path in (task_dir,worker_socket_dir):
            info=path.lstat()
            if info.st_uid not in (0,os.geteuid()) or info.st_mode&0o022:raise RuntimeError('untrusted caller material directory')
        self._worker_permissions(worker_socket_dir)
        task_hashes=self._task_hashes(task_dir)
        for name,expected in (('prompt.txt',plan.prompt.encode()),('config.yaml',plan.config_json.encode()),('launch.json',_json(asdict(plan)).encode())):
            path=task_dir/name
            if name=='launch.json':
                if json.loads(path.read_bytes())!=json.loads(expected):raise RuntimeError('routed launch file changed')
            elif path.read_bytes()!=expected:raise RuntimeError('routed launch file changed')
        spec=CallerSpec(attempt_id,session_id,generation,self.runtime.image,self.runtime.owner,
            str(workspace),str(scratch),str(task_dir),str(worker_socket_dir),plan.workspace_readonly,
            task_hashes,(self.runtime.cpus,self.runtime.memory_mib,self.runtime.pids))
        self._validate_spec(spec);self._mounts(spec)
        return spec

    @staticmethod
    def _worker_permissions(path):
        info=path.lstat()
        if info.st_uid not in (0,os.geteuid()) or info.st_gid!=1001 or stat.S_IMODE(info.st_mode)&0o777!=0o750 or {p.name for p in path.iterdir()}!={'client.json','socket'}:raise RuntimeError('unsafe worker socket directory')
        config=(path/'client.json').lstat();sock=(path/'socket').lstat()
        if (not stat.S_ISREG(config.st_mode) or config.st_nlink!=1 or config.st_uid not in (0,os.geteuid()) or config.st_gid!=1001
            or stat.S_IMODE(config.st_mode)&0o777!=0o440 or not 0<config.st_size<=8192
            or not stat.S_ISSOCK(sock.st_mode) or sock.st_uid not in (0,os.geteuid()) or sock.st_gid!=1001
            or stat.S_IMODE(sock.st_mode)&0o777!=0o660):raise RuntimeError('unsafe worker socket material')

    def _mounts(self,spec):
        values=[(spec.workspace,'/workspace',spec.workspace_readonly),(spec.scratch,'/scratch',False),
            (spec.task_dir,'/run/task',True),(str(Path(spec.task_dir)/'config.yaml'),'/run/tool/hermes/config.yaml',True),
            (spec.worker_socket_dir,'/run/worker-inference',True)]
        # Runtime normally protects /workspace because it supplies it itself. Here it is
        # an explicitly approved isolated revision, never session-derived shared work.
        workspace=_path(spec.workspace)
        roots=self.runtime.allowed_roots if spec.workspace_readonly else self.runtime.writable_roots
        if not any(workspace.is_relative_to(root) for root in roots):raise RuntimeError('workspace source not approved')
        self.runtime._mounts([{'source':source,'target':target,'readonly':readonly} for source,target,readonly in values[1:]])
        return values

    def _labels(self,spec):
        return {LABEL:'true','io.cloudworkbench.owner':spec.owner,'io.cloudworkbench.attempt':spec.attempt_id,
                'io.cloudworkbench.session':spec.session_id,'io.cloudworkbench.generation':str(spec.generation),
                'io.cloudworkbench.role':'routed-caller','io.cloudworkbench.image':spec.image,
                'io.cloudworkbench.caller-spec':spec.digest}

    def _files_unchanged(self,spec):
        self._worker_permissions(Path(spec.worker_socket_dir))
        if self._task_hashes(Path(spec.task_dir))!=spec.task_files:raise RuntimeError('routed task material changed')

    @_serialized
    def create_caller(self,spec):
        self._validate_spec(spec);self._files_unchanged(spec)
        folder=self._folder(spec)
        try:folder.mkdir(mode=0o700)
        except FileExistsError:
            value=self._load(spec)
            self._not_cleaning(value)
            if value['runtime_id'] is not None:
                self.inspect_caller(spec,value['runtime_id']);return value['runtime_id']
            raise RuntimeError('routed create unresolved; reconcile without retry')
        journal={'version':1,'spec_digest':spec.digest,'runtime_id':None,'create':'intent','start':None,'processes':{},'cleanup':None}
        self._save(spec,journal)
        args=['create','--name',spec.name,'--network','none','--dns','127.0.0.1','--read-only','--user','1002:1002',
              '--cap-drop','ALL','--security-opt','no-new-privileges:true','--ipc','private','--restart','no',
              '--cpus',str(spec.limits[0]),'--memory',f'{spec.limits[1]}m','--memory-swap',f'{spec.limits[1]}m','--pids-limit',str(spec.limits[2]),
              '--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2','--workdir','/']
        for key,value in self._labels(spec).items():args+=['--label',key+'='+value]
        for path,options in _TMPFS.items():args+=['--tmpfs',path+':'+options]
        for source,target,readonly in self._mounts(spec):args+=['--mount',f'type=bind,src={source},dst={target}'+(',readonly' if readonly else '')]
        args+=['--entrypoint',_PYTHON,spec.image,'-c',_INIT]
        identity=self.runtime._run(args)
        if not re.fullmatch('[0-9a-f]{64}',identity):raise RuntimeError('routed create identity unresolved')
        if self.inspect_caller(spec,identity).get('Status')!='created':raise RuntimeError('new routed caller is not stopped')
        journal.update(runtime_id=identity,create='confirmed');self._save(spec,journal)
        return identity

    def inspect_caller(self,spec,runtime_id):
        self._validate_spec(spec)
        if not re.fullmatch('[0-9a-f]{64}',runtime_id):raise RuntimeError('full routed runtime identity required')
        values=json.loads(self.runtime._run(['inspect',runtime_id]))
        if type(values) is not list or len(values)!=1:raise RuntimeError('routed inspection unresolved')
        obj=values[0];host=obj.get('HostConfig',{});config=obj.get('Config',{})
        labels=config.get('Labels') or {};expected=self._labels(spec)
        inert={'io.cloudworkbench.build-owner','io.cloudworkbench.candidate'}
        if ('io.cloudworkbench.candidate' in labels and labels['io.cloudworkbench.candidate']!='true') or ('io.cloudworkbench.build-owner' in labels and (not isinstance(labels['io.cloudworkbench.build-owner'],str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}',labels['io.cloudworkbench.build-owner']))):
            raise RuntimeError('invalid routed image build metadata')
        if (obj.get('Id')!=runtime_id or obj.get('Name')!='/'+spec.name or obj.get('Image')!=spec.image
            or any(labels.get(k)!=v for k,v in expected.items()) or any(k.startswith('io.cloudworkbench.') and k not in expected and k not in inert for k in labels)
            or config.get('User')!='1002:1002' or config.get('Entrypoint')!=[_PYTHON] or config.get('Cmd')!=['-c',_INIT]
            or host.get('NetworkMode')!='none' or host.get('Privileged') or not host.get('ReadonlyRootfs')
            or host.get('CapDrop')!=['ALL'] or host.get('CapAdd') or host.get('PortBindings')
            or host.get('IpcMode')!='private' or host.get('PidMode') not in ('',None)
            or host.get('Dns')!=['127.0.0.1'] or set(host.get('SecurityOpt',[])) not in ({'no-new-privileges'},{'no-new-privileges:true'})
            or any(host.get(key) for key in ('Devices','DeviceRequests','VolumesFrom','ExtraHosts','PublishAllPorts','Binds'))
            or set(obj.get('NetworkSettings',{}).get('Networks',{}))!={'none'}
            or host.get('RestartPolicy')!={'Name':'no','MaximumRetryCount':0}
            or host.get('Memory')!=spec.limits[1]*1024*1024 or host.get('MemorySwap')!=spec.limits[1]*1024*1024
            or host.get('PidsLimit')!=spec.limits[2] or host.get('NanoCpus')!=round(spec.limits[0]*1e9)):
            raise RuntimeError('routed container policy mismatch')
        tmpfs=host.get('Tmpfs',{})
        if set(tmpfs)!=set(_TMPFS) or any(set(tmpfs[k].split(','))!=set(v.split(',')) for k,v in _TMPFS.items()):raise RuntimeError('routed tmpfs policy mismatch')
        expected_mounts={target:(source,not readonly) for source,target,readonly in self._mounts(spec)}
        mounts=[m for m in obj.get('Mounts',[]) if m.get('Type')!='tmpfs']
        if len(mounts)!=len(expected_mounts) or {m.get('Destination') for m in mounts}!=set(expected_mounts) or any(m.get('Type')!='bind' or expected_mounts.get(m.get('Destination'))!=(m.get('Source'),m.get('RW')) for m in mounts):raise RuntimeError('routed mounts changed')
        if config.get('WorkingDir')!='/':raise RuntimeError('routed working directory changed')
        if host.get('LogConfig')!={'Type':'local','Config':{'max-size':'1m','max-file':'2'}}:raise RuntimeError('routed log policy mismatch')
        return obj['State']

    @_serialized
    def reconcile_create(self,spec):
        journal=self._load(spec)
        self._not_cleaning(journal)
        args=['container','ls','--all','--no-trunc']
        for key,value in self._labels(spec).items():args+=['--filter','label='+key+'='+value]
        ids=self.runtime._run([*args,'--format','{{.ID}}']).splitlines()
        if not ids:return None  # Absence is not proof an unanswered Docker create settled.
        if len(ids)!=1:raise RuntimeError('ambiguous routed caller discovery')
        state=self.inspect_caller(spec,ids[0])
        if journal['runtime_id'] not in (None,ids[0]):raise RuntimeError('routed discovered identity changed')
        if journal['runtime_id'] is None and state.get('Status')!='created':raise RuntimeError('unacknowledged caller already started')
        journal.update(runtime_id=ids[0],create='confirmed');self._save(spec,journal)
        return ids[0]

    @_serialized
    def start_caller(self,spec,runtime_id):
        self._files_unchanged(spec);journal=self._load(spec)
        self._not_cleaning(journal)
        if journal['runtime_id']!=runtime_id or journal['create']!='confirmed':raise RuntimeError('routed create not confirmed')
        state=self.inspect_caller(spec,runtime_id)
        if journal['start'] is not None:
            if state.get('Status')=='running':journal['start']='confirmed';self._save(spec,journal);return
            raise RuntimeError('routed start unresolved; no replay')
        if state.get('Status')!='created':raise RuntimeError('routed caller not stopped')
        journal['start']='intent';self._save(spec,journal)
        self.runtime._run(['start',runtime_id])
        if self.inspect_caller(spec,runtime_id).get('Status')!='running':raise RuntimeError('routed caller start unconfirmed')
        journal['start']='confirmed';self._save(spec,journal)

    @_serialized
    def start_caller_process(self,spec,runtime_id,*,role):
        if role not in ('relay','hermes'):raise RuntimeError('unknown routed process role')
        self._files_unchanged(spec);journal=self._load(spec)
        self._not_cleaning(journal)
        if journal['runtime_id']!=runtime_id or journal['start']!='confirmed' or self.inspect_caller(spec,runtime_id).get('Status')!='running':raise RuntimeError('routed caller not running')
        if role in journal['processes']:raise RuntimeError('routed process already attempted; no replay')
        if role=='hermes' and journal['processes'].get('relay')!='launched':raise RuntimeError('routed relay not launched')
        journal['processes'][role]='intent';self._save(spec,journal)
        identity='1001:1001' if role=='relay' else '1000:1000'
        argv=[_PYTHON,'-m','cloudworkbench.routed_caller',role]
        args=['exec','-d','--user',identity]
        for key,value in _BOOTSTRAP_ENV.items():args+=['--env',key+'='+value]
        self.runtime._run([*args,runtime_id,*argv])
        journal['processes'][role]='launched';self._save(spec,journal)
        return {'runtime_id':runtime_id,'attempt_id':spec.attempt_id,'generation':spec.generation,'role':role,'uid':int(identity.split(':')[0]),'status':'launched','readiness_confirmed':False}

    @staticmethod
    def _not_cleaning(journal):
        if journal.get('cleanup') is not None:raise RuntimeError('routed cleanup already begun')

    def _cleanup_state(self,spec,runtime_id,journal):
        if journal['runtime_id']!=runtime_id or journal['create']!='confirmed':
            raise RuntimeError('routed cleanup requires confirmed identity')
        if not re.fullmatch('[0-9a-f]{64}',runtime_id):raise RuntimeError('full routed runtime identity required')
        # Query the exact ID without label filters so drift cannot masquerade as absence.
        ids=self.runtime._run(['container','ls','--all','--no-trunc','--filter','id='+runtime_id,'--format','{{.ID}}']).splitlines()
        if not ids:return None
        if ids!=[runtime_id]:raise RuntimeError('routed cleanup identity unresolved')
        return self.inspect_caller(spec,runtime_id)

    @staticmethod
    def _cleanup_receipt(spec,runtime_id,*,removed):
        return {'runtime_id':runtime_id,'attempt_id':spec.attempt_id,'generation':spec.generation,
                'spec_digest':spec.digest,'caller_stopped':True,'caller_removed':removed,
                'authority':'controller_observed_caller_only','provider_cleanup_qualified':False}

    @_serialized
    def stop_caller(self,spec,runtime_id):
        """Fence later launches, stop this exact caller, and require stopped readback."""
        journal=self._load(spec)
        state=self._cleanup_state(spec,runtime_id,journal)
        if journal['cleanup']=='removed':
            if state is not None:raise RuntimeError('removed caller reappeared')
            return self._cleanup_receipt(spec,runtime_id,removed=True)
        journal['cleanup']='stop_intent';self._save(spec,journal)
        if state is not None and state.get('Status')=='running':
            self.runtime.stop(runtime_id,expected_generation=spec.generation)
        state=self._cleanup_state(spec,runtime_id,journal)
        if state is not None and (state.get('Running') or state.get('Status') not in ('created','exited','dead')):
            raise RuntimeError('routed caller stop unconfirmed')
        journal['cleanup']='stopped';self._save(spec,journal)
        return self._cleanup_receipt(spec,runtime_id,removed=state is None)

    @_serialized
    def remove_caller(self,spec,runtime_id):
        """Remove only after durable stop proof; exact ID absence confirms retries."""
        journal=self._load(spec)
        if journal['cleanup'] not in ('stopped','remove_intent','removed'):
            raise RuntimeError('routed caller stop not confirmed')
        state=self._cleanup_state(spec,runtime_id,journal)
        if journal['cleanup']=='removed':
            if state is not None:raise RuntimeError('removed caller reappeared')
            return self._cleanup_receipt(spec,runtime_id,removed=True)
        if state is not None and (state.get('Running') or state.get('Status') not in ('created','exited','dead')):
            raise RuntimeError('refusing removal of live routed caller')
        journal['cleanup']='remove_intent';self._save(spec,journal)
        if state is not None:self.runtime.cleanup(runtime_id,expected_generation=spec.generation)
        if self._cleanup_state(spec,runtime_id,journal) is not None:
            raise RuntimeError('routed caller removal unconfirmed')
        journal['cleanup']='removed';self._save(spec,journal)
        return self._cleanup_receipt(spec,runtime_id,removed=True)


    def read_caller_file(self,spec,runtime_id,*,name,offset=0,max_bytes=65536):
        """Bounded fixed-path worker data, never cleanup or verification authority."""
        paths={'events':('/scratch/.cwb-observations/events.jsonl','1000:1000',16*1024**2,1024**2),
               'result':('/scratch/.cwb-observations/result.json','1000:1000',65536,65536),
               'relay-ready':('/run/relay/ready.json','1001:1001',4096,4096),
               'relay-fenced':('/run/relay/fenced.json','1001:1001',4096,4096)}
        if (type(name) is not str or name not in paths or type(offset) is not int
                or not 0<=offset<=paths[name][2] or type(max_bytes) is not int
                or not 1<=max_bytes<=paths[name][3]):raise RuntimeError('invalid caller collection')
        journal=self._load(spec)
        if journal['runtime_id']!=runtime_id or self.inspect_caller(spec,runtime_id).get('Status')!='running':raise RuntimeError('caller collection unavailable')
        path,identity,file_limit,_=paths[name]
        program="""import os,sys,stat,json,base64
parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
try:
 for part in sys.argv[1].split('/')[1:-1]:
  child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
  os.close(parent);parent=child
 fd=os.open(sys.argv[1].split('/')[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
except FileNotFoundError: print(json.dumps({'present':False}));sys.exit(0)
finally: os.close(parent)
try:
 info=os.fstat(fd)
 if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid() or info.st_size>FILE_LIMIT: raise ValueError('invalid caller output')
 offset=int(sys.argv[2]);limit=int(sys.argv[3])
 if offset>info.st_size: raise ValueError('caller output truncated')
 data=os.pread(fd,limit,offset)
 after=os.fstat(fd)
 if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('caller output changed')
 print(json.dumps({'present':True,'offset':offset,'next_offset':offset+len(data),'inode':info.st_ino,'size':info.st_size,'mtime_ns':info.st_mtime_ns,'data_b64':base64.b64encode(data).decode()}))
finally: os.close(fd)
""".replace('FILE_LIMIT',str(file_limit))
        output=self.runtime._run(['exec','--user',identity,runtime_id,_PYTHON,'-I','-c',program,path,str(offset),str(max_bytes)],timeout=5)
        # Base64 needs four bytes per three decoded bytes, plus a small envelope.
        encoded_limit=4*((max_bytes+2)//3)+4096
        if len(output)>encoded_limit:raise RuntimeError('caller collection bound exceeded')
        try:
            def pairs(items):
                result={}
                for key,value in items:
                    if key in result:raise ValueError
                    result[key]=value
                return result
            value=json.loads(output,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(value) is not dict or type(value.get('present')) is not bool:raise ValueError
            if not value['present']:
                if set(value)!={'present'}:raise ValueError
                return value
            if set(value)!={'present','offset','next_offset','inode','size','mtime_ns','data_b64'}:raise ValueError
            for key in ('offset','next_offset','inode','size','mtime_ns'):
                if type(value[key]) is not int or value[key]<0:raise ValueError
            if (value['inode']==0 or value['size']>file_limit or value['offset']!=offset
                    or value['next_offset']>value['size'] or type(value['data_b64']) is not str
                    or len(value['data_b64'])>4*((max_bytes+2)//3)):raise ValueError
            import base64
            data=base64.b64decode(value['data_b64'],validate=True)
            if (len(data)>max_bytes or value['next_offset']!=offset+len(data)
                    or len(data)!=min(max_bytes,value['size']-offset)):raise ValueError
            return {**{k:v for k,v in value.items() if k!='data_b64'},'data':data,'provenance':'worker_reported'}
        except (ValueError,KeyError,TypeError,RecursionError):raise RuntimeError('invalid caller collection response') from None
