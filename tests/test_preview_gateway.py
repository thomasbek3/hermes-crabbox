import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time

import pytest

from cloudworkbench.preview_gateway import PreviewGateway, Limits, GatewayError, EXCHANGE_PATH
from cloudworkbench.preview_registry import COOKIE_NAME
from test_preview_registry import Fixture, DASH, ORIGIN, OTHER


def run(coro):
    return asyncio.run(coro)


class Harness:
    def __init__(self, fixture, *, limits=None, validator=None):
        self.f=fixture
        self.requests=[]
        self.raw_response=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok'
        self.server=None
        self.port=None
        self.delay=0
        self.opened=0
        self.closed=0
        self.validators=[]
        async def check(authorized):
            self.validators.append(authorized)
            return authorized.backend == fixture.backend
        harness=self
        class FixtureTransport:
            async def connect(self, authorized, limits):
                assert authorized.backend == fixture.backend
                harness.opened+=1
                return await asyncio.open_connection('127.0.0.1',harness.port,limit=limits.header_bytes+1)
        self.gateway=PreviewGateway(fixture.registry, backend_validator=validator or check,
            transport=FixtureTransport(), limits=limits or Limits(recheck_seconds=.25,idle_seconds=1,lifetime_seconds=5))

    async def start(self):
        async def handler(reader,writer):
            try:
                head=await reader.readuntil(b'\r\n\r\n')
                if b'transfer-encoding: chunked' in head:
                    while True:
                        length=int(await reader.readuntil(b'\r\n'),16)
                        if not length:
                            await reader.readexactly(2);break
                        await reader.readexactly(length+2)
                else:
                    for line in head.split(b'\r\n'):
                        if line.startswith(b'content-length:'):
                            await reader.readexactly(int(line.split(b':')[1]))
                self.requests.append(head)
                if self.delay:
                    await asyncio.sleep(self.delay)
                writer.write(self.raw_response);await writer.drain()
                await reader.read()
            except (OSError,asyncio.IncompleteReadError):
                pass
            finally:
                writer.close();await writer.wait_closed();self.closed+=1
        self.server=await asyncio.start_server(handler,'127.0.0.1',0)
        self.port=self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close();await self.server.wait_closed();self.gateway.close()
        await asyncio.sleep(.01)

    async def request(self, *, path='/', headers=None, method='GET', body=b'', scope_extra=None, send_hook=None, receive_hook=None, disconnect_event=None):
        events=[]
        scope={'type':'http','scheme':'https','method':method,'path':path,'raw_path':path.encode(),
            'query_string':b'','headers':headers or [(b'host',ORIGIN[8:].encode()),(b'cookie',(COOKIE_NAME+'='+self.f.cookie().token).encode())]}
        scope.update(scope_extra or {})
        sent=False
        finished=asyncio.Event()
        async def receive():
            nonlocal sent
            if not sent:
                sent=True
                return {'type':'http.request','body':body,'more_body':False}
            if disconnect_event is not None:
                await disconnect_event.wait()
            else:
                await finished.wait()
            return {'type':'http.disconnect'}
        async def send(event):
            if send_hook:
                await send_hook(event)
            events.append(event)
            if event['type']=='http.response.body' and not event.get('more_body',False):
                finished.set()
        await self.gateway(scope,receive_hook or receive,send)
        return events


@pytest.fixture
def f(tmp_path):
    return Fixture(tmp_path)


def test_disabled_without_config_or_backend_authority(f):
    async def test():
        for gateway in (PreviewGateway(),PreviewGateway(f.registry)):
            sent=[]
            async def send(e):sent.append(e)
            await gateway({'type':'http'},None,send)
            assert sent[0]['status']==503
            gateway.close()
    run(test())


