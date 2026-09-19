from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cloudworkbench import routed_export as export
from cloudworkbench.routed_export_protocol import ExportLimits,collector_program,ExportError
from cloudworkbench.runtime import RuntimeError
from test_routed_runtime import setup, Docker, RID

EID='e'*64


def workspace_identity(spec):
    info=Path(spec.workspace).stat()
    return info.st_dev,info.st_ino


@pytest.fixture
def exporter(setup,monkeypatch):
    routed,spec,original=setup
    routed.create_caller(spec);cleanup=routed.stop_caller(spec,RID);cleanup=routed.remove_caller(spec,RID)
    Path(spec.workspace,'answer.txt').write_text('result\n')
    stream=subprocess.run([sys.executable,'-I','-S','-c',collector_program(('answer.txt',),workspace=spec.workspace)],capture_output=True,check=True).stdout
    class Collector(Docker):
        def __init__(self):
            super().__init__();self.lost_create=False;self.lost_start=False;self.lost_remove=False
            self.tamper=None;self.stream=stream;self.exit_code=0
            self.hide_discovery=False;self.start_failure_hook=None
        def __call__(self,args,**kwargs):
            if args[:2]==['container','ls'] and self.hide_discovery and any(a.startswith('label=') for a in args):
                self.calls.append(args);return ''
            if args[0]=='create':
                value=super().__call__(args,**kwargs);obj=self.objects.pop(value)
                obj['Id']=EID;self.objects[EID]=obj
                if self.tamper:self.tamper(obj)
                if self.lost_create:raise RuntimeError('synthetic unknown create')
                return EID
            if args[:2]==['start','--attach']:
                self.calls.append(args)
                self.objects[EID]['State'].update(Status='exited',Running=False,ExitCode=self.exit_code)
                if self.lost_start:
                    if self.start_failure_hook:self.start_failure_hook(self.objects[EID])
                    raise RuntimeError('synthetic unknown start')
                return self.stream
            value=super().__call__(args,**kwargs)
            if args[0]=='rm' and self.lost_remove:raise RuntimeError('synthetic unknown rm')
            return value
    docker=Collector()
    def bounded(runtime,args,**kwargs):
        value=docker(args,**kwargs)
        value=value if type(value) is bytes else value.encode()
        if kwargs.get('stdout_fd') is not None:
            os.write(kwargs['stdout_fd'],value)
            return b''
        return value
    monkeypatch.setattr(export,'_bounded_run',bounded)
    return routed,spec,cleanup,docker


def run(fixture,**kwargs):
    routed,spec,cleanup,_=fixture
    return export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),expected_workspace_identity=workspace_identity(spec),authorize=lambda _:True,**kwargs)


def test_actual_argv_only_workspace_and_cleanup_before_return(exporter):
    routed,spec,_,docker=exporter;value=run(exporter)
    assert value.tree.files[0].data==b'result\n'
    create=next(c for c in docker.calls if c[0]=='create')
    assert create[create.index('--user')+1]=='1000:1000'
    assert create[create.index('--network')+1]=='none'
    assert create.count('--mount')==1 and 'readonly' in create[create.index('--mount')+1]
    assert '--read-only' in create and '--cap-drop' in create and '--security-opt' in create
    assert '/run/secrets' not in str(create) and spec.scratch not in str(create) and spec.task_dir not in str(create)
    assert create[create.index('--entrypoint')+1]=='/usr/bin/env' and '-i' in create
    assert docker.objects=={}
    assert export.validate_workspace_export(routed,spec,value,authorize=lambda _:True) is True
    with pytest.raises(export.WorkspaceExportError,match='already_attempted'):run(exporter)
    assert sum(c[0]=='create' for c in docker.calls)==1


