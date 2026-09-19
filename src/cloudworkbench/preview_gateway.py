"""Disabled-by-default private preview HTTP gateway and WebSocket lease guard.

No DNS/TLS listener, Docker discovery, public route, or RFC6455 relay is installed.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
from dataclasses import dataclass
from http.cookies import SimpleCookie
import re
import ssl
import time
from urllib.parse import parse_qs, urlsplit

from .preview_registry import COOKIE_NAME, PreviewError, PreviewRegistry

EXCHANGE_PATH = '/__cwb/exchange'
_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
        'te', 'trailer', 'transfer-encoding', 'upgrade'}
_SECRET = {'authorization', 'proxy-authorization', 'forwarded', 'x-api-key',
           'x-auth-token', 'x-csrf-token'}


class GatewayError(Exception):
    def __init__(self, code, status=502):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class Limits:
    header_bytes: int = 32768
    header_count: int = 100
    request_bytes: int = 8 * 1024 * 1024
    response_bytes: int = 32 * 1024 * 1024
    chunk_bytes: int = 65536
    max_connections: int = 16
    idle_seconds: float = 15
    lifetime_seconds: float = 120
    recheck_seconds: float = 2
    websocket_frame_bytes: int = 65536
    websocket_total_bytes: int = 8 * 1024 * 1024
    websocket_frames: int = 4096

    def __post_init__(self):
        for key in ('header_bytes','header_count','request_bytes','response_bytes','chunk_bytes',
                    'max_connections','websocket_frame_bytes','websocket_total_bytes','websocket_frames'):
            value = getattr(self,key)
            if type(value) is not int or not 1 <= value <= 64 * 1024 * 1024:
                raise ValueError('invalid_gateway_limit')
        for key in ('idle_seconds','lifetime_seconds','recheck_seconds'):
            value = getattr(self,key)
            if type(value) not in (int,float) or not 0 < value <= 3600:
                raise ValueError('invalid_gateway_limit')
        if self.recheck_seconds > self.idle_seconds:
            raise ValueError('invalid_gateway_limit')


def _headers(raw, limits):
    if len(raw) > limits.header_count:
        raise GatewayError('headers_too_large', 431)
    result = []
    total = 0
    for name,value in raw:
        if not isinstance(name,bytes) or not isinstance(value,bytes):
            raise GatewayError('invalid_headers',400)
        total += len(name) + len(value) + 4
        if total > limits.header_bytes:
            raise GatewayError('headers_too_large',431)
        try:
            key = name.decode('ascii').lower()
            text = value.decode('ascii')
        except UnicodeDecodeError:
            raise GatewayError('invalid_headers',400) from None
        if not _NAME.fullmatch(key) or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise GatewayError('invalid_headers',400)
        result.append((key,text.strip()))
    return result


def _one(headers, name, default=None):
    values = [value for key,value in headers if key == name]
    if len(values) > 1:
        raise GatewayError('duplicate_header',400)
    return values[0] if values else default


def _platform_cookie(name):
    return name.lower().startswith(('__host-cloud_workbench', 'cloud_workbench'))


def _cookies(headers):
    raw = _one(headers,'cookie','')
    result = {}
    if raw:
        for part in raw.split(';'):
            name,sep,value = part.strip().partition('=')
            if (not sep or not _NAME.fullmatch(name) or name in result
                    or not value or any(c in value for c in '"\\, ') or any(ord(c) < 33 or ord(c) > 126 for c in value)):
                raise GatewayError('invalid_cookie',400)
            result[name] = value
    return result


def _path(scope):
    query = scope.get('query_string',b'')
    try:
        raw = scope['raw_path'] if 'raw_path' in scope else scope.get('path','').encode('ascii')
        target = raw.decode('ascii') + (('?' + query.decode('ascii')) if query else '')
    except (UnicodeError, AttributeError):
        raise GatewayError('invalid_target',400) from None
    if (len(target) > 8192 or not target.startswith('/') or target.startswith('//')
            or any(ord(c) < 33 or ord(c) > 126 for c in target) or '\\' in target or '#' in target):
        raise GatewayError('invalid_target',400)
    return target


def _hop_names(headers):
    connection = _one(headers,'connection','')
    names = {v.strip().lower() for v in connection.split(',') if v.strip()}
    if any(not _NAME.fullmatch(name) for name in names):
        raise GatewayError('invalid_connection',400)
    return _HOP | names


def _request_headers(headers, origin, cookies):
    blocked = _hop_names(headers) | _SECRET | {'host','cookie','content-length','expect','referer'}
    result = [(k,v) for k,v in headers if k not in blocked and not k.startswith('x-forwarded-')]
    app_cookies = '; '.join(k+'='+v for k,v in cookies.items() if not _platform_cookie(k))
    if app_cookies and 'cookie' not in _hop_names(headers):
        result.append(('cookie',app_cookies))
    result.extend([('host',urlsplit(origin).hostname), ('connection','close')])
    return result


def _response_headers(headers, origin):
    blocked = _hop_names(headers) | _SECRET | {'content-length','set-cookie', 'cache-control',
                                              'referrer-policy','x-content-type-options','refresh','clear-site-data','set-cookie2','alt-svc'}
    result = [(k,v) for k,v in headers if k not in blocked and not k.startswith('x-forwarded-')]
    location = _one(headers,'location')
    if location:
        p = urlsplit(location)
        if ('\\' in location or location.startswith('//') or p.username or p.password
                or (p.scheme and (p.scheme != 'https' or p.netloc != urlsplit(origin).netloc))
                or (not p.scheme and (p.netloc or not location.startswith('/')))):
            raise GatewayError('redirect_denied')
    for name,value in headers:
        if name == 'set-cookie' and name not in _hop_names(headers):
            first,*attrs = value.split(';')
            key,sep,cookie_value = first.strip().partition('=')
            if not sep or not _NAME.fullmatch(key) or _platform_cookie(key):
                raise GatewayError('upstream_cookie_denied')
            if any(a.strip().partition('=')[0].lower() == 'domain' for a in attrs):
                raise GatewayError('upstream_cookie_denied')
            parsed = SimpleCookie()
            try:
                parsed.load(value)
            except Exception:
                raise GatewayError('upstream_cookie_denied') from None
            if list(parsed) != [key]:
                raise GatewayError('upstream_cookie_denied')
            result.append((name,value))
    result.extend([('cache-control','no-store'),('referrer-policy','no-referrer'),('x-content-type-options','nosniff')])
    return [(k.encode('ascii'),v.encode('ascii')) for k,v in result]


class BackendTransport:
    """Only a validated numeric WorkerBackend can be dialed; never a request URL."""
    async def connect(self, authorized, limits):
        backend = authorized.backend
        context = ssl.create_default_context() if backend.protocol == 'https' else None
        return await asyncio.open_connection(backend.address,backend.port,ssl=context,
            server_hostname=backend.address if context else None,limit=limits.header_bytes + 1)


class _Guard:
    def __init__(self, gateway, cookie, origin, request_origin, method, *, websocket=False):
        self.gateway,self.cookie,self.origin = gateway,cookie,origin
        self.request_origin,self.method,self.websocket = request_origin,method,websocket
        self.started = self.checked = time.monotonic()
        self.authorized = None
        self.disconnected = None

    async def check(self, *, force=False):
        if time.monotonic()-self.started >= self.gateway.limits.lifetime_seconds:
            raise GatewayError('connection_expired',403)
        if force or self.authorized is None or time.monotonic()-self.checked >= self.gateway.limits.recheck_seconds:
            try:
                authorized = await self.gateway._registry_call(self.gateway.registry.lookup_cookie,self.cookie,
                    preview_origin=self.origin,request_origin=self.request_origin,method=self.method,websocket=self.websocket)
                valid = await asyncio.wait_for(self.gateway.backend_validator(authorized),
                    timeout=self.gateway.limits.recheck_seconds)
            except GatewayError:
                raise
            except PreviewError:
                raise GatewayError('preview_denied',403) from None
            except asyncio.TimeoutError:
                raise GatewayError('authorization_timeout',503) from None
            except Exception:
                raise GatewayError('backend_unverified',403) from None
            if time.monotonic()-self.started >= self.gateway.limits.lifetime_seconds:
                raise GatewayError('connection_expired',403)
            if valid is not True or (self.authorized is not None and authorized != self.authorized):
                raise GatewayError('backend_unverified',403)
            self.authorized = authorized
            self.checked = time.monotonic()
        return self.authorized

    async def wait(self, awaitable):
        task = None
        began = time.monotonic()
        try:
            await self.check()
            if self.disconnected and self.disconnected.done():
                raise GatewayError('client_disconnected',499)
            task = asyncio.ensure_future(awaitable)
            while True:
                await self.check()
                if time.monotonic()-began >= self.gateway.limits.idle_seconds:
                    raise GatewayError('upstream_timeout',504)
                watched = {task} | ({self.disconnected} if self.disconnected else set())
                done,_ = await asyncio.wait(watched,timeout=self.gateway.limits.recheck_seconds,return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    return task.result()
                if self.disconnected and self.disconnected in done:
                    raise GatewayError('client_disconnected',499)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            elif task is None and inspect.iscoroutine(awaitable):
                awaitable.close()


class PreviewGateway:
    def __init__(self, registry: PreviewRegistry | None = None, *, backend_validator=None,
                 transport=None, limits=None):
        self.registry,self.backend_validator = registry,backend_validator
        self.transport = transport or BackendTransport()
        self.limits = limits or Limits()
        self.active = 0
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4,thread_name_prefix='preview-registry')
        self._registry_slots = asyncio.BoundedSemaphore(4)
        self.exchanging = 0

    async def _registry_call(self, call, *args, **kwargs):
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(self._registry_slots.acquire(),self.limits.recheck_seconds)
        except asyncio.TimeoutError:
            raise GatewayError('authorization_busy',503) from None
        try:
            future = self._pool.submit(call,*args,**kwargs)
        except Exception:
            self._registry_slots.release()
            raise GatewayError('authorization_unavailable',503) from None
        def release(_):
            try:
                loop.call_soon_threadsafe(self._registry_slots.release)
            except RuntimeError:
                pass  # The owning event loop has already shut down.
        future.add_done_callback(release)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.wait_for(asyncio.shield(wrapped),self.limits.recheck_seconds)

    def close(self):
        self._pool.shutdown(wait=False,cancel_futures=True)

    def _origin(self, scope, headers):
        host = _one(headers,'host')
        if scope.get('scheme') != 'https' or not host or 'https://' + host not in self.registry.origins:
            raise GatewayError('unknown_preview_origin',403)
        return 'https://' + host

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type':'websocket.close','code':1008})
            return
        if scope['type'] != 'http':
            raise RuntimeError('unsupported_scope')
        started = False
        writer = None
        guard = None
        admitted = False
        try:
            if self.registry is None or not callable(self.backend_validator):
                raise GatewayError('preview_unconfigured',503)
            headers = _headers(scope.get('headers',[]),self.limits)
            origin = self._origin(scope,headers)
            target = _path(scope)
            if scope.get('path') == EXCHANGE_PATH:
                if self.exchanging >= 2:
                    raise GatewayError('exchange_busy',503)
                self.exchanging += 1
                try:
                    await self._exchange(scope,headers,origin,receive,send)
                finally:
                    self.exchanging -= 1
                return
            cookies = _cookies(headers)
            cookie = cookies.get(COOKIE_NAME)
            if not cookie:
                raise GatewayError('preview_denied',403)
            if self.active >= self.limits.max_connections:
                raise GatewayError('preview_busy',503)
            self.active += 1; admitted = True
            method = scope.get('method','')
            guard = _Guard(self,cookie,origin,_one(headers,'origin'),method)
            authorized = await guard.check(force=True)
            if _one(headers,'transfer-encoding') or _one(headers,'expect'):
                raise GatewayError('request_framing_denied',400)
            declared = _one(headers,'content-length')
            if declared is not None and (not declared.isdecimal() or len(declared)>10 or int(declared)>self.limits.request_bytes):
                raise GatewayError('request_too_large',413)
            first = await guard.wait(receive())
            if first['type'] != 'http.request':
                raise GatewayError('client_disconnected',499)
            if declared is None and not first.get('body',b'') and not first.get('more_body',False):
                declared = '0'
            async def disconnect():
                while True:
                    event = await receive()
                    if event['type'] == 'http.disconnect':
                        return
            if not first.get('more_body',False):
                guard.disconnected = asyncio.create_task(disconnect())
            reader,writer = await guard.wait(self.transport.connect(authorized,self.limits))
            await guard.check(force=True)
            upstream = _request_headers(headers,origin,cookies)
            upstream.append(('content-length',declared) if declared is not None else ('transfer-encoding','chunked'))
            writer.write((method+' '+target+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in upstream)+'\r\n').encode('ascii'))
            await guard.wait(writer.drain())
            total = 0
            event = first
            while True:
                if event['type'] != 'http.request':
                    raise GatewayError('client_disconnected',499)
                body = event.get('body',b''); total += len(body)
                if total>self.limits.request_bytes or (declared is not None and total>int(declared)):
                    raise GatewayError('request_too_large',413)
                for offset in range(0,len(body),self.limits.chunk_bytes):
                    chunk = body[offset:offset+self.limits.chunk_bytes]
                    await guard.check()
                    writer.write(chunk if declared is not None else format(len(chunk),'x').encode()+b'\r\n'+chunk+b'\r\n')
                    await guard.wait(writer.drain())
                if not event.get('more_body',False):
                    break
                event = await guard.wait(receive())
            if declared is not None and total != int(declared):
                raise GatewayError('request_length_mismatch',400)
            if declared is None:
                writer.write(b'0\r\n\r\n'); await guard.wait(writer.drain())
            if guard.disconnected is None:
                guard.disconnected = asyncio.create_task(disconnect())
            try:
                status,response_headers = await self._read_head(reader,guard)
            except GatewayError as exc:
                if exc.status in (400,431):
                    raise GatewayError('invalid_upstream_headers') from None
                raise
            length = _one(response_headers,'content-length')
            transfer = _one(response_headers,'transfer-encoding')
            if transfer and (transfer.lower() != 'chunked' or length is not None):
                raise GatewayError('response_framing_denied')
            if length is not None and (not length.isdecimal() or len(length)>10 or int(length)>self.limits.response_bytes):
                raise GatewayError('response_too_large')
            if status == 204 and (length not in (None,'0') or transfer):
                raise GatewayError('response_framing_denied')
            outgoing = _response_headers(response_headers,origin)
            if length is not None and status != 204:
                outgoing.append((b'content-length',length.encode()))
            await guard.wait(send({'type':'http.response.start','status':status,'headers':outgoing})); started=True
            if method != 'HEAD' and status not in (204,304):
                async for chunk in self._response_body(reader,guard,length,transfer):
                    await guard.wait(send({'type':'http.response.body','body':chunk,'more_body':True}))
            await guard.wait(send({'type':'http.response.body','body':b'','more_body':False}))
        except (GatewayError,PreviewError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            if started:
                raise ConnectionError('preview_stream_closed') from None
            status = exc.status if isinstance(exc,GatewayError) else 502
            code = exc.code if isinstance(exc,GatewayError) else 'preview_unavailable'
            async with asyncio.timeout(self.limits.idle_seconds):
                await send({'type':'http.response.start','status':status,'headers':[(b'cache-control',b'no-store'),(b'content-type',b'text/plain')]})
                await send({'type':'http.response.body','body':code.encode(),'more_body':False})
        finally:
            if guard and guard.disconnected:
                guard.disconnected.cancel()
                await asyncio.gather(guard.disconnected,return_exceptions=True)
            if writer:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(),min(1,self.limits.idle_seconds))
                except (OSError,asyncio.TimeoutError):
                    writer.transport.abort()
            if admitted:
                self.active -= 1

    async def _read_head(self, reader, guard):
        status_line = await guard.wait(reader.readuntil(b'\r\n'))
        match = re.fullmatch(rb'HTTP/1\.[01] ([2-5][0-9]{2})(?: [\x20-\x7e]*)?\r\n',status_line)
        if not match:
            raise GatewayError('invalid_upstream_status')
        raw=[]; size=len(status_line)
        while True:
            line=await guard.wait(reader.readuntil(b'\r\n')); size+=len(line)
            if size>self.limits.header_bytes or len(raw)>self.limits.header_count:
                raise GatewayError('upstream_headers_too_large')
            if line==b'\r\n':
                break
            key,sep,value=line[:-2].partition(b':')
            if not sep:
                raise GatewayError('invalid_upstream_headers')
            raw.append((key,value.strip(b' ')))
        return int(match[1]),_headers(raw,self.limits)

    async def _response_body(self, reader, guard, length, transfer):
        total=0
        remaining=int(length) if length is not None else None
        while True:
            if transfer:
                line=await guard.wait(reader.readuntil(b'\r\n'))
                if not re.fullmatch(rb'[0-9A-Fa-f]{1,8}\r\n',line):
                    raise GatewayError('invalid_chunk')
                remaining=int(line,16)
                if remaining==0:
                    if await guard.wait(reader.readexactly(2)) != b'\r\n':
                        raise GatewayError('trailers_unsupported')
                    return
            if remaining==0:
                return
            count=min(self.limits.chunk_bytes,remaining) if remaining is not None else self.limits.chunk_bytes
            while count:
                chunk=await guard.wait(reader.read(count))
                if not chunk:
                    if remaining is not None:
                        raise GatewayError('truncated_upstream')
                    return
                total+=len(chunk)
                if total>self.limits.response_bytes:
                    raise GatewayError('response_too_large')
                yield chunk
                if remaining is not None:
                    remaining-=len(chunk)
                    count=min(self.limits.chunk_bytes,remaining)
                else:
                    break
            if not transfer:
                if remaining==0:
                    return
            elif await guard.wait(reader.readexactly(2)) != b'\r\n':
                raise GatewayError('invalid_chunk')

    async def _exchange(self, scope, headers, origin, receive, send):
        if (scope.get('method')!='POST' or scope.get('query_string')
                or _one(headers,'origin')!=self.registry.dashboard_origin
                or _one(headers,'content-type')!='application/x-www-form-urlencoded'):
            raise GatewayError('exchange_denied',403)
        body=bytearray()
        async with asyncio.timeout(min(2,self.limits.idle_seconds)):
            while True:
                event=await receive()
                if event['type']!='http.request':
                    raise GatewayError('client_disconnected',499)
                incoming=event.get('body',b'')
                if len(body)+len(incoming)>1024:
                    raise GatewayError('exchange_too_large',413)
                body.extend(incoming)
                if not event.get('more_body',False):
                    break
        try:
            fields=parse_qs(body.decode('ascii'),strict_parsing=True,max_num_fields=1)
            if set(fields)!={'grant'} or len(fields['grant'])!=1:
                raise ValueError
            cookie=await self._registry_call(self.registry.exchange,fields['grant'][0],preview_origin=origin,
                request_origin=self.registry.dashboard_origin)
        except (ValueError,PreviewError):
            raise GatewayError('exchange_denied',403) from None
        html=b'<!doctype html><meta name="referrer" content="no-referrer"><title>Preview authorized</title><a href="/">Open preview</a>'
        response=[(b'content-type',b'text/html; charset=utf-8'),(b'cache-control',b'no-store'),
            (b'referrer-policy',b'no-referrer'),(b'content-security-policy',b"default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"),
            (b'set-cookie',(COOKIE_NAME+'='+cookie.token+'; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age='+str(cookie.max_age)).encode())]
        async with asyncio.timeout(self.limits.idle_seconds):
            await send({'type':'http.response.start','status':200,'headers':response})
            await send({'type':'http.response.body','body':html,'more_body':False})

    def websocket_lease(self, *, cookie, origin, request_origin):
        if self.registry is None or not callable(self.backend_validator) or origin not in self.registry.origins:
            raise GatewayError('preview_unconfigured',503)
        return WebSocketLease(_Guard(self,cookie,origin,request_origin,'GET',websocket=True))


class WebSocketLease:
    """Lifecycle interface for a future framed transport; not an RFC6455 proxy."""
    def __init__(self, guard):
        self.guard=guard
        self.closed=False
        self.bytes=self.frames=0
        self.activity=time.monotonic()
        self.task=None
        self.on_close=None
        self.counted=False
        self._close_lock=asyncio.Lock()

    async def start(self, on_close):
        if self.task or self.counted or self.closed or not callable(on_close):
            raise GatewayError('websocket_closed',403)
        if self.guard.gateway.active >= self.guard.gateway.limits.max_connections:
            raise GatewayError('preview_busy',503)
        self.guard.gateway.active += 1; self.counted=True
        self.on_close=on_close
        try:
            await self.guard.check(force=True)
        except GatewayError:
            await self.close()
            raise
        async def watch():
            try:
                while True:
                    await asyncio.sleep(self.guard.gateway.limits.recheck_seconds)
                    await self.guard.check(force=True)
                    if time.monotonic()-self.activity>=self.guard.gateway.limits.idle_seconds:
                        raise GatewayError('websocket_idle',403)
            except GatewayError:
                try:
                    await self.close()
                except GatewayError:
                    pass  # Capacity stays reserved until the transport confirms closure.
        self.task=asyncio.create_task(watch())
        return self.guard.authorized

    async def permit_frame(self, data):
        if self.closed or self.task is None:
            raise GatewayError('websocket_closed',403)
        limits=self.guard.gateway.limits
        try:
            if not isinstance(data,bytes) or len(data)>limits.websocket_frame_bytes or self.bytes+len(data)>limits.websocket_total_bytes or self.frames>=limits.websocket_frames:
                raise GatewayError('websocket_limit',403)
            self.bytes+=len(data); self.frames+=1
            authorized=await self.guard.check(force=True)
            if self.closed:
                raise GatewayError('websocket_closed',403)
            self.activity=time.monotonic()
            return authorized
        except GatewayError:
            await self.close()
            raise

    async def close(self):
        self.closed=True
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel(); await asyncio.gather(self.task,return_exceptions=True)
        async with self._close_lock:
            if not self.counted:
                return
            if self.on_close:
                try:
                    await asyncio.wait_for(self.on_close(1008),self.guard.gateway.limits.recheck_seconds)
                except Exception:
                    raise GatewayError('websocket_close_unconfirmed',503) from None
            self.guard.gateway.active -= 1; self.counted=False
