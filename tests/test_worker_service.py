import hashlib
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import threading
import time

import pytest

from cloudworkbench.inference_relay import (
    AttemptBinding, DispatchResult, WorkerDispatcher, WorkerSocketServer, RelayError,
    _json, _receive_frame, _send_frame,
)
from cloudworkbench.inference_service import ServiceResponse
from cloudworkbench.worker_service import WorkerService, WorkerServiceError, WorkerServiceReceipt

CAP = 'b' * 64
BINDING = AttemptBinding('service-test', 1, 'a' * 64)


def request(nonce='c'*64):
    payload = {'synthetic': True}
    return {'binding': BINDING.wire(), 'capability': CAP, 'nonce': nonce,
            'payload': payload, 'payload_digest': hashlib.sha256(_json(payload)).hexdigest()}


@pytest.fixture
def fixture():
    root = Path(tempfile.mkdtemp(prefix='cwb-svc-', dir='/tmp')).resolve()
    root.chmod(0o700)
    worker = WorkerDispatcher(journal_path=root/'journal', binding=BINDING,
        capability_sha256=hashlib.sha256(CAP.encode()).hexdigest(), authorize=lambda _: True,
        execute=lambda payload, cancel: DispatchResult(ServiceResponse(200, 'application/json', b'{}'), True))
    server = WorkerSocketServer(root/'socket', worker, deadline_seconds=2)
    service = WorkerService(server, poll_seconds=.01)
    yield root, worker, server, service
    receipt = service.close(3)
    assert receipt.thread_stopped and receipt.resources_closed
    shutil.rmtree(root)


def call(root, value=None):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(3)
        client.connect(str(root/'socket'))
        _send_frame(client, value or request(), time.monotonic()+3, None)
        return _receive_frame(client, time.monotonic()+3, None, 2*1024**2)


def test_actual_socket_response_then_ordered_stop_and_close(fixture):
    root, worker, server, service = fixture
    started = service.start()
    assert started.started and not service.service_thread.daemon
    assert service.dispatcher is worker and service.binding is worker.binding
    assert call(root)['response']['status'] == 200
    stopped = service.stop(1)
    assert stopped.thread_stopped and stopped.listener_closed and not stopped.resources_closed
    assert not (root/'socket').exists()
    assert worker.journal.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 1
    closed = service.close(1)
    assert type(closed) is WorkerServiceReceipt and closed.resources_closed
    assert not closed.active_callbacks and closed.thread_stopped
    assert closed == service.closed_receipt() == service.close(1)
    assert closed.service_id == service.service_id and closed.binding == BINDING
    with pytest.raises(sqlite3.ProgrammingError): worker.journal.db.execute('SELECT 1')


def test_unstarted_close_does_not_call_blocking_base_shutdown(fixture, monkeypatch):
    _, _, server, service = fixture
    monkeypatch.setattr(server, 'shutdown', lambda: pytest.fail('shutdown deadlocks before serve_forever'))
    started = time.monotonic()
    receipt = service.close(.5)
    assert time.monotonic()-started < 1
    assert not receipt.started and receipt.resources_closed and receipt.thread_stopped
    assert service.service_thread.ident is None
    with pytest.raises(WorkerServiceError, match='not_startable'): service.start()


def test_blocked_callback_retains_live_handle_and_journal_for_retry(fixture):
    root, worker, _, service = fixture
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    def execute(payload, cancel):
        entered.set()
        assert cancel.wait(2); cancelled.set()
        assert release.wait(2)
        return DispatchResult(ServiceResponse(200,'application/json',b'{}'), True)
    worker.execute = execute
    service.start()
    responses = []
    client = threading.Thread(target=lambda: responses.append(call(root)))
    client.start()
    assert entered.wait(1)
    try:
        begin = time.monotonic(); receipt = service.close(.03)
        assert time.monotonic()-begin < .5 and receipt.timed_out
        assert cancelled.wait(1)
        assert not receipt.thread_stopped and receipt.active_callbacks == 1
        assert not receipt.resources_closed and service.service_thread.is_alive()
        assert worker.journal.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 1
        with pytest.raises(WorkerServiceError): service.closed_receipt()
    finally:
        release.set(); client.join(3)
    assert not client.is_alive() and responses[0]['response']['status'] == 409
    assert service.close(1).resources_closed


def test_partial_wire_request_keeps_thread_and_listener_until_drain(fixture):
    root, _, server, service = fixture
    entered = threading.Event()
    handler = server.RequestHandlerClass
    class MarkAccepted(handler):
        def handle(self):
            entered.set()
            super().handle()
    server.RequestHandlerClass = MarkAccepted
    service.start()
    client = socket.socket(socket.AF_UNIX); client.connect(str(root/'socket'))
    client.sendall(b'\x00')
    assert entered.wait(1)
    try:
        receipt = service.close(.02)
        assert receipt.timed_out and not receipt.thread_stopped and not receipt.resources_closed
    finally: client.close()
    assert service.close(1).resources_closed


