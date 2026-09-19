from dataclasses import replace
import hashlib
import threading
from types import SimpleNamespace

import pytest

from test_provider_dispatch import setup
from test_supervised_executor import build
from test_hermes_inference_protocol import REQUEST
from cloudworkbench.provider_dispatch import Resources, OperationResolution
from cloudworkbench.provider_executor import ProviderExecutor, _failure
from cloudworkbench.inference_relay import _json


def settled(spec,row):
    return OperationResolution(spec.lease.request_id,spec.launch_nonce,row['version'],row['operation'],
        'completed',Resources(*(row[k] for k in ('provider_id','gateway_id','internal_network_id','external_network_id'))),'a'*64)


def fail_start(*unused):
    raise RuntimeError('synthetic start result lost')


def test_settled_failure_recovers_after_observer_stops_preserves_response(setup,monkeypatch):
    wrapped,context=build(setup);runtime=setup[6];runtime.hooks['start']=fail_start;runtime.resolution=settled
    original=ProviderExecutor.__call__;results=[];observations=[]
    def capture(self,*args):
        value=original(self,*args);results.append(value);return value
    monkeypatch.setattr(ProviderExecutor,'__call__',capture)
    original_cleanup=setup[3].verifier
    def cleanup(target):
        observations.append((wrapped.last_receipt.stopped,wrapped._current))
        return original_cleanup(target)
    setup[3].verifier=cleanup
    result=wrapped(context,REQUEST,threading.Event())
    assert not results[0].outer_cleanup_confirmed
    assert result.response==results[0].response and result.response.status!=200
    assert result.outer_cleanup_confirmed and wrapped.last_recovery_receipt.cleanup_confirmed
    assert observations==[(True,None)] and not runtime.objects
    assert len([e for e in runtime.events if e[0]=='start'])==1
    assert setup[3].current('account')['reservation']==setup[4]


def test_cancel_post_start_race_cleans_provable_scope_without_success(setup):
    wrapped,context=build(setup);cancel=threading.Event();runtime=setup[6]
    def active(spec,resources):
        cancel.set();assert wrapped.cancel_check(spec)
        # Bind this positive case to revocation before the post-start check.
        # The separate observer-exit test covers revocation racing cleanup CAS.
        observer=wrapped._current
        assert observer._database(observer._revoke,track=False)
    runtime.hooks['start']=active
    result=wrapped(context,REQUEST,cancel)
    assert result.response.status!=200 and result.outer_cleanup_confirmed
    assert wrapped.last_receipt.stopped and wrapped.last_receipt.cancelled
    assert not runtime.objects and not any(e[0]=='collect' for e in runtime.events)
    assert setup[3].current('account')['reservation']==setup[4]


def test_unknown_operation_stays_quarantined_without_cleanup_or_retry(setup):
    wrapped,context=build(setup);runtime=setup[6];runtime.hooks['start']=fail_start
    result=wrapped(context,REQUEST,threading.Event())
    assert result.response.status!=200 and not result.outer_cleanup_confirmed
    assert wrapped.last_recovery_receipt is None and runtime.objects
    assert len([e for e in runtime.events if e[0]=='start'])==1
    assert not any(e[0]=='cleanup' for e in runtime.events)
    with setup[0]._connect() as db:row=db.execute('SELECT state,uncertain FROM provider_dispatch').fetchone()
    assert row['state']=='quarantined' and row['uncertain']==1