@pytest.mark.parametrize('case',['live','uncertain','wrong_receipt','wrong_runtime','workspace_symlink'])
def test_caller_preconditions_refuse_before_collector(exporter,case):
    routed,spec,cleanup,docker=exporter
    if case in ('live','uncertain'):
        journal=routed._load(spec);journal['cleanup']=None if case=='live' else 'stop_intent';routed._save(spec,journal)
    elif case=='wrong_receipt':cleanup={**cleanup,'caller_removed':1}
    elif case=='wrong_runtime':cleanup={**cleanup,'runtime_id':'f'*64}
    else:
        path=Path(spec.workspace);new=path.with_name('moved');path.rename(new);path.symlink_to(new)
    with pytest.raises(RuntimeError):run((routed,spec,cleanup,docker))
    assert not any(c[0]=='create' for c in docker.calls)


@pytest.mark.parametrize('field,value',[('User','0:0'),('NetworkMode','host'),('ReadonlyRootfs',False),('Memory',0),('PidsLimit',0)])
def test_inspection_rejects_policy_drift_and_retains(exporter,field,value):
    _,_,_,docker=exporter
    docker.tamper=lambda obj:obj['Config' if field=='User' else 'HostConfig'].__setitem__(field,value)
    with pytest.raises(export.WorkspaceExportError,match='policy_mismatch'):run(exporter)
    assert EID in docker.objects and not any(c[0]=='start' for c in docker.calls)


def test_mount_extra_refused(exporter):
    *_,docker=exporter;docker.tamper=lambda obj:obj['Mounts'].append(dict(Type='bind',Source='/private',Destination='/secrets',RW=False))
    with pytest.raises(export.WorkspaceExportError,match='mount_policy'):run(exporter)


@pytest.mark.parametrize('fault',['lost_create','lost_start','lost_remove'])
def test_unknown_effect_is_held_no_relaunch_exact_reconciliation(exporter,fault):
    routed,spec,_,docker=exporter;setattr(docker,fault,True)
    with pytest.raises(RuntimeError):run(exporter)
    with pytest.raises(export.WorkspaceExportError,match='already_attempted'):run(exporter)
    setattr(docker,fault,False)
    receipt=export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)
    assert receipt['collector_removed'] and receipt['bytes_recoverable'] is (fault=='lost_remove') and docker.objects=={}
    assert sum(c[0]=='create' for c in docker.calls)==1


def test_unknown_create_absence_is_not_cleanup(exporter):
    routed,spec,_,docker=exporter;docker.lost_create=True;docker.hide_discovery=True
    with pytest.raises(RuntimeError):run(exporter)
    docker.objects.clear();docker.hide_discovery=False
    with pytest.raises(export.WorkspaceExportError,match='create_unresolved'):
        export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)


def test_revocation_denies_launch_but_separate_cleanup_authority_works(exporter):
    routed,spec,cleanup,docker=exporter;actions=[]
    def authorize(action):actions.append(action);return action!='start'
    with pytest.raises(export.WorkspaceExportError,match='authority'):
        export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),expected_workspace_identity=workspace_identity(spec),authorize=authorize)
    assert not docker.objects and not any(c[0]=='start' for c in docker.calls)
    assert 'cleanup_inspect' in actions
    export.reconcile_workspace_export(routed,spec,authorize=lambda action:action in ('reconcile','cleanup_inspect','stop','remove'))
    assert not docker.objects


def test_cleanup_failure_never_returns_bytes(exporter):
    routed,spec,cleanup,docker=exporter
    with pytest.raises(export.WorkspaceExportError,match='authority'):
        export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),expected_workspace_identity=workspace_identity(spec),authorize=lambda action:action!='remove')
    journal=export._Exporter(routed,lambda _:True)._load(spec)
    assert 'receipt' not in journal and EID in docker.objects


@pytest.mark.parametrize('case',['malformed','secret','exit'])
def test_bad_result_cleaned_without_bytes_or_validated_receipt(exporter,case):
    routed,spec,_,docker=exporter
    if case=='malformed':docker.stream=b'not export\n'
    elif case=='exit':docker.exit_code=1
    with pytest.raises((ExportError,export.WorkspaceExportError)):
        run(exporter,forbidden_values=(b'result',) if case=='secret' else ())
    assert not docker.objects and 'receipt' not in export._Exporter(routed,lambda _:True)._load(spec)


