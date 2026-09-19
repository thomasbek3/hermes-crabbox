"""Actual relay/lease/store/dispatch composition with synthetic runtime effects."""
import hashlib
import json
import threading
from dataclasses import replace

import pytest
from test_provider_dispatch import setup
from cloudworkbench.inference_relay import AttemptBinding, DispatchResult, WorkerDispatcher, _json
from cloudworkbench.inference_service import ServiceResponse


def build(setup, tmp_path, *, cleanup_fail=False):
    store,principal,attempt,leases,owner,grant,runtime,dispatch,clock=setup
    binding=AttemptBinding(attempt['attempt_id'],attempt['generation'],'b'*64)
    capability='e'*64
    body={'messages':[{'role':'user','content':'synthetic'}],'tools':[]}
    payload=_json(body)
    nonce='f'*64
    contexts=[]
    def execute(context, value, cancel):
        contexts.append(context)
        assert context.binding==binding
        spec=dispatch.admit(owner,grant,request_nonce=context.request_nonce,
                            payload=_json(value),profile_digest=context.binding.profile_digest)
        dispatch.launch(spec.lease.request_id,_json(value))
        dispatch.collect(spec.lease.request_id)
        dispatch.cleanup(spec.lease.request_id)
        result=dispatch.deliver(spec.lease.request_id)
        return DispatchResult(ServiceResponse(200,'application/json',result),True)
    if cleanup_fail:
        def fail(target):raise RuntimeError('synthetic cleanup unavailable')
        leases.verifier=fail
    worker=WorkerDispatcher(journal_path=tmp_path/'worker.db',binding=binding,
        capability_sha256=hashlib.sha256(capability.encode()).hexdigest(),
        authorize=lambda _:True,
        execute_request=execute)
    request={'binding':binding.wire(),'capability':capability,'nonce':nonce,
             'payload_digest':hashlib.sha256(payload).hexdigest(),'payload':body}
    return worker,request,contexts


def test_relay_nonce_reaches_durable_dispatch_and_cleanup_precedes_response(setup,tmp_path):
    worker,request,contexts=build(setup,tmp_path)
    try:
        result=worker.dispatch(request,threading.Event())
        assert result['response']['status']==200
        again=worker.dispatch(request,threading.Event())
        assert result==again and len(contexts)==1
        store,_,_,leases,owner,_,runtime,dispatch,_=setup
        with store._connect() as db:
            row=dict(db.execute('SELECT * FROM provider_dispatch').fetchone())
        assert row['request_nonce']==request['nonce'] and row['state']=='delivered'
        assert not runtime.objects
        assert leases.current('account')['reservation']==owner
        assert len([e for e in runtime.events if e[0]=='create'])==1
    finally:worker.close()


def test_cleanup_failure_never_returns_provider_response_or_reexecutes(setup,tmp_path):
    worker,request,contexts=build(setup,tmp_path,cleanup_fail=True)
    try:
        result=worker.dispatch(request,threading.Event())
        assert result['response']['status']!=200
        assert worker.dispatch(request,threading.Event())==result
        store,_,_,leases,owner,_,runtime,dispatch,_=setup
        with store._connect() as db:
            row=dict(db.execute('SELECT * FROM provider_dispatch').fetchone())
        assert row['state']=='quarantined' and row['uncertain']==1
        assert runtime.objects and leases.current('account')['reservation']==owner
        assert len(contexts)==1 and len([e for e in runtime.events if e[0]=='create'])==1
    finally:worker.close()
