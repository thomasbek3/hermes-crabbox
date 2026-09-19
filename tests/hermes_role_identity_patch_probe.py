"""Offline patched-source probe for D4; no broker, credentials, inference or shared-source edits."""
from pathlib import Path
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile

source=Path(sys.argv[1]).resolve()
repo=Path(__file__).resolve().parents[1]
patch_file=repo/'patches/hermes/native-role-call-identity.patch'
metadata=json.loads((repo/'patches/hermes/native-role-call-identity.json').read_text())

def apply_overlay(upstream, destination):
    raw=(upstream/'model_tools.py').read_bytes()
    if hashlib.sha256(raw).hexdigest()!='432ab8bcf79bbac321e76385aaa15ad6056999cbf372917f0dbf0ab13e2679dc':
        raise ValueError('upstream_hash_mismatch')
    if hashlib.sha256(patch_file.read_bytes()).hexdigest()!=metadata['patch_sha256']:
        raise ValueError('patch_hash_mismatch')
    for name,expected in metadata['dependencies_sha256'].items():
        if hashlib.sha256((upstream/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('dependency_hash_mismatch')
    destination.mkdir()
    (destination/'model_tools.py').write_bytes(raw)
    subprocess.run(['/usr/bin/patch','--batch','-F','0','-p1','-i',str(patch_file)],cwd=destination,check=True,capture_output=True)
    if hashlib.sha256((destination/'model_tools.py').read_bytes()).hexdigest()!=metadata['patched_sha256']:
        raise ValueError('patched_hash_mismatch')
if len(sys.argv)==2:
    with tempfile.TemporaryDirectory(prefix='cwb-role-patched-') as directory:
        child=subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),str(source),directory],
            env={'HOME':directory,'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,timeout=90)
        sys.stdout.buffer.write(child.stdout);sys.stderr.buffer.write(child.stderr)
        raise SystemExit(child.returncode)
base=Path(sys.argv[2]).resolve()
for name in ('home','profile','empty-bundled','work','codex','claude','config','cache','data'):(base/name).mkdir()
os.environ.clear()
os.environ.update({'HOME':str(base/'home'),'HERMES_HOME':str(base/'profile'),
    'HERMES_BUNDLED_PLUGINS':str(base/'empty-bundled'),'HERMES_ENABLE_PROJECT_PLUGINS':'0',
    'CODEX_HOME':str(base/'codex'),'CLAUDE_CONFIG_DIR':str(base/'claude'),
    'XDG_CONFIG_HOME':str(base/'config'),'XDG_CACHE_HOME':str(base/'cache'),'XDG_DATA_HOME':str(base/'data'),
    'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'})
os.chdir(base/'work');sys.dont_write_bytecode=True;sys.path.insert(0,str(source))
bad=base/'mismatched-source';bad.mkdir();(bad/'model_tools.py').write_text('unqualified source')
try:apply_overlay(bad,base/'must-not-exist')
except ValueError as error:assert str(error)=='upstream_hash_mismatch'
else:raise AssertionError('unqualified_source_accepted')
assert not (base/'must-not-exist').exists()
for index,name in enumerate(metadata['dependencies_sha256']):
    candidate=base/('drift-'+str(index));candidate.mkdir()
    for relative in metadata['dependencies_sha256']:
        path=candidate/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((source/relative).read_bytes())
    drift=candidate/name;drift.write_bytes(drift.read_bytes()+b'\n')
    target=base/('refused-'+str(index))
    try:apply_overlay(candidate,target)
    except ValueError:pass
    else:raise AssertionError('drifted_dependency_accepted')
    assert not target.exists()
overlay=base/'overlay';apply_overlay(source,overlay);sys.path.insert(0,str(overlay))
network_attempts=[];sensitive_reads=[]
def blocked(*args,**kwargs):
    network_attempts.append(True);raise RuntimeError('network_disabled_in_probe')
socket.socket.connect=blocked;socket.socket.connect_ex=blocked;socket.create_connection=blocked;socket.getaddrinfo=blocked

def audit(event,args):
    if event=='open' and isinstance(args[0],(str,bytes)):
        p=Path(os.fsdecode(args[0])).absolute()
        if p.name in {'.env','.op.env','auth.json','.credentials.json','config.yaml'} and not p.is_relative_to(base):
            sensitive_reads.append(p.name);raise PermissionError('external_sensitive_state_forbidden')
sys.addaudithook(audit)
(base/'profile/config.yaml').write_text(json.dumps({'plugins':{'enabled':['cwb-identity-probe']},'mcp_servers':{},'hooks':{},'memory':{'provider':''}}))
plugin=base/'profile/plugins/cwb-identity-probe';plugin.mkdir(parents=True)
(plugin/'plugin.yaml').write_text('name: cwb-identity-probe\nversion: 0.0.1\ndescription: Offline identity probe only\n')
plugin_code='''import hashlib,json
barrier=None
nested=None
invocations=[]

def routed(name,args, *, session_id,tool_call_id,**kwargs):
    invocations.append(name)
    if set(args)!={'role','task'}:return json.dumps({'error':'unexpected_model_identity_fields'})
    if nested is not None and session_id=='outer-native':nested()
    if barrier is not None:barrier.wait(timeout=15)
    key=hashlib.sha256(json.dumps(['controller-attempt-fixture',7,session_id,tool_call_id],separators=(',',':')).encode()).hexdigest()
    digest=hashlib.sha256(json.dumps({'tool':name,'arguments':args},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return json.dumps({'session_id':session_id,'tool_call_id':tool_call_id,'request_key':key,'payload_digest':digest,
       'dispatch_kwarg_names':sorted(['session_id','tool_call_id',*kwargs])},sort_keys=True)

def ordinary(args, **kwargs):return json.dumps({'dispatch_kwarg_names':sorted(kwargs)})

def register(ctx):
    for name in ('cloud_request_roles','cloud_get_role_results'):
        def handler(args,_name=name,**kwargs):return routed(_name,args,**kwargs)
        ctx.register_tool(name=name,toolset='cwb_identity_probe',schema={
          'name':name,'description':'Offline probe','parameters':{'type':'object','properties':{
           'role':{'type':'string'},'task':{'type':'string'}},'required':['role','task'],'additionalProperties':False}},handler=handler)
    ctx.register_tool(name='cwb_role_identity',toolset='cwb_identity_probe',schema={
        'name':'cwb_role_identity','description':'Ordinary ABI probe','parameters':{'type':'object'}},handler=ordinary)
'''
(plugin/'__init__.py').write_text(plugin_code)
from hermes_cli.plugins import get_plugin_manager
manager=get_plugin_manager();manager.discover_and_load()
assert [p['key'] for p in manager.list_plugins() if p['enabled']]==['cwb-identity-probe']
module=manager._plugins['cwb-identity-probe'].module
import model_tools
from tools import approval_context as ac
from tools.registry import registry
assert registry.get_entry('cwb_role_identity').handler is module.ordinary
assert Path(model_tools.__file__).resolve()==overlay/'model_tools.py'
from agent.tool_executor import _ToolCallRef,_resolve_sequential_dispatch
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading

def dispatch(session,call,args=None,name="cloud_request_roles"):
    args={'role':'feature','task':'synthetic'} if args is None else args
    agent=SimpleNamespace(session_id=session,_current_turn_id='turn-fixture',_current_api_request_id='api-fixture',
        valid_tool_names={'cloud_request_roles','cloud_get_role_results','cwb_role_identity'},enabled_toolsets=['cwb_identity_probe'],disabled_toolsets=[],
        _context_engine_tool_names=set(),_memory_manager=None,quiet_mode=False)
    ref=_ToolCallRef(name,args,'task-fixture',call,[])
    previous=(ac._approval_session_id.get(),ac._approval_tool_call_id.get())
    value=_resolve_sequential_dispatch(agent,ref,[]).execute(args)
    assert (ac._approval_session_id.get(),ac._approval_tool_call_id.get())==previous
    return json.loads(value) if isinstance(value,str) else value

first=dispatch('session-A','call-A');repeated=dispatch('session-A','call-A')
assert first==repeated and first['session_id']=='session-A' and first['tool_call_id']=='call-A'
assert 'tool_call_id' in first['dispatch_kwarg_names'] and 'session_id' in first['dispatch_kwarg_names']
assert dispatch('session-A','call-B')['request_key']!=first['request_key']
assert dispatch('session-B','call-A')['request_key']!=first['request_key']
changed=dispatch('session-A','call-A',{'role':'feature','task':'different'})
assert changed['request_key']==first['request_key'] and changed['payload_digest']!=first['payload_digest']
invalid_values=['',None,True,42,[],{},'contains space','line\nfeed','tab\there','null\x00byte','😀','x'*257]
for name in ('cloud_request_roles','cloud_get_role_results'):
    assert dispatch('session-A','call-A',name=name)['tool_call_id']=='call-A'
    for invalid in invalid_values:
        for session,call in [(invalid,'valid-call'),('valid-session',invalid)]:
            count=len(module.invocations)
            refused=dispatch(session,call,name=name)
            assert refused.get('error')=='native_role_call_identity_unavailable',refused
            assert len(module.invocations)==count # refusal happens before handler
    assert dispatch('s'*256,'c'*256,name=name)['tool_call_id']=='c'*256
bridge_results=[]
for name in ('cloud_request_roles','cloud_get_role_results'):
    args={'name':name,'arguments':{'role':'feature','task':'synthetic'}}
    value=model_tools.handle_function_call('tool_call',args,tool_call_id='bridge-native-call',session_id='bridge-native-session',
        enabled_toolsets=['cwb_identity_probe'],skip_pre_tool_call_hook=True,skip_tool_request_middleware=True,skip_tool_execution_middleware=True)
    value=json.loads(value) if isinstance(value,str) else value
    assert value.get('tool_call_id')=='bridge-native-call',value
    bridge_results.append(name)
from hermes_state_ids import new_session_id
from agent.message_sanitization import coalesce_tool_call_id
source_session=new_session_id()
source_call=coalesce_tool_call_id({'call_id':'provider-call|response-item','id':'ignored'})
assert dispatch(source_session,source_call)['tool_call_id']=='provider-call'
assert coalesce_tool_call_id({'id':'model-controlled-duplicate'})=='model-controlled-duplicate'
assert dispatch('', '',name='cwb_role_identity')['dispatch_kwarg_names']==['session_id','task_id','user_task']
assert dispatch('session-A','call-A',name='cwb_role_identity')['dispatch_kwarg_names']==['session_id','task_id','user_task']
assert dispatch('session-A','call-A',{'role':'feature','task':'synthetic','session_id':'forged','tool_call_id':'forged'})=={'error':'unexpected_model_identity_fields'}
assert ac._approval_session_id.get()=='' and ac._approval_tool_call_id.get()==''
module.barrier=threading.Barrier(8)
with ThreadPoolExecutor(max_workers=8) as executor:
    concurrent=list(executor.map(lambda n:dispatch('session-'+str(n),'call-'+str(n)),range(8)))
module.barrier=None
for n,value in enumerate(concurrent):assert (value['session_id'],value['tool_call_id'])==('session-'+str(n),'call-'+str(n))
assert ac._approval_session_id.get()=='' and ac._approval_tool_call_id.get()==''
nested_results=[]
module.nested=lambda:nested_results.append(dispatch('nested-session','nested-call'))
assert dispatch('outer-native','outer-call')['tool_call_id']=='outer-call'
assert nested_results[0]['tool_call_id']=='nested-call'
module.nested=None
outer=ac.set_current_observability_context(session_id='outer-session',tool_call_id='outer-call')
try:
    assert dispatch('inner-session','inner-call')['tool_call_id']=='inner-call'
    assert (ac._approval_session_id.get(),ac._approval_tool_call_id.get())==('outer-session','outer-call')
finally:ac.reset_current_observability_context(outer)
# Fault injection is in this disposable process only. It demonstrates an upstream fail-open limit.
outer=ac.set_current_observability_context(session_id='session-A',tool_call_id='stale-call')
setter=ac.set_current_observability_context
def failing_bind(**kwargs):raise RuntimeError('synthetic_binding_failure')
ac.set_current_observability_context=failing_bind
try:
    stale=dispatch('session-A','fresh-call')
    assert stale['tool_call_id']=='fresh-call' # Direct kwargs are independent of failed observability binding.
    for name in ('cloud_request_roles','cloud_get_role_results'):
        assert dispatch('session-A','fresh-call',name=name)['tool_call_id']=='fresh-call'
        count=len(module.invocations)
        assert dispatch('session-A','',name=name)['error']=='native_role_call_identity_unavailable'
        assert len(module.invocations)==count
finally:
    ac.set_current_observability_context=setter;ac.reset_current_observability_context(outer)
assert not sensitive_reads and not network_attempts
files=['model_tools.py','agent/tool_executor.py','tools/approval_context.py','tools/registry.py','hermes_cli/plugins.py']
receipt={'status':'PASS_isolated_native_identity_patch','hermes_commit':'3b0e392e5a6922034feccac5771041ac78467757',
    'upstream_source_sha256':{p:hashlib.sha256((source/p).read_bytes()).hexdigest() for p in files},
    'patch_sha256':metadata['patch_sha256'],'patched_sha256':metadata['patched_sha256'],
    'mismatched_upstream_refused_before_copy':True,'actual_import_from_patched_overlay':True,
    'ordinary_handler_kwargs_unchanged':True,'both_named_handlers_tested':True,
    'invalid_identity_cases':len(invalid_values)*4,'invalid_refused_before_handler':True,'boundary_256_accepted':True,
    'native_nested_dispatch_tested':True,
    'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'fixture_plugin_sha256':hashlib.sha256(plugin_code.encode()).hexdigest(),
    'actual_plugin_registered':True,'actual_agent_dispatch_closure':True,'bridge_named_handlers_delivered':bridge_results,
    'all_dependency_pins_enforced':True,'dependency_drift_refusal_cases':len(metadata['dependencies_sha256']),
    'actual_session_generator_and_pairing_helper_fixture':True,'provider_live_ids_qualified':False,'repeated_identity_stable':True,
    'different_call_changes_key':True,'changed_payload_preserves_key_changes_digest':True,
    'missing_ids_refused':True,'fixture_handler_refuses_model_identity_fields':True,'parallel_distinct_contexts':8,
    'nested_context_restored':True,'every_worker_context_restored':True,'different_session_changes_key':True,'outer_context_after_dispatch_empty':True,
    'injected_binding_failure_reused_stale_id':False,'private_contextvars_required_for_tool_call_id':False,
    'runtime_identity_not_authentication':True,'durable_broker_implemented':False,
    'provider_calls':0,'network_attempts':0,'external_sensitive_read_attempts':0}
import datetime,platform
receipt.update(at=datetime.datetime.now(datetime.timezone.utc).isoformat(),interpreter=platform.python_version(),
    clean_source_path=str(source),qualified_entrypoint=metadata['qualified_entrypoint'])
print(json.dumps(receipt,indent=2,sort_keys=True))
