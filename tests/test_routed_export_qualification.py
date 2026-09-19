import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/qualify-routed-export-linux.py'


@pytest.fixture
def module():
    spec=importlib.util.spec_from_file_location('export_qualification',SCRIPT)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.fixture
def bundle(module,tmp_path):
    target=tmp_path/'bundle.json';module.prepare(target)
    return json.loads(target.read_text())


def test_prepare_only_freezes_complete_helper_payload_and_ast_closure(module,tmp_path,monkeypatch):
    from cloudworkbench.routed_bootstrap import SOURCE_FILES
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:pytest.fail('prepare cannot execute'))
    path=tmp_path/'bundle.json';result=module.prepare(path);value=json.loads(path.read_text())
    assert result['prepared'] is True
    assert SOURCE_FILES|{'routed_export.py','routed_export_protocol.py','runtime.py'}<=set(value['sources'])
    assert value['harness']==SCRIPT.read_text() and value['image']==module.IMAGE
    assert module.validate_bundle(value)
    with pytest.raises(FileExistsError):module.prepare(path)


@pytest.mark.parametrize('key',['support','harness','image','run_id','closure','payload'])
def test_tampered_bundle_refused(module,bundle,key):
    if key in ('support','harness'):bundle[key]+='\n# drift'
    elif key=='image':bundle[key]='sha256:'+'0'*64
    elif key=='run_id':bundle[key]='../../anything'
    else:
        name='routed_export_protocol.py' if key=='closure' else 'adapters.py'
        del bundle['sources'][name];del bundle['hashes'][name]
    with pytest.raises(ValueError):module.validate_bundle(bundle)


