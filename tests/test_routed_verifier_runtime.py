from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest
from cloudworkbench.runtime import Runtime, RuntimeError
from cloudworkbench import routed_verifier_runtime as module
from cloudworkbench.routed_verifier_runtime import CheckRuntimeSpec, VerifierRuntime, VerifierRuntimeError
from test_routed_runtime import Docker, IMAGE, RID

REAL_LOG_BYTES=module._log_bytes
ALLOW=lambda action:True
CLEANUP={'reconcile','cleanup_inspect','cleanup_logs','stop','remove'}

class CheckDocker(Docker):
    def __init__(self):
        super().__init__();self.running=False;self.exit_code=0;self.oom=False
        self.lost_start=False;self.lost_rm=False;self.stop_fails=False;self.no_create=False
        self.before_inspect=None
    def __call__(self,args,**kwargs):
        if args[0]=='create' and self.no_create:
            self.calls.append(args);raise RuntimeError('unknown create')
        if args[0]=='inspect' and self.before_inspect:self.before_inspect(self.objects[RID])
        if args[0]=='stop' and self.stop_fails:raise RuntimeError('stop unavailable')
        try:result=super().__call__(args,**kwargs)
        except RuntimeError:
            if args[0]=='create' and RID in self.objects:
                for mount in self.objects[RID]['Mounts']:mount['Propagation']='rprivate'
            raise
        if args[0]=='create':
            for mount in self.objects[RID]['Mounts']:mount['Propagation']='rprivate'
        if args[0]=='start':
            if not self.running:self.objects[RID]['State'].update(Status='exited',Running=False,ExitCode=self.exit_code,OOMKilled=self.oom)
            if self.lost_start:raise RuntimeError('lost start response')
        if args[0]=='stop':self.objects[RID]['State'].update(ExitCode=137,OOMKilled=False)
        if args[0]=='rm' and self.lost_rm:raise RuntimeError('lost remove response')
        return result

@pytest.fixture
def setup(tmp_path,monkeypatch):
    candidate=tmp_path/'candidate';candidate.mkdir();candidate.chmod(0o755)
    task=tmp_path/'task';task.mkdir(mode=0o755);task.chmod(0o755)
    (task/'check.py').write_text('print("synthetic")\n');(task/'check.py').chmod(0o555)
    base=Runtime({'root':tmp_path,'image':IMAGE,'test_path_workspace':True,'approved_mount_roots':[tmp_path]})
    runtime=VerifierRuntime(base,journal_root=tmp_path/'journal')
    identity=lambda p:(p.stat().st_dev,p.stat().st_ino)
    spec=CheckRuntimeSpec(base.owner,'child',1,'root',1,'c'*64,'check',IMAGE,candidate,task,
        identity(candidate),identity(task),('/opt/hermes/venv/bin/python','-I','-B','/run/task/check.py'),1)
    docker=CheckDocker()
    monkeypatch.setattr(module,'_bounded_run',lambda base,args,**kw:docker(args).encode())
    monkeypatch.setattr(module,'_log_bytes',lambda base,identity,timeout,cap:b'synthetic diagnostics\n')
    return runtime,spec,docker


def journal(runtime,spec):return json.loads((runtime._folder(spec)/'journal.json').read_text())

def test_completed_result_persists_exact_exit_and_reloads_without_launch(setup):
    runtime,spec,docker=setup
    result=runtime.run(spec,authorize=ALLOW)
    assert result.exit_code==0 and not result.oom and not result.timed_out and result.cleanup_confirmed
    assert result.logs==b'synthetic diagnostics\n' and 'synthetic' not in repr(result)
    assert result.logs_sha256==hashlib.sha256(result.logs).hexdigest()
    assert not docker.objects
    fresh=VerifierRuntime(runtime.base,journal_root=runtime.root)
    assert fresh.load(spec,authorize=ALLOW)==result
    assert fresh.run(spec,authorize=ALLOW)==result
    assert sum(c[0]=='create' for c in docker.calls)==1
    assert sum(c[0]=='start' for c in docker.calls)==1
    assert (runtime._folder(spec)/'logs.bin').stat().st_mode&0o777==0o600

