import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'scripts/legacy/qualify-routed-worker-bootstrap-linux.py'


@pytest.fixture
def module():
    spec=importlib.util.spec_from_file_location('worker_bootstrap_fixture',SCRIPT)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_prepare_freezes_source_and_helper_without_running_children(module,tmp_path,monkeypatch):
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:pytest.fail('prepare must not execute'))
    path=tmp_path/'bundle.json';result=module.prepare(path);bundle=json.loads(path.read_text())
    assert result['prepared'] and module.validate_bundle(bundle)==bundle
    assert bundle['harness']==SCRIPT.read_text()
    assert {'worker_service.py','routed_bootstrap.py','workflow_revisions.py','models.py'}<=set(bundle['sources'])
    assert 'api.py' not in bundle['sources']
    with pytest.raises(FileExistsError):module.prepare(path)


def test_all_actual_bootstrap_payload_files_are_explicit_seeds_and_frozen(module,tmp_path):
    from cloudworkbench.routed_bootstrap import SOURCE_FILES
    assert SOURCE_FILES <= {name+'.py' for name in module.START_MODULES}
    path=tmp_path/'bundle.json';module.prepare(path);bundle=json.loads(path.read_text())
    assert SOURCE_FILES <= set(bundle['sources'])
    assert SOURCE_FILES <= set(bundle['hashes'])
    for name in SOURCE_FILES:
        assert bundle['sources'][name] == (ROOT/'src/cloudworkbench'/name).read_text()


@pytest.mark.parametrize('name',['routed_caller.py','adapters.py'])
def test_data_declared_payload_omission_is_refused(module,tmp_path,name):
    path=tmp_path/'bundle.json';module.prepare(path);bundle=json.loads(path.read_text())
    del bundle['sources'][name];del bundle['hashes'][name]
    with pytest.raises(ValueError,match='frozen_bundle_mismatch'):module.validate_bundle(bundle)


@pytest.mark.parametrize('change',['source','helper','run_id','missing','source_path'])
def test_bundle_tampering_or_missing_closure_refused(module,tmp_path,change):
    path=tmp_path/'bundle.json';module.prepare(path);bundle=json.loads(path.read_text())
    if change=='source':bundle['sources']['worker_service.py']+='\n# changed'
    elif change=='helper':bundle['harness']+='\n# changed'
    elif change=='run_id':bundle['run_id']='../outside'
    elif change=='missing':
        del bundle['sources']['models.py'];del bundle['hashes']['models.py']
    else:
        bundle['sources']['../outside.py']='pass';bundle['hashes']['../outside.py']=module.sha(b'pass')
    with pytest.raises(ValueError):module.validate_bundle(bundle)


