from dataclasses import replace
import threading
import time
import pytest
from test_provider_dispatch import setup
from test_provider_executor import executor
from test_provider_budget_integration import configure
from test_hermes_inference_protocol import REQUEST
from cloudworkbench.supervised_executor import SupervisedProviderExecutor


def build(setup):
    now=time.time();setup[8][0]=now
    original_cleanup=setup[6].cleanup
    setup[3].verifier=lambda target: replace(original_cleanup(target), observed_at=setup[8][0])
    with setup[0]._tx() as db:db.execute('UPDATE provider_execution_grants SET expires_at=?',(now+3600,))
    adapter,context,envelope=executor(setup)
    adapter.dispatch=configure(setup)[0]
    wrapped=SupervisedProviderExecutor(adapter)
    return wrapped,context


def test_budget_required_before_supervised_execution(setup):
    adapter,_,_=executor(setup)
    with pytest.raises(ValueError,match='budgeted_provider_executor_required'):
        SupervisedProviderExecutor(adapter)


def test_success_has_stopped_supervisor_before_return(setup):
    wrapped,context=build(setup)
    output=wrapped(context,REQUEST,threading.Event())
    assert output.response.status==200 and output.outer_cleanup_confirmed
    assert wrapped.last_receipt.stopped and not wrapped.last_receipt.cancelled
    assert not setup[6].objects
    assert wrapped.cancel_check(None)


def test_cancel_during_active_start_reaches_runtime_callback(setup):
    wrapped,context=build(setup);cancel=threading.Event();observed=[]
    revoked=threading.Event();observers=[];factory=wrapped.supervisor_factory
    def instrumented_factory(*args,**kwargs):
        observer=factory(*args,**kwargs);poll=observer._poll
        def tracked_poll(*,final=False):
            result=poll(final=final)
            if observer._revoked:revoked.set()
            return result
        observer._poll=tracked_poll;observers.append(observer);return observer
    wrapped.supervisor_factory=instrumented_factory
    def active(spec,resources):
        cancel.set()
        assert wrapped.cancel_check(spec)
        observed.append(True)
        observers[0]._wake.set()
        assert revoked.wait(2), 'observer revocation did not commit'
    setup[6].hooks['start']=active
    output=wrapped(context,REQUEST,cancel)
    assert observed==[True] and output.response.status!=200
    assert wrapped.last_receipt.stopped and wrapped.last_receipt.cancelled
    assert wrapped.last_receipt.durable_revocation_confirmed
    # This test pins revocation before cleanup; the competing cleanup-CAS order
    # is exercised separately in test_supervised_recovery.
    assert output.outer_cleanup_confirmed and not setup[6].objects
    with setup[0]._connect() as db:
        lease=db.execute('SELECT state,cleanup_receipt FROM provider_request_leases').fetchone()
        assert lease['state']=='released' and lease['cleanup_receipt'] is not None


def test_final_grant_revocation_cannot_escape_as_success(setup):
    wrapped,context=build(setup)
    original=wrapped.executor.dispatch.deliver
    def deliver(request_id):
        value=original(request_id)
        setup[3].revoke_grant(setup[5])
        return value
    wrapped.executor.dispatch.deliver=deliver
    output=wrapped(context,REQUEST,threading.Event())
    assert output.response.status==409 and wrapped.last_receipt.cancelled
    assert not setup[6].objects


def test_failed_cleanup_after_cancellation_is_not_confirmed(setup):
    wrapped,context=build(setup);cancel=threading.Event()
    def active(spec,resources):
        cancel.set()
        assert wrapped.cancel_check(spec)
    def failed_cleanup(target):
        raise RuntimeError('synthetic cleanup failure')
    setup[6].hooks['start']=active
    setup[3].verifier=failed_cleanup
    output=wrapped(context,REQUEST,cancel)
    assert output.response.status!=200
    assert not output.outer_cleanup_confirmed
    assert setup[6].objects
    assert wrapped.last_receipt.stopped and wrapped.last_receipt.cancelled
