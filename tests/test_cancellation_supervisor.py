from dataclasses import replace
from types import SimpleNamespace
import sqlite3
import threading
import time

import pytest
from test_provider_dispatch import setup, admit
from cloudworkbench.cancellation_supervisor import CancellationSupervisor, SupervisorError
from cloudworkbench.inference_relay import AttemptBinding
from cloudworkbench.provider_leases import LeaseError


@pytest.fixture
def ready(setup):
    store, principal, attempt, leases, owner, grant, runtime, dispatch, clock = setup
    clock[0] = time.time()
    with store._tx() as db:
        db.execute('UPDATE provider_execution_grants SET expires_at=? WHERE id=?', (clock[0]+1000, grant))
    return setup


def make(ready, **kwargs):
    store, _, attempt, _, owner, grant, *_ = ready
    return CancellationSupervisor(store, reservation=owner, grant_id=grant,
        binding=AttemptBinding(attempt['attempt_id'], attempt['generation'], 'b'*64),
        controller_instance_id=ready[3].instance_id,
        caller_cancel=kwargs.pop('caller_cancel', threading.Event()), **kwargs)


def wait_cancel(supervisor):
    assert supervisor._cancelled.wait(.8), 'cancellation observer did not settle'
    deadline = time.monotonic()+.5
    while not supervisor._revoked and time.monotonic()<deadline:
        time.sleep(.005)


def grant_row(ready):
    with ready[0]._connect() as db:
        return dict(db.execute('SELECT * FROM provider_execution_grants WHERE id=?', (ready[5],)).fetchone())


def test_clean_context_joins_and_preserves_grant(ready):
    spec = admit(ready)
    with make(ready) as supervisor:
        assert not supervisor.cancel_check(spec)
        assert supervisor._thread.daemon is False
    assert not supervisor._thread.is_alive()
    assert supervisor.receipt.stopped and not supervisor.receipt.cancelled
    assert supervisor.close() == supervisor.receipt
    assert supervisor.cancel_check(spec)
    assert grant_row(ready)['revoked_at'] is None


@pytest.mark.parametrize('source', ['caller', 'disconnect', 'durable_attempt', 'durable_grant', 'durable_client'])
def test_cancellation_detected_and_exact_grant_revoked(ready, source):
    cancel, disconnect = threading.Event(), threading.Event()
    supervisor = make(ready, caller_cancel=cancel, disconnect=disconnect)
    spec = admit(ready)
    supervisor.__enter__()
    began = time.monotonic()
    try:
        if source == 'caller': cancel.set()
        elif source == 'disconnect': disconnect.set()
        else:
            with ready[0]._tx() as db:
                if source == 'durable_attempt': db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (ready[2]['attempt_id'],))
                if source == 'durable_grant': db.execute('UPDATE provider_execution_grants SET revoked_at=? WHERE id=?', (time.time(), ready[5]))
                if source == 'durable_client': db.execute("UPDATE clients SET revoked_at='revoked' WHERE id=?", (ready[1]['id'],))
        wait_cancel(supervisor)
        assert time.monotonic()-began < .6
        assert supervisor.cancel_check(spec)
    finally:
        with pytest.raises(SupervisorError): supervisor.close()
    assert supervisor.receipt.stopped and supervisor.receipt.durable_revocation_confirmed
    with ready[0]._connect() as db:
        assert db.execute('SELECT state FROM provider_request_leases WHERE id=?', (spec.lease.request_id,)).fetchone()[0] == 'quarantined'
        assert db.execute('SELECT cancel_requested FROM provider_dispatch WHERE request_id=?', (spec.lease.request_id,)).fetchone()[0] == 1


