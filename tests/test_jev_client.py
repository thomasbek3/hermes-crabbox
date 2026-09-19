import http.client
import io
import json
import ssl
import traceback

import pytest

from cloudworkbench import jev_client as jev

KEY = "SYNTHETIC_TEST_KEY"
PAYLOAD = {"model": "jev-latest", "state": "route this", "questions": {
    "workflow": {"type": "choice", "instructions": "Choose a workflow", "criteria": {"none": None}}}}
ANSWER = {"model": "jev-latest", "answers": {"workflow": {
    "type": "choice", "choice": "none", "probabilities": {"none": 1}, "confidence": 1}}}


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.body = io.BytesIO(json.dumps(ANSWER).encode() if body is None else body)
        self.status = status
        self.headers = {"Content-Type": "application/json"} if headers is None else headers
        self.closed = False
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        self.reads += 1
        return self.body.read(size)

    def isclosed(self):
        return self.closed

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    state = {"response": Response(), "connections": [], "requests": [], "timeouts": []}

    class Connection:
        def __init__(self, host, **kwargs):
            state["connections"].append((host, kwargs))
            self.sock = self
            self.closed = False
            state["connection"] = self

        def settimeout(self, value):
            state["timeouts"].append(value)

        def connect(self):
            if "error" in state:
                raise state["error"]

        def request(self, method, path, **kwargs):
            state["requests"].append((method, path, kwargs))

        def getresponse(self):
            return state["response"]

        def close(self):
            self.closed = True

    monkeypatch.setattr(jev.http.client, "HTTPSConnection", Connection)
    return state


def test_documented_choice_roundtrip_direct_verified_tls_no_proxy(transport, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:9")
    client = jev.JevClient(KEY)
    assert client.evaluate(PAYLOAD) == ANSWER
    assert len(transport["connections"]) == len(transport["requests"]) == 1
    host, options = transport["connections"][0]
    assert host == "api.typesafe.ai" and options["port"] == 443
    assert options["context"].check_hostname
    assert options["context"].verify_mode == ssl.CERT_REQUIRED
    method, path, request = transport["requests"][0]
    assert (method, path) == ("POST", "/v1/systemone")
    assert json.loads(request["body"]) == PAYLOAD
    assert request["headers"]["Authorization"] == "Bearer " + KEY
    assert request["headers"]["Accept-Encoding"] == "identity"
    assert transport["response"].closed and transport["connection"].closed
    assert all(0 < value <= 10 for value in transport["timeouts"])
    assert client.last_elapsed_seconds >= 0 and KEY not in repr(client)


@pytest.mark.parametrize("status,code", [(301, "redirect_refused"), (302, "redirect_refused"),
    (307, "redirect_refused"), (308, "redirect_refused"), (401, "unauthorized"), (403, "unauthorized"),
    (400, "request_rejected"), (422, "request_rejected"), (429, "rate_limited"),
    (500, "unavailable"), (529, "unavailable"), (201, "http_error")])
def test_http_errors_no_retry_redirect_or_body_read(transport, status, code, capsys):
    transport["response"] = Response(KEY.encode(), status, {"Location": "https://evil.invalid/" + KEY})
    with pytest.raises(jev.JevError) as caught:
        jev.JevClient(KEY).evaluate(PAYLOAD)
    assert caught.value.code == code and str(caught.value) == code
    assert caught.value.__context__ is None
    assert caught.value.elapsed_seconds >= 0
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert len(transport["requests"]) == 1 and transport["response"].reads == 0
    assert transport["response"].closed and transport["connection"].closed
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("body", [b'[]', b'null', b'1', b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}',
    b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}', b'{"a":1e999}', b'{"a":', b'{"a":"\xff"}',
    b'{"a":' + b'[' * 70 + b'0' + b']' * 70 + b'}'])
def test_invalid_json_is_sanitized(transport, body):
    transport["response"] = Response(body)
    with pytest.raises(jev.JevError, match="^invalid_response$"):
        jev.JevClient(KEY).evaluate(PAYLOAD)
    assert transport["response"].closed