def test_forged_export_bytes_refused_by_private_journal(exporter):
    routed,spec,*_=exporter;value=run(exporter)
    badfile=replace(value.tree.files[0],data=b'forged')
    for bad in (replace(value,tree=replace(value.tree,files=(badfile,))),replace(value,collector_id='f'*64),replace(value,stream_sha256='0'*64)):
        with pytest.raises(export.WorkspaceExportError,match='binding'):
            export.validate_workspace_export(routed,spec,bad,authorize=lambda _:True)


def test_workspace_replacement_before_start_retains_collector(exporter):
    routed,spec,cleanup,docker=exporter;count=[0]
    def authorize(action):
        if action=='inspect':
            count[0]+=1
            if count[0]==3:
                path=Path(spec.workspace);path.rename(path.with_name('retained'));path.mkdir()
        return True
    with pytest.raises(export.WorkspaceExportError,match='workspace_changed'):
        export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),expected_workspace_identity=workspace_identity(spec),authorize=authorize)
    assert not any(c[0]=='start' for c in docker.calls)


@pytest.mark.parametrize('kind',['stdout','stderr','timeout','exit'])
def test_bounded_real_local_process_has_safe_failure(tmp_path,kind):
    binary=tmp_path/'fake-docker'
    programs={'stdout':'print("x"*10000)','stderr':'import sys;sys.stderr.write("private"*20000)',
        'timeout':'import time;time.sleep(10)','exit':'import sys;sys.stderr.write("private");sys.exit(7)'}
    binary.write_text('#!'+sys.executable+'\n'+programs[kind]);binary.chmod(0o700)
    with pytest.raises(export.WorkspaceExportError) as caught:
        export._bounded_run(SimpleNamespace(docker=str(binary)),[],timeout=.1,cap=100)
    assert 'private' not in str(caught.value)


def test_truthy_authorization_is_not_authority(exporter):
    routed,spec,cleanup,docker=exporter
    with pytest.raises(export.WorkspaceExportError,match='authority'):
        export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),expected_workspace_identity=workspace_identity(spec),authorize=lambda _:1)
    assert not docker.calls


def test_reconcile_missing_workspace_does_not_require_data_access(exporter):
    routed,spec,_,docker=exporter;docker.lost_start=True
    with pytest.raises(RuntimeError):run(exporter)
    path=Path(spec.workspace);path.rename(path.with_name('retained'))
    export.reconcile_workspace_export(routed,spec,authorize=lambda action:action in ('reconcile','cleanup_inspect','stop','remove'))
    assert not docker.objects


def test_reconcile_identity_drift_never_removes_object(exporter):
    routed,spec,_,docker=exporter;docker.lost_start=True
    docker.start_failure_hook=lambda obj:obj['Config']['Labels'].__setitem__('io.cloudworkbench.generation','999')
    with pytest.raises(RuntimeError):run(exporter)
    with pytest.raises(export.WorkspaceExportError,match='policy_mismatch'):
        export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)
    assert EID in docker.objects and not any(c[0]=='rm' for c in docker.calls)


def test_concurrent_calls_use_same_caller_lock_and_one_create(exporter):
    from concurrent.futures import ThreadPoolExecutor
    def attempt():
        try:return run(exporter)
        except export.WorkspaceExportError as error:return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(lambda _:attempt(),range(2)))
    assert sum(type(v) is export.WorkspaceExport for v in values)==1
    assert sum(c[0]=='create' for c in exporter[-1].calls)==1


def test_successful_local_cli_never_signals_reaped_pid(tmp_path,monkeypatch):
    binary=tmp_path/'fake-docker';binary.write_text('#!'+sys.executable+'\nprint("ok")\n');binary.chmod(0o700)
    def kill(*args):pytest.fail('signaled already reaped successful process')
    monkeypatch.setattr(export.os,'killpg',kill)
    assert export._bounded_run(SimpleNamespace(docker=str(binary)),[],timeout=5,cap=100)==b'ok\n'


