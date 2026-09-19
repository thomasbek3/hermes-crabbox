import json
import pytest
from cloudworkbench.adapters import claude_command, parse_events, capabilities, AdapterError


def test_model_is_argv_not_shell_and_no_prompt():
    cmd = claude_command({"model": "claude-sonnet-test", "prompt": "$(touch /tmp/bad)"})
    assert "$(touch /tmp/bad)" not in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-test"
    assert "--no-session-persistence" in cmd
    with pytest.raises(AdapterError):
        claude_command({"model": "--bad"})


def test_parser_does_not_promote_model_verification_or_thinking():
    secret = b"unique-secret-value"
    raw = json.dumps({"type":"assistant","message":{"content":[{"type":"thinking","thinking":"do not expose"},{"type":"text","text":"unique-secret-value verified"}]}}).encode()
    events = parse_events(raw, (secret,))
    assert events == [{"type":"assistant.message","payload":{"text":"[REDACTED] verified"}}]
    assert parse_events(b'not json\n[1,2]\n') == []


def test_auth_presence_does_not_enable_unqualified_adapter(tmp_path):
    token = tmp_path / 'token'
    token.write_text('synthetic')
    assert not capabilities(token)['claude']['enabled']


def test_multibyte_and_unbounded_provider_metadata_fit_event_budget():
    events = parse_events((json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'漢'*30000}]}})+'\n'+json.dumps({'type':'result','result':'漢'*30000,'usage':{'x':'x'*100000},'modelUsage':{'x':'x'*100000},'permission_denials':['x'*100000]})).encode())
    assert len(events)==2
    assert all(len(json.dumps(e['payload'],ensure_ascii=False).encode())<65536 for e in events)
    assert events[-1]['payload']['usage'] is None
    assert events[-1]['payload']['permission_denials'] is True


def test_provider_metadata_is_explicitly_worker_reported():
    raw = b'\n'.join(json.dumps(event).encode() for event in [
        {'type':'system','subtype':'init','model':'claimed-model','claude_code_version':'claimed-version'},
        {'type':'result','result':'claimed success','usage':{'input_tokens':1}},
        {'type':'assistant','message':{'content':[{'type':'tool_use','id':'1','name':'Bash'}]}},
    ])
    events = parse_events(raw)
    assert {event['type'] for event in events} == {'adapter.provenance','adapter.result','tool.started'}
    assert all(event['payload']['provenance'] == 'worker_reported' for event in events)


@pytest.mark.parametrize('item,code',[
    ({'type':'assistant','error':'authentication_failed'},'provider_auth_rejected'),
    ({'type':'assistant','error':'rate_limit'},'rate_limited'),
    ({'type':'result','is_error':True,'api_error_status':401},'provider_auth_rejected'),
    ({'type':'result','is_error':True,'api_error_status':429},'rate_limited'),
])
def test_explicit_cli_failure_fields_only(item,code):
    from cloudworkbench.adapters import classify_cli_failure
    assert classify_cli_failure(item)['code']==code


@pytest.mark.parametrize('item',[
    {'type':'assistant','message':{'content':[{'type':'text','text':'authentication_failed 401 rate_limit'}]}},
    {'type':'result','is_error':True,'result':'auth missing authentication_failed 429'},
    {'type':'result','is_error':True,'subtype':'authentication_error'},
    {'type':'result','is_error':False,'api_error_status':401},
    {'type':'result','is_error':True,'api_error_status':'401'},
    {'type':'assistant','error':{'message':'authentication_failed'}},
])
def test_prose_subtype_or_untyped_status_never_classifies_auth(item):
    from cloudworkbench.adapters import classify_cli_failure
    assert classify_cli_failure(item) is None


def test_usage_absence_explicit_and_worker_trust_preserved():
    from cloudworkbench.adapters import public_spool_event
    event=parse_events(b'{"type":"result","is_error":true,"api_error_status":401}')[0]
    result=public_spool_event(event)['payload']
    assert result['failure_code']=='provider_auth_rejected'
    assert result['usage_status']=={'state':'unknown','reason':'provider_did_not_report'}
    assert result['provenance']=='worker_reported'


def test_malformed_failure_metadata_cannot_become_trusted_classification():
    from cloudworkbench.adapters import public_spool_event
    result=public_spool_event({'type':'adapter.result','payload':{'is_error':True,'failure_code':[],'failure_basis':{}}})
    assert result['payload']['failure_code'] is None
    result=public_spool_event({'type':'error','payload':{'reason':'example','classification_basis':[]}})
    assert result['payload']['classification_basis'] is None


def test_failure_code_requires_compatible_protocol_basis():
    from cloudworkbench.adapters import public_spool_event
    payload=public_spool_event({'type':'adapter.result','payload':{'is_error':True,'failure_code':'provider_auth_rejected','failure_basis':'credential_file'}})['payload']
    assert payload['failure_code'] is None