def test_http_roundtrip_strips_platform_secrets_and_hop_headers(f):
    async def test():
        h=await Harness(f).start()
        try:
            cookie=f.cookie()
            headers=[(b'host',ORIGIN[8:].encode()),(b'cookie',(COOKIE_NAME+'='+cookie.token+'; __Host-cloud_workbench_session=dashboard; app_cookie=value').encode()),
                (b'authorization',b'Bearer should-not-pass'),(b'x-api-key',b'secret'),
                (b'connection',b'x-private'),(b'x-private',b'hidden'),(b'x-forwarded-host',b'evil.test'),
                (b'forwarded',b'for=bad'),(b'x-csrf-token',b'hidden')]
            events=await h.request(headers=headers)
            assert events[0]['status']==200
            assert b''.join(e.get('body',b'') for e in events)==b'ok'
            request=h.requests[0]
            for value in (cookie.token.encode(),b'dashboard',b'Bearer',b'should-not-pass',b'x-private',b'x-forwarded',b'forwarded:',b'x-api-key',b'x-csrf-token'):
                assert value not in request
            assert b'cookie: app_cookie=value' in request
            assert len(h.validators)>=2
        finally:await h.stop()
    run(test())


@pytest.mark.parametrize('extra', [[(b'host',b'evil.test')],[(b'host',ORIGIN[8:].encode()),(b'host',ORIGIN[8:].encode())],
    [(b'host',ORIGIN[8:].encode()),(b'x-test',b'x\r\nAuthorization: hi')],
    [(b'host',ORIGIN[8:].encode()),(b'cookie',b'a=b; a=c')]])
def test_invalid_headers_or_missing_cookie_never_dial_backend(f,extra):
    async def test():
        h=await Harness(f).start()
        try:
            events=await h.request(headers=extra)
            assert events[0]['status'] in (400,403)
            assert h.opened==0
        finally:await h.stop()
    run(test())


@pytest.mark.parametrize('changes', [{'raw_path':b'//evil.test/'},{'raw_path':b'/x\r\nBad: yes'},{'raw_path':b'/\\evil.test'}, {'scheme':'http'}])
def test_target_cannot_select_upstream(f,changes):
    async def test():
        h=await Harness(f).start()
        try:
            events=await h.request(scope_extra=changes)
            assert events[0]['status'] in (400,403)
            assert h.opened==0
        finally:await h.stop()
    run(test())


def test_backend_identity_validator_mandatory_before_connect(f):
    async def test():
        async def reject(_):return False
        h=await Harness(f,validator=reject).start()
        try:
            assert (await h.request())[0]['status']==403
            assert h.opened==0
        finally:await h.stop()
    run(test())


@pytest.mark.parametrize('header', [b'Location: https://evil.test/\r\n',b'Location: //evil.test/\r\n',
    b'Location: /\\evil.test/\r\n',b'Set-Cookie: __Host-cloud_workbench_preview=forged; Secure; Path=/\r\n',
    b'Set-Cookie: app=ok; Domain=example.test\r\n',b'Content-Length: 2\r\n',b'Transfer-Encoding: chunked\r\n'])
def test_upstream_redirect_cookie_and_framing_fail_closed(f,header):
    async def test():
        h=await Harness(f).start()
        h.raw_response=b'HTTP/1.1 302 Found\r\nContent-Length: 2\r\n'+header+b'\r\nok'
        try:
            events=await h.request()
            assert events[0]['status'] in (400,502)
            assert b'ok' != b''.join(e.get('body',b'') for e in events)
        finally:await h.stop()
    run(test())


def test_same_origin_redirect_and_app_cookie_allowed(f):
    async def test():
        h=await Harness(f).start()
        h.raw_response=b'HTTP/1.1 302 Found\r\nContent-Length: 0\r\nLocation: /next\r\nSet-Cookie: app=ok; Secure; Path=/\r\nAuthorization: secret\r\n\r\n'
        try:
            events=await h.request()
            assert events[0]['status']==302
            assert (b'location',b'/next') in events[0]['headers']
            assert all(k!=b'authorization' for k,v in events[0]['headers'])
        finally:await h.stop()
    run(test())