def test_workspace_changed_in_start_authorization_is_rechecked(exporter):
    routed,spec,cleanup,docker=exporter
    def authorize(action):
        if action=='start':
            path=Path(spec.workspace);path.rename(path.with_name('retained'));path.mkdir()
        return True
    with pytest.raises(export.WorkspaceExportError,match='workspace_changed'):
        export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),expected_workspace_identity=workspace_identity(spec),authorize=authorize)
    assert not any(c[0]=='start' for c in docker.calls) and not docker.objects


@pytest.mark.parametrize('identity',[None,(True,1),(1,False),(0,1),(1,-1),[1,2],(1,),('1',2)])
def test_expected_workspace_identity_mandatory_and_strict(exporter,identity):
    routed,spec,cleanup,docker=exporter
    with pytest.raises(export.WorkspaceExportError,match='expected_workspace_identity'):
        export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),
            expected_workspace_identity=identity,authorize=lambda _:True)
    assert not (routed._folder(spec)/'workspace-export.json').exists() and not docker.calls


def test_workspace_replaced_between_caller_and_export_refused(exporter):
    routed,spec,cleanup,docker=exporter;original=workspace_identity(spec)
    path=Path(spec.workspace);path.rename(path.with_name('original'));path.mkdir()
    with pytest.raises(export.WorkspaceExportError,match='workspace_changed'):
        export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),
            expected_workspace_identity=original,authorize=lambda _:True)
    assert not (routed._folder(spec)/'workspace-export.json').exists()
    assert not any(c[0]=='create' for c in docker.calls)


class ControllerCrash(BaseException):
    pass


def spool_path(routed,spec):return routed._folder(spec)/'workspace-export.stdout'


def test_restart_loads_exact_result_without_create_or_start(exporter):
    routed,spec,_,docker=exporter;first=run(exporter)
    from cloudworkbench.routed_runtime import RoutedRuntime
    restarted=RoutedRuntime(routed.runtime,journal_root=routed.root)
    before=sum(c[0] in ('create','start') for c in docker.calls)
    second=export.load_workspace_export(restarted,spec,authorize=lambda _:True)
    third=export.load_workspace_export(restarted,spec,authorize=lambda _:True)
    assert first==second==third
    assert sum(c[0] in ('create','start') for c in docker.calls)==before
    assert export.validate_workspace_export(restarted,spec,second,authorize=lambda _:True)
    assert (spool_path(routed,spec).stat().st_mode&0o777)==0o600


@pytest.mark.parametrize('boundary',['before_marker','before_cleanup','receipt_write'])
def test_complete_stream_crash_recovery_never_relaunches(exporter,monkeypatch,boundary):
    routed,spec,_,docker=exporter;save=export._Exporter._save;cleanup=export._Exporter._cleanup
    def broken_save(self,spec,value,**kwargs):
        if ((boundary=='before_marker' and value.get('collection') and value['state']=='exited')
            or (boundary=='receipt_write' and value.get('receipt'))):raise ControllerCrash()
        return save(self,spec,value,**kwargs)
    def broken_cleanup(self,spec,value):
        if boundary=='before_cleanup':raise ControllerCrash()
        return cleanup(self,spec,value)
    monkeypatch.setattr(export._Exporter,'_save',broken_save)
    monkeypatch.setattr(export._Exporter,'_cleanup',broken_cleanup)
    with pytest.raises(ControllerCrash):run(exporter)
    monkeypatch.setattr(export._Exporter,'_save',save);monkeypatch.setattr(export._Exporter,'_cleanup',cleanup)
    raw=spool_path(routed,spec).read_bytes()
    before=sum(c[0] in ('create','start') for c in docker.calls)
    receipt=export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)
    assert receipt['bytes_recoverable'] and receipt['collector_removed']
    restored=export.load_workspace_export(routed,spec,authorize=lambda _:True)
    assert restored.tree.files[0].data==b'result\n'
    assert restored.stream_sha256==export._sha(raw)
    assert export.load_workspace_export(routed,spec,authorize=lambda _:True)==restored
    assert sum(c[0] in ('create','start') for c in docker.calls)==before


