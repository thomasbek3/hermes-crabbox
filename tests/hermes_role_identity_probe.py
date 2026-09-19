"""Offline source probe for D4; no broker, credentials, inference or shared-source edits."""
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
if len(sys.argv)==2:
    with tempfile.TemporaryDirectory(prefix='cwb-role-identity-') as directory:
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
plugin_code='''import hashlib,json,re
from tools.approval_context import _approval_session_id,_approval_tool_call_id
barrier=None

def _read_identity(args, **kwargs):
    if set(args)!={'role','task'}:return {'error':'unexpected_model_identity_fields'}
    session=_approval_session_id.get();call=_approval_tool_call_id.get()
    if not all(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',v) for v in (session,call)):
        return {'error':'native_identity_unavailable'}
    if kwargs.get('session_id')!=session:return {'error':'native_identity_mismatch'}
    if barrier is not None:barrier.wait(timeout=15)
    assert (_approval_session_id.get(),_approval_tool_call_id.get())==(session,call)
    key=hashlib.sha256(json.dumps(['controller-attempt-fixture',7,session,call],separators=(',',':')).encode()).hexdigest()
    digest=hashlib.sha256(json.dumps({'tool':'cwb_role_identity','arguments':args},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'session_id':session,'tool_call_id':call,'request_key':key,'payload_digest':digest,'dispatch_kwarg_names':sorted(kwargs)}

def handler(args, **kwargs):
    return json.dumps(_read_identity(args, **kwargs),sort_keys=True)

def register(ctx):
    ctx.register_tool(name='cwb_role_identity',toolset='cwb_identity_probe',schema={
      'name':'cwb_role_identity','description':'Offline probe','parameters':{'type':'object','properties':{
       'role':{'type':'string'},'task':{'type':'string'}},'required':['role','task'],'additionalProperties':False}},handler=handler)
'''
(plugin/'__init__.py').write_text(plugin_code)
from hermes_cli.plugins import get_plugin_manager
manager=get_plugin_manager();manager.discover_and_load()
assert [p['key'] for p in manager.list_plugins() if p['enabled']]==['cwb-identity-probe']
module=manager._plugins['cwb-identity-probe'].module
import model_tools
from tools import approval_context as ac
from tools.registry import registry
assert registry.get_entry('cwb_role_identity').handler is module.handler
from agent.tool_executor import _ToolCallRef,_resolve_sequential_dispatch
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading

def dispatch(session,call,args=None):
    args={'role':'feature','task':'synthetic'} if args is None else args
    agent=SimpleNamespace(session_id=session,_current_turn_id='turn-fixture',_current_api_request_id='api-fixture',
        valid_tool_names={'cwb_role_identity'},enabled_toolsets=['cwb_identity_probe'],disabled_toolsets=[],
        _context_engine_tool_names=set(),_memory_manager=None,quiet_mode=False)
    ref=_ToolCallRef('cwb_role_identity',args,'task-fixture',call,[])
    previous=(ac._approval_session_id.get(),ac._approval_tool_call_id.get())
    value=_resolve_sequential_dispatch(agent,ref,[]).execute(args)
    assert (ac._approval_session_id.get(),ac._approval_tool_call_id.get())==previous
    return json.loads(value) if isinstance(value,str) else value

first=dispatch('session-A','call-A');repeated=dispatch('session-A','call-A')
assert first==repeated and first['session_id']=='session-A' and first['tool_call_id']=='call-A'
assert 'tool_call_id' not in first['dispatch_kwarg_names'] and 'session_id' in first['dispatch_kwarg_names']
assert dispatch('session-A','call-B')['request_key']!=first['request_key']
assert dispatch('session-B','call-A')['request_key']!=first['request_key']
changed=dispatch('session-A','call-A',{'role':'feature','task':'different'})
assert changed['request_key']==first['request_key'] and changed['payload_digest']!=first['payload_digest']
for session,call in [('', 'call-A'),('session-A',''),('', '')]:
    assert dispatch(session,call)=={'error':'native_identity_unavailable'}
assert dispatch('session-A','call-A',{'role':'feature','task':'synthetic','session_id':'forged','tool_call_id':'forged'})=={'error':'unexpected_model_identity_fields'}
assert ac._approval_session_id.get()=='' and ac._approval_tool_call_id.get()==''
module.barrier=threading.Barrier(8)
with ThreadPoolExecutor(max_workers=8) as executor:
    concurrent=list(executor.map(lambda n:dispatch('session-'+str(n),'call-'+str(n)),range(8)))
module.barrier=None
for n,value in enumerate(concurrent):assert (value['session_id'],value['tool_call_id'])==('session-'+str(n),'call-'+str(n))
assert ac._approval_session_id.get()=='' and ac._approval_tool_call_id.get()==''
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
    assert stale['tool_call_id']=='stale-call' # Private observability alone cannot detect this fault.
finally:
    ac.set_current_observability_context=setter;ac.reset_current_observability_context(outer)
assert not sensitive_reads and not network_attempts
files=['model_tools.py','agent/tool_executor.py','tools/approval_context.py','tools/registry.py','hermes_cli/plugins.py']
receipt={'status':'PASS_normal_dispatch_WITH_known_binding_failure_gate','hermes_commit':'3b0e392e5a6922034feccac5771041ac78467757',
    'source_sha256':{p:hashlib.sha256((source/p).read_bytes()).hexdigest() for p in files},
    'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'fixture_plugin_sha256':hashlib.sha256(plugin_code.encode()).hexdigest(),
    'actual_plugin_registered':True,'actual_agent_dispatch_closure':True,'repeated_identity_stable':True,
    'different_call_changes_key':True,'changed_payload_preserves_key_changes_digest':True,
    'missing_ids_refused':True,'model_identity_fields_refused':True,'parallel_distinct_contexts':8,
    'nested_context_restored':True,'every_worker_context_restored':True,'different_session_changes_key':True,'outer_context_after_dispatch_empty':True,
    'injected_binding_failure_reused_stale_id':True,'private_contextvars_required_for_tool_call_id':True,
    'runtime_identity_not_authentication':True,'durable_broker_implemented':False,
    'provider_calls':0,'network_attempts':0,'external_sensitive_read_attempts':0}
print(json.dumps(receipt,indent=2,sort_keys=True))