@pytest.mark.parametrize('change', ['client_revoked','attempt_cancelled','attempt_terminal','generation','grant_expired','grant_revoked','reservation_state','active_owner','epoch','controller','persistent_owner','client_projects'])
def test_predicate_parity_with_provider_leases(ready, change):
    store, principal, attempt, leases, owner, grant, *_ = ready
    statements = {
        'client_revoked': ("UPDATE clients SET revoked_at='revoked' WHERE id=?", principal['id']),
        'attempt_cancelled': ('UPDATE attempts SET cancel_requested=1 WHERE id=?', attempt['attempt_id']),
        'attempt_terminal': ("UPDATE attempts SET state='failed' WHERE id=?", attempt['attempt_id']),
        'generation': ('UPDATE attempts SET generation=generation+1 WHERE id=?', attempt['attempt_id']),
        'grant_expired': ('UPDATE provider_execution_grants SET expires_at=1 WHERE id=?', grant),
        'grant_revoked': ('UPDATE provider_execution_grants SET revoked_at=1 WHERE id=?', grant),
        'reservation_state': ("UPDATE provider_reservations SET state='quarantined' WHERE id=?", owner.reservation_id),
        'active_owner': ('UPDATE provider_accounts SET active_id=NULL WHERE account_id=?', owner.account_id),
        'epoch': ('UPDATE provider_reservations SET epoch=epoch+1 WHERE id=?', owner.reservation_id),
        'controller': ("UPDATE provider_reservations SET controller_instance_id='other' WHERE id=?", owner.reservation_id),
        'persistent_owner': ("UPDATE provider_accounts SET persistent_owner_id='other' WHERE account_id=?", owner.account_id),
        'client_projects': ("UPDATE clients SET projects='[]' WHERE id=?", principal['id']),
    }
    sql, value = statements[change]
    with store._tx() as db: db.execute(sql, (value,))
    supervisor = make(ready)
    observed = supervisor._database(supervisor._authorized)
    try:
        with store._tx() as db:
            leases._reservation(db, owner, held=True)
            expected = leases._grant_error(db, grant, owner.reservation_id, leases._now()) is None
    except LeaseError: expected = False
    assert observed == expected
    # Existing execution grant semantics do not re-evaluate a client's project list.
    assert observed == (change == 'client_projects')


def test_pre_cancel_fails_before_start_and_no_thread(ready):
    event = threading.Event(); event.set()
    supervisor = make(ready, caller_cancel=event)
    with pytest.raises(SupervisorError, match='caller_cancelled'): supervisor.__enter__()
    assert supervisor._thread is None and supervisor.receipt.stopped
    assert grant_row(ready)['revoked_at'] is not None


def test_final_check_prevents_success_after_cancel_at_context_exit(ready):
    event = threading.Event()
    def operation():
        with make(ready, caller_cancel=event):
            event.set()
            return 'must not return success'
    with pytest.raises(SupervisorError, match='caller_cancelled'): operation()


def test_final_durable_check_prevents_stale_success(ready):
    supervisor = make(ready)
    with pytest.raises(SupervisorError, match='execution_not_authorized'):
        with supervisor:
            with ready[0]._tx() as db:
                db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (ready[2]['attempt_id'],))
    assert supervisor.receipt.durable_revocation_confirmed


def test_body_exception_revokes_and_joins(ready):
    supervisor = make(ready)
    with pytest.raises(RuntimeError, match='private exception text'):
        with supervisor: raise RuntimeError('private exception text')
    assert supervisor.receipt.stopped and supervisor.receipt.durable_revocation_confirmed


def test_cancel_check_is_pure_memory_and_rejects_foreign_spec(ready, monkeypatch):
    spec = admit(ready)
    with make(ready) as supervisor:
        with monkeypatch.context() as patch:
            patch.setattr(supervisor, '_database', lambda operation: (_ for _ in ()).throw(AssertionError('must not query')))
            assert not supervisor.cancel_check(spec)
            for other in (None, replace(spec, generation=2), replace(spec, profile_digest='a'*64),
                          replace(spec, lease=replace(spec.lease, grant_id='other'))):
                assert supervisor.cancel_check(other)
            assert not supervisor._cancelled.is_set()


def test_revoke_targets_only_bound_grant_and_request(ready):
    other_grant = ready[3].issue_grant(ready[4], attempt_id=ready[2]['attempt_id'], generation=ready[2]['generation'])
    event = threading.Event(); event.set()
    with pytest.raises(SupervisorError): make(ready, caller_cancel=event).__enter__()
    with ready[0]._connect() as db:
        assert db.execute('SELECT revoked_at FROM provider_execution_grants WHERE id=?', (other_grant,)).fetchone()[0] is None


def test_wrong_binding_never_revokes_someone_elses_grant(ready):
    supervisor = make(ready)
    supervisor.binding = replace(supervisor.binding, attempt_id='other')
    with pytest.raises(SupervisorError): supervisor.__enter__()
    assert not supervisor.receipt.durable_revocation_confirmed
    assert grant_row(ready)['revoked_at'] is None


def test_locked_database_revocation_is_bounded_and_truthful(ready):
    event = threading.Event(); event.set()
    lock = sqlite3.connect(ready[0].path, timeout=.05)
    lock.execute('BEGIN IMMEDIATE')
    try:
        supervisor = make(ready, caller_cancel=event)
        start = time.monotonic()
        with pytest.raises(SupervisorError): supervisor.__enter__()
        assert time.monotonic()-start < .4
        assert supervisor.receipt.stopped and not supervisor.receipt.durable_revocation_confirmed
    finally:
        lock.rollback(); lock.close()
    assert grant_row(ready)['revoked_at'] is None


def test_missing_database_fails_closed_without_creating_file(ready, tmp_path):
    supervisor = make(ready)
    supervisor.path = tmp_path/'missing.db'
    with pytest.raises(SupervisorError, match='authorization_database_unavailable'): supervisor.__enter__()
    assert not supervisor.path.exists()
    assert not supervisor.receipt.durable_revocation_confirmed


