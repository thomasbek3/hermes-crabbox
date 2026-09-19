import copy
import importlib.util
import json
from pathlib import Path
import pytest

PATH=Path(__file__).resolve().parents[1]/'scripts/qualify-routed-caller-linux.py'
spec=importlib.util.spec_from_file_location('routed_qualification',PATH)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def bundle():
    sources={name:'# synthetic\n' for name in module.MODULES}
    return {'version':1,'run_id':'routed-'+'a'*12,'image':'sha256:'+'b'*64,'profiles':[list(module.PROFILES[0])],
            'launch_receipts':[module.expected_plan(module.PROFILES[0]).receipt_json],'sources':sources,'hashes':{n:module.sha(t.encode()) for n,t in sources.items()},'harness_sha256':module.sha(PATH.read_bytes())}


def receipt(value):
    launch=value['launch_receipts'][0]
    binding={'attempt_id':value['run_id']+'-0','generation':1,'profile_digest':json.loads(launch)['profile_digest']}
    case={'profile':value['profiles'][0],'passed':True,'runtime_id':'c'*64,'create_state':'created','durable_binding_before_start':True,
          'launch_receipt_json':launch,'caller_state_before_cleanup':'running','result':{'status':'completed','verification_pass':False,'outer_cleanup_required':True,'launch_receipt_sha256':module.sha(launch.encode())},
          'requests':[{'nonce':n*64,'tool_result_seen':seen,'model':value['profiles'][0][1],'effort':value['profiles'][0][2],'binding':binding,'payload_sha256':'e'*64} for n,seen in [('a',False),('b',True)]],
          'worker_states':['cached','cached'],'removed':True,'binding':binding,'relay_ready':{'uid':1001,'binding':binding},
          'process':{'uid':1000,'gid':1000,'no_new_privs':'1','cap_eff':'0','cpu_max':'140000 100000','memory_max':str(3008*1024**2),'pids_max':'352'},
          'denials':{'workspace_write':30,'worker_config_read':13,'worker_socket_connect':13,'relay_state_read':13,'init_signal':1,'source_write':30,'docker_socket':2},
          'events':[{'type':'tool.started','payload':{'name':'read_file'}},{'type':'adapter.result','payload':{'summary':module.FINAL}}]}
    case['stop_receipt']={'caller_stopped':True,'caller_removed':False,'runtime_id':case['runtime_id'],
        'attempt_id':binding['attempt_id'],'generation':1,'provider_cleanup_qualified':False}
    case['remove_receipt']={**case['stop_receipt'],'caller_removed':True}
    return {'passed':True,'run_id':value['run_id'],'image':value['image'],'host':'omarchy','source_hashes':value['hashes'],
            'harness_sha256':value['harness_sha256'],'real_provider_calls':False,'real_credentials_used':False,
            'services_before':{name:{'ActiveState':'active','MainPID':'123'} for name in module.SERVICES},'services_after':{name:{'ActiveState':'active','MainPID':'123'} for name in module.SERVICES},'started_at':'2026-09-17T00:00:00+00:00','finished_at':'2026-09-17T00:01:00+00:00','headroom_before':{'memory_available_kib':8*1024**2,'docker_available_bytes':8*1024**3},'headroom_after':{'memory_available_kib':8*1024**2,'docker_available_bytes':8*1024**3},'remaining':[],'cleanup_complete':True,'cases':[case]}


def test_bundle_archive_binding_and_no_implicit_remote(monkeypatch,tmp_path):
    value=bundle();module.validate_bundle(value,PATH.read_bytes())
    value['sources']['runtime.py']='changed'
    with pytest.raises(ValueError):module.validate_bundle(value,PATH.read_bytes())
    monkeypatch.setattr(module,'remote',lambda *args:pytest.fail('Preparation must not execute'))
    target=tmp_path/'prepared'
    module.prepare(target,'sha256:'+'b'*64,True)
    prepared=json.loads((target/'bundle.json').read_text())
    module.validate_bundle(prepared,(target/'harness.py').read_bytes())
    assert len(prepared['profiles'])==3
    with pytest.raises(FileExistsError):module.prepare(target,'sha256:'+'b'*64,True)