@pytest.mark.parametrize("payload", [[], {1: "key"}, {"a": float("nan")}, {"a": float("inf")},
    {"a": object()}, {"a": (1, 2)}, {"a": "\ud800"}])
def test_invalid_request_rejected_before_network(transport, payload):
    with pytest.raises(jev.JevError, match="^invalid_request$"):
        jev.JevClient(KEY).evaluate(payload)
    assert not transport["connections"]


def test_request_cycles_and_size_bound(transport):
    cycle = {}; cycle["cycle"] = cycle
    with pytest.raises(jev.JevError, match="^invalid_request$"):
        jev.JevClient(KEY).evaluate(cycle)
    with pytest.raises(jev.JevError, match="^request_too_large$"):
        jev.JevClient(KEY).evaluate({"state": "x" * jev.MAX_REQUEST_BYTES})
    assert not transport["connections"]


@pytest.mark.parametrize("headers,code", [
    ({"Content-Type": "text/html"}, "invalid_response"),
    ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, "invalid_response"),
    ({"Content-Type": "application/json", "Content-Length": "1, 1"}, "invalid_response"),
    ({"Content-Type": "application/json", "Content-Length": "999"}, "invalid_response"),
    ({"Content-Type": "application/json", "Content-Length": str(jev.MAX_RESPONSE_BYTES + 1)}, "response_too_large"),
])
def test_response_headers_and_truncation(transport, headers, code):
    transport["response"] = Response(b'{}', headers=headers)
    with pytest.raises(jev.JevError, match="^" + code + "$"):
        jev.JevClient(KEY).evaluate(PAYLOAD)


def test_unknown_length_response_capped(transport):
    transport["response"] = Response(b"x" * (jev.MAX_RESPONSE_BYTES + 10))
    with pytest.raises(jev.JevError, match="^response_too_large$"):
        jev.JevClient(KEY).evaluate(PAYLOAD)
    assert transport["response"].body.tell() == jev.MAX_RESPONSE_BYTES + 1


@pytest.mark.parametrize("error,code", [(TimeoutError(KEY), "timeout"),
    (ssl.SSLError(KEY), "tls_error"), (OSError(KEY), "network_error"),
    (http.client.HTTPException(KEY), "network_error")])
def test_network_exception_never_retains_secret_cause(transport, error, code):
    transport["error"] = error
    with pytest.raises(jev.JevError) as caught:
        jev.JevClient(KEY).evaluate(PAYLOAD)
    assert caught.value.code == code and caught.value.__context__ is None
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert len(transport["connections"]) == 1 and transport["connection"].closed


def test_slow_response_is_not_success_after_budget(transport, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(jev.time, "monotonic", lambda: now[0])
    read = transport["response"].read1
    def slow_read(size):
        now[0] += 11
        return read(size)
    transport["response"].read1 = slow_read
    client = jev.JevClient(KEY)
    with pytest.raises(jev.JevError, match="^timeout$") as caught:
        client.evaluate(PAYLOAD)
    assert client.last_elapsed_seconds == caught.value.elapsed_seconds == 11
    assert transport["response"].closed


@pytest.mark.parametrize("key", ["", "line\r\nheader", "has space", "\u2603", None, "x" * 4097])
def test_invalid_key(key):
    with pytest.raises(jev.JevError, match="^invalid_api_key$"):
        jev.JevClient(key)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "10", 61])
def test_invalid_timeout(timeout):
    with pytest.raises(jev.JevError, match="^invalid_timeout$"):
        jev.JevClient(KEY, timeout)