@pytest.mark.parametrize('exit_code,oom',[(2,False),(137,True)])
def test_result_uses_docker_state_not_stdout(setup,monkeypatch,exit_code,oom):
    runtime,spec,docker=setup;docker.exit_code=exit_code;docker.oom=oom
    monkeypatch.setattr(module,'_log_bytes',lambda *a:b'{"passed":true,"exit_code":0}')
    result=runtime.run(spec,authorize=ALLOW)
    assert result.exit_code==exit_code and result.oom is oom


def test_flags_are_exact_no_credentials_or_other_mounts(setup):
    runtime,spec,docker=setup;runtime.run(spec,authorize=ALLOW)
    args=docker.calls[0]
    assert args[0]=='create' and '--network' in args and args[args.index('--network')+1]=='none'
    assert args[args.index('--user')+1]=='1000:1000'
    assert args[args.index('--entrypoint')+1]=='/usr/bin/env'
    assert args[args.index(IMAGE)+1:]==runtime._command(spec)
    assert sum(a=='--mount' for a in args)==2
    assert not any('secrets' in a or 'socket' in a for a in args)

@pytest.mark.parametrize('change',[
    {'generation':True},{'root_generation':0},{'owner':[]},{'plan_sha256':'bad'},{'image':'latest'},
    {'uid':0},{'gid':0},{'timeout_seconds':601},{'timeout_seconds':True},{'cpus':float('nan')},
    {'cpus':3},{'memory_mib':127},{'pids':513},{'max_log_bytes':True},{'max_log_bytes':1048577},
    {'candidate_identity':(True,1)},{'argv':('/bin/sh','-c','/run/task/check.py')},
    {'argv':('/usr/bin/python3','-c','/run/task/check.py')},{'argv':('/workspace/check.py',)},
    {'argv':('/run/task/../check.py',)},{'argv':('/usr/bin/node','--eval','/run/task/check.py')},
    {'argv':['/run/task/check.py']},{'check_id':'../x'},
])
def test_spec_rejects_unsafe_input(setup,change):
    _,spec,_=setup
    with pytest.raises((RuntimeError,TypeError)):replace(spec,**change)

@pytest.mark.parametrize('argv',[
    ('/run/task/check.py',),('/bin/sh','-e','/run/task/check.py'),('/bin/bash','--noprofile','--norc','/run/task/check.py'),
    ('/usr/bin/python3','-I','-B','/run/task/check.py'),('/usr/local/bin/node','/run/task/check.py')])
def test_trusted_interpreters_supported(setup,argv):
    _,spec,_=setup;assert replace(spec,argv=argv).argv==argv

@pytest.mark.parametrize('tamper', ['image','uid','network','rw','extra_mount','cap','command','memory','labels','tmpfs'])
def test_tampered_object_never_started_or_deleted(setup,tamper):
    runtime,spec,docker=setup
    def change(obj):
        if tamper=='image':obj['Image']='sha256:'+'f'*64
        elif tamper=='uid':obj['Config']['User']='0:0'
        elif tamper=='network':obj['HostConfig']['NetworkMode']='host'
        elif tamper=='rw':obj['Mounts'][0]['RW']=True
        elif tamper=='extra_mount':obj['Mounts'].append(dict(obj['Mounts'][0]))
        elif tamper=='cap':obj['HostConfig']['CapAdd']=['SYS_ADMIN']
        elif tamper=='command':obj['Config']['Cmd']=['evil']
        elif tamper=='memory':obj['HostConfig']['Memory']=0
        elif tamper=='labels':obj['Config']['Labels']['io.cloudworkbench.generation']='2'
        else:obj['HostConfig']['Tmpfs']['/tmp']='rw,size=1g'
    docker.before_inspect=change
    with pytest.raises(RuntimeError) as error:runtime.run(spec,authorize=ALLOW)
    assert error.value.cleanup_status=='unresolved' and RID in docker.objects
    assert not any(c[0] in ('start','rm','stop') for c in docker.calls)


