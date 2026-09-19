import copy
import hashlib
import json
import threading

import pytest
from test_provider_dispatch import setup
from test_native_responses import PROFILES, request as full_request, message
from test_supervised_executor import build
from cloudworkbench.hermes_inference_protocol import ProtocolError
from cloudworkbench.inference_relay import AttemptBinding, DispatchContext, _json
from cloudworkbench.provider_executor import ProviderExecutor
from cloudworkbench.provider_executor import _failure
from cloudworkbench.provider_protocol import admit_provider_request, encode_provider_result


def request(profile):
    value=full_request(profile)
    value['tools']=[tool for tool in value['tools'] if tool['name']=='read_file']
    return value


def native_executor(setup, profile):
    binding=AttemptBinding(setup[2]['attempt_id'],setup[2]['generation'],profile.digest)
    executor=ProviderExecutor(setup[7],reservation=setup[4],grant_id=setup[5],binding=binding,
                              profile=profile,remaining_seconds=lambda:160)
    req=request(profile)
    value={'profile_digest':profile.digest,'transport_stopped':True,'response':{
        'id':'resp_test','model':profile.model,'status':'completed','output':[message()],
        'usage':{'input_tokens':12,'output_tokens':8}}}
    envelope={'version':1,'status':'ok','error':None,'result':value,
              'container_cleanup_required':True,'credential_reuse_authorized':False}
    setup[6].hooks['collect']=lambda *args:json.dumps(envelope).encode()
    context=DispatchContext(binding,'c'*64,hashlib.sha256(_json(req)).hexdigest())
    return executor,context,req,envelope


@pytest.mark.parametrize('profile',PROFILES)
def test_native_dispatch_delivers_sse_only_after_cleanup_and_replay_reuses_it(setup,profile):
    executor,context,req,_=native_executor(setup,profile)
    response=executor(context,req,threading.Event())
    assert response.response.status==200 and response.outer_cleanup_confirmed
    assert response.response.content_type=='text/event-stream'
    assert b'response.output_item.done' in response.response.body
    assert not setup[6].objects
    assert executor(context,req,threading.Event())==response
    assert sum(event[0]=='create' for event in setup[6].events)==1


@pytest.mark.parametrize('mutation',['model','digest','live_transport','unknown_field','invalid_call','cleanup'])
def test_native_provider_result_cannot_bypass_validation_or_cleanup(setup,mutation):
    executor,context,req,envelope=native_executor(setup,PROFILES[0])
    value=envelope['result']
    if mutation=='model':value['response']['model']='gpt-5.5'
    if mutation=='digest':value['profile_digest']='f'*64
    if mutation=='live_transport':value['transport_stopped']=False
    if mutation=='unknown_field':value['untrusted']=True
    if mutation=='invalid_call':value['response']['output']=[{'type':'function_call','name':'terminal',
        'call_id':'call1','arguments':'{"command":"unexpected"}'}]
    if mutation=='cleanup':
        def fail(_):raise RuntimeError('cleanup refused')
        setup[3].verifier=fail
    response=executor(context,req,threading.Event())
    assert response.response.status>=400
    assert response.outer_cleanup_confirmed==(mutation!='cleanup')
    assert bool(setup[6].objects)==(mutation=='cleanup')


def test_native_profile_cannot_accept_chat_protocol_or_unapproved_tools(setup):
    executor,context,req,_=native_executor(setup,PROFILES[0])
    for body in ({'model':PROFILES[0].model,'messages':[{'role':'user','content':'Hi'}]},
                 {**req,'tools':[{'type':'function','name':'terminal','parameters':{'type':'object','properties':{}}}]}):
        context=DispatchContext(executor.binding,'c'*64,hashlib.sha256(_json(body)).hexdigest())
        assert executor(context,body,threading.Event()).response.status==400
    assert not setup[6].events


