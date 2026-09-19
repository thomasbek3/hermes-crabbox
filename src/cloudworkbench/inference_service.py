"""Private per-execution HTTP boundary; no provider credentials or tool execution.

The controller supplies a capability digest and trusted callbacks. This listener
is not a grant issuer: current authorization and provider lease/outer cleanup
must be enforced by those callbacks. Bind only inside the isolated provider
network (or loopback for tests), never publish its port on the host.
"""
from dataclasses import dataclass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import time
import re
import select
import socket
import threading


@dataclass(frozen=True)
class ServiceResponse:
    status: int
    content_type: str
    body: bytes
    delivery_token: str | None = None


class InferenceService(HTTPServer):
    allow_reuse_address = True

    def __init__(self, address, *, capability_sha256, authorize, execute,
                 read_timeout=2.0, request_timeout=5.0, write_timeout=5.0, execution_timeout=120.0, event=None, delivery=None, delivery_result=None, max_request_bytes=262144, max_response_bytes=2097152,
                 request_path='/v1/chat/completions'):
        if not isinstance(capability_sha256, str) or not re.fullmatch('[0-9a-f]{64}', capability_sha256):
            raise ValueError('invalid capability digest')
        if not callable(authorize) or not callable(execute):
            raise ValueError('trusted callbacks required')
        if request_path not in ('/v1/chat/completions', '/v1/responses'):
            raise ValueError('invalid inference protocol path')
        self.request_path = request_path
        if not 0 < request_timeout <= 60 or not 0 < write_timeout <= 30 or not 0 < execution_timeout <= 3600 or not 0 < read_timeout <= 30 or not 1 <= max_request_bytes <= 1048576 or not 1 <= max_response_bytes <= 8388608:
            raise ValueError('invalid service bounds')
        self.capability_sha256 = capability_sha256
        self.authorize = authorize
        self.execute = execute
        self.read_timeout = read_timeout
        self.execution_timeout = execution_timeout
        self.request_timeout = request_timeout
        self.write_timeout = write_timeout
        if event is not None and not callable(event):
            raise ValueError("invalid event callback")
        self.event = event
        if delivery is not None and not callable(delivery):
            raise ValueError("invalid delivery callback")
        self.delivery = delivery
        if delivery_result is not None and not callable(delivery_result):
            raise ValueError("invalid delivery result callback")
        self.delivery_result = delivery_result
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        super().__init__(address, _Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.read_timeout)
        return connection, address

    def handle_error(self, request, client_address):
        # HTTPServer's default prints exceptions, which may contain request data.
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        def abort_read():
            try:self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        self._read_timer = threading.Timer(self.server.request_timeout, abort_read)
        self._read_timer.daemon = True
        self._read_timer.start()

    def finish(self):
        self._read_timer.cancel()
        super().finish()

    def _event(self, status, code):
        if self.server.event is not None:
            try:self.server.event(status, code)
            except Exception:pass

    def log_message(self, *_):
        pass

    def _reply(self, status, body, content_type='application/json'):
        self._read_timer.cancel()
        self.connection.settimeout(self.server.write_timeout)
        self.close_connection = True
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, code):
        self._event(status, code)
        self._reply(status, json.dumps({'error': {'code': code}}).encode())

    def send_error(self, code, message=None, explain=None):
        if self.request_version in {"HTTP/0.9", ""}:
            self.request_version = "HTTP/1.1"
        self._error(code, "http_request_error")

    def do_POST(self):
        self.close_connection = True
        if self.path != self.server.request_path:
            return self._error(404, 'not_found')
        auth = self.headers.get_all('Authorization', [])
        if len(auth) != 1 or not re.fullmatch(r'Bearer [0-9a-f]{64}', auth[0]):
            return self._error(401, 'unauthorized')
        digest = hashlib.sha256(auth[0][7:].encode()).hexdigest()
        if not hmac.compare_digest(digest, self.server.capability_sha256):
            return self._error(401, 'unauthorized')
        try:
            if self.server.authorize() is not True:
                return self._error(403, 'execution_grant_unavailable')
        except Exception:
            return self._error(503, 'authorization_unavailable')
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1 or not re.fullmatch('[0-9]{1,8}', lengths[0]):
            return self._error(400, 'invalid_framing')
        length = int(lengths[0])
        if not 0 < length <= self.server.max_request_bytes:
            self._error(413, 'request_limit')
            # Bounded drain improves delivery for ordinary oversized requests;
            # unlimited uploads may still be reset when the bound is reached.
            self.connection.settimeout(.05)
            end = time.monotonic() + .1
            remaining = min(length, 1048576)
            while remaining and time.monotonic() < end:
                try:data = self.rfile.read1(min(65536, remaining))
                except OSError:break
                if not data:break
                remaining -= len(data)
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            return self._error(415, 'unsupported_content_type')
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self._error(400, 'incomplete_request')
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate')
                    result[key] = value
                return result
            def finite_float(value):
                result = float(value)
                if not math.isfinite(result):raise ValueError("nonfinite")
                return result
            request = json.loads(raw, object_pairs_hook=unique, parse_float=finite_float,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
            if not isinstance(request, dict):
                raise ValueError('object required')
        except (ValueError, UnicodeError, RecursionError):
            return self._error(400, 'invalid_json')
        except (OSError, TimeoutError):
            return self._error(408, 'request_timeout')
        self._read_timer.cancel()
        cancel, finished, deadline = threading.Event(), threading.Event(), threading.Event()
        def expired():
            deadline.set()
            cancel.set()
        timer = threading.Timer(self.server.execution_timeout, expired)
        timer.daemon = True
        timer.start()
        def monitor():
            while not finished.wait(.05):
                try:
                    if select.select([self.connection], [], [], 0)[0]:
                        # No further client bytes are valid on this one-request connection.
                        cancel.set()
                        return
                except (OSError, ValueError):
                    cancel.set()
                    return
        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        response_valid = False
        executor_response = None
        try:
            response = self.server.execute(request, cancel)
            executor_response = response if isinstance(response, ServiceResponse) else None
            if not isinstance(response, ServiceResponse) or type(response.status) is not int or response.status not in {200,400,401,403,409,413,422,429,502,503,504}:
                raise ValueError('invalid response')
            if response.content_type not in {'application/json', 'text/event-stream'} or not isinstance(response.body, bytes) or len(response.body) > self.server.max_response_bytes:
                raise ValueError('invalid response')
            response_valid = True
        except Exception:
            self._event(502, "inference_failed")
            response = ServiceResponse(502, 'application/json', b'{"error":{"code":"inference_failed"}}')
        finally:
            timer.cancel()
            finished.set()
            watcher.join(timeout=1)
        response_sent = False
        try:
            if deadline.is_set():
                return self._error(504, "inference_timeout")
            if cancel.is_set():
                self._event(409, "client_cancelled")
            else:
                self._event(response.status, "response")
                self._reply(response.status, response.body, response.content_type)
                response_sent = response_valid
        finally:
            if self.server.delivery is not None:
                try:self.server.delivery(response_sent)
                except Exception:self._event(503, "delivery_callback_failed")
            if self.server.delivery_result is not None:
                try:self.server.delivery_result(executor_response, response_sent)
                except Exception:self._event(503, "delivery_callback_failed")