def test_running_deadline_stops_removes_and_is_not_pass(setup):
    runtime,spec,docker=setup;docker.running=True
    result=runtime.run(spec,authorize=ALLOW)
    assert result.timed_out and result.exit_code==137 and not docker.objects
    assert sum(c[0]=='start' for c in docker.calls)==1


def test_lost_create_ack_discovers_only_exact_stopped_object(setup):
    runtime,spec,docker=setup;docker.fail_create=True
    with pytest.raises(RuntimeError,match='lost create') as error:runtime.run(spec,authorize=ALLOW)
    assert error.value.cleanup_status=='confirmed' and not docker.objects
    proof=runtime.reconcile(spec,authorize=lambda action:action in CLEANUP)
    assert proof['aborted']=='check_not_completed' and not proof['result_available']
    with pytest.raises(RuntimeError):runtime.run(spec,authorize=ALLOW)
    assert sum(c[0]=='create' for c in docker.calls)==1


def test_zero_ids_after_unknown_create_stays_held(setup):
    runtime,spec,docker=setup;docker.no_create=True
    with pytest.raises(RuntimeError) as error:runtime.run(spec,authorize=ALLOW)
    assert error.value.cleanup_status=='unresolved'
    with pytest.raises(RuntimeError,match='create_unresolved'):runtime.reconcile(spec,authorize=ALLOW)
    with pytest.raises(RuntimeError):runtime.run(spec,authorize=ALLOW)
    assert sum(c[0]=='create' for c in docker.calls)==1

@pytest.mark.parametrize('running',[False,True])
def test_lost_start_cleanup_never_relaunches(setup,running):
    runtime,spec,docker=setup;docker.running=running;docker.lost_start=True
    with pytest.raises(RuntimeError,match='lost start') as error:runtime.run(spec,authorize=ALLOW)
    assert error.value.cleanup_status=='confirmed' and not docker.objects
    if running:
        assert journal(runtime,spec)['aborted']=='interrupted'
        with pytest.raises(RuntimeError):runtime.load(spec,authorize=ALLOW)
    else:
        assert journal(runtime,spec)['aborted']=='completion_timing_unproven'
        with pytest.raises(RuntimeError):runtime.load(spec,authorize=ALLOW)
    assert sum(c[0]=='start' for c in docker.calls)==1


def test_unexplained_absence_never_proves_cleanup(setup):
    runtime,spec,docker=setup;docker.running=True;docker.lost_start=True;docker.stop_fails=True
    with pytest.raises(RuntimeError):runtime.run(spec,authorize=ALLOW)
    docker.objects.clear()
    with pytest.raises(RuntimeError,match='absence_not_cleanup'):runtime.reconcile(spec,authorize=ALLOW)


def test_lost_remove_ack_absence_is_held_without_confirmed_removal(setup):
    runtime,spec,docker=setup;docker.lost_rm=True
    with pytest.raises(RuntimeError,match='lost remove') as error:runtime.run(spec,authorize=ALLOW)
    assert error.value.cleanup_status=='unresolved'
    with pytest.raises(RuntimeError,match='absence_not_cleanup'):runtime.reconcile(spec,authorize=ALLOW)
    with pytest.raises(RuntimeError):runtime.load(spec,authorize=ALLOW)


def test_cancel_blocks_start_but_cleanup_remains_authorized(setup):
    runtime,spec,docker=setup
    with pytest.raises(RuntimeError,match='authority') as error:runtime.run(spec,authorize=lambda action:action!='start')
    assert error.value.cleanup_status=='confirmed' and not docker.objects
    assert not any(c[0]=='start' for c in docker.calls)