def test_unknown_remove_restores_durable_bytes(exporter):
    routed,spec,_,docker=exporter;docker.lost_remove=True
    with pytest.raises(RuntimeError):run(exporter)
    assert export.load_workspace_export(routed,spec,authorize=lambda _:True).tree.files[0].data==b'result\n'
    docker.lost_remove=False
    assert export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)['bytes_recoverable']
    value=export.load_workspace_export(routed,spec,authorize=lambda _:True)
    assert value.tree.files[0].data==b'result\n' and not docker.objects


@pytest.mark.parametrize('damage',['truncate','same_length','hardlink','symlink','mode','inode'])
def test_durable_spool_damage_cannot_reload_or_validate(exporter,damage):
    routed,spec,_,docker=exporter;value=run(exporter);path=spool_path(routed,spec)
    if damage=='truncate':path.write_bytes(path.read_bytes()[:-1])
    elif damage=='same_length':path.write_bytes(b'X'+path.read_bytes()[1:])
    elif damage=='hardlink':os.link(path,path.with_name('linked'))
    elif damage=='symlink':
        original=path.with_name('retained');path.rename(original);path.symlink_to(original)
    elif damage=='mode':path.chmod(0o640)
    else:
        original=path.with_name('retained');path.rename(original);path.write_bytes(original.read_bytes());path.chmod(0o600)
    with pytest.raises(export.WorkspaceExportError):export.load_workspace_export(routed,spec,authorize=lambda _:True)
    with pytest.raises(export.WorkspaceExportError):export.validate_workspace_export(routed,spec,value,authorize=lambda _:True)
    assert not docker.objects


def test_reload_rechecks_current_secrets_and_authority(exporter):
    routed,spec,_,docker=exporter;run(exporter)
    with pytest.raises(ExportError):
        export.load_workspace_export(routed,spec,authorize=lambda _:True,forbidden_values=(b'result',))
    with pytest.raises(export.WorkspaceExportError,match='authority'):
        export.load_workspace_export(routed,spec,authorize=lambda _:False)
    assert not docker.objects


@pytest.mark.parametrize('case',['owner','generation','program'])
def test_reload_rejects_stale_binding(exporter,case):
    routed,spec,*_=exporter;run(exporter)
    if case=='generation':spec=replace(spec,generation=2)
    else:
        helper=export._Exporter(routed,lambda _:True);journal=helper._load(spec)
        if case=='owner':journal['spool']['owner_uid']+=1
        else:journal['program_sha256']='0'*64
        helper._save(spec,journal)
    with pytest.raises((RuntimeError,FileNotFoundError)):
        export.load_workspace_export(routed,spec,authorize=lambda _:True)


def test_partial_stream_recovery_only_cleans(exporter,monkeypatch):
    routed,spec,_,docker=exporter;original=export._bounded_run
    def crash(runtime,args,**kwargs):
        if args[:2]==['start','--attach']:
            os.write(kwargs['stdout_fd'],docker.stream[:40]);docker.objects[EID]['State'].update(Status='exited',Running=False,ExitCode=0)
            raise ControllerCrash()
        return original(runtime,args,**kwargs)
    monkeypatch.setattr(export,'_bounded_run',crash)
    with pytest.raises(ControllerCrash):run(exporter)
    result=export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)
    assert result['bytes_recoverable'] is False and not docker.objects
    with pytest.raises(export.WorkspaceExportError,match='aborted'):
        export.load_workspace_export(routed,spec,authorize=lambda _:True)


def test_complete_spool_without_zero_exit_proof_is_not_adopted(exporter,monkeypatch):
    routed,spec,_,docker=exporter;original=export._bounded_run
    def crash(runtime,args,**kwargs):
        if args[:2]==['start','--attach']:
            os.write(kwargs['stdout_fd'],docker.stream);docker.objects[EID]['State'].update(Status='exited',Running=False,ExitCode=1)
            raise ControllerCrash()
        return original(runtime,args,**kwargs)
    monkeypatch.setattr(export,'_bounded_run',crash)
    with pytest.raises(ControllerCrash):run(exporter)
    assert not export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)['bytes_recoverable']
    with pytest.raises(export.WorkspaceExportError):export.load_workspace_export(routed,spec,authorize=lambda _:True)