@pytest.fixture(scope="module")
def local_tls_contexts(tmp_path_factory):
    import shutil
    import subprocess
    executable = shutil.which("openssl")
    if executable is None:
        pytest.skip("openssl is required for the isolated real TLS regression")
    folder = tmp_path_factory.mktemp("jev-local-tls")
    cert, key = folder / "cert.pem", folder / "synthetic-key.pem"
    subprocess.run([executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
        "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=10)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=str(cert))
    return server, client


@pytest.mark.parametrize("framing", ["length", "chunked", "eof", "truncated"])
@pytest.mark.parametrize("large", [False, True])
def test_real_tls_connection_close_response_lifecycle(monkeypatch, local_tls_contexts, framing, large):
    import socket
    import threading
    server_context, client_context = local_tls_contexts
    expected = dict(ANSWER, state="x" * 200_000) if large else ANSWER
    payload = json.dumps(expected).encode()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    observations, failures, requests = [], [], []

    def serve():
        try:
            raw, _ = listener.accept()
            with server_context.wrap_socket(raw, server_side=True) as sock:
                sock.settimeout(3)
                with sock.makefile("rb") as incoming:
                    request_line = incoming.readline(8192)
                    headers = {}
                    while True:
                        line = incoming.readline(8192)
                        if line == b"\r\n":
                            break
                        name, value = line.decode().split(":", 1)
                        headers[name.lower()] = value.strip()
                    body = incoming.read(int(headers["content-length"]))
                requests.append((request_line, body))
                headers = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                if framing in ("length", "truncated"):
                    headers += b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
                    body = payload[:-2] if framing == "truncated" else payload
                elif framing == "chunked":
                    headers += b"Transfer-Encoding: chunked\r\n"
                    body = format(len(payload), "x").encode() + b"\r\n" + payload + b"\r\n0\r\n\r\n"
                else:
                    body = payload
                sock.sendall(headers + b"\r\n" + body)
        except Exception as exc:
            failures.append(type(exc).__name__)
        finally:
            listener.close()

    original_connection = http.client.HTTPSConnection

    class ObservedResponse(http.client.HTTPResponse):
        def __init__(self, sock, *args, **kwargs):
            self.observed_socket = sock
            super().__init__(sock, *args, **kwargs)

        def read1(self, size):
            data = super().read1(size)
            observations.append({"bytes": len(data), "closed": self.isclosed(),
                                 "fileno": self.observed_socket.fileno()})
            return data

    def local_connection(host, *, port, timeout, context):
        assert (host, port) == ("api.typesafe.ai", 443)
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        connection = original_connection("127.0.0.1", listener.getsockname()[1],
            timeout=timeout, context=client_context)
        connection.response_class = ObservedResponse
        return connection

    from types import SimpleNamespace
    monkeypatch.setattr(jev, "http", SimpleNamespace(client=SimpleNamespace(
        HTTPSConnection=local_connection, HTTPException=http.client.HTTPException)))
    thread = threading.Thread(target=serve, name="jev-local-tls")
    thread.start()
    try:
        client = jev.JevClient(KEY, timeout_seconds=2)
        if framing == "truncated":
            with pytest.raises(jev.JevError, match="^invalid_response$"):
                client.evaluate(PAYLOAD)
            assert client.last_error_code == "invalid_response"
        else:
            try:
                result = client.evaluate(PAYLOAD)
            except jev.JevError as exc:
                pytest.fail(f"{exc.code}: read lifecycle {observations}")
            assert result == expected, observations
            assert client.last_error_code is None
    finally:
        thread.join(4)
        listener.close()
    assert not thread.is_alive() and not failures
    assert len(requests) == 1 and json.loads(requests[0][1]) == PAYLOAD
    expected_length = len(payload) - (2 if framing == "truncated" else 0)
    assert observations and sum(item["bytes"] for item in observations) == expected_length
    if framing == "length":
        assert observations[-1]["closed"] and observations[-1]["fileno"] == -1


def test_last_error_code_is_fixed_and_resets_on_success(transport):
    client = jev.JevClient(KEY)
    transport["response"] = Response(KEY.encode(), status=429)
    with pytest.raises(jev.JevError):
        client.evaluate(PAYLOAD)
    assert client.last_error_code == "rate_limited"
    transport["response"] = Response()
    assert client.evaluate(PAYLOAD) == ANSWER
    assert client.last_error_code is None