def test_frozen_package_imports_without_checkout_fallback(module,tmp_path):
    sources=module.source_closure(ROOT);package=tmp_path/'cloudworkbench';package.mkdir()
    for name,body in sources.items():(package/name).write_text(body)
    code="""import sys,socket,subprocess,importlib
sys.path.insert(0,sys.argv[1])
def deny(*a,**k):raise AssertionError('unexpected network/process')
socket.create_connection=deny
subprocess.Popen=deny
for name in sys.argv[2:]:
 module=importlib.import_module('cloudworkbench.'+name)
 assert module.__file__.startswith(sys.argv[1]+'/cloudworkbench/')
print('isolated imports passed')
"""
    result=subprocess.run([sys.executable,'-I','-c',code,str(tmp_path),*module.START_MODULES],
        capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='isolated imports passed'


def test_root_execution_refuses_nonlinux_before_identity_or_mutation(module,tmp_path,monkeypatch):
    path=tmp_path/'bundle.json';module.prepare(path);bundle=json.loads(path.read_text())
    monkeypatch.setattr(module.sys,'platform','darwin')
    monkeypatch.setattr(module,'identities',lambda:pytest.fail('must refuse first'))
    with pytest.raises(ValueError,match='linux_root_required'):module.run(bundle,tmp_path/'receipt.json')


def test_identity_preflight_requires_exact_live_groups(module,monkeypatch):
    replies={'getent passwd 959':'worker:x:959:960:worker:/nonexistent:/usr/bin/nologin',
             'id -G worker':'960 959 966', 'getent group 959':'docker:x:959:worker',
             'getent group 960':'worker:x:960:', 'getent group 966':'project:x:966:worker'}
    monkeypatch.setattr(module,'command',lambda args:replies[' '.join(args)])
    assert module.identities()['groups']==[959,960,966]
    replies['id -G worker']='960 959 966 1000'
    with pytest.raises(ValueError,match='groups_mismatch'):module.identities()


def test_bounded_subprocess_timeout_kills_exact_process_group_and_reaps(module,monkeypatch):
    class Child:
        pid=43123
        def __init__(self):self.calls=0
        def communicate(self,**kwargs):
            self.calls+=1
            if self.calls==1:raise subprocess.TimeoutExpired('fixture',1)
            assert kwargs['timeout']==5
            return b'',b''
    child=Child();signals=[]
    monkeypatch.setattr(module.os,'killpg',lambda pid,sig:signals.append((pid,sig)))
    with pytest.raises(ValueError,match='bounded_child_timeout'):module.finish(child,timeout=1)
    assert signals==[(43123,module.signal.SIGKILL)] and child.calls==2


def test_frozen_probe_scripts_compile_and_contain_only_synthetic_wire(module):
    compile(module.RELAY_PROBE,'relay-probe','exec');compile(module.TOOL_PROBE,'tool-probe','exec')
    assert 'AF_UNIX' in module.RELAY_PROBE and 'AF_INET' not in module.RELAY_PROBE
    assert 'worker_config_denied' in module.TOOL_PROBE
    assert 'setgroups(' not in module.RELAY_PROBE+module.TOOL_PROBE


def test_service_preflight_requires_active_nonzero_exact_services(module,monkeypatch):
    calls=[]
    def reply(args):calls.append(args);return 'MainPID=123\nActiveState=active'
    monkeypatch.setattr(module,'command',reply)
    value=module.service_pids()
    assert set(value)==set(module.SERVICES) and len(calls)==3
    monkeypatch.setattr(module,'command',lambda _: 'MainPID=0\nActiveState=inactive')
    with pytest.raises(ValueError):module.service_pids()


@pytest.fixture
def receipt(module,tmp_path):
    path=tmp_path/'bundle.json';module.prepare(path);bundle=json.loads(path.read_text())
    identity={'uid':959,'primary_gid':960,'groups':[959,960,966],'name':'worker'}
    services={n:{'MainPID':100+i,'ActiveState':'active'} for i,n in enumerate(module.SERVICES)}
    parent=Path('/tmp')/('cwb2-workerqual-'+bundle['run_id'])/'b'/(module.sha(b'attempt')[:24]+'.1')/'stages/review'
    value={'status':'passed','hostname':'archived-worker.invalid','run_id':bundle['run_id'],'source_hashes':bundle['hashes'],
        'harness_sha256':bundle['harness_sha256'],'identity_before':identity,'identity_after':dict(identity),
        'service_pids_before':services,'service_pids_after':dict(services),'remote_inference':False,'docker_used':False,
        'scoped_fd_entry_not_actual_bind_mount':True,'identities_unchanged':True,'service_pids_unchanged':True,
        'children_reaped':True,'child_groups_absent':True,'fixture_absent':True,'cleanup':'exact_fixture_removed',
        'attempt_id':'attempt','profile_digest':'a'*64,'bootstrap_binding_digest':'b'*64,'bootstrap_receipt_sha256':'c'*64,
        'worker':{'ready':True,'worker_uid':959,'worker_gid':960,'worker_groups':[959,966],
                  'socket_gid':1001,'socket_mode':0o660,'source':str(parent/'source'),'scratch':str(parent/'scratch')},
        'relay':{'uid':1001,'gid':1001,'connected':True,'status':200},
        'tool':{'uid':1000,'gid':1000,'task_read':True,'worker_config_denied':True,'source_read':True,'source_write_denied':True,'scratch_write':True},
        'service':{'synthetic_calls':1,'worker_groups_after':[959,966],'closed':{'started':True,'stop_requested':True,
                   'thread_stopped':True,'listener_closed':True,'resources_closed':True,'active_callbacks':0,
                   'timed_out':False,'error':None,'service_id':'a'*32,
                   'binding':{'attempt_id':'attempt','generation':1,'profile_digest':'a'*64}}}}
    return value,bundle


def test_strict_success_receipt_accepts_bound_claims(module,receipt):
    value,bundle=receipt
    assert module.validate_receipt(value,bundle)==value


@pytest.mark.parametrize('field',['hostname','hash','groups','path','read_denial','numeric_bool','callback','cleanup','service_pid','attempt'])
def test_strict_receipt_rejects_changed_claim(module,receipt,field):
    value,bundle=receipt
    if field=='hostname':value['hostname']='other'
    elif field=='hash':value['harness_sha256']='0'*64
    elif field=='groups':value['worker']['worker_groups'].append(1000)
    elif field=='path':value['worker']['source']='/etc'
    elif field=='read_denial':value['tool']['worker_config_denied']=False
    elif field=='numeric_bool':value['relay']['connected']=1
    elif field=='callback':value['service']['closed']['active_callbacks']=1
    elif field=='cleanup':value['child_groups_absent']=False
    elif field=='service_pid':value['service_pids_after']={}
    else:value['service']['closed']['binding']['attempt_id']='other'
    with pytest.raises(ValueError):module.validate_receipt(value,bundle)


def test_cleanup_refuses_mounts_and_special_files(module,tmp_path,monkeypatch):
    import os
    root=tmp_path/'fixture';root.mkdir();identity=(root.stat().st_dev,root.stat().st_ino)
    original=Path.lstat
    def root_owned(path):
        info=original(path)
        if path==root:
            values=list(info);values[4]=0;return os.stat_result(values)
        return info
    monkeypatch.setattr(Path,'lstat',root_owned)
    baseline='1 0 0:1 / / rw - tmpfs tmpfs rw'
    assert module.cleanup_safe(root,identity,baseline)
    assert not module.cleanup_safe(root,identity,baseline+'\n2 1 0:2 / '+str(root/'nested')+' rw - tmpfs tmpfs rw')
    (root/'link').symlink_to('/etc')
    assert not module.cleanup_safe(root,identity,baseline)
    (root/'link').unlink();os.mkfifo(root/'fifo')
    assert not module.cleanup_safe(root,identity,baseline)