@pytest.mark.parametrize('framing', ['length','chunked','eof'])
def test_response_streaming_framing_and_chunk_bounds(f,framing):
    async def test():
        h=await Harness(f,limits=Limits(chunk_bytes=2,recheck_seconds=.25)).start()
        head={'length':b'Content-Length: 6\r\n','chunked':b'Transfer-Encoding: chunked\r\n','eof':b''}[framing]
        if framing=='eof':
            # EOF fixture closes after sending; normal fixture waits for gateway close.
            async def handler(reader,writer):
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\n\r\nabcdef');await writer.drain();writer.close()
            h.server.close();await h.server.wait_closed()
            h.server=await asyncio.start_server(handler,'127.0.0.1',0);h.port=h.server.sockets[0].getsockname()[1]
        h.raw_response=b'HTTP/1.1 200 OK\r\n'+head+b'\r\n'+(b'6\r\nabcdef\r\n0\r\n\r\n' if framing=='chunked' else b'abcdef')
        try:
            events=await h.request()
            chunks=[e['body'] for e in events if e['type']=='http.response.body']
            assert b''.join(chunks)==b'abcdef'
            assert all(len(c)<=2 for c in chunks)
        finally:await h.stop()
    run(test())


def test_known_oversized_response_denied_before_headers(f):
    async def test():
        h=await Harness(f,limits=Limits(response_bytes=1,recheck_seconds=.25)).start()
        try:assert (await h.request())[0]['status']==502
        finally:await h.stop()
    run(test())


def test_chunked_oversize_aborts_after_started(f):
    async def test():
        h=await Harness(f,limits=Limits(response_bytes=3,chunk_bytes=2,recheck_seconds=.25)).start()
        h.raw_response=b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nabcd\r\n0\r\n\r\n'
        try:
            with pytest.raises(ConnectionError,match='preview_stream_closed'):
                await h.request()
        finally:await h.stop()
    run(test())


def test_revocation_while_waiting_closes_upstream_without_body(f):
    async def test():
        h=await Harness(f).start();h.delay=.5
        async def revoke():
            await asyncio.sleep(.1)
            f.active=False
        task=asyncio.create_task(revoke())
        try:
            events=await h.request()
            assert events[0]['status']==403
            assert b'ok' != b''.join(e.get('body',b'') for e in events)
        finally:
            await task;await h.stop()
    run(test())


def test_exchange_is_single_use_200_landing_without_grant_in_url_or_body(f):
    async def test():
        h=await Harness(f).start()
        try:
            grant=f.grant()
            headers=[(b'host',ORIGIN[8:].encode()),(b'origin',DASH.encode()),(b'content-type',b'application/x-www-form-urlencoded')]
            events=await h.request(path=EXCHANGE_PATH,headers=headers,method='POST',body=('grant='+grant.token).encode())
            assert events[0]['status']==200
            assert all(k!=b'location' for k,v in events[0]['headers'])
            cookie=dict(events[0]['headers'])[b'set-cookie']
            assert b'Secure; HttpOnly; SameSite=Strict' in cookie and b'Domain=' not in cookie
            html=events[1]['body']
            assert b'href="/"' in html and grant.token.encode() not in html
            assert h.opened==0
            assert (await h.request(path=EXCHANGE_PATH,headers=headers,method='POST',body=('grant='+grant.token).encode()))[0]['status']==403
        finally:await h.stop()
    run(test())


def test_websocket_wire_route_remains_disabled(f):
    async def test():
        h=Harness(f);events=[]
        async def send(e):events.append(e)
        await h.gateway({'type':'websocket'},None,send)
        assert events==[{'type':'websocket.close','code':1008}]
        h.gateway.close()
    run(test())


def test_websocket_guard_frame_and_periodic_revocation(f):
    async def test():
        h=Harness(f)
        closed=[]
        async def close(code):closed.append(code)
        lease=h.gateway.websocket_lease(cookie=f.cookie().token,origin=ORIGIN,request_origin=ORIGIN)
        try:
            assert await lease.start(close)
            assert await lease.permit_frame(b'hello')
            f.active=False
            await asyncio.sleep(.35)
            assert closed==[1008] and lease.closed and h.gateway.active==0
            with pytest.raises(GatewayError):await lease.permit_frame(b'late')
        finally:await lease.close();h.gateway.close()
    run(test())