def test_database_exception_text_not_exposed(ready, monkeypatch):
    supervisor = make(ready)
    monkeypatch.setattr(supervisor, '_database', lambda operation: (_ for _ in ()).throw(RuntimeError('PRIVATE')))
    with pytest.raises(SupervisorError) as error: supervisor.__enter__()
    assert 'PRIVATE' not in str(error.value)
    assert supervisor.receipt.reason == 'authorization_database_unavailable'


def test_retries_pending_revocation_when_lock_released(ready):
    spec = admit(ready); event = threading.Event()
    supervisor = make(ready, caller_cancel=event).__enter__()
    lock = sqlite3.connect(ready[0].path, timeout=.05); lock.execute('BEGIN IMMEDIATE')
    try:
        event.set(); assert supervisor.cancel_check(spec)
        assert supervisor._cancelled.wait(.1)
        time.sleep(.08)
        assert not supervisor._revoked
    finally: lock.rollback(); lock.close()
    wait_cancel(supervisor)
    with pytest.raises(SupervisorError): supervisor.close()
    assert supervisor.receipt.durable_revocation_confirmed


def test_shutdown_failure_never_success_or_daemon_cleanup_claim(ready, monkeypatch):
    import cloudworkbench.cancellation_supervisor as module
    entered, release = threading.Event(), threading.Event()
    supervisor = make(ready)
    original = supervisor._poll
    def stalled(*, final=False):
        if threading.current_thread() is not threading.main_thread():
            entered.set(); release.wait(2)
        else: original(final=final)
    monkeypatch.setattr(supervisor, '_poll', stalled)
    monkeypatch.setattr(module, 'JOIN_SECONDS', .03)
    supervisor.__enter__(); supervisor._wake.set()
    assert entered.wait(.5)
    try:
        start = time.monotonic()
        with pytest.raises(SupervisorError, match='supervisor_shutdown_failed'): supervisor.close()
        assert time.monotonic()-start < .15
        assert not supervisor.receipt.stopped and supervisor.receipt.cancelled
        with pytest.raises(SupervisorError, match='supervisor_shutdown_failed'): supervisor.close()
    finally:
        release.set(); supervisor._thread.join(.5)
    assert not supervisor._thread.is_alive()


def test_cancelled_observer_does_not_busy_spin(ready):
    event = threading.Event(); supervisor = make(ready, caller_cancel=event).__enter__()
    try:
        event.set(); wait_cancel(supervisor)
        polls = supervisor._polls
        time.sleep(.28)
        assert supervisor._polls-polls <= 2
    finally:
        with pytest.raises(SupervisorError): supervisor.close()


def test_reentry_refused(ready):
    supervisor = make(ready)
    with supervisor:
        with pytest.raises(SupervisorError, match='already_used'): supervisor.__enter__()
    with pytest.raises(SupervisorError, match='already_used'): supervisor.__enter__()


def test_thread_start_failure_revokes_and_never_enters(ready, monkeypatch):
    supervisor = make(ready)
    monkeypatch.setattr(threading.Thread, 'start', lambda self: (_ for _ in ()).throw(RuntimeError('synthetic')))
    with pytest.raises(SupervisorError, match='supervisor_start_failed'): supervisor.__enter__()
    assert supervisor.receipt.stopped and supervisor.receipt.durable_revocation_confirmed


def test_stale_observer_causes_local_adapter_cancellation(ready):
    spec = admit(ready); supervisor = make(ready).__enter__()
    try:
        supervisor._last_observed = time.monotonic()-1
        assert supervisor.cancel_check(spec)
    finally:
        with pytest.raises(SupervisorError, match='authorization_observation_stale'): supervisor.close()
    assert supervisor.receipt.durable_revocation_confirmed


def test_profile_conflict_and_dispatch_cancel_are_denied(ready):
    spec = admit(ready)
    supervisor = make(ready)
    with ready[0]._tx() as db:
        db.execute("UPDATE provider_dispatch SET profile_digest=? WHERE request_id=?", ('c'*64, spec.lease.request_id))
    assert not supervisor._database(supervisor._authorized)
    with ready[0]._tx() as db:
        db.execute("UPDATE provider_dispatch SET profile_digest=?,cancel_requested=1 WHERE request_id=?", ('b'*64, spec.lease.request_id))
    assert not supervisor._database(supervisor._authorized)


def test_closed_receipt_remains_immutable_after_later_cancel(ready):
    event = threading.Event()
    with make(ready, caller_cancel=event) as supervisor: pass
    receipt = supervisor.receipt
    event.set()
    assert supervisor.close() is receipt
    supervisor.raise_if_cancelled()
    assert supervisor.receipt.cancelled is False


