from dataclasses import FrozenInstanceError,replace
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import pytest
from cloudworkbench import hermes_adapter as h
from cloudworkbench.pstack_routing import BackendProfile

@pytest.fixture
def profile():return BackendProfile('synthetic-claude','anthropic','claude-synthetic','anthropic_messages','high','anthropic','a'*64,('high',))
def line(value):return json.dumps(value).encode()+b'\n'
def init(**kwargs):return {'type':'system','subtype':'init','model':'reported','session_id':'native-id',**kwargs}
def result(**kwargs):return {'type':'result','exit_code':0,'text':'done','session_id':'native-id',**kwargs}

def test_plan_is_immutable_bound_and_does_not_inherit_environment(profile,monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-secret');monkeypatch.setenv('HERMES_HOME','/personal')
    plan=h.build_launch(profile);env=dict(plan.environment);cfg=json.loads(plan.config_json);receipt=json.loads(plan.route_receipt_json)
    assert env['HERMES_HOME']=='/state/hermes/profile' and 'ANTHROPIC_API_KEY' not in env
    assert '--ignore-rules' in plan.argv and '--safe-mode' not in plan.argv # CLI safe-mode would ignore our config.
    assert plan.argv[plan.argv.index('--provider')+1]==profile.provider
    assert cfg['model']['provider']==profile.provider and cfg['delegation']['model']==profile.model
    assert cfg['fallback_model']==[] and cfg['mcp_servers']=={} and cfg['hooks']=={}
    assert cfg['auxiliary']['title_generation']['enabled'] is False and cfg['auxiliary']['compression']['model']==profile.model
    assert receipt['execution_authorized'] is False and receipt['routed_execution_requires']=='provider_service_only_transport'
    assert receipt['config_sha256']==h._sha(plan.config_json) and not receipt['provider_route_observed'] and not plan.live_qualified
    with pytest.raises(FrozenInstanceError):plan.config_json='changed'

@pytest.mark.parametrize('provider,transport,family',[('anthropic','anthropic_messages','anthropic'),('openai-codex','codex_responses','openai'),('xai','codex_responses','xai'),('xai-oauth','codex_responses','xai')])
def test_supported_native_transports(profile,provider,transport,family):
    assert h.build_launch(replace(profile,provider=provider,transport=transport,family=family)).argv

@pytest.mark.parametrize('kwargs',[{'provider':'claude-code'},{'provider':'claude-cli'},{'transport':'codex_app_server'},{'model':'xai/grok'},{'model':'anthropic:other'}])
def test_unqualified_aliases_transport_and_model_prefix_refused(profile,kwargs):
    with pytest.raises(h.HermesAdapterError):h.build_launch(replace(profile,**kwargs))

@pytest.mark.parametrize('kwargs',[{'toolsets':('delegation',)},{'toolsets':('terminal','terminal')},{'max_turns':True},{'max_turns':129},{'run_budget_seconds':0}])
def test_launch_bounds(profile,kwargs):
    with pytest.raises(h.HermesAdapterError):h.build_launch(profile,**kwargs)

def layout(tmp_path,profile):
    root=tmp_path.resolve();source=root/'source';work=root/'work';state=root/'state';p=state/'profile'
    for path in (source,work,p,*(state/n for n in ('home','empty-codex','empty-claude','xdg-config','xdg-cache','xdg-data'))):path.mkdir(parents=True)
    plan=h.build_launch(profile);(p/'config.yaml').write_text(plan.config_json)
    return dict(source_root=source,profile_root=p,workspace=work,isolation_root=state,plan=plan)

@pytest.mark.parametrize('root_key,name',[('source_root','.env'),('source_root','.op.env'),('profile_root','.env'),('profile_root','plugins'),('workspace','.hermes/plugins')])
def test_ambient_files_never_read(tmp_path,profile,root_key,name):
    params=layout(tmp_path,profile);assert h.check_clean_layout(**params)['isolation_directories_empty']
    path=params[root_key]/name;path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to('/not-existing-sensitive-file')
    with pytest.raises(h.HermesAdapterError):h.check_clean_layout(**params)

@pytest.mark.parametrize('mutation',['auth','foreign-config','leftover','symlink-config','hardlink-config'])
def test_preflight_binds_config_and_empty_state(tmp_path,profile,mutation):
    params=layout(tmp_path,profile);p=params['profile_root'];config=p/'config.yaml'
    if mutation=='auth':(p/'auth.json').write_text('synthetic')
    elif mutation=='foreign-config':config.write_text('{}')
    elif mutation=='leftover':(params['isolation_root']/'empty-codex/auth.json').write_text('synthetic')
    elif mutation=='symlink-config':config.unlink();config.symlink_to('/absent')
    else:(tmp_path/'hardlink').hardlink_to(config)
    with pytest.raises(h.HermesAdapterError):h.check_clean_layout(**params)


def test_jsonl_partial_transport_and_known_secret_fields():
    secret=b'synthetic-known-secret';parser=h.JsonlParser((secret,));events=[]
    wire=b''.join(map(line,[init(model=secret.decode()),{'type':'text','text':'hello '+secret.decode()},
        {'type':'tool_use','name':'read','tool_call_id':'1','input':{'secret':secret.decode()}},
        {'type':'tool_result','name':'read','output':secret.decode()},result(text=secret.decode(),error='RAW:'+secret.decode(),tokens={'input':17,'output':3})]))
    for index in range(0,len(wire),7):events.extend(parser.feed(wire[index:index+7]))
    parser.finish();serialized=json.dumps(events)
    assert secret.decode() not in serialized and 'RAW:' not in serialized and '[REDACTED]' in serialized
    assert events[0]['payload']['provenance']=='worker_reported' and events[0]['payload']['provider'] is None
    assert next(e for e in events if e['type']=='tool.started')['payload'].keys()=={'id','name','provenance'}
    assert events[-1]['payload']['usage']['input']==17 and events[-1]['payload']['reported_cost_usd'] is None

def test_secret_split_across_deltas_and_interleaved_tool_events():
    parser=h.JsonlParser((b'very-long-secret',));events=parser.feed(line(init()))
    for value in [{'type':'text','text':'prefix very-'},{'type':'tool_use','name':'test'}, {'type':'text','text':'long-'},{'type':'text','text':'secret suffix'},result()]:events.extend(parser.feed(line(value)))
    parser.finish();texts=''.join(e['payload']['text'] for e in events if e['type']=='assistant.message')
    assert texts=='prefix [REDACTED] suffix'
    # Held secret suffix crosses tool boundary; public order is safe publication order.
    assert next(i for i,e in enumerate(events) if e['type']=='tool.started') < next(i for i,e in enumerate(events) if e['type']=='assistant.message')

def test_truncation_occurs_after_secret_redaction():
    parser=h.JsonlParser((b'secret-value',));parser.feed(line(init()))
    events=parser.feed(line(result(text='x'*(h.MAX_TEXT-3)+'secret-value')));parser.finish()
    assert 'sec' not in events[-1]['payload']['summary'][-3:]

@pytest.mark.parametrize('tokens',[{},None,{'input':0,'output':0},{'input':True},{'input':-1},{'input':10**15}])
def test_missing_or_emitter_default_usage_remains_unknown(tokens):
    p=h.JsonlParser();p.feed(line(init()));event=p.feed(line(result(tokens=tokens)))[-1];p.finish()
    assert event['payload']['usage_status']['state']=='unknown'

def test_error_prose_does_not_become_auth_classification():
    p=h.JsonlParser();p.feed(line(init()));event=p.feed(line(result(exit_code=1,error='401 api key invalid raw credential')))[-1]
    assert event['payload']['failure_code']=='hermes_execution_failed' and event['payload']['api_error_status'] is None
    assert '401' not in json.dumps(event)

@pytest.mark.parametrize('wire',[line(result()),line(init())+line(init()),line(init())+b'{broken}\n',line(init())+line({'type':'text','text':3}),line(init())+line(result(exit_code=True)),line(init())+line(result())+line({'type':'text','text':'later'}),line(init())+b'{"type":"result","exit_code":NaN}\n'])
def test_invalid_protocol_closes_parser(wire):
    p=h.JsonlParser()
    with pytest.raises(h.HermesAdapterError):p.feed(wire)
    with pytest.raises(h.HermesAdapterError,match='parser_closed'):p.feed(b'')

@pytest.mark.parametrize('wire',[line(init()),line(init())+b'{"type":"result"'])
def test_missing_or_partial_result_is_not_success(wire):
    p=h.JsonlParser();p.feed(wire)
    with pytest.raises(h.HermesAdapterError):p.finish()

def test_parser_input_total_line_and_count_limits(monkeypatch):
    with pytest.raises(h.HermesAdapterError,match='chunk'):h.JsonlParser().feed(b'x'*(h.MAX_LINE+1))
    p=h.JsonlParser();p.feed(b'x'*h.MAX_LINE)
    with pytest.raises(h.HermesAdapterError,match='line'):p.feed(b'x')
    monkeypatch.setattr(h,'MAX_TOTAL',3)
    with pytest.raises(h.HermesAdapterError,match='output'):h.JsonlParser().feed(b'1234')
    monkeypatch.setattr(h,'MAX_TOTAL',10000);monkeypatch.setattr(h,'MAX_EVENTS',1)
    with pytest.raises(h.HermesAdapterError,match='count'):h.JsonlParser().feed(line(init())+line(result()))


def test_actual_pinned_hermes_emitter_without_auth_or_inference():
    source=Path(__file__).parents[1]/'evidence/hermes-adapter-source-snapshot/audited-hermes/hermes_cli/stream_json.py'
    import hashlib
    assert hashlib.sha256(source.read_bytes()).hexdigest()=='e9229445739e6755ee1f088b0f6eb000a9761b47a8a56e932a9922e778c5d31a'
    spec=importlib.util.spec_from_file_location('hermes_audited_emitter',source);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    output=io.StringIO()
    with contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
        emitter=module.StreamJsonEmitter('native-model','native-id');emitter.on_text_delta('synthetic live text')
        emitter.on_tool_progress('tool.started','read',args={'path':'fixture'},tool_call_id='1')
        emitter.on_tool_progress('tool.completed','read',result='fixture contents',tool_call_id='1')
        assert emitter.emit_result({'final_response':'synthetic final','input_tokens':7,'output_tokens':2})==0
    parser=h.JsonlParser();events=parser.feed(output.getvalue().encode());parser.finish()
    assert [e['type'] for e in events]==['adapter.provenance','assistant.message','tool.started','tool.completed','adapter.result']
    assert events[-1]['payload']['usage']['input']==7 and events[-1]['payload']['summary']=='synthetic final'

@pytest.mark.parametrize('event',[init(model='\ud800'),{'type':'text','text':'\udfff'},result(text='\ud800')])
def test_lone_surrogate_closes_parser_with_fixed_code(event):
    p=h.JsonlParser()
    if event['type']!='system':p.feed(line(init()))
    with pytest.raises(h.HermesAdapterError,match='invalid_provider_fields'):p.feed(line(event))
    with pytest.raises(h.HermesAdapterError,match='parser_closed'):p.feed(b'')

def test_text_output_byte_bound():
    p=h.JsonlParser();p.feed(line(init()));events=[]
    for _ in range(6):events+=p.feed(line({'type':'text','text':'😀'*2000}))
    events+=p.feed(line(result()));p.finish()
    assert all(len(e['payload']['text'].encode())<=h.MAX_TEXT for e in events if e['type']=='assistant.message')

def test_plan_cannot_claim_live_qualified(profile):
    with pytest.raises((ValueError,TypeError)):replace(h.build_launch(profile),live_qualified=True)

@pytest.mark.parametrize('data,code',[({'failed':True,'error':'raw synthetic error'},0),({},130),({'error':'raw synthetic error'},0)])
def test_actual_emitter_failure_signals_are_not_success(data,code):
    source=Path(__file__).parents[1]/'evidence/hermes-adapter-source-snapshot/audited-hermes/hermes_cli/stream_json.py'
    spec=importlib.util.spec_from_file_location('hermes_audited_failure',source);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    output=io.StringIO()
    with contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
        emitter=module.StreamJsonEmitter('test','id');emitter.on_tool_progress('reasoning','ignored');emitter.emit_result(data,exit_code=code)
    p=h.JsonlParser();events=p.feed(output.getvalue().encode());p.finish()
    assert events[-1]['payload']['is_error'] and 'raw synthetic error' not in json.dumps(events)