def test_websocket_frame_limit_and_concurrency_accounting(f):
    async def test():
        h=Harness(f,limits=Limits(websocket_frame_bytes=2,max_connections=1,recheck_seconds=.25))
        async def close(_):pass
        first=h.gateway.websocket_lease(cookie=f.cookie().token,origin=ORIGIN,request_origin=ORIGIN)
        second=h.gateway.websocket_lease(cookie=f.cookie().token,origin=ORIGIN,request_origin=ORIGIN)
        try:
            await first.start(close)
            with pytest.raises(GatewayError,match='preview_busy'):await second.start(close)
            with pytest.raises(GatewayError,match='websocket_limit'):await first.permit_frame(b'123')
            assert h.gateway.active==0
            await second.start(close)
        finally:await first.close();await second.close();h.gateway.close()
    run(test())


def test_request_length_limit_and_mismatch(f):
    async def test():
        h=await Harness(f,limits=Limits(request_bytes=2,recheck_seconds=.25)).start()
        try:
            headers=[(b'host',ORIGIN[8:].encode()),(b'cookie',(COOKIE_NAME+'='+f.cookie().token).encode()),(b'content-length',b'3')]
            assert (await h.request(headers=headers,body=b'123'))[0]['status']==413
            assert h.opened==0
            headers[-1]=(b'content-length',b'2')
            assert (await h.request(headers=headers,body=b'x'))[0]['status']==400
        finally:await h.stop()
    run(test())


@pytest.mark.parametrize('response', [b'HTTP/1.1 200 OK\r\nBad\r\n\r\n',
    b'HTTP/1.1 101 Upgrade\r\n\r\n', b'HTTP/1.1 200 OK\r\n'+b'x: '+b'x'*33000+b'\r\n\r\n'])
def test_invalid_status_and_bounded_header_parser(f,response):
    async def test():
        h=await Harness(f).start();h.raw_response=response
        try:assert (await h.request())[0]['status']==502
        finally:await h.stop()
    run(test())


def test_header_refresh_and_clear_site_data_never_pass(f):
    async def test():
        h=await Harness(f).start()
        h.raw_response=b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nRefresh: 0;url=https://evil.test\r\nClear-Site-Data: "cookies"\r\nAlt-Svc: other\r\n\r\n'
        try:
            response=(await h.request())[0]
            assert response['status']==200
            assert not {k for k,v in response['headers']} & {b'refresh',b'clear-site-data',b'alt-svc'}
        finally:await h.stop()
    run(test())


def test_websocket_checks_exact_origin_and_new_generation_each_frame(f):
    async def test():
        h=Harness(f)
        async def close(_):pass
        wrong=h.gateway.websocket_lease(cookie=f.cookie().token,origin=ORIGIN,request_origin=DASH)
        with pytest.raises(GatewayError):await wrong.start(close)
        lease=h.gateway.websocket_lease(cookie=f.cookie().token,origin=ORIGIN,request_origin=ORIGIN)
        try:
            await lease.start(close)
            f.binding=replace(f.binding,generation=2)
            with pytest.raises(GatewayError):await lease.permit_frame(b'new')
            assert lease.closed
        finally:await lease.close();h.gateway.close()
    run(test())


def test_websocket_cumulative_limit_concurrent_frames(f):
    async def test():
        h=Harness(f,limits=Limits(websocket_total_bytes=3,recheck_seconds=.25))
        async def close(_):pass
        lease=h.gateway.websocket_lease(cookie=f.cookie().token,origin=ORIGIN,request_origin=ORIGIN)
        try:
            await lease.start(close)
            results=await asyncio.gather(lease.permit_frame(b'12'),lease.permit_frame(b'34'),return_exceptions=True)
            assert any(isinstance(r,GatewayError) for r in results)
            assert lease.bytes<=3 and lease.closed
        finally:await lease.close();h.gateway.close()
    run(test())


def test_lifetime_and_idle_guards_bound_unresponsive_backend(f):
    async def test():
        h=await Harness(f,limits=Limits(idle_seconds=.1,lifetime_seconds=.2,recheck_seconds=.03)).start();h.delay=.4
        try:
            before=time.monotonic()
            events=await h.request()
            assert events[0]['status'] in (403,504)
            assert time.monotonic()-before < .35
        finally:await h.stop()
    run(test())


