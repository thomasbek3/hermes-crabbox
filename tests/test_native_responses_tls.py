"""Actual localhost TLS transport lifecycle; all credentials and SSE are synthetic."""
from contextlib import contextmanager
import http.client
import json
import socket
import ssl
import threading
import time

import pytest

from cloudworkbench.native_responses import NativeProfile, NativeResponses
from test_jev_client import local_tls_contexts  # Existing temporary test CA fixture.


PROFILE = NativeProfile('openai-codex', 'gpt-6-astra', 'high')
REQUEST = {'model': PROFILE.model, 'reasoning': {'effort': PROFILE.effort},
           'store': False, 'stream': True, 'tools': [],
           'input': [{'role': 'user', 'content': 'Synthetic TLS fixture.'}]}


def stream():
    item = {'type': 'message', 'id': 'msg_tls', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': 'TLS fixture complete', 'annotations': []}]}
    events = [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
              {'type': 'response.completed', 'response': {
                  'id': 'resp_tls', 'status': 'completed', 'model': PROFILE.model, 'output': None}}]
    return b''.join(b'event: ' + e['type'].encode() + b'\ndata: ' +
                    json.dumps(e).encode() + b'\n\n' for e in events)


@contextmanager
def tls_upstream(contexts, framing):
    server_context, client_context = contexts
    assert client_context.check_hostname and client_context.verify_mode == ssl.CERT_REQUIRED
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(3)
    port = listener.getsockname()[1]
    stop, reading = threading.Event(), threading.Event()
    requests, failures, connections, responses, factories = [], [], [], [], []

    def serve():
        try:
            raw, address = listener.accept()
            assert address[0] == '127.0.0.1'
            with server_context.wrap_socket(raw, server_side=True) as sock:
                sock.settimeout(3)
                with sock.makefile('rb') as incoming:
                    request_line = incoming.readline(8192)
                    headers = {}
                    for _ in range(64):
                        line = incoming.readline(8192)
                        if line == b'\r\n':
                            break
                        name, value = line.decode().split(':', 1)
                        headers[name.lower()] = value.strip()
                    size = int(headers['content-length'])
                    assert 0 < size < 262144
                    body = incoming.read(size)
                assert headers['authorization'] == 'Bearer synthetic-tls-only'
                requests.append((request_line, json.loads(body)))
                payload = stream()
                head = b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n'
                if framing in ('length', 'truncated_length', 'blocked'):
                    head += b'Content-Length: ' + str(len(payload)).encode() + b'\r\n'
                    body = payload if framing == 'length' else payload[:-24]
                elif framing in ('chunked', 'truncated_chunked'):
                    head += b'Transfer-Encoding: chunked\r\n'
                    if framing == 'chunked':
                        chunks = [payload[i:i + 31] for i in range(0, len(payload), 31)]
                        body = b''.join(format(len(c), 'x').encode() + b'\r\n' + c + b'\r\n' for c in chunks)
                        body += b'0\r\n\r\n'
                    else:
                        body = format(len(payload), 'x').encode() + b'\r\n' + payload[:-24]
                else:
                    body = payload if framing == 'eof' else payload[:20]
                sock.sendall(head + b'\r\n' + (b'' if framing == 'blocked' else body))
                if framing == 'blocked':
                    assert stop.wait(4), 'test did not release blocked fixture'
                elif framing == 'abrupt':
                    sock.shutdown(socket.SHUT_RDWR)
        except Exception as error:
            failures.append(type(error).__name__ + ': ' + str(error))
        finally:
            listener.close()

    class ObservedResponse(http.client.HTTPResponse):
        def __init__(self, sock, *args, **kwargs):
            self.original_socket = sock
            super().__init__(sock, *args, **kwargs)
            responses.append(self)

        def read1(self, size):
            reading.set()
            return super().read1(size)

    def factory(host, remote_port, *, timeout):
        assert (host, remote_port) == ('chatgpt.com', 443)
        factories.append((host, remote_port))
        connection = http.client.HTTPSConnection('127.0.0.1', port, timeout=timeout, context=client_context)
        connection.response_class = ObservedResponse
        connections.append(connection)
        return connection

    thread = threading.Thread(target=serve, name='native-responses-local-tls')
    thread.start()
    try:
        yield NativeResponses(PROFILE, timeout=2, connection_factory=factory), reading, connections, responses
    finally:
        stop.set()
        thread.join(4)
        listener.close()
        assert not thread.is_alive() and not failures, failures
        assert len(factories) == len(requests) == 1, 'automatic retry or missing request'
        assert requests[0] == (b'POST /backend-api/codex/responses HTTP/1.1\r\n', REQUEST)
        assert connections[0].sock is None
        assert responses and all(r.isclosed() and r.original_socket.fileno() == -1 for r in responses)
        assert not any(t.name == 'native-provider-request' for t in threading.enumerate())


@pytest.mark.parametrize('framing', ['length', 'chunked', 'eof'])
def test_tls_success_across_connection_close_framing(local_tls_contexts, framing):
    with tls_upstream(local_tls_contexts, framing) as (api, _, _, _):
        result = api.execute(REQUEST, credential='synthetic-tls-only')
        assert result.status == 'ok' and result.transport_stopped
        assert result.response['output'][0]['content'][0]['text'] == 'TLS fixture complete'
        assert result.usage_status == 'unknown'
        assert result.container_cleanup_required and not result.credential_reuse_authorized


def test_tls_cancel_stops_response_owned_blocked_socket(local_tls_contexts):
    with tls_upstream(local_tls_contexts, 'blocked') as (api, reading, connections, responses):
        cancel, timer_done = threading.Event(), threading.Event()
        observations = []

        def cancel_after_read_begins():
            try:
                if reading.wait(1.5):
                    # The server sends headers only and stays open until execute returns.
                    time.sleep(.05)
                    observations.append((connections[0].sock is None, responses[0].original_socket.fileno() >= 0))
                cancel.set()
            finally:
                timer_done.set()

        observer = threading.Thread(target=cancel_after_read_begins)
        observer.start()
        started = time.monotonic()
        try:
            result = api.execute(REQUEST, credential='synthetic-tls-only', cancel=cancel)
        finally:
            observer.join(2)
        assert timer_done.is_set() and not observer.is_alive()
        assert observations == [(True, True)]  # HTTPResponse owns the live TLS socket.
        assert time.monotonic() - started < 1.5
        assert result.status == 'error' and result.error == 'cancelled'
        assert result.transport_stopped and result.response is None and not result.sse


@pytest.mark.parametrize('framing', ['truncated_length', 'truncated_chunked', 'abrupt'])
def test_tls_truncated_terminal_never_returns_success_or_retries(local_tls_contexts, framing):
    with tls_upstream(local_tls_contexts, framing) as (api, _, _, _):
        result = api.execute(REQUEST, credential='synthetic-tls-only')
        assert result.status == 'error' and result.error
        assert result.transport_stopped and result.response is None and not result.sse
        assert 'synthetic-tls-only' not in repr(result)
