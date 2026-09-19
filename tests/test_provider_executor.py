from dataclasses import asdict,replace
import hashlib
import json
import threading
import pytest
from test_provider_dispatch import setup
from test_hermes_inference_protocol import PROFILE,REQUEST,result
from cloudworkbench.inference_relay import AttemptBinding,DispatchContext,_json
from cloudworkbench.provider_executor import ProviderExecutor,canonical_pinned_profile_digest


def executor(setup):
    store,principal,attempt,leases,owner,grant,runtime,dispatch,clock=setup
    binding=AttemptBinding(attempt['attempt_id'],attempt['generation'],canonical_pinned_profile_digest(PROFILE))
    adapter=ProviderExecutor(dispatch,reservation=owner,grant_id=grant,binding=binding,
                             profile=PROFILE,remaining_seconds=lambda:160,clock=lambda:1000)
    envelope={'version':1,'status':'ok','error':None,'result':asdict(result()),
              'container_cleanup_required':True,'credential_reuse_authorized':False}
    runtime.hooks['collect']=lambda *args:json.dumps(envelope).encode()
    context=DispatchContext(binding,'c'*64,hashlib.sha256(_json(REQUEST)).hexdigest())
    return adapter,context,envelope


def test_success_uses_real_dispatch_and_codec_after_cleanup(setup):
    adapter,context,envelope=executor(setup)
    response=adapter(context,REQUEST,threading.Event())
    assert response.outer_cleanup_confirmed and response.response.status==200
    assert json.loads(response.response.body)['choices'][0]['message']['content']=='Done.'
    runtime=setup[6]
    assert not runtime.objects
    assert setup[3].current('account')['reservation']==setup[4]
    assert adapter(context,REQUEST,threading.Event()).response.body==response.response.body
    assert len([e for e in runtime.events if e[0]=='create'])==1


@pytest.mark.parametrize('kind',['binding','digest','cancel','revoked'])
def test_refusal_never_creates_runtime(setup,kind):
    adapter,context,envelope=executor(setup);cancel=threading.Event()
    if kind=='binding':context=replace(context,binding=AttemptBinding('other',1,'b'*64))
    if kind=='digest':context=replace(context,payload_digest='f'*64)
    if kind=='cancel':cancel.set()
    if kind=='revoked':setup[3].revoke_grant(setup[5])
    response=adapter(context,REQUEST,cancel)
    assert response.response.status in (403,409)
    assert not setup[6].events


@pytest.mark.parametrize('kind',['cleanup','bad_envelope','wrong_identity','provider_auth'])
def test_failed_response_never_becomes_success(setup,kind):
    adapter,context,envelope=executor(setup)
    if kind=='cleanup':
        def fail(target):raise RuntimeError('synthetic cleanup failure')
        setup[3].verifier=fail
    if kind=='bad_envelope':envelope['credential_reuse_authorized']=True
    if kind=='wrong_identity':envelope['result']['requested_identity']['model']='other'
    if kind=='provider_auth':envelope.update(status='error',error='provider_auth_rejected',result=None)
    response=adapter(context,REQUEST,threading.Event())
    assert response.response.status!=200
    assert response.outer_cleanup_confirmed==(kind!='cleanup')
    assert bool(setup[6].objects)==(kind=='cleanup')