def test_foreign_controller_reservation_rejected_even_if_database_matches(ready):
    with ready[0]._tx() as db:
        db.execute("UPDATE provider_reservations SET controller_instance_id='foreign' WHERE id=?", (ready[4].reservation_id,))
    foreign = replace(ready[4], controller_instance_id='foreign')
    with pytest.raises(LeaseError, match='owner_requires_reconciliation'):
        with ready[0]._tx() as db: ready[3]._reservation(db, foreign, held=True)
    modified = (*ready[:4], foreign, *ready[5:])
    with pytest.raises(SupervisorError, match='foreign_controller_instance'): make(modified)
    assert grant_row(ready)['revoked_at'] is None


def test_observer_crash_fails_closed_and_joins(ready, monkeypatch):
    supervisor = make(ready); spec = admit(ready)
    original = supervisor._poll
    def fail(*, final=False):
        if threading.current_thread() is not threading.main_thread(): raise RuntimeError('synthetic')
        return original(final=final)
    monkeypatch.setattr(supervisor, '_poll', fail)
    supervisor.__enter__(); supervisor._wake.set()
    assert supervisor._cancelled.wait(.5)
    assert supervisor.cancel_check(spec)
    with pytest.raises(SupervisorError, match='supervisor_failed'): supervisor.close()
    assert supervisor.receipt.stopped and supervisor.receipt.durable_revocation_confirmed


def test_sqlite_progress_handler_interrupts_expensive_vm_work(ready):
    supervisor = make(ready)
    start = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        supervisor._database(lambda db: db.execute('''WITH RECURSIVE counts(x) AS
            (SELECT 1 UNION ALL SELECT x+1 FROM counts WHERE x<1000000000)
            SELECT SUM(x) FROM counts''').fetchone())
    assert time.monotonic()-start < .4
    assert supervisor._connection is None


def test_concurrent_adapter_checks_only_revoke_bound_grant(ready):
    from concurrent.futures import ThreadPoolExecutor
    event = threading.Event(); supervisor = make(ready, caller_cancel=event).__enter__(); spec = admit(ready)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert not any(pool.map(supervisor.cancel_check, [spec]*80))
            event.set()
            assert all(pool.map(supervisor.cancel_check, [spec]*80))
    finally:
        with pytest.raises(SupervisorError, match='caller_cancelled'): supervisor.close()
    assert supervisor.receipt.stopped and supervisor.receipt.durable_revocation_confirmed


def test_invalid_disconnect_rejected(ready):
    with pytest.raises(SupervisorError, match='invalid_supervisor_config'): make(ready, disconnect=lambda:False)


def test_stale_margin_tolerates_scheduling_gap_without_relaxing_sql_bound(ready):
    spec = admit(ready)
    with make(ready) as supervisor:
        # Simulate a 400ms scheduling gap; actual slow SQL still has its 50ms limit.
        supervisor._last_observed = time.monotonic()-.4
        assert not supervisor.cancel_check(spec)
        assert not supervisor._cancelled.is_set()


def test_typed_provider_error_survives_successful_observer_shutdown(ready):
    from cloudworkbench.provider_docker import DockerError
    supervisor = make(ready)
    with pytest.raises(DockerError, match='gateway_not_ready'):
        with supervisor: raise DockerError('gateway_not_ready')
    assert supervisor.receipt.stopped and supervisor.receipt.durable_revocation_confirmed


def test_grant_revokes_without_waiting_for_account_flock(ready):
    event = threading.Event(); supervisor = make(ready, caller_cancel=event).__enter__()
    try:
        with ready[3].account_lock(ready[4].account_id):
            event.set(); wait_cancel(supervisor)
            assert supervisor._revoked
    finally:
        with pytest.raises(SupervisorError): supervisor.close()


def test_shutdown_failure_takes_precedence_over_body_error(ready, monkeypatch):
    import cloudworkbench.cancellation_supervisor as module
    entered, release = threading.Event(), threading.Event()
    supervisor = make(ready); original = supervisor._poll
    def stalled(*, final=False):
        if threading.current_thread() is not threading.main_thread():
            entered.set(); release.wait(2)
        else: original(final=final)
    monkeypatch.setattr(supervisor, '_poll', stalled)
    monkeypatch.setattr(module, 'JOIN_SECONDS', .03)
    try:
        with pytest.raises(SupervisorError, match='supervisor_shutdown_failed'):
            with supervisor:
                supervisor._wake.set(); assert entered.wait(.5)
                raise ValueError('original_body_error')
        assert not supervisor.receipt.stopped
        assert supervisor.receipt.durable_revocation_confirmed
    finally:
        release.set(); supervisor._thread.join(.5)
    assert not supervisor._thread.is_alive()
