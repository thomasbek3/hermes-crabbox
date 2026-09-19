"""Offline pinned Hermes native-dispatch -> role UDS -> durable broker proof."""
from pathlib import Path
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile

repo=Path(__file__).resolve().parents[1]
source=Path(sys.argv[1]).resolve()
if len(sys.argv)==2 or (len(sys.argv)==3 and sys.argv[2]=='bad-config'):
    with tempfile.TemporaryDirectory(prefix='cwb-rb-',dir='/tmp') as directory:
        child=subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),str(source),directory]+(['bad-config'] if len(sys.argv)==3 else []),
            env={'HOME':directory,'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,timeout=90)
        sys.stdout.buffer.write(child.stdout);sys.stderr.buffer.write(child.stderr)
        raise SystemExit(child.returncode)
base=Path(sys.argv[2]).resolve()
bad_config=len(sys.argv)>3 and sys.argv[3]=='bad-config'
for name in ('home','profile','empty-bundled','work','codex','claude','config','cache','data','overlay'):(base/name).mkdir()
os.environ.clear()
os.environ.update({'HOME':str(base/'home'),'HERMES_HOME':str(base/'profile'),
    'HERMES_BUNDLED_PLUGINS':str(base/'empty-bundled'),'HERMES_ENABLE_PROJECT_PLUGINS':'0',
    'CODEX_HOME':str(base/'codex'),'CLAUDE_CONFIG_DIR':str(base/'claude'),
    'XDG_CONFIG_HOME':str(base/'config'),'XDG_CACHE_HOME':str(base/'cache'),'XDG_DATA_HOME':str(base/'data'),
    'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'})
os.chdir(base/'work');sys.dont_write_bytecode=True
metadata=json.loads((repo/'patches/hermes/native-role-call-identity.json').read_text())
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
for path,expected in metadata['dependencies_sha256'].items():assert sha(source/path)==expected
additional_pins={'agent/agent_init.py':'76a8a6b206ca67e3dc96f666557811813f1a4ccfc3162ebb2a4fb19a7cb7f222',
    'run_agent.py':'3e87bfc369d3328c4182f63af4cf27850090579740b553603786154e0e59079a'}
for path,expected in additional_pins.items():assert sha(source/path)==expected
patch=repo/'patches/hermes/native-role-call-identity.patch';assert sha(patch)==metadata['patch_sha256']
overlay=base/'overlay';(overlay/'model_tools.py').write_bytes((source/'model_tools.py').read_bytes())
subprocess.run(['/usr/bin/patch','--batch','-F','0','-p1','-i',str(patch)],cwd=overlay,check=True,capture_output=True)
assert sha(overlay/'model_tools.py')==metadata['patched_sha256']
sys.path[:0]=[str(overlay),str(source),str(repo/'src')]
network_attempts=[];sensitive_reads=[]
original_connect=socket.socket.connect

def connect(sock,address):
    if sock.family != socket.AF_UNIX:
        network_attempts.append([frame.name for frame in __import__('traceback').extract_stack(limit=35)]);raise RuntimeError('network_disabled')
    return original_connect(sock,address)

def forbidden(*a,**k):
    network_attempts.append([frame.name for frame in __import__('traceback').extract_stack(limit=35)]);raise RuntimeError('network_disabled')
socket.socket.connect=connect;socket.socket.connect_ex=forbidden;socket.create_connection=forbidden;socket.getaddrinfo=forbidden

def audit(event,args):
    if event=='open' and isinstance(args[0],(str,bytes)):
        p=Path(os.fsdecode(args[0])).absolute()
        if p.name in {'.env','.op.env','auth.json','.credentials.json','config.yaml'} and not p.is_relative_to(base):
            sensitive_reads.append(p.name);raise PermissionError('external_sensitive_state_forbidden')
sys.addaudithook(audit)
from cloudworkbench.role_broker import RoleBroker,ParentScope
from cloudworkbench.role_broker_transport import RoleClient,RoleSocketServer,_write
import threading,time
clock=[1000]
scope=ParentScope('owner','project','root','parent',1,'native-session')

def authorize(db,scope):
    row=db.execute('SELECT * FROM fixture_parent WHERE id=?',(scope.parent_attempt_id,)).fetchone()
    return bool(row and row['active']==1 and row['generation']==scope.generation and row['scope']==json.dumps(scope.__dict__,sort_keys=True))
broker=RoleBroker(base/'controller.db',authorize_parent=authorize,clock=lambda:clock[0])
with broker.transaction() as db:
    db.execute('CREATE TABLE fixture_parent(id TEXT PRIMARY KEY,active INT,generation INT,scope TEXT)')
    db.execute('INSERT INTO fixture_parent VALUES(?,1,1,?)',(scope.parent_attempt_id,json.dumps(scope.__dict__,sort_keys=True)))
grant,token=broker.issue(scope,roles=('feature',),expires_at=2000)
server=RoleSocketServer(base/'socket',broker,scope,hashlib.sha256(token.encode()).hexdigest(),burst=100,requests_per_second=100)
thread=threading.Thread(target=server.serve);thread.start()
configuration=base/'client.json';configuration.write_text(json.dumps({'capability':token,'native_session_id':scope.native_session_id}));configuration.chmod(0o600 if bad_config else 0o400)
# Only mount-path substitution; actual parser, plugin bytes, loader and socket remain real.
original_from_file=RoleClient.from_file
RoleClient.from_file=classmethod(lambda cls:original_from_file(configuration,socket_path=str(base/'socket'),expected_owner_uid=os.getuid()))
plugin=base/'profile/plugins/cloud-roles';plugin.mkdir(parents=True)
for name in ('__init__.py','plugin.yaml'):(plugin/name).write_bytes((repo/'plugins/cloud_roles'/name).read_bytes())
(base/'profile/config.yaml').write_text(json.dumps({'plugins':{'enabled':['cloud-roles']},'mcp_servers':{},'hooks':{},'memory':{'provider':''},'model':{'default':'fixture-offline','provider':'custom','base_url':'http://127.0.0.1:9/v1','context_length':65536,'ollama_num_ctx':0},'compression':{'enabled':False}}))
from hermes_cli.plugins import get_plugin_manager
manager=get_plugin_manager();manager.discover_and_load()
if bad_config:
    from tools.registry import registry
    assert registry.get_entry('cloud_request_roles') is None and registry.get_entry('cloud_get_role_results') is None
    server.close();thread.join(5);assert not thread.is_alive()
    assert not network_attempts and not sensitive_reads
    print(json.dumps({'kind':'offline_plugin_bad_config','no_role_tools_registered':True,'tcp_network_attempts':0,'external_sensitive_reads':0,
        'probe_sha256':sha(Path(__file__)),'plugin_sha256':sha(repo/'plugins/cloud_roles/__init__.py'),'transport_sha256':sha(repo/'src/cloudworkbench/role_broker_transport.py')}))
    raise SystemExit(0)
assert [p['key'] for p in manager.list_plugins() if p['enabled']]==['cloud-roles']
from agent.tool_executor import _ToolCallRef,_resolve_sequential_dispatch
from tools import approval_context as ac
from types import SimpleNamespace
import model_tools
assert Path(model_tools.__file__).resolve()==overlay/'model_tools.py'

def dispatch(call='native-call',args=None,name='cloud_request_roles',session='native-session'):
    args={'role':'feature','task':'Synthetic task'} if args is None else args
    agent=SimpleNamespace(session_id=session,_current_turn_id='turn',_current_api_request_id='api',
        valid_tool_names={'cloud_request_roles','cloud_get_role_results'},enabled_toolsets=['cloud_roles'],disabled_toolsets=[],
        _context_engine_tool_names=set(),_memory_manager=None,quiet_mode=False)
    ref=_ToolCallRef(name,args,'task',call,[])
    value=_resolve_sequential_dispatch(agent,ref,[]).execute(args)
    return json.loads(value) if isinstance(value,str) else value
checks={}
try:
    from run_agent import AIAgent
    actual_agent=AIAgent(session_id=scope.native_session_id,model='fixture-offline',provider='custom',api_mode='chat_completions',
        api_key='synthetic-offline-no-provider-credential',base_url='http://127.0.0.1:9/v1',enabled_toolsets=['cloud_roles'],
        skip_context_files=True,skip_memory=True,skip_background_review=True,quiet_mode=True,max_iterations=1)
    assert actual_agent.session_id==scope.native_session_id and actual_agent._ollama_num_ctx==0
    real_args={'role':'feature','task':'Actual offline agent'}
    real_ref=_ToolCallRef('cloud_request_roles',real_args,'task','actual-agent-call',[])
    real_value=_resolve_sequential_dispatch(actual_agent,real_ref,[]).execute(real_args)
    assert json.loads(real_value)['result']['state']=='pending'
    actual_agent.close()
    checks['actual_agent_explicit_session_constructor_and_dispatch']=True
    first=dispatch();assert first['result']['state']=='pending'
    assert dispatch()==first;checks['native_dispatch_durable_retry']=True
    assert dispatch(args={'role':'feature','task':'different'})['error']=='native_call_payload_conflict';checks['payload_conflict']=True
    assert dispatch('read',{'request_id':first['result']['id']},'cloud_get_role_results')==first;checks['native_result_handler']=True
    assert dispatch(args={'role':'feature','task':'x','tool_call_id':'forged'})['error']=='invalid_role_arguments';checks['forged_model_identity_refused']=True
    assert dispatch(call='')['error']=='native_role_call_identity_unavailable';checks['missing_native_identity_refused']=True
    assert dispatch(session='other')['error']=='native_identity_mismatch';checks['native_session_mismatch_refused']=True
    assert dispatch('big',{'role':'feature','task':'x'*32769})['error']=='role_request_exceeds_bound';checks['task_bound']=True
    assert dispatch('huge',{'role':'feature','task':'x'*(256*1024)})['error']=='role_request_exceeds_bound';checks['pre_serialization_bound']=True
    original_binding=ac.set_current_observability_context
    def broken(**kwargs):raise RuntimeError('synthetic binder failure')
    ac.set_current_observability_context=broken
    assert dispatch()==first;ac.set_current_observability_context=original_binding;checks['binder_failure_direct_native_ids']=True
    # Commit through actual native dispatch, discard the socket response, then replay native call.
    original_write=__import__('cloudworkbench.role_broker_transport',fromlist=['_write'])._write
    transport=sys.modules['cloudworkbench.role_broker_transport'];drop=[True]
    def lost_write(sock,deadline,firstline,value,**kwargs):
        if kwargs.get('response') and firstline.startswith('HTTP/1.1 200') and drop[0]:
            drop[0]=False;sock.shutdown(socket.SHUT_RDWR);raise OSError('synthetic lost response')
        return original_write(sock,deadline,firstline,value,**kwargs)
    transport._write=lost_write
    assert dispatch('lost-response')=={'error':'role_broker_unavailable','status':503}
    transport._write=original_write
    recovered=dispatch('lost-response');assert recovered['result']['state']=='pending'
    with broker.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM role_broker_pending WHERE native_call_id='lost-response'").fetchone()[0]==1
    checks['native_lost_response_recovery']=True
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:copies=list(pool.map(lambda _:dispatch('concurrent'),range(8)))
    assert len({x['result']['id'] for x in copies})==1;checks['concurrent_native_retry']=True
    with broker.transaction() as db:db.execute('UPDATE fixture_parent SET active=0')
    assert dispatch()['error']=='parent_scope_denied';checks['cancellation']=True
    with broker.transaction() as db:db.execute('UPDATE fixture_parent SET active=1')
    clock[0]=2000;assert dispatch()['error']=='capability_invalid';clock[0]=1000;checks['expiry']=True
    broker.revoke(grant);assert dispatch()['error']=='capability_invalid';checks['revocation']=True
    if network_attempts or sensitive_reads:print(json.dumps({'blocked_network_attempts':network_attempts,'blocked_sensitive_reads':sensitive_reads}))
    assert not network_attempts and not sensitive_reads
finally:
    server.close();thread.join(5);assert not thread.is_alive()
    assert not (base/'socket').exists()
result={'kind':'offline_native_role_socket_broker','checks':checks,'tcp_network_attempts':len(network_attempts),'external_sensitive_reads':len(sensitive_reads),
    'sources':{str(p.relative_to(repo)):sha(p) for p in [repo/'src/cloudworkbench/role_broker.py',repo/'src/cloudworkbench/role_broker_transport.py',repo/'plugins/cloud_roles/__init__.py',repo/'plugins/cloud_roles/plugin.yaml',Path(__file__)]},
    'upstream_pins':metadata['dependencies_sha256'],'patch_sha256':sha(patch),'patched_model_tools_sha256':sha(overlay/'model_tools.py'),
    'python':sys.version,'personal_source_modified':False,'provider_called':False,'controller_fixture_only':True,'mount_path_substitution':True,'actual_scheduler_integrated':False,'additional_upstream_pins':{p:sha(source/p) for p in ['run_agent.py','agent/agent_init.py']}}
print(json.dumps(result,sort_keys=True))