def test_stalled_asgi_send_has_backpressure_and_deadline(f):
    async def test():
        h=await Harness(f,limits=Limits(chunk_bytes=1,idle_seconds=.1,recheck_seconds=.03)).start()
        count=0
        async def stall(event):
            nonlocal count
            if event['type']=='http.response.body':
                count+=1
                await asyncio.sleep(.3)
        try:
            with pytest.raises(ConnectionError):await h.request(send_hook=stall)
            assert count==1
        finally:await h.stop()
    run(test())


def test_slow_registry_authority_has_bounded_slots_and_timeout(f):
    async def test():
        h=Harness(f,limits=Limits(recheck_seconds=.03,idle_seconds=.1))
        def slow():
            time.sleep(.15)
            return True
        try:
            calls=[asyncio.create_task(h.gateway._registry_call(slow)) for _ in range(4)]
            await asyncio.sleep(.01)
            with pytest.raises(GatewayError,match='authorization_busy'):
                await h.gateway._registry_call(slow)
            results=await asyncio.gather(*calls,return_exceptions=True)
            assert all(isinstance(r,asyncio.TimeoutError) for r in results)
            with pytest.raises(GatewayError,match='authorization_busy'):
                await h.gateway._registry_call(slow)
            await asyncio.sleep(.2)
            assert await h.gateway._registry_call(lambda:True)
        finally:h.gateway.close()
    run(test())


def test_stale_authority_is_checked_before_scheduling_output(f):
    from cloudworkbench.preview_gateway import _Guard
    async def test():
        h=Harness(f)
        guard=_Guard(h.gateway,f.cookie().token,ORIGIN,None,'GET')
        written=[]
        async def write():written.append(b'not-authorized')
        try:
            await guard.check(force=True)
            f.active=False
            guard.checked-=1
            with pytest.raises(GatewayError):await guard.wait(write())
            assert written==[]
        finally:h.gateway.close()
    run(test())


def test_connection_named_cookies_are_not_reintroduced(f):
    async def test():
        h=await Harness(f).start()
        h.raw_response=b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: set-cookie\r\nSet-Cookie: app=server\r\n\r\n'
        try:
            headers=[(b'host',ORIGIN[8:].encode()),(b'cookie',(COOKIE_NAME+'='+f.cookie().token+'; app=client').encode()),(b'connection',b'cookie')]
            events=await h.request(headers=headers)
            assert b'cookie:' not in h.requests[0]
            assert all(k!=b'set-cookie' for k,v in events[0]['headers'])
        finally:await h.stop()
    run(test())


def test_failed_websocket_close_keeps_capacity_reserved_until_retry(f):
    async def test():
        h=Harness(f,limits=Limits(max_connections=1,recheck_seconds=.1))
        bad=True
        async def close(_):
            if bad:raise OSError('socket close not confirmed')
        lease=h.gateway.websocket_lease(cookie=f.cookie().token,origin=ORIGIN,request_origin=ORIGIN)
        try:
            await lease.start(close)
            with pytest.raises(GatewayError,match='websocket_close_unconfirmed'):await lease.close()
            assert lease.closed and h.gateway.active==1
            with pytest.raises(GatewayError):await lease.permit_frame(b'late')
            bad=False
            await lease.close()
            assert h.gateway.active==0
        finally:
            bad=False;await lease.close();h.gateway.close()
    run(test())


def test_six_concurrent_authorized_gets_queue_without_false_revocation(f):
    async def test():
        h=await Harness(f).start()
        cookie=f.cookie()
        original=f.registry.lookup_cookie
        def slow(*args,**kwargs):
            time.sleep(.05)
            return original(*args,**kwargs)
        f.registry.lookup_cookie=slow
        headers=[(b'host',ORIGIN[8:].encode()),(b'cookie',(COOKIE_NAME+'='+cookie.token).encode())]
        try:
            results=await asyncio.gather(*(h.request(headers=headers) for _ in range(6)))
            assert [events[0]['status'] for events in results]==[200]*6
        finally:await h.stop()
    run(test())


