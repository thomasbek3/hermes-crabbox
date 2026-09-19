"""A routed observer join miss retains ownership, not permission to reuse it."""
import hashlib
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_routed_cleanup import cleanup, driven, stage, setup
from tests.test_routed_driver import run
from tests.test_native_provider_executor import request
from cloudworkbench import cancellation_supervisor as supervisor_module
from cloudworkbench.inference_relay import DispatchContext, _json
from cloudworkbench.provider_leases import LeaseError, ProviderLeases
from cloudworkbench.routed_cleanup import RoutedChildCleanup, ChildCleanupError
from cloudworkbench.store import StoreError


def test_real_routed_observer_join_miss_retains_root_account_and_blocks_reuse(cleanup, monkeypatch):
    value = cleanup
    scheduler, wrapped = value['scheduler'], value['wrapped']
    reservation = value['reservation']
    wrapped.executor.remaining_seconds = lambda: 160
    child = value['driven'][2]
    root_id = child['workflow_root_id']
    cancel, entered, release = threading.Event(), threading.Event(), threading.Event()
    observers, results = [], []
    # The routed fixture uses a frozen wall clock. Keep real monotonic time and
    # the production 500 ms join bound; no shortened join or fake receipt.
    monkeypatch.setattr(supervisor_module, 'time', SimpleNamespace(
        time=lambda: 1000, monotonic=time.monotonic))
    factory = wrapped.supervisor_factory

    def slow_factory(*args, **kwargs):
        observer = factory(*args, **kwargs)
        revoke = observer._revoke

        def slow_revoke(db):
            if threading.current_thread() is observer._thread:
                entered.set()
                assert release.wait(5), 'test must release the blocked observer'
            return revoke(db)

        observer._revoke = slow_revoke
        observers.append(observer)
        return observer

    wrapped.supervisor_factory = slow_factory

    def provider_started(spec, resources):
        cancel.set()
        observers[0]._wake.set()
        assert entered.wait(2)

    value['provider'].hooks['start'] = provider_started
    payload = request(wrapped.executor.profile)
    context = DispatchContext(value['binding'], 'c' * 64,
                              hashlib.sha256(_json(payload)).hexdigest())
    start_process = value['runtime'].start_caller_process

    def start_and_request(spec, runtime_id, *, role):
        result = start_process(spec, runtime_id, role=role)
        if role == 'hermes':
            results.append(wrapped(context, payload, cancel))
        return result

    monkeypatch.setattr(value['runtime'], 'start_caller_process', start_and_request)
    try:
        outcome = run(value['driven'])
        assert len(results) == 1 and results[0].response.status >= 400
        assert len(observers) == 1 and observers[0]._thread.is_alive(), (results[0].response.body, value['provider'].events, wrapped.last_receipt)
        receipt = wrapped.last_receipt
        assert receipt.stopped is False and receipt.cancelled is True
        assert receipt.durable_revocation_confirmed is True
        assert wrapped._fenced and not wrapped.authorize(value['binding'])
        assert outcome.cleanup_error == 'grant_fence_unconfirmed'
        assert outcome.caller_cleanup['caller_removed'] is True
        assert not outcome.provider_cleanup_qualified and not outcome.scheduler_seat_released
        with pytest.raises(ChildCleanupError, match='caller_cleanup_unconfirmed'):
            RoutedChildCleanup(scheduler, value['runtime'], value['spec'], outcome,
                               services=(value['service'],))

        current = scheduler.leases.current('account')
        assert current['state'] == 'quarantined'
        assert current['reservation'] == reservation
        with scheduler.store._connect() as db:
            account = db.execute('SELECT * FROM provider_accounts WHERE account_id=?', ('account',)).fetchone()
            root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?', (root_id,)).fetchone()
            assert account['active_id'] == reservation.reservation_id
            assert account['epoch'] == reservation.epoch
            assert account['persistent_owner_id'] == reservation.persistent_owner_id
            assert root['state'] == 'held' and root['child_attempt_id'] == child['id']
            assert not scheduler._owner_current(db, root_id)
            assert db.execute('SELECT revoked_at FROM provider_execution_grants WHERE id=?',
                              (value['grant'],)).fetchone()[0] is not None
        assert scheduler.claim_child(root_id) is None
        foreign = ProviderLeases(scheduler.store, cleanup_verifier=lambda _: None,
                                 inspector_id='foreign-controller', clock=lambda: 1000)
        with pytest.raises(LeaseError, match='account_reserved'):
            foreign.reserve('account', persistent_owner_id=reservation.persistent_owner_id)
        callbacks = []
        with pytest.raises(StoreError, match='Child cleanup target changed'):
            scheduler.release_child(child['id'], expected_generation=1,
                                    verifier=lambda target: callbacks.append(target))
        assert not callbacks
        assert value['service'].close(timeout_seconds=2).resources_closed
        quiescence = wrapped.quiesce()
        assert quiescence.executor_stopped and not quiescence.supervisors_stopped
    finally:
        release.set()
        for observer in observers:
            observer._thread.join(2)
            assert not observer._thread.is_alive()

    # A later observer exit cannot retroactively bless the failed receipt or
    # clear the persistent fence. Recovery requires an explicit exact-scope act.
    assert not wrapped.quiesce().supervisors_stopped
    assert scheduler.leases.current('account')['reservation'] == reservation
    assert scheduler.leases.current('account')['state'] == 'quarantined'
    assert scheduler.claim_child(root_id) is None