@pytest.mark.parametrize('change',['nonce','payload','context_digest','generation','profile','grant','reservation'])
def test_foreign_or_changed_call_cannot_recover_existing_request(setup,change):
    wrapped,context=build(setup);runtime=setup[6];runtime.hooks['start']=fail_start
    result=wrapped(context,REQUEST,threading.Event());runtime.resolution=settled
    before=list(runtime.events);payload=REQUEST
    if change=='nonce':context=replace(context,request_nonce='e'*64)
    elif change=='payload':
        payload={**REQUEST,'messages':[{'role':'user','content':'different request'}]}
        context=replace(context,payload_digest=hashlib.sha256(_json(payload)).hexdigest())
    elif change=='context_digest':context=replace(context,payload_digest='f'*64)
    elif change=='generation':context=replace(context,binding=replace(context.binding,generation=2))
    elif change=='profile':context=replace(context,binding=replace(context.binding,profile_digest='f'*64))
    elif change=='grant':wrapped.executor.grant_id='foreign'
    else:wrapped.executor.reservation=replace(wrapped.executor.reservation,reservation_id='foreign')
    assert wrapped._recover_failed_call(context,payload,result)==result
    assert runtime.events==before and runtime.objects


def test_running_call_is_not_a_recovery_candidate(setup):
    from cloudworkbench.hermes_inference_protocol import admit_request
    wrapped,context=build(setup);executor=wrapped.executor;runtime=setup[6]
    transcript=admit_request(REQUEST,executor.profile).request_json
    spec=executor.dispatch.admit(executor.reservation,executor.grant_id,request_nonce=context.request_nonce,
        payload=transcript,profile_digest=context.binding.profile_digest)
    executor.dispatch.launch(spec.lease.request_id,transcript)
    before=list(runtime.events);result=_failure('synthetic_failure')
    assert wrapped._recover_failed_call(context,REQUEST,result)==result
    assert runtime.events==before and runtime.objects


def test_stuck_observer_never_invokes_recovery(setup,monkeypatch):
    from cloudworkbench.cancellation_supervisor import SupervisorError
    import cloudworkbench.supervised_executor as module
    wrapped,context=build(setup);setup[6].hooks['start']=fail_start;setup[6].resolution=settled
    class Stuck:
        receipt=None
        def __enter__(self):return self
        def raise_if_cancelled(self):pass
        def __exit__(self,*args):
            self.receipt=SimpleNamespace(stopped=False)
            raise SupervisorError('supervisor_shutdown_failed')
        def _database(self,callback,**kwargs):
            with setup[0]._tx() as db:return callback(db)
    wrapped.supervisor_factory=lambda *args,**kwargs:Stuck()
    calls=[];monkeypatch.setattr(module,'recover_request',lambda *args,**kwargs:calls.append(True))
    result=wrapped(context,REQUEST,threading.Event())
    assert result.response.status!=200 and not result.outer_cleanup_confirmed
    assert wrapped._fenced and not calls and setup[6].objects
    assert not wrapped.authorize(context.binding)


def test_cleanup_failure_retains_original_failure_and_ownership(setup):
    wrapped,context=build(setup);runtime=setup[6];runtime.hooks['start']=fail_start;runtime.resolution=settled
    setup[3].verifier=lambda target:False
    result=wrapped(context,REQUEST,threading.Event())
    assert b'provider_dispatch_failed' in result.response.body
    assert not result.outer_cleanup_confirmed and runtime.objects
    assert wrapped.last_recovery_receipt is None
    assert setup[3].current('account')['reservation']==setup[4]


def test_execution_lock_remains_held_through_recovery_cleanup(setup):
    from concurrent.futures import ThreadPoolExecutor
    wrapped,context=build(setup);runtime=setup[6];runtime.hooks['start']=fail_start;runtime.resolution=settled
    entered,release=threading.Event(),threading.Event();original=setup[3].verifier
    def blocked_cleanup(target):
        entered.set();assert release.wait(3);return original(target)
    setup[3].verifier=blocked_cleanup
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(wrapped,context,REQUEST,threading.Event())
        try:
            assert entered.wait(2)
            other=wrapped(replace(context,request_nonce='d'*64),REQUEST,threading.Event())
            assert b'attempt_execution_busy' in other.response.body
            assert len([e for e in runtime.events if e[0]=='create'])==1
        finally:release.set()
        result=first.result(timeout=2)
    assert result.outer_cleanup_confirmed and result.response.status!=200 and not runtime.objects