def test_recovered_marker_fsync_precedes_cleanup(exporter,monkeypatch):
    routed,spec,_,docker=exporter;original=export._bounded_run
    def crash(runtime,args,**kwargs):
        if args[:2]==['start','--attach']:
            os.write(kwargs['stdout_fd'],docker.stream);docker.objects[EID]['State'].update(Status='exited',Running=False,ExitCode=0)
            raise ControllerCrash()
        return original(runtime,args,**kwargs)
    monkeypatch.setattr(export,'_bounded_run',crash)
    with pytest.raises(ControllerCrash):run(exporter)
    fsync=os.fsync;observed=[];identity=spool_path(routed,spec).stat().st_ino
    def checked(fd):
        if os.fstat(fd).st_ino==identity:observed.append('spool_fsync')
        return fsync(fd)
    cleanup=export._Exporter._cleanup
    def checked_cleanup(self,*args):
        assert observed==['spool_fsync'];return cleanup(self,*args)
    monkeypatch.setattr(os,'fsync',checked);monkeypatch.setattr(export._Exporter,'_cleanup',checked_cleanup)
    assert export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)['bytes_recoverable']


def test_real_stdout_capture_spools_without_returning_bytes(tmp_path):
    binary=tmp_path/'fake-docker';binary.write_text('#!'+sys.executable+'\nprint("x"*100000)\n');binary.chmod(0o700)
    fd=os.open(tmp_path/'spool',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
    try:
        assert export._bounded_run(SimpleNamespace(docker=str(binary)),[],timeout=5,cap=100001,stdout_fd=fd)==b''
        assert os.fstat(fd).st_size==100001
    finally:os.close(fd)


def test_legacy_hash_only_receipt_not_upgraded_to_durable_result(exporter):
    routed,spec,*_=exporter;value=run(exporter);helper=export._Exporter(routed,lambda _:True)
    journal=helper._load(spec);journal['version']=1;journal.pop('collection');journal.pop('spool');journal.pop('outcome');helper._save(spec,journal)
    with pytest.raises(export.WorkspaceExportError):export.load_workspace_export(routed,spec,authorize=lambda _:True)
    with pytest.raises(export.WorkspaceExportError):export.validate_workspace_export(routed,spec,value,authorize=lambda _:True)
    assert export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)['bytes_recoverable'] is False


def test_complete_spool_without_exit_observation_and_missing_container_remains_held(exporter,monkeypatch):
    routed,spec,_,docker=exporter;original=export._bounded_run
    def crash(runtime,args,**kwargs):
        if args[:2]==['start','--attach']:
            os.write(kwargs['stdout_fd'],docker.stream);docker.objects.clear();raise ControllerCrash()
        return original(runtime,args,**kwargs)
    monkeypatch.setattr(export,'_bounded_run',crash)
    with pytest.raises(ControllerCrash):run(exporter)
    with pytest.raises(export.WorkspaceExportError,match='absence_not_cleanup'):
        export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)
    with pytest.raises(export.WorkspaceExportError):export.load_workspace_export(routed,spec,authorize=lambda _:True)


def test_fresh_secret_policy_prevents_recovery_adoption_but_not_cleanup(exporter,monkeypatch):
    routed,spec,_,docker=exporter;original=export._bounded_run
    def crash(runtime,args,**kwargs):
        if args[:2]==['start','--attach']:
            os.write(kwargs['stdout_fd'],docker.stream);docker.objects[EID]['State'].update(Status='exited',Running=False,ExitCode=0)
            raise ControllerCrash()
        return original(runtime,args,**kwargs)
    monkeypatch.setattr(export,'_bounded_run',crash)
    with pytest.raises(ControllerCrash):run(exporter)
    result=export.reconcile_workspace_export(routed,spec,authorize=lambda _:True,forbidden_values=(b'result',))
    assert result['bytes_recoverable'] is False and not docker.objects
    with pytest.raises(export.WorkspaceExportError):export.load_workspace_export(routed,spec,authorize=lambda _:True)