@pytest.mark.parametrize('failure',['remaining','service_pid','image','source','missing_tool','missing_final','wrong_uid','writable_workspace','third_request','unbound_relay','not_removed','no_binding','false_verification','request_model','request_effort','request_binding','launch_hash','launch_plan','missing_tool_result','cleanup_receipt'])
def test_validator_refuses_incomplete_proof(failure):
    value=bundle();data=receipt(value);module.validate_receipt(data,value);case=data['cases'][0]
    if failure=='remaining':data['remaining']=['c'*64]
    elif failure=='service_pid':data['services_after']={'changed':'PID'}
    elif failure=='image':data['image']='sha256:'+'d'*64
    elif failure=='source':data['source_hashes']={}
    elif failure=='missing_tool':case['events']=case['events'][1:]
    elif failure=='missing_final':case['events']=case['events'][:1]
    elif failure=='wrong_uid':case['process']['uid']=0
    elif failure=='writable_workspace':case['denials']['workspace_write']=0
    elif failure=='third_request':case['requests'].append({'nonce':'e'*64,'tool_result_seen':True})
    elif failure=='unbound_relay':case['relay_ready']['binding']={}
    elif failure=='not_removed':case['removed']=False
    elif failure=='no_binding':case['durable_binding_before_start']=False
    elif failure=='cleanup_receipt':case['remove_receipt']['caller_removed']=False
    elif failure=='request_model':case['requests'][0]['model']='wrong'
    elif failure=='request_effort':case['requests'][1]['effort']='low'
    elif failure=='request_binding':case['requests'][0]['binding']={}
    elif failure=='launch_hash':case['result']['launch_receipt_sha256']='f'*64
    elif failure=='launch_plan':case['launch_receipt_json']='{}'
    elif failure=='missing_tool_result':case['requests'][1]['tool_result_seen']=False
    else:case['result']['verification_pass']=True
    with pytest.raises(ValueError):module.validate_receipt(data,value)


def test_synthetic_sse_is_native_tool_then_final():
    from cloudworkbench.native_responses import NativeProfile, ResponsesDecoder
    schema={'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}
    for values in module.PROFILES:
        profile=NativeProfile(*values)
        for ordinal in (1,2):
            raw=module.synthetic_sse(values,ordinal)
            decoder=ResponsesDecoder(profile,{'read_file':schema},set())
            for index in range(0,len(raw),17):decoder.feed(raw[index:index+17])
            decoder.finish()
            assert b'response.completed' in raw
            assert (b'call_routed_fixture' in raw)==(ordinal==1)
            assert (module.FINAL.encode() in raw)==(ordinal==2)


def test_partial_json_publication_waits_boundedly():
    ticks=[0.0]; values=iter([b'',b'{',b'{"done":true}'])
    def sleep(delta):ticks[0]+=delta
    assert module.wait_json(lambda:{'present':True,'data':next(values)},1,clock=lambda:ticks[0],sleep=sleep)=={'done':True}
    with pytest.raises(RuntimeError,match='before_deadline'):
        module.wait_json(lambda:{'present':True,'data':b'{'},1,clock=lambda:ticks[0],sleep=sleep)


def test_diagnostics_do_not_copy_arbitrary_error_or_env():
    secret='SYNTHETIC_MUST_NOT_APPEAR'
    assert secret not in json.dumps(module.safe_diagnostic(RuntimeError(secret)))
    assert module.safe_diagnostic(RuntimeError('routed container policy mismatch'))['code']=='routed container policy mismatch'
    safe=module.safe_inspection({'Config':{'Env':[secret],'Cmd':[secret],'Labels':{'unknown':secret}},'State':{'Error':secret}})
    assert secret not in json.dumps(safe)