def test_no_journal_reconcile_does_not_create_attempt(setup):
    runtime,spec,docker=setup
    result=runtime.reconcile(spec,authorize=lambda action:action in CLEANUP)
    assert result['state']=='not_started' and result['cleanup_confirmed']
    assert not runtime._folder(spec).exists() and docker.calls==[]


def test_cleanup_not_blocked_by_removed_material(setup):
    runtime,spec,docker=setup;docker.running=True;docker.lost_start=True;docker.stop_fails=True
    with pytest.raises(RuntimeError):runtime.run(spec,authorize=ALLOW)
    (spec.task_path/'check.py').unlink();spec.task_path.rmdir();docker.stop_fails=False
    assert runtime.reconcile(spec,authorize=lambda action:action in CLEANUP)['cleanup_confirmed']
    assert not docker.objects


def test_authority_side_effect_script_change_prevents_start(setup):
    runtime,spec,docker=setup
    def authorize(action):
        if action=='start':
            (spec.task_path/'check.py').chmod(0o755);(spec.task_path/'check.py').write_text('changed')
        return True
    with pytest.raises(RuntimeError,match='script_changed'):runtime.run(spec,authorize=authorize)
    assert not any(c[0]=='start' for c in docker.calls) and not docker.objects


def test_secret_rejected_before_diagnostic_persistence(setup,monkeypatch):
    runtime,spec,docker=setup;monkeypatch.setattr(module,'_log_bytes',lambda *a:b'synthetic-secret')
    with pytest.raises(RuntimeError,match='logs_rejected') as error:runtime.run(spec,authorize=ALLOW,forbidden_values=(b'synthetic-secret',))
    assert error.value.cleanup_status=='confirmed' and not docker.objects
    assert not (runtime._folder(spec)/'logs.bin').exists()
    assert 'synthetic-secret' not in (runtime._folder(spec)/'journal.json').read_text()
    with pytest.raises(RuntimeError):runtime.load(spec,authorize=ALLOW)

@pytest.mark.parametrize('tamper',['bytes','link','permissions','inode'])
def test_durable_logs_refuse_tampering(setup,tamper):
    runtime,spec,docker=setup;runtime.run(spec,authorize=ALLOW)
    path=runtime._folder(spec)/'logs.bin'
    if tamper=='bytes':path.write_bytes(b'changed')
    elif tamper=='link':os.link(path,path.with_suffix('.link'))
    elif tamper=='permissions':path.chmod(0o640)
    else:body=path.read_bytes();path.unlink();path.write_bytes(body);path.chmod(0o600)
    with pytest.raises(RuntimeError):runtime.load(spec,authorize=ALLOW)


def test_load_rescans_new_secret_policy(setup):
    runtime,spec,docker=setup;runtime.run(spec,authorize=ALLOW)
    with pytest.raises(RuntimeError,match='logs_rejected'):runtime.load(spec,authorize=ALLOW,forbidden_values=(b'synthetic',))


def test_log_write_before_observation_crash_cannot_prove_completion_timing(setup,monkeypatch):
    runtime,spec,docker=setup;original=runtime._save;failed=False
    def save(spec,value):
        nonlocal failed
        if value['state']=='observed' and not failed:failed=True;raise KeyboardInterrupt()
        original(spec,value)
    monkeypatch.setattr(runtime,'_save',save)
    with pytest.raises(KeyboardInterrupt):runtime.run(spec,authorize=ALLOW)
    assert RID in docker.objects
    fresh=VerifierRuntime(runtime.base,journal_root=runtime.root)
    proof=fresh.reconcile(spec,authorize=ALLOW)
    assert proof['aborted']=='completion_timing_unproven' and not proof['result_available']
    assert proof['cleanup_confirmed'] and not docker.objects
    with pytest.raises(RuntimeError):fresh.load(spec,authorize=ALLOW)
    assert (runtime._folder(spec)/'logs.bin').read_bytes()==b'synthetic diagnostics\n'
    assert sum(c[0]=='start' for c in docker.calls)==1