def test_real_spool_byte_bound_is_cumulative(tmp_path):
    binary=tmp_path/'fake-docker'
    binary.write_text('#!'+sys.executable+'\nimport os\nfor i in range(100):os.write(1,b"x"*4096)\n');binary.chmod(0o700)
    fd=os.open(tmp_path/'spool',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with pytest.raises(export.WorkspaceExportError,match='output_bound'):
            export._bounded_run(SimpleNamespace(docker=str(binary)),[],timeout=5,cap=70000,stdout_fd=fd)
        assert os.fstat(fd).st_size<=70000
    finally:os.close(fd)


def test_running_start_timeout_automatically_stops_and_removes_without_relaunch(exporter,monkeypatch):
    routed,spec,_,docker=exporter;original=export._bounded_run
    def timeout(runtime,args,**kwargs):
        if args[:2]==['start','--attach']:
            docker.calls.append(args);docker.objects[EID]['State'].update(Status='running',Running=True)
            raise export.WorkspaceExportError('export_docker_timeout')
        return original(runtime,args,**kwargs)
    monkeypatch.setattr(export,'_bounded_run',timeout)
    with pytest.raises(export.WorkspaceExportError,match='docker_timeout') as caught:run(exporter)
    assert caught.value.cleanup_status=='confirmed' and caught.value.export_outcome=='aborted'
    assert not docker.objects
    assert [c[0] for c in docker.calls].count('stop')==1
    assert [c[0] for c in docker.calls].count('rm')==1
    status=export.workspace_export_status(routed,spec,authorize=lambda _:True)
    assert status=={'state':'cleaned','outcome':'aborted','collector_id':EID,'failure_code':'export_docker_timeout'}
    for _ in range(2):
        proof=export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)
        assert proof['terminal_aborted'] and not proof['bytes_recoverable'] and proof['outcome']=='aborted'
    with pytest.raises(export.WorkspaceExportError,match='already_attempted'):run(exporter)
    assert sum(c[0]=='create' for c in docker.calls)==1 and sum(c[0]=='start' for c in docker.calls)==1


def test_lost_create_ack_discovered_and_cleaned_before_original_error_returns(exporter):
    routed,spec,_,docker=exporter;docker.lost_create=True
    with pytest.raises(RuntimeError,match='synthetic unknown create') as caught:run(exporter)
    assert caught.value.cleanup_status=='confirmed' and caught.value.export_outcome=='aborted'
    assert not docker.objects
    discovery=[c for c in docker.calls if c[:2]==['container','ls'] and any(v.startswith('label=') for v in c)]
    assert len(discovery)==1 and any('io.cloudworkbench.caller-spec='+spec.digest in v for v in discovery[0])
    assert not any(c[0]=='start' for c in docker.calls)


def test_unknown_create_zero_ids_stays_held_without_terminal_abort(exporter):
    routed,spec,_,docker=exporter;docker.lost_create=True;docker.hide_discovery=True
    with pytest.raises(RuntimeError) as caught:run(exporter)
    assert caught.value.cleanup_status=='unresolved'
    assert 'unresolved' in caught.value.__notes__[0]
    assert EID in docker.objects and not any(c[0]=='rm' for c in docker.calls)
    status=export.workspace_export_status(routed,spec,authorize=lambda _:True)
    assert status['outcome']=='held' and status['collector_id'] is None and status['state']=='create_intent'
    with pytest.raises(export.WorkspaceExportError,match='already_attempted'):run(exporter)
    assert sum(c[0]=='create' for c in docker.calls)==1