def test_registry_busy_is_503_not_revocation(f):
    from cloudworkbench.preview_gateway import _Guard
    async def test():
        h=Harness(f)
        async def busy(*args,**kwargs):raise GatewayError('authorization_busy',503)
        h.gateway._registry_call=busy
        guard=_Guard(h.gateway,f.cookie().token,ORIGIN,None,'GET')
        try:
            with pytest.raises(GatewayError) as e:await guard.check(force=True)
            assert e.value.code=='authorization_busy' and e.value.status==503
        finally:h.gateway.close()
    run(test())


def test_slow_unauthenticated_exchange_cannot_take_http_capacity(f):
    async def test():
        h=await Harness(f,limits=Limits(max_connections=1,recheck_seconds=.25,idle_seconds=.5)).start()
        release=asyncio.Event()
        headers=[(b'host',ORIGIN[8:].encode()),(b'origin',DASH.encode()),(b'content-type',b'application/x-www-form-urlencoded')]
        def receive_factory():
            first=True
            async def receive():
                nonlocal first
                if first:
                    first=False
                    return {'type':'http.request','body':b'grant=','more_body':True}
                await release.wait()
                return {'type':'http.disconnect'}
            return receive
        tasks=[asyncio.create_task(h.request(path=EXCHANGE_PATH,headers=headers,method='POST',receive_hook=receive_factory())) for _ in range(2)]
        try:
            await asyncio.sleep(.02)
            assert h.gateway.exchanging==2 and h.gateway.active==0
            assert (await h.request())[0]['status']==200
            assert (await h.request(path=EXCHANGE_PATH,headers=headers,method='POST'))[0]['status']==503
        finally:
            release.set();await asyncio.gather(*tasks);await h.stop()
    run(test())


def test_plain_get_has_zero_length_not_chunked_upstream(f):
    async def test():
        h=await Harness(f).start()
        try:
            assert (await h.request())[0]['status']==200
            assert b'content-length: 0\r\n' in h.requests[0]
            assert b'transfer-encoding' not in h.requests[0]
        finally:await h.stop()
    run(test())


@pytest.mark.parametrize('method,status,length', [('HEAD',200,2),('GET',204,0),('GET',304,2)])
def test_head_and_bodiless_status_do_not_read_body(f,method,status,length):
    async def test():
        h=await Harness(f).start()
        h.raw_response=f'HTTP/1.1 {status} Test\r\nContent-Length: {length}\r\n\r\n'.encode()
        try:
            events=await h.request(method=method)
            assert events[0]['status']==status
            assert b''.join(e.get('body',b'') for e in events)==b''
        finally:await h.stop()
    run(test())


def test_mid_response_disconnect_closes_socket_promptly(f):
    async def test():
        h=await Harness(f).start()
        h.raw_response=b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nab\r\n'
        disconnected=asyncio.Event();chunks=[]
        async def hook(event):
            if event['type']=='http.response.body' and event.get('body'):
                chunks.append(event['body']);disconnected.set()
        try:
            start=time.monotonic()
            with pytest.raises(ConnectionError):
                await h.request(send_hook=hook,disconnect_event=disconnected)
            await asyncio.sleep(.02)
            assert h.closed==1 and h.gateway.active==0 and chunks==[b'ab']
            assert time.monotonic()-start<.5
        finally:await h.stop()
    run(test())


def test_completed_request_disconnect_during_connect_cancels_dial(f):
    async def test():
        h=await Harness(f).start()
        cancelled=[]
        async def delayed(*args):
            try:await asyncio.sleep(.5)
            finally:cancelled.append(True)
        h.gateway.transport.connect=delayed
        disconnected=asyncio.Event()
        async def drop():
            await asyncio.sleep(.03);disconnected.set()
        task=asyncio.create_task(drop())
        try:
            before=time.monotonic()
            events=await h.request(disconnect_event=disconnected)
            assert events[0]['status']==499 and cancelled==[True]
            assert time.monotonic()-before<.2
        finally:await task;await h.stop()
    run(test())


def test_upstream_header_parse_error_is_502(f):
    async def test():
        h=await Harness(f).start();h.raw_response=b'HTTP/1.1 200 OK\r\nX: a\tb\r\n\r\n'
        try:assert (await h.request())[0]['status']==502
        finally:await h.stop()
    run(test())
