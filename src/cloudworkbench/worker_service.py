"""Owned synchronous UDS service with bounded, retryable lifecycle waits."""
from dataclasses import dataclass
import math
import secrets
import threading
import time

from .inference_relay import AttemptBinding, RelayError, WorkerDispatcher, WorkerSocketServer


class WorkerServiceError(ValueError):
    pass


@dataclass(frozen=True)
class WorkerServiceReceipt:
    service_id: str
    binding: AttemptBinding
    started: bool
    stop_requested: bool
    thread_stopped: bool
    active_callbacks: int
    listener_closed: bool
    resources_closed: bool
    timed_out: bool
    error: str | None


class _GuardedDispatcher:
    def __init__(self, service):
        self._service = service

    def dispatch(self, request, cancel):
        service = self._service
        with service._state:
            if (not service._started or service._stop.is_set() or service._resources_closed
                    or service._callbacks or type(cancel) is not threading.Event):
                raise RelayError('execution_grant_unavailable')
            service._callbacks.add(cancel)
        try:
            return service.dispatcher.dispatch(request, cancel)
        finally:
            with service._state:
                service._callbacks.remove(cancel)

    def close(self):
        # The socket handler only needs dispatch. Ownership-sensitive cleanup
        # must pass through the bounded service.close(), never this proxy.
        raise WorkerServiceError('use_owned_service_close')


class WorkerService:
    """Exclusively own an unstarted WorkerSocketServer and its dispatcher.

    stop() fences admission, signals callback cancellation and drains the handler,
    then closes the listener. close() additionally closes the dispatcher journal.
    Both retain non-daemon thread handles on timeout so cleanup can be retried.
    """
    def __init__(self, server, *, poll_seconds=.05):
        if (type(server) is not WorkerSocketServer or type(server.dispatcher) is not WorkerDispatcher
                or type(poll_seconds) not in (int, float) or not math.isfinite(poll_seconds)
                or not .001 <= poll_seconds <= .25 or server.fileno() < 0):
            raise WorkerServiceError('invalid_worker_service')
        self._server = server
        self._dispatcher = server.dispatcher
        self._binding = self._dispatcher.binding
        self._service_id = secrets.token_hex(16)
        self._state = threading.Lock()
        self._lifecycle = threading.Lock()
        self._stop = threading.Event()
        self._callbacks = set()
        self._started = False
        self._listener_closed = False
        self._resources_closed = False
        self._error = None
        self._cleanup_thread = None
        self._retained_threads = []
        self._thread = threading.Thread(target=self._serve, name='worker-service-' + self._service_id, daemon=False)
        self._retained_threads.append(self._thread)
        server.timeout = poll_seconds
        server.dispatcher = _GuardedDispatcher(self)

    @property
    def service_id(self):
        return self._service_id

    @property
    def binding(self):
        return self._binding

    @property
    def dispatcher(self):
        return self._dispatcher

    @property
    def service_thread(self):
        return self._thread

    @property
    def receipt(self):
        return self._receipt()

    def _receipt(self, *, timed_out=False):
        with self._state:
            return WorkerServiceReceipt(self.service_id, self.binding, self._started, self._stop.is_set(),
                not any(t.is_alive() for t in self._retained_threads), len(self._callbacks),
                self._listener_closed, self._resources_closed, timed_out, self._error)

    def _close_listener(self):
        if self._listener_closed:
            return
        try:
            self._server.server_close()
        except BaseException:
            with self._state:
                self._error = 'listener_close_failed'
        else:
            with self._state:
                self._listener_closed = True
                if self._error == 'listener_close_failed': self._error = None

    def _serve(self):
        try:
            # BaseServer.shutdown waits forever without an active serve_forever.
            # A bounded synchronous handle_request loop avoids that dependency.
            while not self._stop.is_set():
                self._server.handle_request()
        except BaseException:
            with self._state:
                self._error = 'service_loop_failed'
        finally:
            self._stop.set()
            with self._state:
                for cancel in self._callbacks: cancel.set()
            self._close_listener()

    def start(self):
        if not self._lifecycle.acquire(False):
            raise WorkerServiceError('service_lifecycle_busy')
        try:
            with self._state:
                if self._stop.is_set() or self._thread.ident is not None:
                    raise WorkerServiceError('service_not_startable')
                self._started = True
            try:
                self._thread.start()
            except Exception:
                self._stop.set()
                with self._state: self._error = 'service_start_failed'
                raise WorkerServiceError('service_start_failed') from None
            return self.receipt
        finally:
            self._lifecycle.release()

    def _teardown(self, close_resources):
        self._close_listener()
        with self._state:
            allowed = self._listener_closed and not self._callbacks and not self._resources_closed
        if not close_resources or not allowed:
            return
        try:
            self.dispatcher.close()
        except BaseException:
            with self._state: self._error = 'dispatcher_close_failed'
        else:
            with self._state:
                self._resources_closed = True
                if self._error == 'dispatcher_close_failed': self._error = None

    def _finish(self, timeout_seconds, *, close_resources):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 <= timeout_seconds <= 30):
            raise WorkerServiceError('invalid_shutdown_timeout')
        if threading.current_thread() in self._retained_threads:
            raise WorkerServiceError('service_self_join_refused')
        deadline = time.monotonic() + timeout_seconds
        if not self._lifecycle.acquire(timeout=max(0, deadline-time.monotonic())):
            return self._receipt(timed_out=True)
        try:
            self._stop.set()
            with self._state:
                for cancel in self._callbacks: cancel.set()
            if self._thread.ident is not None:
                self._thread.join(max(0, deadline-time.monotonic()))
            if self._thread.is_alive():
                return self._receipt(timed_out=True)
            if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
                self._cleanup_thread.join(max(0, deadline-time.monotonic()))
                if self._cleanup_thread.is_alive(): return self._receipt(timed_out=True)
            with self._state:
                active = bool(self._callbacks)
                needed = not self._listener_closed or (close_resources and not self._resources_closed)
            if active:
                return self._receipt(timed_out=True)
            if needed and time.monotonic() < deadline:
                thread = threading.Thread(target=self._teardown, args=(close_resources,),
                    name='worker-teardown-' + self.service_id, daemon=False)
                self._cleanup_thread = thread
                with self._state: self._retained_threads = [self._thread, thread]
                try:
                    thread.start()
                except Exception:
                    with self._state: self._error = 'teardown_start_failed'
                    return self.receipt
                thread.join(max(0, deadline-time.monotonic()))
            value = self.receipt
            finished = value.listener_closed and value.thread_stopped and not value.active_callbacks
            if close_resources: finished = finished and value.resources_closed
            return self._receipt(timed_out=not finished and time.monotonic() >= deadline)
        finally:
            self._lifecycle.release()

    def stop(self, timeout_seconds=5):
        return self._finish(timeout_seconds, close_resources=False)

    def close(self, timeout_seconds=5):
        return self._finish(timeout_seconds, close_resources=True)

    def closed_receipt(self):
        value = self.receipt
        if (not value.stop_requested or not value.thread_stopped or value.active_callbacks
                or not value.listener_closed or not value.resources_closed):
            raise WorkerServiceError('worker_service_not_closed')
        return value