@pytest.mark.parametrize('revoke_during_cleanup', [False, True])
def test_cancelled_observer_exit_does_not_replace_executor_failure(setup,monkeypatch,revoke_during_cleanup):
    wrapped,context=build(setup);cancel=threading.Event();original=ProviderExecutor.__call__;seen=[]
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    observers=[];factory=wrapped.supervisor_factory
    def instrumented_factory(*args,**kwargs):
        observer=factory(*args,**kwargs);revoke=observer._revoke
        def controlled_revoke(db):
            if threading.current_thread() is observer._thread:
                entered.set()
                assert release.wait(2)
                result=revoke(db);revoked.set();return result
            return revoke(db)
        observer._revoke=controlled_revoke;observers.append(observer);return observer
    wrapped.supervisor_factory=instrumented_factory
    def capture(self,*args):
        result=original(self,*args);seen.append(result);return result
    monkeypatch.setattr(ProviderExecutor,'__call__',capture)
    def active(spec,resources):
        cancel.set();assert wrapped.cancel_check(spec)
        observers[0]._wake.set();assert entered.wait(2)
        if not revoke_during_cleanup:
            release.set();assert revoked.wait(2)
    setup[6].hooks['start']=active
    original_cleanup=setup[3].verifier
    def cleanup(target):
        receipt=original_cleanup(target)
        if revoke_during_cleanup:
            # Force revocation between physical removal and cleanup's CAS.
            release.set();assert revoked.wait(2)
        return receipt
    setup[3].verifier=cleanup
    try:
        result=wrapped(context,REQUEST,cancel)
    finally:
        release.set()
        for observer in observers:
            if observer._thread is not None:
                observer._thread.join(2);assert not observer._thread.is_alive()
    assert wrapped.last_receipt.cancelled and wrapped.last_receipt.stopped
    assert result.response==seen[0].response and result.response.status!=200
    assert not setup[6].objects
    assert setup[3].current('account')['reservation']==setup[4]
    with setup[0]._connect() as db:
        row=db.execute('SELECT state,uncertain,operation FROM provider_dispatch').fetchone()
        lease=db.execute('SELECT state,cleanup_receipt FROM provider_request_leases').fetchone()
    if revoke_during_cleanup:
        assert not result.outer_cleanup_confirmed
        assert tuple(row)==('cleaning',1,'cleanup')
        assert lease['state']=='cleaning' and lease['cleanup_receipt'] is None
        assert wrapped.last_recovery_error=='recovery_candidate_not_found'
        assert wrapped.last_recovery_receipt is None
    else:
        assert result.outer_cleanup_confirmed
        assert tuple(row)==('cleaned',0,None)
        assert lease['state']=='released' and lease['cleanup_receipt'] is not None


@pytest.mark.parametrize('field',['generation','profile_digest'])
def test_binding_mismatch_reaches_sql_discriminators(setup,field):
    wrapped,context=build(setup);runtime=setup[6];runtime.hooks['start']=fail_start
    result=wrapped(context,REQUEST,threading.Event());runtime.resolution=settled
    context=replace(context,binding=replace(context.binding,**{field:2 if field=='generation' else 'f'*64}))
    wrapped.executor.binding=context.binding
    before=list(runtime.events)
    assert wrapped._recover_failed_call(context,REQUEST,result)==result
    assert wrapped.last_recovery_error=='recovery_candidate_not_found'
    assert runtime.events==before and runtime.objects


