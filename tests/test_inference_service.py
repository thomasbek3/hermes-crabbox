import hashlib
import http.client
import socket
import threading
from contextlib import contextmanager

from cloudworkbench.inference_service import InferenceService, ServiceResponse

TOKEN = 'a' * 64

@contextmanager
def service(execute=None, authorize=lambda:True, **kwargs):
    server=InferenceService(('127.0.0.1',0),capability_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),authorize=authorize,
        execute=execute or (lambda request,cancel:ServiceResponse(200,'application/json',b'{"ok":true}')),**kwargs)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join(2)

def post(server,body=b'{}',token=TOKEN,headers=None):
    conn=http.client.HTTPConnection(*server.server_address,timeout=3)
    conn.request('POST','/v1/chat/completions',body,headers=headers or {'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    response=conn.getresponse();result=(response.status,response.read());conn.close();return result

def test_success_and_revocation_each_request():
    live=[True];calls=[]
    with service(lambda req,cancel:(calls.append(req) or ServiceResponse(200,'text/event-stream',b'data: [DONE]\n\n')),authorize=lambda:live[0]) as server:
        assert post(server)==(200,b'data: [DONE]\n\n')
        live[0]=False
        assert post(server)[0]==403
        assert len(calls)==1

def test_wrong_capability_never_calls_authorizer():
    def forbidden():raise AssertionError('must not run')
    with service(authorize=forbidden) as server:assert post(server,token='b'*64)[0]==401

def test_invalid_json_duplicate_and_limit():
    calls=[]
    with service(lambda req,cancel:calls.append(req),max_request_bytes=32) as server:
        for body in [b'[]',b'{"a":1,"a":2}',b'{"a":NaN}',b'\xff']:
            assert post(server,body)[0]==400
        assert post(server,b'x'*33)[0]==413
        assert not calls

def test_exception_not_exposed():
    def fail(*args):raise RuntimeError('synthetic-secret')
    with service(fail) as server:
        status,body=post(server)
        assert status==502 and b'synthetic-secret' not in body

def test_disconnect_cancels_execution():
    entered=threading.Event();observed=threading.Event()
    def execute(req,cancel):
        entered.set()
        if cancel.wait(2):observed.set()
        return ServiceResponse(200,'application/json',b'{}')
    with service(execute) as server:
        client=socket.create_connection(server.server_address)
        client.sendall(('POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+TOKEN+'\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}').encode())
        assert entered.wait(2)
        client.close()
        assert observed.wait(2)

def test_invalid_or_oversized_response_fails_closed():
    for response in [None,ServiceResponse(200,'text/html',b'x'),ServiceResponse(200,'application/json',b'x'*33)]:
        with service(lambda req,cancel:response,max_response_bytes=32) as server:
            assert post(server)[0]==502


def test_execution_deadline_signals_callback():
    def execute(req,cancel):
        assert cancel.wait(1)
        return ServiceResponse(200,'application/json',b'{}')
    with service(execute,execution_timeout=.05) as server:
        assert post(server)[0]==504


def raw(server, payload):
    client=socket.create_connection(server.server_address,timeout=2)
    client.sendall(payload)
    chunks=[]
    try:
        while data:=client.recv(65536):chunks.append(data)
    except ConnectionResetError:pass
    finally:client.close()
    return b''.join(chunks)


def test_malformed_request_line_has_http_status():
    with service() as server:
        response=raw(server,b'POST /v1/chat/completions\r\n\r\n')
        assert response.startswith(b'HTTP/1.1 400 ')


def test_framing_and_auth_rejections_before_execution():
    calls=[]
    base=b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
    auth=('Authorization: Bearer '+TOKEN+'\r\n').encode()
    with service(lambda *args:calls.append(args)) as server:
        for headers,status in [(b'Content-Length: 2\r\n',401),
                (auth+auth+b'Content-Length: 2\r\n',401),
                (auth+b'Content-Length: 2\r\nContent-Length: 2\r\n',400),
                (auth+b'Transfer-Encoding: chunked\r\nContent-Length: 2\r\n',400),
                (auth+b'Content-Length: 0\r\n',413)]:
            assert raw(server,base+headers+b'\r\n{}').startswith(('HTTP/1.1 '+str(status)+' ').encode())
        assert raw(server,b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n').startswith(b'HTTP/1.1 501 ')
        assert not calls


def test_float_overflow_never_reaches_executor():
    calls=[]
    with service(lambda *args:calls.append(args)) as server:
        assert post(server,b'{"value":1e999}')[0]==400
        assert not calls


def test_slow_header_total_deadline_and_listener_recovery():
    import time
    with service(read_timeout=.2,request_timeout=.1) as server:
        client=socket.create_connection(server.server_address)
        client.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nX-Test: ')
        start=time.monotonic()
        for _ in range(20):
            try:client.sendall(b'a')
            except OSError:break
            time.sleep(.02)
        client.close()
        assert time.monotonic()-start<.35
        assert post(server)[0]==200


def test_safe_event_callback_and_invalid_status():
    events=[]
    for value in [True,500]:
        with service(lambda *args:ServiceResponse(value,'application/json',b'{}'),event=lambda status,code:events.append((status,code))) as server:
            assert post(server)[0]==502
    assert events and all(code in {'inference_failed','response'} for _,code in events)


def test_large_rejection_delivers_status_with_bounded_drain():
    with service(max_request_bytes=32) as server:
        assert post(server,b'x'*1048576)[0]==413


def test_provider_auth_error_preserved():
    with service(lambda *args:ServiceResponse(401,'application/json',b'{"error":{"code":"provider_auth_rejected"}}')) as server:
        assert post(server)[0]==401


def test_revoked_request_rejected_without_waiting_for_body():
    payload=('POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+TOKEN+'\r\nContent-Type: application/json\r\nContent-Length: 200\r\n\r\n').encode()
    with service(authorize=lambda:False) as server:
        assert raw(server,payload).startswith(b'HTTP/1.1 403 ')


def test_incomplete_upload_inactivity_timeout():
    payload=('POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+TOKEN+'\r\nContent-Type: application/json\r\nContent-Length: 200\r\n\r\n{').encode()
    with service(read_timeout=.05) as server:
        assert raw(server,payload).startswith(b'HTTP/1.1 408 ')


def test_write_half_close_is_cancellation_by_contract():
    entered=threading.Event();observed=threading.Event()
    def execute(req,cancel):
        entered.set()
        if cancel.wait(1):observed.set()
        return ServiceResponse(200,'application/json',b'{}')
    with service(execute) as server:
        client=socket.create_connection(server.server_address)
        client.sendall(('POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+TOKEN+'\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}').encode())
        assert entered.wait(1)
        client.shutdown(socket.SHUT_WR)
        assert observed.wait(1)
        assert client.recv(1024)==b''
        client.close()


def test_codec_body_limit_status_preserved():
    with service(lambda *args:ServiceResponse(413,'application/json',b'{"error":{"code":"body_limit"}}')) as server:
        assert post(server)[0]==413


def test_delivery_hook_means_socket_write_not_client_ack():
    events=[];seen=threading.Event()
    def delivery(sent):events.append(sent);seen.set()
    with service(delivery=delivery) as server:
        assert post(server)[0]==200
        assert seen.wait(1) and events==[True]


def test_delivery_hook_false_on_write_failure(monkeypatch):
    from cloudworkbench.inference_service import _Handler
    events=[];seen=threading.Event()
    def broken(*args,**kwargs):raise BrokenPipeError('synthetic private detail')
    monkeypatch.setattr(_Handler,'_reply',broken)
    def delivery(sent):events.append(sent);seen.set()
    with service(delivery=delivery) as server:
        try:post(server)
        except http.client.RemoteDisconnected:pass
        assert seen.wait(1) and events==[False]


def test_delivery_false_on_execution_deadline():
    events=[];seen=threading.Event()
    def execute(request,cancel):
        assert cancel.wait(1)
        return ServiceResponse(200,'application/json',b'{}')
    def delivery(sent):events.append(sent);seen.set()
    with service(execute,execution_timeout=.03,delivery=delivery) as server:
        assert post(server)[0]==504
        assert seen.wait(1) and events==[False]


def test_response_aware_delivery_preserves_opaque_token_and_legacy_hook():
    response=ServiceResponse(200,'application/json',b'{}','d'*64)
    events=[];legacy=[];seen=threading.Event()
    def aware(value,sent):events.append((value,sent));seen.set()
    with service(lambda *args:response,delivery=legacy.append,delivery_result=aware) as server:
        assert post(server)==(200,b'{}')
        assert seen.wait(1) and events==[(response,True)] and legacy==[True]


def test_response_aware_deadline_keeps_exact_token():
    response=ServiceResponse(200,'application/json',b'{}','e'*64)
    events=[];seen=threading.Event()
    def execute(request,cancel):cancel.wait(1);return response
    def aware(value,sent):events.append((value,sent));seen.set()
    with service(execute,execution_timeout=.03,delivery_result=aware) as server:
        assert post(server)[0]==504
        assert seen.wait(1) and events==[(response,False)]