def test_direct_proxy_admission_is_fenced_and_proxy_close_refused(fixture):
    _, _, server, service = fixture
    with pytest.raises(RelayError): server.dispatcher.dispatch(request(), threading.Event())
    service.start(); service.stop(1)
    with pytest.raises(RelayError): server.dispatcher.dispatch(request(), threading.Event())
    with pytest.raises(WorkerServiceError, match='owned_service_close'): server.dispatcher.close()


def test_service_loop_exception_keeps_fixed_diagnostic_and_can_close(fixture, monkeypatch):
    _, _, server, service = fixture
    monkeypatch.setattr(server,'handle_request',lambda: (_ for _ in ()).throw(RuntimeError('synthetic-secret')))
    service.start(); service.service_thread.join(1)
    value = service.close(1)
    assert value.error == 'service_loop_failed' and value.resources_closed
    assert 'synthetic-secret' not in repr(value)


def test_slow_dispatcher_close_retains_nondaemon_teardown_handle(fixture, monkeypatch):
    _, worker, _, service = fixture
    entered, release = threading.Event(), threading.Event()
    original = worker.close
    def close():
        entered.set(); assert release.wait(2); original()
    monkeypatch.setattr(worker,'close',close)
    try:
        value = service.close(.02)
        assert entered.wait(1) and value.timed_out and not value.resources_closed
        assert not value.thread_stopped and not service._cleanup_thread.daemon
        assert service.close(.02).timed_out
    finally: release.set()
    assert service.close(1).resources_closed


@pytest.mark.parametrize('target', ['listener', 'dispatcher'])
def test_teardown_error_is_fixed_and_retryable(fixture, monkeypatch, target):
    _, worker, server, service = fixture
    obj, name = (server,'server_close') if target == 'listener' else (worker,'close')
    original = getattr(obj,name)
    monkeypatch.setattr(obj,name,lambda: (_ for _ in ()).throw(RuntimeError('synthetic-secret')))
    value = service.close(1)
    assert not value.resources_closed and value.error == target + '_close_failed'
    with pytest.raises(WorkerServiceError): service.closed_receipt()
    monkeypatch.setattr(obj,name,original)
    value = service.close(1)
    assert value.resources_closed and value.error is None


def test_foreign_socket_replacement_is_preserved(fixture):
    root, _, _, service = fixture
    (root/'socket').unlink(); (root/'socket').write_text('foreign')
    assert service.close(1).resources_closed
    assert (root/'socket').read_text() == 'foreign'


def test_receipt_cannot_be_swapped_between_service_instances(fixture):
    _, _, _, service = fixture
    value = service.close(1)
    assert len(value.service_id) == 32 and value.binding == service.binding
    with pytest.raises(AttributeError): service.service_id = 'foreign'
    with pytest.raises(AttributeError): service.dispatcher = object()


def test_start_twice_refused(fixture):
    _, _, _, service = fixture
    service.start()
    with pytest.raises(WorkerServiceError): service.start()


@pytest.mark.parametrize('value', [True, -1, 31, float('nan'), float('inf')])
def test_bad_shutdown_deadline_refused(fixture, value):
    _, _, _, service = fixture
    with pytest.raises(WorkerServiceError): service.close(value)


def test_zero_budget_fences_without_claiming_close(fixture):
    _, _, _, service = fixture
    result = service.close(0)
    assert result.timed_out and result.stop_requested and not result.resources_closed
    assert service.close(1).resources_closed


def test_concurrent_close_times_out_without_second_resource_close(fixture, monkeypatch):
    _, worker, _, service = fixture
    entered, release = threading.Event(), threading.Event()
    original = worker.close
    calls = []
    def close():
        calls.append(1); entered.set(); assert release.wait(2); original()
    monkeypatch.setattr(worker, 'close', close)
    results = []
    first = threading.Thread(target=lambda: results.append(service.close(1)))
    first.start()
    assert entered.wait(1)
    try:
        value = service.close(.02)
        assert value.timed_out and not value.resources_closed and len(calls) == 1
    finally:
        release.set(); first.join(2)
    assert results[0].resources_closed and service.close(1).resources_closed and len(calls) == 1


def test_start_failure_still_has_cleanup_path(fixture, monkeypatch):
    _, _, _, service = fixture
    monkeypatch.setattr(service.service_thread, 'start', lambda: (_ for _ in ()).throw(RuntimeError('private')))
    with pytest.raises(WorkerServiceError, match='service_start_failed'): service.start()
    result = service.close(1)
    assert result.resources_closed and result.thread_stopped and result.error == 'service_start_failed'


def test_callback_cannot_join_its_own_service_thread(fixture):
    root, worker, _, service = fixture
    refusals = []
    def execute(payload, cancel):
        with pytest.raises(WorkerServiceError, match='self_join_refused'):
            service.close(.1)
        refusals.append(True)
        return DispatchResult(ServiceResponse(200,'application/json',b'{}'), True)
    worker.execute = execute; service.start()
    assert call(root)['response']['status'] == 200 and refusals == [True]


def test_refuses_second_wrapper_and_nonserver(fixture):
    _, _, server, _ = fixture
    with pytest.raises(WorkerServiceError): WorkerService(server)
    with pytest.raises(WorkerServiceError): WorkerService(object())