@pytest.mark.parametrize('kind,expected', [('success',200),('model',400),('reserve',503),('busy',409),('timeout',504),('malformed',502),('cleanup',503)])
def test_actual_worker_uses_executor_authorization_and_callback(setup,tmp_path,kind,expected):
    import base64
    from cloudworkbench.inference_relay import WorkerDispatcher
    adapter,context,envelope=executor(setup)
    payload=json.loads(json.dumps(REQUEST))
    if kind=='model':payload['model']='not-qualified'
    if kind=='reserve':adapter.remaining_seconds=lambda:0
    if kind=='busy':setup[3].acquire_request(setup[4],setup[5])
    if kind=='timeout':envelope.update(status='error',error='wall_timeout',result=None)
    if kind=='malformed':setup[6].hooks['collect']=lambda *args:b'not json'
    if kind=='cleanup':
        def fail(target):raise RuntimeError('cleanup unavailable')
        setup[3].verifier=fail
    cap='e'*64
    worker=WorkerDispatcher(journal_path=tmp_path/'worker.db',binding=adapter.binding,
        capability_sha256=hashlib.sha256(cap.encode()).hexdigest(),authorize=adapter.authorize,execute_request=adapter)
    value={'binding':adapter.binding.wire(),'capability':cap,'nonce':context.request_nonce,
           'payload_digest':hashlib.sha256(_json(payload)).hexdigest(),'payload':payload}
    try:
        response=worker.dispatch(value,threading.Event())
        assert response['response']['status']==expected
        assert worker.dispatch(value,threading.Event())==response
        body=json.loads(base64.b64decode(response['response']['body']))
        if kind in ('model','reserve','busy','timeout','malformed'):
            assert body['error']['code']!='outer_cleanup_unconfirmed'
    finally:worker.close()


def test_profile_digest_mismatch_refused_before_runtime(setup):
    adapter,context,envelope=executor(setup)
    with pytest.raises(ValueError,match='profile_digest_mismatch'):
        ProviderExecutor(setup[7],reservation=setup[4],grant_id=setup[5],
            binding=AttemptBinding(adapter.binding.attempt_id,adapter.binding.generation,'a'*64),
            profile=PROFILE,remaining_seconds=lambda:160)
    assert not setup[6].events


@pytest.mark.parametrize('raw', [b'{"version":1,"version":1}',b'[]',b'{"version":true}',b'not json'])
def test_malformed_container_envelopes_never_succeed(setup,raw):
    adapter,context,envelope=executor(setup)
    setup[6].hooks['collect']=lambda *args:raw
    response=adapter(context,REQUEST,threading.Event())
    assert response.response.status==502 and response.outer_cleanup_confirmed
    assert not setup[6].objects


def test_durable_response_timestamp_survives_changed_wall_clock(setup):
    adapter,context,envelope=executor(setup)
    first=adapter(context,REQUEST,threading.Event())
    adapter.clock=lambda:123456789
    second=adapter(context,REQUEST,threading.Event())
    assert first.response.status==200 and first.response.body==second.response.body


def test_collection_error_reports_confirmed_final_cleanup(setup):
    adapter,context,envelope=executor(setup)
    def fail(*args):raise RuntimeError('synthetic collection error')
    setup[6].hooks['collect']=fail
    response=adapter(context,REQUEST,threading.Event())
    assert response.response.status==503 and response.outer_cleanup_confirmed
    assert not setup[6].objects


def test_material_failure_never_upgrades_physical_clean_state(setup):
    adapter,context,envelope=executor(setup);runtime=setup[6]
    def pending(_):raise RuntimeError('synthetic material still present')
    runtime.after_request_cleanup=pending
    result=adapter(context,REQUEST,threading.Event())
    assert result.response.status==503 and result.outer_cleanup_confirmed is False
    assert not runtime.objects
    with setup[0]._connect() as db:
        assert db.execute('SELECT state FROM provider_dispatch').fetchone()[0]=='cleaned'
    assert sum(e[0]=='cleanup' for e in runtime.events)==1
    assert setup[3].current(setup[4].account_id)['state']=='held'


def test_material_retry_can_confirm_cleanup_without_rewriting_failure(setup):
    adapter,context,envelope=executor(setup);runtime=setup[6];calls=[]
    def material(_):
        calls.append(True)
        if len(calls)<3:raise RuntimeError('synthetic transient material failure')
    runtime.after_request_cleanup=material
    result=adapter(context,REQUEST,threading.Event())
    assert result.response.status==503 and result.outer_cleanup_confirmed is True
    assert len(calls)==3 and sum(e[0]=='cleanup' for e in runtime.events)==1
    assert setup[3].current(setup[4].account_id)['state']=='held'