@pytest.mark.parametrize('change',['state','grant'])
def test_helper_rechecks_after_lookup_before_cleanup(setup,monkeypatch,change):
    import cloudworkbench.supervised_executor as module
    wrapped,context=build(setup);runtime=setup[6];runtime.hooks['start']=fail_start
    result=wrapped(context,REQUEST,threading.Event());runtime.resolution=settled
    original=module.recover_request;before=list(runtime.events)
    def raced(dispatch,request_id,**kwargs):
        if change=='state':
            with setup[0]._tx() as db:db.execute("UPDATE provider_dispatch SET state='running',uncertain=0 WHERE request_id=?",(request_id,))
        else:
            foreign=setup[3].issue_grant(setup[4],attempt_id=context.binding.attempt_id,generation=context.binding.generation)
            with setup[0]._tx() as db:db.execute('UPDATE provider_request_leases SET grant_id=? WHERE id=?',(foreign,request_id))
        return original(dispatch,request_id,**kwargs)
    monkeypatch.setattr(module,'recover_request',raced)
    assert wrapped._recover_failed_call(context,REQUEST,result)==result
    assert wrapped.last_recovery_error==('recovery_requires_quarantine' if change=='state' else 'recovery_binding_invalid')
    assert runtime.events==before and runtime.objects


def test_unsettled_reconcile_stub_still_cannot_reach_physical_cleanup(setup,monkeypatch):
    wrapped,context=build(setup);runtime=setup[6];runtime.hooks['start']=fail_start
    result=wrapped(context,REQUEST,threading.Event());before=list(runtime.events)
    monkeypatch.setattr(wrapped.executor.dispatch,'reconcile',lambda request_id:None)
    assert wrapped._recover_failed_call(context,REQUEST,result)==result
    assert wrapped.last_recovery_error=='operation_still_unknown'
    assert runtime.events==before and runtime.objects


def test_recovery_unexpected_error_is_bounded_and_not_raw_text(setup,monkeypatch):
    import cloudworkbench.supervised_executor as module
    wrapped,context=build(setup);setup[6].hooks['start']=fail_start
    result=wrapped(context,REQUEST,threading.Event())
    def failure(*args,**kwargs):raise RuntimeError('SYNTHETIC-PRIVATE-ERROR-TEXT')
    monkeypatch.setattr(module,'recover_request',failure)
    assert wrapped._recover_failed_call(context,REQUEST,result)==result
    assert wrapped.last_recovery_error=='recovery_failed'
    assert b'SYNTHETIC-PRIVATE' not in result.response.body


def test_real_observer_join_timeout_does_not_recover(setup,monkeypatch):
    import cloudworkbench.cancellation_supervisor as supervisor_module
    import cloudworkbench.supervised_executor as module
    wrapped,context=build(setup);entered,release=threading.Event(),threading.Event();supervisors=[];calls=[]
    original_factory=wrapped.supervisor_factory
    def factory(*args,**kwargs):
        supervisor=original_factory(*args,**kwargs);original_poll=supervisor._poll
        def stalled(*,final=False):
            if threading.current_thread() is not threading.main_thread():entered.set();release.wait(3)
            else:original_poll(final=final)
        supervisor._poll=stalled;supervisors.append(supervisor);return supervisor
    wrapped.supervisor_factory=factory
    monkeypatch.setattr(supervisor_module,'JOIN_SECONDS',.03)
    monkeypatch.setattr(module,'recover_request',lambda *args,**kwargs:calls.append(True))
    def active(*args):
        supervisors[0]._wake.set();assert entered.wait(1);raise RuntimeError('synthetic start interruption')
    setup[6].hooks['start']=active
    try:
        result=wrapped(context,REQUEST,threading.Event())
        assert result.response.status!=200 and not result.outer_cleanup_confirmed
        assert wrapped._fenced and not wrapped.last_receipt.stopped and not calls
        with setup[0]._connect() as db:row=db.execute('SELECT state FROM provider_reservations WHERE id=?',(setup[4].reservation_id,)).fetchone()
        assert row['state']=='quarantined'
    finally:
        release.set()
        for supervisor in supervisors:supervisor._thread.join(1);assert not supervisor._thread.is_alive()
