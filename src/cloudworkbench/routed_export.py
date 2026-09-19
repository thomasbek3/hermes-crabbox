"""One-shot, controller-authorized export from an exactly stopped caller workspace."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tempfile
import time

from .runtime import RuntimeError, _path
from .routed_runtime import RoutedRuntime, _serialized
from .routed_export_protocol import (ExportLimits, ExportTree, collector_program,
    decode_export, max_stream_bytes, normalize_selectors, ExportError)

_PYTHON = '/opt/hermes/venv/bin/python'
_ID = re.compile(r'[0-9a-f]{64}\Z')
_METADATA_CAP = 256 * 1024
_CLEANUP_SECONDS = 15
_FAILURE_CODES = frozenset({'export_docker_timeout','export_docker_unavailable','export_docker_failed',
    'export_docker_output_bound','export_docker_stderr_bound','export_authority_unavailable',
    'export_collector_failed','export_workspace_changed'})


class WorkspaceExportError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class WorkspaceExport:
    spec_digest: str
    runtime_id: str
    workspace_identity: tuple[int,int]
    selected_paths: tuple[str,...]
    tree: ExportTree
    collector_id: str
    caller_cleanup_sha256: str
    stream_sha256: str
    receipt_sha256: str


def _tree_digest(tree):
    if type(tree) is not ExportTree:
        raise WorkspaceExportError('export_tree_invalid')
    return _sha(_json({'directories':tree.directories,'sha256':tree.sha256,
        'files':[(f.path,_sha(f.data),len(f.data),f.executable) for f in tree.files]}))


def _bounded_run(runtime,args,*,timeout,cap,stdout_fd=None):
    """No shell, ambient Docker config, unbounded communicate, or raw stderr."""
    try:
        process=subprocess.Popen([runtime.docker,*args],stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},start_new_session=True)
    except OSError:
        raise WorkspaceExportError('export_docker_unavailable') from None
    stdout=bytearray();stdout_size=0;stderr_size=0;deadline=time.monotonic()+timeout;reaped=False
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout,process.stderr):
                os.set_blocking(pipe.fileno(),False);selector.register(pipe,selectors.EVENT_READ)
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:raise WorkspaceExportError('export_docker_timeout')
                for key,_ in selector.select(min(remaining,.1)):
                    data=os.read(key.fd,65536)
                    if not data:selector.unregister(key.fileobj);continue
                    if key.fileobj is process.stdout:
                        stdout_size+=len(data)
                        if stdout_size>cap:raise WorkspaceExportError('export_docker_output_bound')
                        if stdout_fd is None:stdout.extend(data)
                        else:
                            pending=memoryview(data)
                            while pending:
                                written=os.write(stdout_fd,pending)
                                if written<=0:raise WorkspaceExportError('export_spool_write_failed')
                                pending=pending[written:]
                    else:
                        stderr_size+=len(data)
                        if stderr_size>65536:raise WorkspaceExportError('export_docker_stderr_bound')
            remaining=deadline-time.monotonic()
            if remaining<=0:raise WorkspaceExportError('export_docker_timeout')
            try:
                code=process.wait(timeout=remaining);reaped=True
            except subprocess.TimeoutExpired:raise WorkspaceExportError('export_docker_timeout') from None
            if code:raise WorkspaceExportError('export_docker_failed')
            return bytes(stdout)
    finally:
        if not reaped:
            import signal
            # Keep the direct child unreaped until signaling its group: a reaped
            # successful PID could already belong to an unrelated process group.
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:pass
        process.stdout.close();process.stderr.close()


class _BoundedRuntime:
    def __init__(self,base,authorize):self.base=base;self.authorize=authorize
    def __getattr__(self,key):return getattr(self.base,key)
    def _run(self,args,*,timeout=5):
        self.authorize('inspect')
        return _bounded_run(self.base,args,timeout=min(timeout,5),cap=_METADATA_CAP).decode('utf-8').strip()


class _Exporter:
    def __init__(self,routed,authorize):
        if type(routed) is not RoutedRuntime or not callable(authorize):
            raise WorkspaceExportError('export_trusted_runtime_authority_required')
        self.original=routed;self.root=routed.root;self.authorize=authorize
        self.uncertain=False;self.owned_operation=False;self.workspace_guard=None;self.cleanup_deadline=None
        self.routed=object.__new__(RoutedRuntime)
        self.routed.__dict__=dict(routed.__dict__)
        self.routed.runtime=_BoundedRuntime(routed.runtime,self._authorize)

    def _validate_spec(self,spec):self.routed._validate_spec(spec)

    def _authorize(self,action):
        if self.authorize(action) is not True:raise WorkspaceExportError('export_authority_unavailable')

    def _workspace(self,spec):
        path=_path(spec.workspace)
        if not path.is_absolute() or path.resolve(strict=True)!=path:
            raise WorkspaceExportError('export_workspace_changed')
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode):raise WorkspaceExportError('export_workspace_changed')
        if not any(path.is_relative_to(root) for root in self.original.runtime.allowed_roots+self.original.runtime.writable_roots):
            raise WorkspaceExportError('export_workspace_unapproved')
        return [info.st_dev,info.st_ino]

    def _caller(self,spec,runtime_id,cleanup):
        if type(runtime_id) is not str or not _ID.fullmatch(runtime_id):raise WorkspaceExportError('export_caller_identity')
        journal=self.routed._load(spec)
        if journal['cleanup'] not in ('stopped','removed'):
            raise WorkspaceExportError('export_caller_cleanup_unconfirmed')
        state=self.routed._cleanup_state(spec,runtime_id,journal)
        if state is not None and (state.get('Running') is not False or state.get('Status') not in ('created','exited','dead')):
            raise WorkspaceExportError('export_caller_not_stopped')
        expected=self.routed._cleanup_receipt(spec,runtime_id,removed=state is None)
        if type(cleanup) is not dict or _json(cleanup)!=_json(expected):
            raise WorkspaceExportError('export_caller_receipt_mismatch')

    def _path(self,spec):
        folder=self.routed._folder(spec);self.routed._private(folder,directory=True)
        return folder/'workspace-export.json'

    def _save(self,spec,value,*,initial=False):
        path=self._path(spec);body=_json(value)
        if len(body)>65536:raise WorkspaceExportError('export_journal_bound')
        if initial:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            temporary=None
        else:
            self.routed._private(path)
            fd,name=tempfile.mkstemp(prefix='export-',dir=path.parent);temporary=Path(name)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(body);stream.flush();os.fsync(stream.fileno())
            if temporary is not None:os.replace(temporary,path)
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
        finally:
            if temporary is not None and temporary.exists():temporary.unlink()

    def _load(self,spec):
        path=self._path(spec);self.routed._private(path)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            if os.fstat(fd).st_size>65536:raise WorkspaceExportError('export_journal_bound')
            body=os.read(fd,65537)
        finally:os.close(fd)
        try:value=json.loads(body)
        except (ValueError,UnicodeError):raise WorkspaceExportError('export_journal_invalid') from None
        if (type(value) is not dict or value.get('version') not in (1,2) or value.get('spec_digest')!=spec.digest
                or value.get('state') not in ('create_intent','created','start_intent','exited','remove_intent','cleaned')
                or value.get('collector_id') is not None and not _ID.fullmatch(value['collector_id'])):
            raise WorkspaceExportError('export_journal_invalid')
        failure=value.get('failure_code')
        if failure is not None and (type(failure) is not str or failure not in _FAILURE_CODES|{'export_failed','export_validation_failed','export_result_unavailable'}):
            raise WorkspaceExportError('export_journal_invalid')
        outcome=value.get('outcome')
        if outcome is not None:
            if (type(outcome) is not dict or set(outcome)!={'status','collector_id','binding_digest','collector_removed','reason'}
                or type(outcome['status']) is not str or outcome['status'] not in ('aborted','recoverable','ready') or value['state']!='cleaned'
                or (outcome['reason'] is not None and type(outcome['reason']) is not str)
                or outcome['collector_removed'] is not True or outcome['collector_id']!=value['collector_id']
                or outcome['binding_digest']!=value['binding_digest']
                or (outcome['status']=='aborted' and outcome['reason'] not in _FAILURE_CODES|{'export_failed','export_validation_failed','export_result_unavailable'})
                or (outcome['status']!='aborted' and (outcome['reason'] is not None or type(value.get('collection')) is not dict))
                or (outcome['status']=='ready' and type(value.get('receipt')) is not dict)):
                raise WorkspaceExportError('export_journal_invalid')
        return value

    def _labels(self,spec,value):
        return {**self.routed._labels(spec),'io.cloudworkbench.role':'routed-export',
            'io.cloudworkbench.export-binding':value['binding_digest'],
            'io.cloudworkbench.source-runtime':value['runtime_id']}

    def _command(self,selected,limits):
        return ['-i','PATH=/usr/bin:/bin','LANG=C.UTF-8',_PYTHON,'-I','-S','-c',collector_program(selected,limits)]

    def _rpc(self,action,args,*,timeout=5,cap=_METADATA_CAP,stdout_fd=None):
        self._authorize(action)
        if self.cleanup_deadline is not None:
            remaining=self.cleanup_deadline-time.monotonic()
            if remaining<=0:raise WorkspaceExportError('export_cleanup_deadline')
            timeout=min(timeout,remaining)
        if action in ('create','start') and self.workspace_guard is not None:
            spec,identity=self.workspace_guard
            if self._workspace(spec)!=identity:raise WorkspaceExportError('export_workspace_changed')
        try:
            options={'timeout':timeout,'cap':cap}
            if stdout_fd is not None:options['stdout_fd']=stdout_fd
            return _bounded_run(self.original.runtime,args,**options)
        except Exception:
            if action in ('create','start','stop','remove'):self.uncertain=True
            raise

    def _ids(self,runtime_id,*,cleanup=False):
        result=self._rpc('cleanup_inspect' if cleanup else 'inspect',['container','ls','--all','--no-trunc','--filter','id='+runtime_id,'--format','{{.ID}}']).decode().strip().splitlines()
        if result not in ([],[runtime_id]):raise WorkspaceExportError('export_identity_unresolved')
        return result

    def _inspect(self,spec,value,*,cleanup=False):
        identity=value['collector_id']
        if not _ID.fullmatch(identity):raise WorkspaceExportError('export_collector_identity')
        objects=json.loads(self._rpc('cleanup_inspect' if cleanup else 'inspect',['inspect',identity]))
        if type(objects) is not list or len(objects)!=1:raise WorkspaceExportError('export_inspection_unresolved')
        obj=objects[0];host=obj.get('HostConfig',{});config=obj.get('Config',{})
        labels=config.get('Labels') or {};expected=self._labels(spec,value)
        inert={'io.cloudworkbench.build-owner','io.cloudworkbench.candidate'}
        if (obj.get('Id')!=identity or obj.get('Name')!='/'+value['name'] or obj.get('Image')!=spec.image
            or any(labels.get(k)!=v for k,v in expected.items())
            or any(k.startswith('io.cloudworkbench.') and k not in expected and k not in inert for k in labels)
            or config.get('User')!='1000:1000' or config.get('Entrypoint')!=['/usr/bin/env']
            or config.get('Cmd')!=self._command(tuple(value['selected_paths']),ExportLimits(**value['limits']))
            or config.get('WorkingDir')!='/workspace' or host.get('NetworkMode')!='none'
            or host.get('ReadonlyRootfs') is not True or host.get('Privileged')
            or host.get('CapDrop')!=['ALL'] or host.get('CapAdd') or host.get('PortBindings')
            or host.get('IpcMode')!='private' or host.get('PidMode') not in ('',None)
            or host.get('Dns')!=['127.0.0.1'] or set(host.get('SecurityOpt',[])) not in ({'no-new-privileges'},{'no-new-privileges:true'})
            or any(host.get(k) for k in ('Devices','DeviceRequests','VolumesFrom','ExtraHosts','PublishAllPorts','Binds','Tmpfs'))
            or host.get('RestartPolicy')!={'Name':'no','MaximumRetryCount':0}
            or host.get('Memory')!=256*1024**2 or host.get('MemorySwap')!=256*1024**2
            or host.get('PidsLimit')!=32 or host.get('NanoCpus')!=500000000
            or host.get('LogConfig')!={'Type':'none','Config':{}}
            or set(obj.get('NetworkSettings',{}).get('Networks',{}))!={'none'}):
            raise WorkspaceExportError('export_container_policy_mismatch')
        mounts=obj.get('Mounts',[])
        if (len(mounts)!=1 or mounts[0].get('Type')!='bind' or mounts[0].get('Source')!=spec.workspace
            or mounts[0].get('Destination')!='/workspace' or mounts[0].get('RW') is not False):
            raise WorkspaceExportError('export_mount_policy_mismatch')
        return obj['State']

    def _cleanup(self,spec,value):
        identity=value['collector_id']
        if self._ids(identity,cleanup=True):
            if value['state']=='cleaned':raise WorkspaceExportError('export_collector_reappeared')
            state=self._inspect(spec,value,cleanup=True)
            if state.get('Running') or state.get('Status') not in ('created','exited','dead'):
                self._rpc('stop',['stop','--time','1',identity])
                state=self._inspect(spec,value,cleanup=True)
            if state.get('Running') is not False or state.get('Status') not in ('created','exited','dead'):
                raise WorkspaceExportError('export_stop_unconfirmed')
            value['state']='remove_intent';self._save(spec,value)
            self._rpc('remove',['rm',identity])
        elif value['state'] not in ('remove_intent','cleaned'):
            raise WorkspaceExportError('export_absence_not_cleanup')
        if self._ids(identity,cleanup=True):raise WorkspaceExportError('export_remove_unconfirmed')
        value['state']='cleaned';self._save(spec,value)

    @_serialized
    def run(self,spec,runtime_id,caller_cleanup,*,selected_paths,limits,forbidden_values,expected_workspace_identity):
        try:
            return self._run_export(spec,runtime_id,caller_cleanup,selected_paths=selected_paths,
                limits=limits,forbidden_values=forbidden_values,expected_workspace_identity=expected_workspace_identity)
        except Exception as error:
            if self.owned_operation:
                try:
                    value=self._load(spec)
                    value.setdefault('failure_code',self._failure_code(error))
                    self._save(spec,value)
                except Exception:
                    pass  # Recording failure must not prevent an exact cleanup attempt.
                try:
                    proof=self._reconcile_locked(spec,forbidden_values=forbidden_values)
                except Exception:
                    try:error.cleanup_status='unresolved'
                    except Exception:pass
                    error.add_note('Exact collector cleanup remains unresolved; reconcile the retained export journal.')
                else:
                    try:
                        error.cleanup_status='confirmed';error.export_outcome=proof['outcome']
                    except Exception:pass
            raise

    @staticmethod
    def _failure_code(error):
        if isinstance(error,ExportError):return 'export_validation_failed'
        if isinstance(error,WorkspaceExportError) and str(error) in _FAILURE_CODES:return str(error)
        return 'export_failed'

    @staticmethod
    def _outcome(value,status):
        if value.get('outcome',{}).get('status')=='aborted':return
        value['outcome']={'status':status,'collector_id':value['collector_id'],
            'binding_digest':value['binding_digest'],'collector_removed':True,
            'reason':value.get('failure_code','export_result_unavailable') if status=='aborted' else None}

    def _run_export(self,spec,runtime_id,caller_cleanup,*,selected_paths,limits,forbidden_values,expected_workspace_identity):
        if (type(expected_workspace_identity) is not tuple or len(expected_workspace_identity)!=2
                or any(type(v) is not int or v<=0 for v in expected_workspace_identity)):
            raise WorkspaceExportError('export_expected_workspace_identity_required')
        selected=normalize_selectors(selected_paths)
        program=collector_program(selected,limits)
        self._authorize('prepare');self._caller(spec,runtime_id,caller_cleanup)
        workspace=self._workspace(spec)
        if workspace!=list(expected_workspace_identity):raise WorkspaceExportError('export_workspace_changed')
        self.workspace_guard=(spec,workspace)
        binding={'spec_digest':spec.digest,'runtime_id':runtime_id,'workspace_identity':workspace,
            'selected_paths':selected,'limits':asdict(limits),'caller_cleanup_sha256':_sha(_json(caller_cleanup)),
            'program_sha256':_sha(program.encode())}
        value={**binding,'version':2,'binding_digest':_sha(_json(binding)),
            'name':'cwb2-'+spec.owner+'-export-'+_sha(spec.digest.encode())[:24],
            'state':'create_intent','collector_id':None}
        try:
            self._save(spec,value,initial=True);self.owned_operation=True
        except FileExistsError:raise WorkspaceExportError('export_already_attempted_reconcile_only') from None
        args=['create','--name',value['name'],'--network','none','--dns','127.0.0.1','--read-only',
            '--user','1000:1000','--cap-drop','ALL','--security-opt','no-new-privileges:true','--ipc','private',
            '--restart','no','--cpus','0.5','--memory','256m','--memory-swap','256m','--pids-limit','32',
            '--log-driver','none','--workdir','/workspace']
        for key,label in self._labels(spec,value).items():args+=['--label',key+'='+label]
        args+=['--mount',f'type=bind,src={spec.workspace},dst=/workspace,readonly',
            '--entrypoint','/usr/bin/env',spec.image,*self._command(selected,limits)]
        if self._workspace(spec)!=workspace:raise WorkspaceExportError('export_workspace_changed')
        identity=self._rpc('create',args).decode().strip()
        if not _ID.fullmatch(identity):raise WorkspaceExportError('export_create_unconfirmed')
        value['collector_id']=identity;self._save(spec,value)
        if self._inspect(spec,value).get('Status')!='created':raise WorkspaceExportError('export_created_state_mismatch')
        value['state']='created';self._save(spec,value)
        self._caller(spec,runtime_id,caller_cleanup)
        if self._workspace(spec)!=workspace:raise WorkspaceExportError('export_workspace_changed')
        fd=self._create_spool(spec,value)
        try:
            value['state']='start_intent';self._save(spec,value)
            self._rpc('start',['start','--attach',identity],timeout=limits.max_seconds+5,
                cap=max_stream_bytes(limits),stdout_fd=fd)
            os.fsync(fd)
        finally:os.close(fd)
        state=self._inspect(spec,value)
        if state.get('Running') is not False or state.get('Status')!='exited':raise WorkspaceExportError('export_exit_unconfirmed')
        value['state']='exited'
        if type(state.get('ExitCode')) is int and state['ExitCode']==0:
            data=self._read_spool(spec,value)
            self._mark_collection(value,data)
        self._save(spec,value)
        self._cleanup(spec,value)
        if type(state.get('ExitCode')) is not int or state['ExitCode']!=0:raise WorkspaceExportError('export_collector_failed')
        return self._restore(spec,value,forbidden_values=forbidden_values)

    def _create_spool(self,spec,value):
        path=self._path(spec).parent/'workspace-export.stdout'
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            value['spool']={'name':path.name,'identity':[info.st_dev,info.st_ino],
                'owner_uid':os.geteuid(),'max_bytes':max_stream_bytes(ExportLimits(**value['limits']))}
            os.fsync(fd)
            self._save(spec,value)  # Includes parent fsync before start is possible.
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _read_spool(self,spec,value,*,sync=False):
        if value.get('version')!=2 or type(value.get('spool')) is not dict:
            raise WorkspaceExportError('export_durable_bytes_unavailable')
        spool=value['spool'];cap=max_stream_bytes(ExportLimits(**value['limits']))
        if (set(spool)!={'name','identity','owner_uid','max_bytes'} or spool['name']!='workspace-export.stdout'
                or type(spool['owner_uid']) is not int or spool['owner_uid']!=os.geteuid() or spool['max_bytes']!=cap
                or type(spool['identity']) is not list or len(spool['identity'])!=2
                or any(type(i) is not int or i<=0 for i in spool['identity'])):
            raise WorkspaceExportError('export_spool_binding')
        path=self._path(spec).parent/spool['name']
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                before=os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid!=os.geteuid()
                        or stat.S_IMODE(before.st_mode)!=0o600 or not 0<before.st_size<=cap
                        or [before.st_dev,before.st_ino]!=spool['identity']):
                    raise WorkspaceExportError('export_spool_untrusted')
                chunks=[];size=0
                while size<=cap:
                    chunk=os.read(fd,min(65536,cap+1-size))
                    if not chunk:break
                    chunks.append(chunk);size+=len(chunk)
                if sync:os.fsync(fd)
                after=os.fstat(fd);current=path.lstat()
                fields=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_nlink)
                if size!=before.st_size or size>cap or fields(before)!=fields(after) or fields(after)!=fields(current):
                    raise WorkspaceExportError('export_spool_changed')
                data=b''.join(chunks)
                collection=value.get('collection')
                if collection is not None and (_json(collection)!=_json({'bytes':size,'sha256':_sha(data),
                        'collector_id':value['collector_id'],'status':'exited','exit_code':0})):
                    raise WorkspaceExportError('export_spool_digest_mismatch')
                return data
            finally:os.close(fd)
        except OSError:
            raise WorkspaceExportError('export_spool_unavailable') from None

    @staticmethod
    def _mark_collection(value,data):
        value['collection']={'bytes':len(data),'sha256':_sha(data),
            'collector_id':value['collector_id'],'status':'exited','exit_code':0}

    def _restore(self,spec,value,*,forbidden_values):
        self._authorize('publish')
        if value.get('outcome',{}).get('status')=='aborted':raise WorkspaceExportError('export_aborted')
        if value['state']!='cleaned' or type(value.get('collection')) is not dict:
            raise WorkspaceExportError('export_complete_cleanup_required')
        data=self._read_spool(spec,value)
        self._clean_context(spec,value)
        selected=tuple(value['selected_paths']);limits=ExportLimits(**value['limits'])
        tree=decode_export(data,selected_paths=selected,limits=limits,forbidden_values=forbidden_values)
        binding={key:value[key] for key in ('spec_digest','runtime_id','workspace_identity','selected_paths',
            'limits','caller_cleanup_sha256','program_sha256')}
        if (_sha(_json(binding))!=value['binding_digest']
                or _sha(collector_program(selected,limits).encode())!=binding['program_sha256']):
            raise WorkspaceExportError('export_program_binding_changed')
        receipt={**binding,'collector_id':value['collector_id'],'stream_sha256':_sha(data),
            'tree_digest':_tree_digest(tree),'collector_removed':True,
            'spool_identity':value['spool']['identity'],'spool_bytes':len(data)}
        receipt_sha256=_sha(_json(receipt))
        if value.get('receipt') is not None and (value['receipt']!=receipt or value.get('receipt_sha256')!=receipt_sha256):
            raise WorkspaceExportError('export_receipt_changed')
        self._authorize('publish')
        if value.get('receipt') is None or value.get('outcome',{}).get('status')!='ready':
            value['receipt']=receipt;value['receipt_sha256']=receipt_sha256
            self._outcome(value,'ready');self._save(spec,value)
        self._authorize('publish')
        return WorkspaceExport(spec.digest,value['runtime_id'],tuple(value['workspace_identity']),selected,tree,
            value['collector_id'],value['caller_cleanup_sha256'],_sha(data),receipt_sha256)

    def _clean_context(self,spec,value):
        if self._workspace(spec)!=value['workspace_identity']:raise WorkspaceExportError('export_workspace_changed')
        if self._ids(value['collector_id']):raise WorkspaceExportError('export_collector_reappeared')
        journal=self.routed._load(spec)
        if journal['cleanup'] not in ('stopped','removed'):raise WorkspaceExportError('export_caller_cleanup_unconfirmed')
        state=self.routed._cleanup_state(spec,value['runtime_id'],journal)
        if state is not None and (state.get('Running') is not False or state.get('Status') not in ('created','exited','dead')):
            raise WorkspaceExportError('export_caller_not_stopped')

    @_serialized
    def reconcile(self,spec,*,forbidden_values=()):
        return self._reconcile_locked(spec,forbidden_values=forbidden_values)

    def _reconcile_locked(self,spec,*,forbidden_values):
        previous=self.cleanup_deadline
        self.cleanup_deadline=time.monotonic()+_CLEANUP_SECONDS
        try:return self._reconcile_body(spec,forbidden_values=forbidden_values)
        finally:self.cleanup_deadline=previous

    def _reconcile_body(self,spec,*,forbidden_values):
        self._authorize('reconcile');value=self._load(spec)
        if value['collector_id'] is None:
            args=['container','ls','--all','--no-trunc']
            for key,label in self._labels(spec,value).items():args+=['--filter','label='+key+'='+label]
            ids=self._rpc('cleanup_inspect',[*args,'--format','{{.ID}}']).decode().strip().splitlines()
            if len(ids)!=1 or not _ID.fullmatch(ids[0]):raise WorkspaceExportError('export_create_unresolved')
            value['collector_id']=ids[0]
            if self._inspect(spec,value,cleanup=True).get('Status')!='created':raise WorkspaceExportError('export_unknown_create_started')
            self._save(spec,value)
        recoverable=False
        if value.get('spool') is not None and value.get('outcome',{}).get('status')!='aborted':
            try:
                data=self._read_spool(spec,value,sync=True)
                decode_export(data,selected_paths=tuple(value['selected_paths']),
                    limits=ExportLimits(**value['limits']),forbidden_values=forbidden_values)
                if value.get('collection') is None:
                    if self._ids(value['collector_id'],cleanup=True):
                        state=self._inspect(spec,value,cleanup=True)
                        if (state.get('Running') is False and state.get('Status')=='exited'
                                and type(state.get('ExitCode')) is int and state['ExitCode']==0):
                            self._mark_collection(value,data);self._save(spec,value)
                recoverable=value.get('collection') is not None
            except (WorkspaceExportError,ExportError):
                pass  # Invalid private bytes do not obstruct exact physical cleanup.
        self._cleanup(spec,value)
        self._outcome(value,('ready' if value.get('receipt') else 'recoverable') if recoverable else 'aborted')
        self._save(spec,value)
        return {'collector_id':value['collector_id'],'collector_removed':True,'spec_digest':spec.digest,
            'bytes_recoverable':recoverable,'outcome':value['outcome']['status'],
            'terminal_aborted':value['outcome']['status']=='aborted','failure_code':value.get('failure_code')}

    @_serialized
    def status(self,spec):
        self._authorize('status')
        if not os.path.lexists(self._path(spec)):
            return {'state':'not_started','outcome':None,'collector_id':None,'failure_code':None}
        value=self._load(spec)
        return {'state':value['state'],'outcome':value.get('outcome',{}).get('status','held'),
            'collector_id':value['collector_id'],'failure_code':value.get('failure_code')}

    @_serialized
    def load(self,spec,*,forbidden_values):
        self._authorize('load')
        return self._restore(spec,self._load(spec),forbidden_values=forbidden_values)

    @_serialized
    def validate(self,spec,export):
        self._authorize('validate')
        if type(export) is not WorkspaceExport or export.spec_digest!=spec.digest:raise WorkspaceExportError('export_result_binding')
        value=self._load(spec);receipt=value.get('receipt')
        if (value.get('outcome',{}).get('status')=='aborted' or value['state']!='cleaned' or type(receipt) is not dict or type(value.get('collection')) is not dict
            or value.get('receipt_sha256')!=export.receipt_sha256 or _sha(_json(receipt))!=export.receipt_sha256
            or value['collector_id']!=export.collector_id or value['runtime_id']!=export.runtime_id
            or tuple(value['workspace_identity'])!=export.workspace_identity
            or tuple(value['selected_paths'])!=export.selected_paths
            or receipt['tree_digest']!=_tree_digest(export.tree)
            or receipt['stream_sha256']!=export.stream_sha256
            or receipt['caller_cleanup_sha256']!=export.caller_cleanup_sha256):
            raise WorkspaceExportError('export_result_binding')
        data=self._read_spool(spec,value)
        if _sha(data)!=export.stream_sha256:raise WorkspaceExportError('export_result_binding')
        if self._workspace(spec)!=list(export.workspace_identity):raise WorkspaceExportError('export_workspace_changed')
        if self._ids(export.collector_id):raise WorkspaceExportError('export_collector_reappeared')
        journal=self.routed._load(spec)
        if journal['cleanup'] not in ('stopped','removed'):raise WorkspaceExportError('export_caller_cleanup_unconfirmed')
        state=self.routed._cleanup_state(spec,export.runtime_id,journal)
        if state is not None and (state.get('Running') is not False or state.get('Status') not in ('created','exited','dead')):
            raise WorkspaceExportError('export_caller_not_stopped')
        return True


def export_stopped_workspace(runtime,spec,runtime_id,caller_cleanup,*,selected_paths,authorize,expected_workspace_identity,
                             limits=ExportLimits(),forbidden_values=()):
    return _Exporter(runtime,authorize).run(spec,runtime_id,caller_cleanup,
        selected_paths=selected_paths,limits=limits,forbidden_values=forbidden_values,
        expected_workspace_identity=expected_workspace_identity)


def reconcile_workspace_export(runtime,spec,*,authorize,forbidden_values=()):
    return _Exporter(runtime,authorize).reconcile(spec,forbidden_values=forbidden_values)


def validate_workspace_export(runtime,spec,export,*,authorize):
    return _Exporter(runtime,authorize).validate(spec,export)


def load_workspace_export(runtime,spec,*,authorize,forbidden_values=()):
    return _Exporter(runtime,authorize).load(spec,forbidden_values=forbidden_values)


def workspace_export_status(runtime,spec,*,authorize):
    return _Exporter(runtime,authorize).status(spec)