def test_concurrent_calls_create_and_start_once(setup):
    from concurrent.futures import ThreadPoolExecutor
    runtime,spec,docker=setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:runtime.run(spec,authorize=ALLOW),range(2)))
    assert results[0]==results[1]
    assert sum(c[0]=='create' for c in docker.calls)==1


def test_cleanup_authority_is_not_inherited_from_other_thread(setup):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    runtime,_,_=setup;barrier=threading.Barrier(2)
    def call(allowed):
        runtime.authorize=lambda _:allowed;barrier.wait()
        try:runtime._auth('inspect');return True
        except VerifierRuntimeError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert list(pool.map(call,[True,False]))==[True,False]


def test_reconstructed_cleanup_survives_replaced_material_symlink(setup):
    runtime,spec,docker=setup;docker.running=True;docker.lost_start=True;docker.stop_fails=True
    with pytest.raises(RuntimeError):runtime.run(spec,authorize=ALLOW)
    old=spec.task_path.with_name('old-task');spec.task_path.rename(old);spec.task_path.symlink_to(old)
    docker.stop_fails=False
    fresh=VerifierRuntime(runtime.base,journal_root=runtime.root)
    assert fresh.reconcile(spec,authorize=lambda action:action in CLEANUP)['cleanup_confirmed']


def test_partial_log_before_crash_is_aborted_and_cleaned(setup,monkeypatch):
    runtime,spec,docker=setup
    original=runtime._observe
    def crash(spec,value,state,forbidden):
        path=runtime._folder(spec)/'logs.bin';path.write_bytes(b'partial');path.chmod(0o600)
        raise KeyboardInterrupt()
    monkeypatch.setattr(runtime,'_observe',crash)
    with pytest.raises(KeyboardInterrupt):runtime.run(spec,authorize=ALLOW)
    fresh=VerifierRuntime(runtime.base,journal_root=runtime.root)
    result=fresh.reconcile(spec,authorize=ALLOW)
    assert result['cleanup_confirmed'] and not result['result_available'] and not docker.objects
    with pytest.raises(RuntimeError):fresh.load(spec,authorize=ALLOW)


def test_cleanup_denied_keeps_object_until_exact_retry(setup):
    runtime,spec,docker=setup
    with pytest.raises(RuntimeError) as error:runtime.run(spec,authorize=lambda action:action!='remove')
    assert error.value.cleanup_status=='unresolved' and RID in docker.objects
    assert runtime.reconcile(spec,authorize=lambda action:action in CLEANUP)['result_available']
    assert not docker.objects

@pytest.mark.parametrize('field,value',[('timed_out',1),('deadline_at','bad'),('observation',[]),('runtime_id','short')])
def test_malformed_private_journal_cannot_produce_result(setup,field,value):
    runtime,spec,docker=setup;runtime.run(spec,authorize=ALLOW)
    doc=journal(runtime,spec);doc[field]=value;runtime._save(spec,doc)
    with pytest.raises(RuntimeError,match='journal_invalid'):runtime.load(spec,authorize=ALLOW)


def test_stale_generation_and_changed_spec_cannot_reuse_result(setup):
    runtime,spec,docker=setup;runtime.run(spec,authorize=ALLOW)
    with pytest.raises((RuntimeError,OSError)):runtime.load(replace(spec,generation=2),authorize=ALLOW)
    with pytest.raises(RuntimeError,match='journal_invalid'):runtime.load(replace(spec,plan_sha256='d'*64),authorize=ALLOW)

