import threading
from concurrent.futures import ThreadPoolExecutor
from test_provider_dispatch import setup
from test_supervised_executor import build
from test_hermes_inference_protocol import REQUEST


def test_concurrent_call_cannot_execute_or_replace_active_supervisor(setup):
    wrapped, context = build(setup)
    entered = threading.Event()
    release = threading.Event()

    def active(spec, resources):
        entered.set()
        assert release.wait(3)
        assert not wrapped.cancel_check(spec)

    setup[6].hooks['start'] = active
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(wrapped, context, REQUEST, threading.Event())
        try:
            assert entered.wait(3)
            second = wrapped(context, REQUEST, threading.Event())
            assert second.response.status == 409
            assert b'attempt_execution_busy' in second.response.body
            assert len([event for event in setup[6].events if event[0] == 'create']) == 1
            assert wrapped.last_receipt is None
        finally:
            release.set()
        result = first.result(timeout=3)
    assert result.response.status == 200 and result.outer_cleanup_confirmed
    assert wrapped.last_receipt.stopped and not wrapped.last_receipt.cancelled
    assert not setup[6].objects


def test_precancelled_call_does_not_create_resources(setup):
    wrapped, context = build(setup)
    cancel = threading.Event()
    cancel.set()
    result = wrapped(context, REQUEST, cancel)
    assert result.response.status != 200
    assert result.outer_cleanup_confirmed
    assert not setup[6].events
    assert wrapped.last_receipt.stopped and wrapped.last_receipt.cancelled


def test_stalled_observer_fences_wrapper_and_quarantines_owner(setup, monkeypatch):
    import cloudworkbench.cancellation_supervisor as module
    wrapped, context = build(setup)
    entered, release = threading.Event(), threading.Event()
    supervisors = []
    original_factory = wrapped.supervisor_factory
    def factory(*args, **kwargs):
        supervisor = original_factory(*args, **kwargs)
        original_poll = supervisor._poll
        def stalled(*, final=False):
            if threading.current_thread() is not threading.main_thread():
                entered.set()
                release.wait(3)
            else:
                original_poll(final=final)
        supervisor._poll = stalled
        supervisors.append(supervisor)
        return supervisor
    wrapped.supervisor_factory = factory
    monkeypatch.setattr(module, 'JOIN_SECONDS', .03)
    def active(spec, resources):
        supervisors[0]._wake.set()
        assert entered.wait(1)
    setup[6].hooks['start'] = active
    try:
        first = wrapped(context, REQUEST, threading.Event())
        assert first.response.status != 200
        assert not wrapped.last_receipt.stopped
        second = wrapped(context, REQUEST, threading.Event())
        assert b'attempt_executor_fenced' in second.response.body
        assert not wrapped.authorize(context.binding)
        assert len(supervisors) == 1
        with setup[0]._tx() as db:
            row = db.execute('SELECT state FROM provider_reservations WHERE id=?', (setup[4].reservation_id,)).fetchone()
        assert row['state'] == 'quarantined'
    finally:
        release.set()
        for supervisor in supervisors:
            supervisor._thread.join(1)
            assert not supervisor._thread.is_alive()


def test_durable_cancel_reaches_observer_without_caller_event(setup):
    import time
    wrapped, context = build(setup)
    cancel = threading.Event()
    observed = []
    def active(spec, resources):
        with setup[0]._tx() as db:
            db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (context.binding.attempt_id,))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if wrapped.cancel_check(spec):
                observed.append(True)
                return
            time.sleep(.01)
        raise AssertionError('durable cancellation was not observed')
    setup[6].hooks['start'] = active
    result = wrapped(context, REQUEST, cancel)
    assert not cancel.is_set()
    assert observed == [True] and result.response.status != 200
    assert not any(event[0] == 'collect' for event in setup[6].events)
    assert wrapped.last_receipt.stopped and wrapped.last_receipt.cancelled


def test_foreign_controller_factory_refusal_creates_no_resources(setup):
    from cloudworkbench.cancellation_supervisor import CancellationSupervisor
    wrapped, context = build(setup)
    def foreign_factory(*args, **kwargs):
        kwargs['controller_instance_id'] = 'foreign-controller'
        return CancellationSupervisor(*args, **kwargs)
    wrapped.supervisor_factory = foreign_factory
    result = wrapped(context, REQUEST, threading.Event())
    assert result.response.status == 409 and result.outer_cleanup_confirmed
    assert not setup[6].events


def test_budget_refusal_through_wrapper_creates_no_resources(setup):
    wrapped, context = build(setup)
    with setup[0]._tx() as db:
        db.execute('UPDATE inference_budget_roots SET used=128')
    result = wrapped(context, REQUEST, threading.Event())
    assert result.response.status == 429 and result.outer_cleanup_confirmed
    assert b'root_request_limit' in result.response.body
    assert not setup[6].events
    assert wrapped.last_receipt.stopped and not wrapped.last_receipt.cancelled


def test_observer_cancel_blocks_collect_when_revocation_write_fails(setup):
    import sqlite3
    import time
    wrapped, context = build(setup)
    original_factory = wrapped.supervisor_factory
    revoked = []
    def factory(*args, **kwargs):
        supervisor = original_factory(*args, **kwargs)
        def unavailable(db):
            revoked.append(True)
            raise sqlite3.OperationalError('database is locked')
        supervisor._revoke = unavailable
        return supervisor
    wrapped.supervisor_factory = factory
    def active(spec, resources):
        with setup[0]._tx() as db:
            db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (context.binding.attempt_id,))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if wrapped.cancel_check(spec):
                return
            time.sleep(.01)
        raise AssertionError('observer cancellation not seen')
    setup[6].hooks['start'] = active
    result = wrapped(context, REQUEST, threading.Event())
    assert result.response.status != 200
    assert revoked
    assert not any(event[0] == 'collect' for event in setup[6].events)
    assert wrapped.last_receipt.cancelled and wrapped.last_receipt.stopped
    assert not wrapped.last_receipt.durable_revocation_confirmed


def test_active_budget_deadline_cancels_supervised_runtime(setup):
    import time
    wrapped, context = build(setup)
    observed = []
    def active(spec, resources):
        with setup[0]._tx() as db:
            db.execute('UPDATE inference_budget_attempts SET deadline_us=? WHERE attempt_id=?',
                       (int((time.time()+.1)*1_000_000), context.binding.attempt_id))
        deadline = time.monotonic()+1
        while time.monotonic() < deadline:
            if wrapped.cancel_check(spec):
                observed.append(True)
                return
            time.sleep(.01)
        raise AssertionError('active budget expiry was not observed')
    setup[6].hooks['start'] = active
    result = wrapped(context, REQUEST, threading.Event())
    assert observed == [True] and result.response.status != 200
    assert wrapped.last_receipt.cancelled and wrapped.last_receipt.stopped
    assert not any(event[0] == 'collect' for event in setup[6].events)