def test_isolated_frozen_imports_and_real_launch_plan(module,bundle,tmp_path):
    package=tmp_path/'cloudworkbench';package.mkdir()
    for name,body in bundle['sources'].items():(package/name).write_text(body)
    helper=tmp_path/'helper.py';helper.write_text(bundle['harness'])
    code="""import sys,socket,subprocess,importlib.util
sys.path.insert(0,sys.argv[1])
def deny(*a,**k):raise AssertionError('unexpected network or process')
socket.create_connection=deny;subprocess.Popen=deny
spec=importlib.util.spec_from_file_location('fixture',sys.argv[2]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
for name in m.SEEDS:
 module=__import__('cloudworkbench.'+name,fromlist=['_'])
 assert module.__file__.startswith(sys.argv[1]+'/cloudworkbench/')
launch,profile=m.plan()
assert launch.workspace_readonly is False and profile.provider=='openai-codex'
compile(m.TOOL_PROGRAM,'tool-program','exec')
print('isolated frozen imports and plan passed')
"""
    result=subprocess.run([sys.executable,'-I','-c',code,str(tmp_path),str(helper)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='isolated frozen imports and plan passed'


def test_no_remote_execution_and_host_preflight_before_mutation(module,bundle,tmp_path,monkeypatch):
    monkeypatch.setattr(module.sys,'platform','darwin')
    monkeypatch.setattr(module,'headroom',lambda:pytest.fail('host check first'))
    with pytest.raises(ValueError,match='linux_omarchy_root_required'):
        module.root_run(bundle,tmp_path/'receipt.json')
    assert 'ssh' not in SCRIPT.read_text().lower()


@pytest.fixture
def receipt(module,bundle):
    run=bundle['run_id'];root=Path('/tmp')/('cwb2-exportqual-'+run)
    attempt='export-'+run;attempt_root=root/'b'/(module.sha(attempt.encode())[:24]+'.1')
    spec={'attempt_id':attempt,'session_id':'session-'+run,'generation':1,'owner':'exportqual-'+run,
          'image':module.IMAGE,'workspace':str(root/'state/workspace'),'scratch':str(root/'state/scratch'),
          'task_dir':str(attempt_root/'task'),'worker_socket_dir':str(attempt_root/'worker'),
          'workspace_readonly':False,'limits':[.5,256,64],'task_files':[]}
    digest=module.sha(json.dumps(spec,sort_keys=True,separators=(',',':')).encode())
    caller='a'*64;collector='b'*64;profile='c'*64
    stop={'runtime_id':caller,'attempt_id':attempt,'generation':1,'spec_digest':digest,'caller_stopped':True,
          'caller_removed':False,'authority':'controller_observed_caller_only','provider_cleanup_qualified':False}
    removed={**stop,'caller_removed':True}
    closed={'started':True,'stop_requested':True,'resources_closed':True,'thread_stopped':True,'listener_closed':True,
            'active_callbacks':0,'timed_out':False,'error':None,'service_id':'d'*32,
            'binding':{'attempt_id':attempt,'generation':1,'profile_digest':profile}}
    report={'runtime_id':caller,'collector_id':collector,'workspace_identity':[41,42],'spec_digest':digest,
            'selected_paths':list(module.SELECTED),'stream_sha256':'e'*64,'receipt_sha256':'f'*64,
            'caller_cleanup_sha256':module.sha(json.dumps(removed,sort_keys=True,separators=(',',':')).encode())}
    observations=[{'id':collector,'image':module.IMAGE,'user':'1000:1000','network':'none','readonly_root':True,
                   'mounts':[{'Type':'bind','RW':False,'Source':spec['workspace'],'Destination':'/workspace'}],
                   'state':state} for state in ('created','exited')]
    proof={'passed':True,'worker_uid':959,'worker_gid':960,'worker_groups':[959,966],
           'tool':{'uid':1000,'gid':1000,'answer_mode':0o600,'answer_uid':1000},'host_worker_read_denied':True,
           'export_current_validation':True,'no_inference_requests':True,'workspace_identity_before_start':[41,42],
           'caller_id':caller,'stop_receipt':stop,'remove_receipt':removed,'export':report,
           'tree':{'files':module.expected_files(),'directories':['nested'],'sha256':'e'*64},
           'service_closed':closed,'collector_observations':observations,
           'durable_reload':{'fresh_runtime_view':True,'equal_complete_export':True,'current_validation':True,
               'receipt_sha256':report['receipt_sha256'],'stream_sha256':report['stream_sha256'],
               'tree_sha256':'e'*64,'collector_id':collector,'create_calls':0,'start_calls':0}}
    identity={'uid':959,'primary_gid':960,'groups':[959,960,966]}
    services={name:{'ActiveState':'active','MainPID':100+i} for i,name in enumerate(module.validate_bundle(bundle).SERVICES)}
    return {'passed':True,'host':'omarchy','run_id':run,'image':module.IMAGE,'image_after':module.IMAGE,
            'source_hashes':bundle['hashes'],'harness_sha256':bundle['harness_sha256'],'support_sha256':bundle['support_sha256'],
            'identity_before':identity,'identity_after':copy.deepcopy(identity),'services_before':services,'services_after':copy.deepcopy(services),
            'remaining':[],'fixture_absent':True,'children_reaped':True,'child_groups_absent':True,
            'real_provider_calls':False,'real_credentials_used':False,
            'ready':{'ready':True,'spec':spec,'spec_digest':digest,'workspace_identity':[41,42],'profile_digest':profile},'proof':proof}


def test_strict_valid_receipt_accepted(module,bundle,receipt):
    assert module.validate_receipt(receipt,bundle)==receipt


@pytest.mark.parametrize('mutation',[
    ('child_groups_absent',False),('image_after','other'),('remaining',['a'*64]),('host','other'),
    ('services_after.cloud-workbench-api.service.MainPID',0),('proof.worker_groups',[959,966,1000]),
    ('proof.host_worker_read_denied',False),('proof.export_current_validation',False),
    ('proof.workspace_identity_before_start',[41,43]),('proof.remove_receipt.caller_removed',False),
    ('proof.remove_receipt.provider_cleanup_qualified',True),('proof.no_inference_requests',False),
    ('proof.service_closed.active_callbacks',False),('proof.service_closed.listener_closed',False),
    ('proof.service_closed.binding.profile_digest','b'*64),('ready.spec_digest','f'*64),
    ('proof.durable_reload.fresh_runtime_view',False),('proof.durable_reload.equal_complete_export',False),
    ('proof.durable_reload.create_calls',1),('proof.durable_reload.start_calls',False),
    ('proof.durable_reload.receipt_sha256','0'*64),('proof.durable_reload.tree_sha256','0'*64),
    ('proof.export.collector_id','a'*64),('proof.tree.files',[]),('proof.tree.directories',[]),
])
def test_strict_receipt_rejects_missing_or_contradictory_evidence(module,bundle,receipt,mutation):
    path,value=mutation
    # Service names include dots, unlike the deliberately dotted test selectors.
    if path.startswith('services_after.'):
        receipt['services_after']['cloud-workbench-api.service']['MainPID']=value
    else:
        cursor=receipt;parts=path.split('.')
        for part in parts[:-1]:cursor=cursor[part]
        cursor[parts[-1]]=value
    with pytest.raises(ValueError,match='export_qualification_receipt_invalid'):module.validate_receipt(receipt,bundle)


@pytest.mark.parametrize('change',['rw','extra_mount','network','uid','state'])
def test_actual_collector_policy_receipt_is_required(module,bundle,receipt,change):
    observed=receipt['proof']['collector_observations'][1]
    if change=='rw':observed['mounts'][0]['RW']=True
    elif change=='extra_mount':observed['mounts'].append({'Type':'bind','Source':'/var/run/docker.sock'})
    elif change=='network':observed['network']='bridge'
    elif change=='uid':observed['user']='0:0'
    else:observed['state']='running'
    with pytest.raises(ValueError):module.validate_receipt(receipt,bundle)


@pytest.mark.parametrize('failure',['stop','reconcile','service',None])
def test_cleanup_keeps_service_stop_even_when_docker_cleanup_refuses(module,tmp_path,failure):
    from types import SimpleNamespace
    calls=[];original=object();(tmp_path/'workspace-export.json').write_text('{}')
    def operation(name):
        calls.append(name)
        if name==failure:raise ValueError(name)
    routed=SimpleNamespace(stop_caller=lambda *args:operation('stop'),remove_caller=lambda *args:operation('remove'),
                           _folder=lambda _:tmp_path)
    export=SimpleNamespace(_bounded_run=object(),reconcile_workspace_export=lambda *args,**kw:operation('reconcile'))
    service=SimpleNamespace(close=lambda timeout:operation('service'))
    if failure:
        with pytest.raises(ValueError,match=failure):module.cleanup_worker(routed,object(),'a'*64,None,export,service,original)
    else:module.cleanup_worker(routed,object(),'a'*64,None,export,service,original)
    assert calls[-1]=='service' and 'reconcile' in calls and export._bounded_run is original


def test_safe_failure_keeps_only_fixed_source_error_and_static_location(module):
    from cloudworkbench.routed_export import WorkspaceExportError
    known=module.safe_failure(WorkspaceExportError('export_spool_digest_mismatch'),'reload')
    assert known['code']=='export_spool_digest_mismatch' and known['phase']=='reload'
    secret='synthetic-do-not-log-credential-value'
    detail=module.safe_failure(ValueError(secret),'tool_exec')
    assert detail['code']=='detail_redacted' and secret not in json.dumps(detail)
    assert module.checked_failure(detail)==detail


def test_safe_failure_keeps_docker_category_without_argv_or_stderr(module):
    from cloudworkbench.runtime import RuntimeError
    failure=module.safe_failure(RuntimeError('sensitive argv body',code='docker_mount_invalid',operation='create'),'create_caller')
    assert failure['code']=='docker_mount_invalid' and 'sensitive' not in json.dumps(failure)


def test_real_local_failed_child_diagnostic_survives_finish(module):
    detail={'phase':'export','type':'WorkspaceExportError','code':'export_spool_digest_mismatch','frames':[]}
    cleanup={'phase':'cleanup','type':'WorkspaceExportError','code':'export_docker_timeout','frames':[]}
    wire=json.dumps({'worker_failure':detail,'cleanup_failure':cleanup})
    child=subprocess.Popen([sys.executable,'-I','-c','import sys;sys.stderr.write(sys.argv[1]);sys.exit(1)',wire],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    with pytest.raises(module.WorkerFailed) as raised:module.finish_worker(child,timeout=5)
    assert raised.value.detail==detail and raised.value.cleanup==cleanup and child.returncode==1


@pytest.mark.parametrize('wire',['secret arbitrary traceback',json.dumps({'worker_failure':{'phase':'export','type':'ValueError','code':'sensitive-value','frames':[]},'cleanup_failure':None})])
def test_failed_child_raw_stderr_is_never_exposed(module,wire):
    from types import SimpleNamespace
    child=SimpleNamespace(communicate=lambda **_: (b'',wire.encode()),returncode=1)
    with pytest.raises(module.WorkerFailed) as raised:module.finish_worker(child)
    assert raised.value.detail['code']=='worker_failed' and wire not in str(raised.value)


def test_finish_worker_reads_success_after_actual_ready_line_without_loss(module):
    support=module.support_module({'support':(ROOT/'scripts'/module.SUPPORT).read_text()})
    code="import json; print(json.dumps({'ready':True}),flush=True);print(json.dumps({'passed':True,'fixture':'safe'}),flush=True)"
    child=subprocess.Popen([sys.executable,'-I','-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    assert support.ready_line(child)=={'ready':True}
    assert module.finish_worker(child,timeout=5)=={'passed':True,'fixture':'safe'}


def test_finish_worker_timeout_reaps_exact_group_and_records_fixed_reason(module,monkeypatch):
    class Child:
        pid=43212
        def __init__(self):self.calls=0
        def communicate(self,**kwargs):
            self.calls+=1
            if self.calls==1:raise subprocess.TimeoutExpired('secret-argv',1)
            assert kwargs['timeout']==5
            return b'',b'sensitive-body'
    child=Child();signals=[];monkeypatch.setattr(module.os,'killpg',lambda p,s:signals.append((p,s)))
    with pytest.raises(module.WorkerFailed) as raised:module.finish_worker(child,timeout=1)
    assert raised.value.detail['code']=='worker_timeout' and signals==[(child.pid,module.signal.SIGKILL)] and child.calls==2


def test_docker_worker_identity_and_resource_bounds_are_explicit(module,monkeypatch):
    calls=[]
    monkeypatch.setattr(module.os,'setgroups',lambda value:calls.append(('groups',value)))
    monkeypatch.setattr(module.os,'setgid',lambda value:calls.append(('gid',value)))
    monkeypatch.setattr(module.os,'setuid',lambda value:calls.append(('uid',value)))
    monkeypatch.setattr(module.resource,'setrlimit',lambda kind,value:calls.append(('limit',kind,value)))
    module.worker_child_setup()
    assert calls==[('groups',(959,966)),('gid',960),('uid',959),
        ('limit',module.resource.RLIMIT_CORE,(0,0)),('limit',module.resource.RLIMIT_CPU,(45,45)),
        ('limit',module.resource.RLIMIT_AS,(4*1024**3,4*1024**3)),
        ('limit',module.resource.RLIMIT_NOFILE,(128,128))]


def test_docker_worker_spawn_preserves_clean_environment_and_cleanup_tracking(module,monkeypatch):
    from types import SimpleNamespace
    captured={};process=object();support=SimpleNamespace(CHILDREN=[])
    def start(args,**kwargs):captured.update({'args':args,**kwargs});return process
    monkeypatch.setattr(module.subprocess,'Popen',start)
    args=['/usr/bin/python','-I','/tmp/exact/helper.py','--worker','/tmp/exact']
    assert module.spawn_worker(support,args) is process and support.CHILDREN==[process]
    assert captured=={'args':args,'stdin':subprocess.PIPE,'stdout':subprocess.PIPE,'stderr':subprocess.PIPE,
        'env':{'PATH':'/usr/bin:/bin','LANG':'C','HOME':'/nonexistent','PYTHONDONTWRITEBYTECODE':'1'},
        'preexec_fn':module.worker_child_setup,'start_new_session':True}