@pytest.mark.parametrize('case',['merged','bound','timeout','exit'])
def test_actual_bounded_log_subprocess(tmp_path,case):
    import sys
    from types import SimpleNamespace
    path=tmp_path/'fake-docker'
    body={'merged':"import os;os.write(1,b'out');os.write(2,b'err')",
        'bound':"import os;os.write(1,b'x'*1000)",'timeout':"import time;time.sleep(10)",
        'exit':"import sys;print('unsafe diagnostic');sys.exit(7)"}[case]
    path.write_text('#!'+sys.executable+'\n'+body+'\n');path.chmod(0o700)
    if case=='merged':assert module._log_bytes(SimpleNamespace(docker=str(path)),RID,2,100)==b'outerr'
    else:
        with pytest.raises(RuntimeError) as error:module._log_bytes(SimpleNamespace(docker=str(path)),RID,.2 if case=='timeout' else 2,100)
        assert 'unsafe diagnostic' not in str(error.value)


def test_acknowledged_removal_receipt_crash_replays_without_launch(setup,monkeypatch):
    runtime,spec,docker=setup;original=runtime._save
    def save(spec,value):
        if value['state']=='cleaned':raise KeyboardInterrupt()
        original(spec,value)
    monkeypatch.setattr(runtime,'_save',save)
    with pytest.raises(KeyboardInterrupt):runtime.run(spec,authorize=ALLOW)
    assert journal(runtime,spec)['state']=='removal_confirmed' and not docker.objects
    fresh=VerifierRuntime(runtime.base,journal_root=runtime.root)
    assert fresh.reconcile(spec,authorize=ALLOW)['result_available']
    assert fresh.load(spec,authorize=ALLOW).exit_code==0
    assert sum(c[0]=='create' for c in docker.calls)==1


def test_failed_removal_confirmation_persistence_does_not_credit_absence(setup,monkeypatch):
    runtime,spec,docker=setup;original=runtime._save
    def save(spec,value):
        if value['state']=='removal_confirmed':raise KeyboardInterrupt()
        original(spec,value)
    monkeypatch.setattr(runtime,'_save',save)
    with pytest.raises(KeyboardInterrupt):runtime.run(spec,authorize=ALLOW)
    assert journal(runtime,spec)['state']=='remove_intent' and not docker.objects
    fresh=VerifierRuntime(runtime.base,journal_root=runtime.root)
    with pytest.raises(RuntimeError,match='absence_not_cleanup'):fresh.reconcile(spec,authorize=ALLOW)

@pytest.mark.parametrize('change',['extra_tmpfs','shared_bind','extra_group'])
def test_additional_host_escape_surface_refused(setup,change):
    runtime,spec,docker=setup
    def tamper(obj):
        if change=='extra_tmpfs':obj['Mounts'].append({'Type':'tmpfs','Destination':'/etc','RW':True})
        elif change=='shared_bind':obj['Mounts'][0]['Propagation']='rshared'
        else:obj['HostConfig']['GroupAdd']=['0']
    docker.before_inspect=tamper
    with pytest.raises(RuntimeError):runtime.run(spec,authorize=ALLOW)
    assert not any(c[0] in ('start','rm') for c in docker.calls)


def test_json_escaped_secret_never_persisted(setup,monkeypatch):
    runtime,spec,docker=setup
    monkeypatch.setattr(module,'_log_bytes',lambda *a:b'{"key":"synthetic\\u002dsecret"}\n')
    with pytest.raises(RuntimeError,match='logs_rejected'):runtime.run(spec,authorize=ALLOW,forbidden_values=[b'synthetic-secret'])
    assert not (runtime._folder(spec)/'logs.bin').exists() and not docker.objects

@pytest.mark.parametrize('policy',[[b'x']*65,[b'x'*4097],[b'x'*4096]*17,[b''],['bad']])
def test_shared_secret_policy_bound_before_any_create(setup,policy):
    runtime,spec,docker=setup
    with pytest.raises(RuntimeError,match='secret_policy'):runtime.run(spec,authorize=ALLOW,forbidden_values=policy)
    assert docker.calls==[]


def test_zero_log_limit_and_long_check_id_are_bound(setup,monkeypatch):
    runtime,spec,docker=setup;spec=replace(spec,max_log_bytes=0,check_id='c'*128)
    monkeypatch.setattr(module,'_log_bytes',lambda base,identity,timeout,cap:b'' if cap==0 else b'bad')
    result=runtime.run(spec,authorize=ALLOW)
    assert result.logs==b'' and runtime.load(spec,authorize=ALLOW)==result