@pytest.mark.parametrize('error',['auth_expired','auth_schema_unsupported','provider_auth_rejected'])
def test_native_auth_failure_quarantines_account_after_cleanup(setup,error):
    executor,context,req,envelope=native_executor(setup,PROFILES[0])
    envelope.update(status='error',error=error,result=None)
    output=executor(context,req,threading.Event())
    assert output.response.status==401 and output.outer_cleanup_confirmed
    assert json.loads(output.response.body)['error']['code']==error
    assert not setup[6].objects
    assert setup[3].current('account')['state']=='quarantined'


def test_native_codec_keeps_tool_ids_and_rejects_reused_call_ids():
    profile=PROFILES[0];req=request(profile)
    call={'type':'function_call','call_id':'call1','name':'read_file','arguments':'{"path":"x"}'}
    response={'id':'resp_test','model':profile.model,'status':'completed','output':[call]}
    value={'profile_digest':profile.digest,'transport_stopped':True,'response':response}
    encoded=encode_provider_result(admit_provider_request(req,profile),value,completion_id='unused',created=1)
    assert b'"call_id":"call1"' in encoded.body
    req['input'] += [copy.deepcopy(call),{'type':'function_call_output','call_id':'call1','output':'42'}]
    with pytest.raises(ProtocolError,match='invalid_native_response'):
        encode_provider_result(admit_provider_request(req,profile),value,completion_id='unused',created=1)


def test_native_supervisor_stops_and_confirms_cleanup_after_collection_failure(setup):
    wrapped,_=build(setup)
    old_dispatch=wrapped.executor.dispatch
    executor,context,req,_=native_executor(setup,PROFILES[0])
    executor.dispatch=old_dispatch
    wrapped.executor=executor
    original_collect=setup[6].collect
    def collect(spec,resources,**kwargs):
        # Definite collection failure is cleaned through the existing dispatcher.
        original_collect(spec,resources,**kwargs)
        raise RuntimeError('synthetic collect failure')
    setup[6].collect=collect
    response=wrapped(context,req,threading.Event())
    assert response.response.status>=400 and response.outer_cleanup_confirmed
    assert wrapped.last_receipt.stopped
    assert not setup[6].objects
    assert sum(event[0]=='create' for event in setup[6].events)==1


def test_native_recovery_finds_exact_durable_normalized_request(setup):
    wrapped,_=build(setup)
    dispatch=wrapped.executor.dispatch
    executor,context,req,_=native_executor(setup,PROFILES[0])
    executor.dispatch=dispatch
    wrapped.executor=executor
    assert wrapped(context,req,threading.Event()).response.status==200
    # A reply lost after durable cleanup must be recoverable without relaunching.
    recovered=wrapped._recover_failed_call(context,req,_failure('synthetic_lost_reply'))
    assert recovered.outer_cleanup_confirmed and recovered.response.status==503
    assert wrapped.last_recovery_receipt.cleanup_confirmed
    assert wrapped.last_recovery_error is None
    assert sum(event[0]=='create' for event in setup[6].events)==1


def test_authenticated_worker_caches_native_sse_after_supervised_cleanup(setup,tmp_path):
    import base64
    from cloudworkbench.inference_relay import WorkerDispatcher
    wrapped,_=build(setup)
    dispatch=wrapped.executor.dispatch
    executor,context,req,_=native_executor(setup,PROFILES[0])
    executor.dispatch=dispatch
    wrapped.executor=executor
    cap='e'*64
    worker=WorkerDispatcher(journal_path=tmp_path/'native-worker.db',binding=executor.binding,
        capability_sha256=hashlib.sha256(cap.encode()).hexdigest(),
        authorize=wrapped.authorize,execute_request=wrapped)
    call={'binding':executor.binding.wire(),'capability':cap,'nonce':context.request_nonce,
          'payload_digest':context.payload_digest,'payload':req}
    try:
        result=worker.dispatch(call,threading.Event())
        assert result['response']['status']==200
        assert b'response.completed' in base64.b64decode(result['response']['body'])
        assert worker.dispatch(call,threading.Event())==result
        assert wrapped.last_receipt.stopped and not setup[6].objects
        assert sum(event[0]=='create' for event in setup[6].events)==1
    finally:
        worker.close()
