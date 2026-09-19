from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import pytest
from test_provider_dispatch import setup
from test_supervised_executor import build
from test_hermes_inference_protocol import REQUEST
from cloudworkbench.supervised_executor import ExecutorQuiescenceError
from cloudworkbench.cancellation_supervisor import ShutdownReceipt,SupervisorError


def test_empty_executor_quiescence_is_bound_irreversible_and_not_cleanup(setup):
    wrapped,context=build(setup)
    receipt=wrapped.quiesce()
    assert receipt.binding==wrapped.executor.binding
    assert receipt.reservation==setup[4] and receipt.grant_id==setup[5]
    assert receipt.controller_instance_id==setup[3].instance_id
    assert receipt.executor_stopped and receipt.supervisors_stopped
    assert not wrapped._fenced
    assert wrapped.quiesce()==receipt
    assert not wrapped.authorize(context.binding)
    assert wrapped(context,REQUEST,threading.Event()).response.status==503
    assert not setup[6].events
    assert setup[3].current(setup[4].account_id)['state']=='held'


def test_quiesce_busy_closes_new_admission_then_can_retry_after_drain(setup):
    wrapped,context=build(setup);entered=threading.Event();release=threading.Event()
    def collect(*unused):entered.set();assert release.wait(3)
    setup[6].hooks['collect']=collect
    with ThreadPoolExecutor(max_workers=1) as pool:
        active=pool.submit(wrapped,context,REQUEST,threading.Event())
        assert entered.wait(2)
        try:
            with pytest.raises(ExecutorQuiescenceError,match='executor_quiesce_busy'):wrapped.quiesce()
            assert not wrapped.authorize(context.binding)
        finally:release.set()
        active.result(timeout=3)
    receipt=wrapped.quiesce()
    assert receipt.executor_stopped and receipt.supervisors_stopped
    assert not wrapped._fenced
    before=list(setup[6].events)
    assert wrapped(context,REQUEST,threading.Event()).response.status==503
    assert setup[6].events==before


def test_sticky_unstopped_observer_cannot_be_hidden_by_last_receipt(setup):
    wrapped,context=build(setup)
    class FailedObserver:
        receipt=ShutdownReceipt(False,True,'supervisor_shutdown_failed',False,1)
        def __init__(self,*a,**kw):pass
        def __enter__(self):raise SupervisorError('supervisor_shutdown_failed')
        def __exit__(self,*a):pass
        def _database(self,*a,**kw):raise RuntimeError('synthetic unavailable')
    wrapped.supervisor_factory=FailedObserver
    assert wrapped(context,REQUEST,threading.Event()).response.status!=200
    assert wrapped._fenced
    wrapped.last_receipt=ShutdownReceipt(True,False,None,True,2)
    receipt=wrapped.quiesce()
    assert receipt.executor_stopped and not receipt.supervisors_stopped
    assert not wrapped.authorize(context.binding)


def test_inflight_authorization_cannot_escape_after_quiesce(setup,monkeypatch):
    wrapped,context=build(setup);entered=threading.Event();release=threading.Event()
    def authorize(_):entered.set();assert release.wait(3);return True
    monkeypatch.setattr(wrapped.executor,'authorize',authorize)
    with ThreadPoolExecutor(max_workers=1) as pool:
        checking=pool.submit(wrapped.authorize,context.binding)
        assert entered.wait(2)
        try:assert wrapped.quiesce().executor_stopped
        finally:release.set()
        assert checking.result(timeout=2) is False