def test_diagnostic_failure_preserves_original_error_and_cleans(setup,monkeypatch):
    runtime,spec,docker=setup
    def unavailable(*args):raise VerifierRuntimeError('verifier_logs_bound')
    monkeypatch.setattr(module,'_log_bytes',unavailable)
    with pytest.raises(RuntimeError,match='logs_bound') as error:runtime.run(spec,authorize=ALLOW)
    assert error.value.cleanup_status=='confirmed' and not docker.objects
    proof=runtime.reconcile(spec,authorize=ALLOW)
    assert proof['aborted']=='diagnostics_unavailable' and not proof['result_available']


def test_recovered_unobserved_exit_after_wall_clock_rollback_cannot_pass(setup,monkeypatch):
    from types import SimpleNamespace
    import time as real_time
    runtime,spec,docker=setup;wall=[1000.0]
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:wall[0],monotonic=real_time.monotonic,sleep=real_time.sleep))
    def crash_before_observation(*args):raise KeyboardInterrupt()
    monkeypatch.setattr(runtime,'_observe',crash_before_observation)
    with pytest.raises(KeyboardInterrupt):runtime.run(spec,authorize=ALLOW)
    assert journal(runtime,spec)['deadline_at']==1001.0
    assert docker.objects[RID]['State']['Status']=='exited'
    wall[0]=500.0
    fresh=VerifierRuntime(runtime.base,journal_root=runtime.root)
    proof=fresh.reconcile(spec,authorize=lambda action:action in CLEANUP)
    assert proof['cleanup_confirmed'] and proof['aborted']=='completion_timing_unproven'
    assert not proof['result_available'] and not docker.objects
    with pytest.raises(RuntimeError,match='result_unavailable'):fresh.load(spec,authorize=ALLOW)
    with pytest.raises(RuntimeError,match='result_unavailable'):fresh.run(spec,authorize=ALLOW)
    assert sum(c[0]=='start' for c in docker.calls)==1


def test_persisted_live_observation_survives_recovery_clock_rollback(setup,monkeypatch):
    from types import SimpleNamespace
    import time as real_time
    runtime,spec,docker=setup;wall=[1000.0];save=runtime._save
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:wall[0],monotonic=real_time.monotonic,sleep=real_time.sleep))
    def crash_after_observation(spec,value):
        save(spec,value)
        if value['state']=='observed':raise KeyboardInterrupt()
    monkeypatch.setattr(runtime,'_save',crash_after_observation)
    with pytest.raises(KeyboardInterrupt):runtime.run(spec,authorize=ALLOW)
    assert journal(runtime,spec)['observation']['timed_out'] is False
    wall[0]=500.0
    fresh=VerifierRuntime(runtime.base,journal_root=runtime.root)
    assert fresh.reconcile(spec,authorize=ALLOW)['result_available']
    result=fresh.load(spec,authorize=ALLOW)
    assert result.exit_code==0 and not result.timed_out and not docker.objects


def test_active_monotonic_deadline_survives_wall_rollback(setup,monkeypatch):
    from types import SimpleNamespace
    runtime,spec,docker=setup;wall=[1000.0];monotonic=[0.0]
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:wall[0],monotonic=lambda:monotonic[0],sleep=lambda seconds:None))
    def invoke(base,args,**kwargs):
        value=docker(args).encode()
        if args[0]=='start':wall[0]=500.0;monotonic[0]=2.0
        return value
    monkeypatch.setattr(module,'_bounded_run',invoke)
    result=runtime.run(spec,authorize=ALLOW)
    assert result.exit_code==0 and result.timed_out and result.cleanup_confirmed