def test_cancel_after_start_allows_only_cleanup_authority(exporter,monkeypatch):
    routed,spec,cleanup,docker=exporter;original=export._bounded_run;revoked=[False];actions=[]
    def authorize(action):
        actions.append((revoked[0],action))
        return not revoked[0] or action in ('reconcile','cleanup_inspect','stop','remove','status')
    def timeout(runtime,args,**kwargs):
        if args[:2]==['start','--attach']:
            docker.calls.append(args);docker.objects[EID]['State'].update(Status='running',Running=True);revoked[0]=True
            raise export.WorkspaceExportError('export_docker_timeout')
        return original(runtime,args,**kwargs)
    monkeypatch.setattr(export,'_bounded_run',timeout)
    with pytest.raises(export.WorkspaceExportError,match='docker_timeout') as caught:
        export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),
            expected_workspace_identity=workspace_identity(spec),authorize=authorize)
    assert caught.value.cleanup_status=='confirmed' and not docker.objects
    assert {a for revoked,a in actions if revoked}<={'reconcile','cleanup_inspect','stop','remove','status'}


def test_abort_after_secret_rejection_cannot_be_relaxed_on_reload(exporter):
    routed,spec,*_=exporter
    with pytest.raises(ExportError):run(exporter,forbidden_values=(b'result',))
    first=export.workspace_export_status(routed,spec,authorize=lambda _:True)
    assert first['outcome']=='aborted' and first['failure_code']=='export_validation_failed'
    with pytest.raises(export.WorkspaceExportError,match='aborted'):
        export.load_workspace_export(routed,spec,authorize=lambda _:True,forbidden_values=())
    second=export.reconcile_workspace_export(routed,spec,authorize=lambda _:True,forbidden_values=())
    assert second['terminal_aborted'] and second['bytes_recoverable'] is False
    assert export.workspace_export_status(routed,spec,authorize=lambda _:True)==first


def test_status_not_started_requires_no_journal_and_fresh_authority(exporter):
    routed,spec,*_=exporter
    assert export.workspace_export_status(routed,spec,authorize=lambda _:True)=={
        'state':'not_started','outcome':None,'collector_id':None,'failure_code':None}
    with pytest.raises(export.WorkspaceExportError,match='authority'):
        export.workspace_export_status(routed,spec,authorize=lambda _:False)
    (routed._folder(spec)/'workspace-export.json').write_bytes(b'{')
    (routed._folder(spec)/'workspace-export.json').chmod(0o600)
    with pytest.raises(export.WorkspaceExportError,match='journal_invalid'):
        export.workspace_export_status(routed,spec,authorize=lambda _:True)


def test_cleanup_authority_denial_preserves_running_collector_and_original_error(exporter,monkeypatch):
    routed,spec,cleanup,docker=exporter;original=export._bounded_run
    def timeout(runtime,args,**kwargs):
        if args[:2]==['start','--attach']:
            docker.calls.append(args);docker.objects[EID]['State'].update(Status='running',Running=True)
            raise export.WorkspaceExportError('export_docker_timeout')
        return original(runtime,args,**kwargs)
    monkeypatch.setattr(export,'_bounded_run',timeout)
    with pytest.raises(export.WorkspaceExportError,match='docker_timeout') as caught:
        export.export_stopped_workspace(routed,spec,RID,cleanup,selected_paths=('answer.txt',),
            expected_workspace_identity=workspace_identity(spec),authorize=lambda action:action not in ('stop','remove'))
    assert caught.value.cleanup_status=='unresolved' and docker.objects[EID]['State']['Running']
    assert export.workspace_export_status(routed,spec,authorize=lambda _:True)['outcome']=='held'


def test_cleanup_deadline_retains_uncertainty_instead_of_claiming_abort(exporter,monkeypatch):
    routed,spec,_,docker=exporter;docker.lost_create=True
    monkeypatch.setattr(export,'_CLEANUP_SECONDS',0)
    with pytest.raises(RuntimeError) as caught:run(exporter)
    assert caught.value.cleanup_status=='unresolved' and EID in docker.objects
    assert export.workspace_export_status(routed,spec,authorize=lambda _:True)['outcome']=='held'
