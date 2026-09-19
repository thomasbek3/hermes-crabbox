from dataclasses import replace
import json
from pathlib import Path
import pytest
from cloudworkbench.inference_transport import PinnedCLI, InferenceResult
from cloudworkbench.hermes_inference_protocol import admit_request, encode_response, ProtocolError, FILE_TOOLS

PROFILE = PinnedCLI(Path('/synthetic/claude'), 'a'*64, '2.1.274', 'claude-opus-4-6', 'high')
TOOL = {'type':'function','function':{'name':'read_file','parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}}}
REQUEST = {'model':PROFILE.native_model,'messages':[{'role':'user','content':'Read it.'}],'tools':[TOOL]}
FINAL = {'kind':'final','text':'Done.','tool_calls':[]}
DECISION = {'kind':'tool_calls','text':None,'tool_calls':[{'id':'call_1','name':'read_file','arguments':{'path':'a.txt'}}]}

def result(**changes):
    value = InferenceResult('ok',FINAL,None,{'model':PROFILE.native_model,'effort':'high','cli_version':PROFILE.version,'binary_sha256':PROFILE.sha256,'trust':'controller_pinned'},
        {'model':None,'effort':None,'status':'unknown'},None,{'state':'unknown'},None,'unknown','worker_reported',
        {'scope':'process_group','process_group_stopped':True,'container_cleanup_required':True,'credential_reuse_authorized':False})
    return replace(value,**changes)

def encode(request=REQUEST, value=None, **kwargs):
    return encode_response(admit_request(request,PROFILE), value or result(), completion_id='chatcmpl-test',created=123,**kwargs)

def frames(body):
    return [json.loads(frame[6:]) for frame in body.decode().split('\n\n') if frame and frame!='data: [DONE]']

def test_final_unknown_omitted_and_identity_not_verified():
    response=encode();value=json.loads(response.body)
    assert response.status_code==200 and value['choices'][0]['finish_reason']=='stop'
    assert value['choices'][0]['message']['content']=='Done.' and 'usage' not in value
    assert response.metadata['model_identity']['verified'] is False
    assert response.metadata['usage_status']['state']=='unknown'
    assert response.metadata['cleanup']['credential_reuse_authorized'] is False

def test_immutable_request_and_metadata():
    raw=json.loads(json.dumps(REQUEST));admitted=admit_request(raw,PROFILE)
    raw['messages'][0]['content']='changed';admitted.transport_request['messages'].clear()
    assert admitted.transport_request['messages'][0]['content']=='Read it.'
    response=encode();response.metadata.clear();assert response.metadata['schema_version']==1

def test_tool_call_json_and_sse_roundtrip():
    response=encode(value=result(decision=DECISION));value=json.loads(response.body)
    call=value['choices'][0]['message']['tool_calls'][0]
    assert call['id']=='call_1' and json.loads(call['function']['arguments'])=={'path':'a.txt'}
    stream=encode({**REQUEST,'stream':True,'stream_options':{'include_usage':True}},result(decision=DECISION))
    chunks=frames(stream.body)
    assert stream.body.endswith(b'data: [DONE]\n\n')
    assert chunks[1]['choices'][0]['delta']['tool_calls'][0]=={'index':0,**call}
    assert chunks[-1]['choices'][0]['finish_reason']=='tool_calls'
    assert all('usage' not in chunk for chunk in chunks)

def test_complete_usage_cache_accounting_and_partial_unknown():
    usage={'input_tokens':10,'output_tokens':5,'cache_creation_input_tokens':3,'cache_read_input_tokens':7}
    value=result(usage=usage,usage_status={'state':'reported'})
    assert json.loads(encode(value=value).body)['usage']['total_tokens']==25
    streamed=frames(encode({**REQUEST,'stream':True,'stream_options':{'include_usage':True}},value).body)
    assert streamed[-1]['choices']==[] and streamed[-1]['usage']['prompt_tokens']==20
    partial=encode(value=result(usage={'input_tokens':10,'output_tokens':5},usage_status={'state':'reported'}))
    assert 'usage' not in json.loads(partial.body) and partial.metadata['usage_status']['state']=='partial'

@pytest.mark.parametrize('change,code',[
 ({'model':'alias'},'model_mismatch'),({'reasoning_effort':'low'},'effort_mismatch'),
 ({'reasoning':{'enabled':False,'effort':'high'}},'effort_mismatch'),
 ({'temperature':None},'unsupported_parameter'),({'max_tokens':10},'unsupported_parameter'),
 ({'parallel_tool_calls':True},'unsupported_parameter'),({'response_format':{}},'unsupported_parameter'),
 ({'stream':None},'invalid_stream'),({'stream':1},'invalid_stream'),
 ({'stream_options':None},'unsupported_stream_options'),({'n':True},'unsupported_choice_count'),
 ({'n':2},'unsupported_choice_count'),({'tool_choice':'required'},'unsupported_tool_choice'),
])
def test_reject_semantic_changes(change,code):
    with pytest.raises(ProtocolError,match=code):admit_request({**REQUEST,**change},PROFILE)

@pytest.mark.parametrize('body,code',[(b'{"model":"a","model":"b"}','duplicate_json_key'),(b'NaN','invalid_json'),(b'\xff','invalid_json'),(b'[]','invalid_request'),(b'x'*262145,'body_limit')])
def test_hostile_input(body,code):
    with pytest.raises(ProtocolError,match=code):admit_request(body,PROFILE)

def test_default_capability_rejects_process_manage_explicit_registration_supported():
    tool=json.loads(json.dumps(TOOL));tool['function']['name']='future_tool'
    request={**REQUEST,'tools':[tool]}
    with pytest.raises(ProtocolError,match='unsupported_tool'):admit_request(request,PROFILE)
    admitted=admit_request(request,PROFILE,allowed_tool_names=FILE_TOOLS|{'future_tool'})
    decision={**DECISION,'tool_calls':[{**DECISION['tool_calls'][0],'name':'future_tool'}]}
    assert encode_response(admitted,result(decision=decision),completion_id='chatcmpl-x',created=1).status_code==200
    tool['function']['name']='process_manage'
    with pytest.raises(ProtocolError,match='unsupported_tool'):admit_request(request,PROFILE)

def test_transcript_preserves_roles_tool_ids_and_rejects_reuse():
    request={**REQUEST,'messages':REQUEST['messages']+[
        {'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'read_file','arguments':'{"path":"a.txt"}'}}]},
        {'role':'tool','tool_call_id':'call_1','content':'contents'}]}
    admitted=admit_request(request,PROFILE)
    assert admitted.transport_request['messages']==request['messages']
    with pytest.raises(ProtocolError,match='invalid_decision'):encode(request,result(decision=DECISION))

@pytest.mark.parametrize('error,status',[('provider_auth_rejected',401),('rate_limited',429),('cancelled',409),('wall_timeout',504),('process_group_cleanup_unconfirmed',503),('secret freeform',502)])
def test_error_mapping_stream_never_fake_success(error,status):
    response=encode({**REQUEST,'stream':True},result(status='error',decision=None,error_code=error))
    assert response.status_code==status and response.content_type=='application/json'
    assert b'secret freeform' not in response.body and 'choices' not in json.loads(response.body)

def test_result_identity_and_cleanup_guard():
    with pytest.raises(ProtocolError,match='result_identity_mismatch'):encode(value=result(requested_identity={}))
    with pytest.raises(ProtocolError,match='result_identity_mismatch'):encode(value=result(reported_identity={'model':'alias','status':'reported'}))
    assert encode(value=result(cleanup={})).status_code==503

def test_empty_final_stream_has_terminal_marker():
    chunks=frames(encode({**REQUEST,'stream':True},result(decision={**FINAL,'text':''})).body)
    assert len(chunks)==2 and chunks[-1]['choices'][0]['finish_reason']=='stop'

def test_matching_optional_parameters_accept():
    assert admit_request({**REQUEST,'reasoning':{'enabled':True,'effort':'high'},'reasoning_effort':'high','n':1,'tool_choice':'auto'},PROFILE)

@pytest.mark.parametrize('kwargs',[{'error_code':['x']},{'reported_identity':{'model':PROFILE.native_model,'status':'unknown'}},{'reported_identity':{'model':None,'status':'reported'}}])
def test_malformed_results_fixed_502(kwargs):
    with pytest.raises(ProtocolError) as caught:encode(value=result(**kwargs))
    assert caught.value.status_code==502

def test_error_cleanup_truth_and_usage_missing_result():
    response=encode(value=result(status='error',error_code='rate_limited',cleanup={},usage_status={'state':'unknown','reason':'missing_result'}))
    assert response.status_code==503 and response.metadata['cleanup']['process_group_stopped'] is None
    assert response.metadata['usage_status']['reason']=='missing_result'

def test_forged_admitted_request_is_upstream_error():
    admitted=admit_request(REQUEST,PROFILE)
    for corrupted in (replace(admitted,profile=None),replace(admitted,stream='yes'),replace(admitted,allowed_tool_names=frozenset())):
        with pytest.raises(ProtocolError) as caught:encode_response(corrupted,result(),completion_id='chatcmpl-x',created=1)
        assert caught.value.status_code==502

def test_stream_text_unicode_and_newlines():
    text=('hello\r\n\u2028'*3000)
    stream=encode({**REQUEST,'stream':True},result(decision={**FINAL,'text':text}))
    assert frames(stream.body)[1]['choices'][0]['delta']['content']==text

@pytest.mark.parametrize('capability',[[],None,frozenset({'bad name'})])
def test_invalid_trusted_capability(capability):
    with pytest.raises(ProtocolError,match='invalid_tool_capability'):admit_request(REQUEST,PROFILE,allowed_tool_names=capability)

@pytest.mark.parametrize('raw',[b'['*2000+b']'*2000,b'{"model":"\\ud800"}'])
def test_deep_or_surrogate_json_fixed_error(raw):
    with pytest.raises(ProtocolError):admit_request(raw,PROFILE)