@pytest.mark.parametrize('owner,group,mode,allowed',[
    (1000,999,0o500,True),(1000,1000,0o050,False),
    (959,1000,0o550,True),(959,999,0o550,False),
    (959,999,0o555,True),(959,1000,0o540,False),
    (959,1000,0o510,False),(959,999,0o551,False),
])
def test_exact_posix_access_uses_owner_group_other_precedence(owner,group,mode,allowed):
    from types import SimpleNamespace
    info=SimpleNamespace(st_uid=owner,st_gid=group,st_mode=mode)
    assert module._posix_access(info,1000,1000) is allowed

@pytest.mark.parametrize('target',['candidate','task','script'])
def test_inaccessible_protected_material_refuses_before_create(setup,monkeypatch,target):
    runtime,spec,docker=setup
    original=module._posix_access
    # Local host UID/group may differ; simulate only the target's Linux DAC
    # metadata while retaining real paths, inode checks and controller reads.
    selected=spec.candidate_path if target=='candidate' else spec.task_path if target=='task' else spec.task_path/'check.py'
    identity=(selected.stat().st_dev,selected.stat().st_ino)
    from types import SimpleNamespace
    def access(info,uid,gid,required=0o5):
        if (info.st_dev,info.st_ino)==identity:
            info=SimpleNamespace(st_uid=959,st_gid=999,st_mode=0o550)
        return original(info,uid,gid,required)
    monkeypatch.setattr(module,'_posix_access',access)
    with pytest.raises(RuntimeError,match='access_unavailable'):runtime.run(spec,authorize=ALLOW)
    assert docker.calls==[] and not runtime._folder(spec).exists()


def test_access_drift_after_cancellation_does_not_block_cleanup(setup,monkeypatch):
    runtime,spec,docker=setup;docker.running=True;docker.lost_start=True;docker.stop_fails=True
    with pytest.raises(RuntimeError):runtime.run(spec,authorize=ALLOW)
    monkeypatch.setattr(module,'_posix_access',lambda *args:False)
    docker.stop_fails=False
    assert runtime.reconcile(spec,authorize=lambda action:action in CLEANUP)['cleanup_confirmed']
    assert not docker.objects


def test_secret_before_1001_lines_is_read_and_rejected(setup,monkeypatch,tmp_path):
    import sys
    runtime,spec,docker=setup
    path=tmp_path/'synthetic-docker'
    path.write_text('#!'+sys.executable+'\nimport sys\nassert sys.argv[1:]==["logs","'+RID+'"]\n'
        'print("SYNTHETIC_SECRET")\nfor i in range(1001):print("ordinary diagnostic")\n')
    path.chmod(0o700);runtime.base.docker=str(path)
    monkeypatch.setattr(module,'_log_bytes',REAL_LOG_BYTES)
    with pytest.raises(RuntimeError,match='logs_rejected') as error:
        runtime.run(spec,authorize=ALLOW,forbidden_values=(b'SYNTHETIC_SECRET',))
    assert error.value.cleanup_status=='confirmed' and not docker.objects
    assert not (runtime._folder(spec)/'logs.bin').exists()


def test_retained_log_overflow_cleans_without_persisted_result(setup,monkeypatch,tmp_path):
    import sys
    runtime,spec,docker=setup;spec=replace(spec,max_log_bytes=64)
    path=tmp_path/'synthetic-docker'
    path.write_text('#!'+sys.executable+'\nimport os,sys\nassert sys.argv[1:]==["logs","'+RID+'"]\nos.write(1,b"x"*65)\n')
    path.chmod(0o700);runtime.base.docker=str(path)
    monkeypatch.setattr(module,'_log_bytes',REAL_LOG_BYTES)
    with pytest.raises(RuntimeError,match='logs_bound') as error:runtime.run(spec,authorize=ALLOW)
    assert error.value.cleanup_status=='confirmed' and not docker.objects
    assert not (runtime._folder(spec)/'logs.bin').exists()
    with pytest.raises(RuntimeError):runtime.load(spec,authorize=ALLOW)
