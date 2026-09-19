"""Per-attempt bounded role HTTP over UDS; no scheduler, TCP or provider credentials."""
from dataclasses import asdict, dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time

from .role_broker import BrokerError, ParentScope, RoleBroker

REQUEST_LIMIT = 256 * 1024
RESPONSE_LIMIT = 1024 * 1024
HEADER_LIMIT = 2048
SOCKET_PATH = '/run/cloud-role-broker/socket'
CONFIG_PATH = '/run/cloud-role-broker/client.json'
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_CODE = re.compile(r'[a-z_]{1,80}\Z')


def native_id(value):
    return type(value) is str and 1 <= len(value) <= 256 and all(33 <= ord(c) <= 126 for c in value)


def _arguments(action, args):
    if type(args) is not dict:
        raise BrokerError(400, 'invalid_role_arguments')
    if action == 'cloud_request_roles':
        if not {'role','task'} <= set(args) or not set(args) <= {'role','task','context_refs'} or type(args.get('context_refs',[])) is not list:
            raise BrokerError(400, 'invalid_role_arguments')
        refs=args.get('context_refs',[])
        if len(refs) > 32:
            raise BrokerError(400, 'role_request_exceeds_bound')
        # Broker validation bounds every field BEFORE JSON serialization or copying refs.
        return json.loads(RoleBroker._payload(args['role'],args['task'],tuple(refs)))['arguments']
    if action == 'cloud_get_role_results':
        if not {'request_id'} <= set(args) or not set(args) <= {'request_id','cursor'} or args.get('cursor') is not None:
            raise BrokerError(400, 'invalid_role_arguments')
        identity=args['request_id']
        if type(identity) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',identity):
            raise BrokerError(400, 'invalid_request_id')
        return {'request_id':identity}
    raise BrokerError(400, 'unsupported_role_action')


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _decode(data):
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise BrokerError(400, 'invalid_json') from None
    if type(value) is not dict:
        raise BrokerError(400, 'invalid_json')
    return value


def _encode(value, limit):
    try:
        data = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    except (ValueError, TypeError, RecursionError):
        raise BrokerError(400, 'invalid_json') from None
    if len(data) > limit:
        raise BrokerError(413, 'message_exceeds_bound')
    return data


def _remaining(sock, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError()
    sock.settimeout(left)


def _read(sock, deadline, limit, *, response=False):
    data = bytearray()
    while b'\r\n\r\n' not in data:
        if len(data) >= HEADER_LIMIT:
            raise BrokerError(413, 'header_exceeds_bound')
        _remaining(sock, deadline)
        chunk = sock.recv(min(1024, HEADER_LIMIT - len(data)))
        if not chunk:
            raise BrokerError(400, 'incomplete_message')
        data.extend(chunk)
    raw, body = bytes(data).split(b'\r\n\r\n', 1)
    try:
        lines = raw.decode('ascii').split('\r\n')
        headers = {}
        for line in lines[1:]:
            key, value = line.split(':', 1)
            key = key.lower()
            if key in headers or key not in {'host','content-type','content-length','connection'} or value != ' ' + value.strip():
                raise ValueError()
            headers[key] = value.strip()
        required = {'content-type','content-length','connection'} | (set() if response else {'host'})
        if set(headers) != required or headers['content-type'] != 'application/json' or headers['connection'] != 'close':
            raise ValueError()
        if not response and headers['host'] != 'localhost':
            raise ValueError()
        if not re.fullmatch(r'0|[1-9][0-9]{0,6}', headers['content-length']):
            raise ValueError()
        size = int(headers['content-length'])
    except (ValueError, UnicodeError):
        raise BrokerError(400, 'invalid_http_framing') from None
    if size > limit:
        raise BrokerError(413, 'message_exceeds_bound')
    if len(body) > size:
        raise BrokerError(400, 'invalid_http_framing')
    while len(body) < size:
        _remaining(sock, deadline)
        chunk = sock.recv(min(8192, size - len(body)))
        if not chunk:
            raise BrokerError(400, 'incomplete_message')
        body += chunk
    return lines[0], _decode(body)


def _write(sock, deadline, first, value, *, response=False):
    body = _encode(value, RESPONSE_LIMIT if response else REQUEST_LIMIT)
    host = '' if response else 'Host: localhost\r\n'
    head = f'{first}\r\n{host}Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode('ascii')
    _remaining(sock, deadline)
    sock.sendall(head + body)


@dataclass(frozen=True)
class RoleClient:
    # These values come only from a controller-generated, read-only attempt mount.
    capability: str = field(repr=False)
    native_session_id: str
    socket_path: str = SOCKET_PATH
    timeout: float = 3.0

    def __post_init__(self):
        if type(self.capability) is not str or not _HEX.fullmatch(self.capability) or not native_id(self.native_session_id):
            raise BrokerError(500, 'invalid_client_configuration')
        if type(self.timeout) not in (int,float) or not 0 < self.timeout <= 10:
            raise BrokerError(500, 'invalid_client_configuration')

    @classmethod
    def from_file(cls, path=CONFIG_PATH, *, socket_path=SOCKET_PATH, expected_owner_uid=0):
        # Alternate paths are a trusted harness/operator interface, never model fields.
        if type(expected_owner_uid) is not int or expected_owner_uid < 0:
            raise BrokerError(500,'invalid_client_configuration')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != expected_owner_uid or stat.S_IMODE(info.st_mode) not in {0o400,0o440} or info.st_size > 4096:
                    raise BrokerError(500, 'unsafe_client_configuration')
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise BrokerError(500, 'unsafe_client_configuration')
            config = _decode(raw)
            if set(config) != {'capability','native_session_id'}:
                raise BrokerError(500, 'invalid_client_configuration')
            return cls(**config, socket_path=str(socket_path))
        except (OSError, BrokerError):
            raise BrokerError(500, 'client_configuration_unavailable') from None

    def call(self, action, arguments, *, session_id, tool_call_id):
        if session_id != self.native_session_id or not native_id(session_id) or not native_id(tool_call_id):
            raise BrokerError(403, 'native_identity_mismatch')
        route = {'cloud_request_roles':'/v1/request','cloud_get_role_results':'/v1/result'}.get(action)
        if route is None:
            raise BrokerError(400, 'unsupported_role_action')
        arguments = _arguments(action,arguments)
        public={'native_session_id':session_id,'native_call_id':tool_call_id,'arguments':arguments}
        if self.capability.encode() in _encode(public,REQUEST_LIMIT):
            raise BrokerError(400,'capability_in_request_data')
        request = {'capability':self.capability,**public}
        _encode(request, REQUEST_LIMIT)  # Bound before connect; no automatic retransmit.
        deadline = time.monotonic() + self.timeout
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                _remaining(connection, deadline)
                connection.connect(self.socket_path)
                _write(connection, deadline, f'POST {route} HTTP/1.1', request)
                try:
                    first, result = _read(connection, deadline, RESPONSE_LIMIT, response=True)
                except BrokerError as error:
                    # A missing reply may follow a committed request: never label it a bad task.
                    if error.code == 'incomplete_message':
                        raise BrokerError(503,'role_broker_unavailable') from None
                    raise BrokerError(502,'invalid_broker_response') from None
            match = re.fullmatch(r'HTTP/1.1 ([1-5][0-9]{2}) RoleBroker', first)
            if not match:
                raise BrokerError(502, 'invalid_broker_response')
            status = int(match[1])
            if status != 200:
                if set(result) != {'error'} or type(result['error']) is not str or not _CODE.fullmatch(result['error']):
                    raise BrokerError(502, 'invalid_broker_response')
                raise BrokerError(status,result['error'])
            if set(result) != {'result'} or type(result['result']) is not dict:
                raise BrokerError(502, 'invalid_broker_response')
            return result['result']
        except OSError:
            raise BrokerError(503, 'role_broker_unavailable') from None


class RoleSocketServer:
    """Controller-owned per-attempt endpoint. Serial bounded work; no task admission.

    scope and capability_hash are injected by trusted controller setup, never request
    arguments. Broker's same-DB parent authorization is still checked on every call.
    """
    def __init__(self, path, broker, scope: ParentScope, capability_hash, *, timeout=2.0,
                 requests_per_second=10, burst=20, socket_gid=None):
        if not isinstance(scope,ParentScope) or type(capability_hash) is not str or not _HEX.fullmatch(capability_hash):
            raise BrokerError(500,'invalid_server_configuration')
        if type(timeout) not in (int,float) or not 0 < timeout <= 10 or type(requests_per_second) is not int or not 1 <= requests_per_second <= 100 or type(burst) is not int or not 1 <= burst <= 100:
            raise BrokerError(500,'invalid_server_configuration')
        with broker._snapshot() as db:
            grant=db.execute('SELECT scope FROM role_broker_grants WHERE token_hash=?',(capability_hash,)).fetchone()
            if grant is None or json.loads(grant['scope']) != asdict(scope):
                raise BrokerError(500,'endpoint_scope_mismatch')
        self.path, self.broker, self.scope, self.capability_hash = Path(path),broker,scope,capability_hash
        self.timeout,self.rate,self.burst = timeout,requests_per_second,burst
        self.tokens,self.last = float(burst),time.monotonic()
        self.stop_event = threading.Event()
        self.accept_failures=0
        self.last_accept_errno=None
        self.state='ready'
        parent=self.path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise BrokerError(500,'unsafe_socket_directory')
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.path))  # Never remove an existing path, including stale sockets.
            self.identity=self.path.lstat()
            os.chmod(self.path,0o600)
            if socket_gid is not None:
                os.chown(self.path,-1,socket_gid)
                os.chmod(self.path,0o660)
            self.listener.listen(8)
            self.listener.settimeout(0.1)
        except BaseException:
            self.listener.close()
            self._unlink_owned()
            raise

    def _unlink_owned(self):
        if not hasattr(self,'identity'):
            return
        try:
            info=self.path.lstat()
            if (info.st_dev,info.st_ino)==(self.identity.st_dev,self.identity.st_ino):
                self.path.unlink()
        except FileNotFoundError:
            pass

    def dispatch(self, first, body):
        route={'POST /v1/request HTTP/1.1':'request','POST /v1/result HTTP/1.1':'result'}.get(first)
        if route is None:
            raise BrokerError(404,'role_route_not_found')
        if set(body) != {'capability','native_session_id','native_call_id','arguments'}:
            raise BrokerError(400,'invalid_role_envelope')
        token=body['capability']
        if type(token) is not str or not _HEX.fullmatch(token) or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),self.capability_hash):
            raise BrokerError(401,'capability_invalid')
        if not native_id(body['native_session_id']) or body['native_session_id'] != self.scope.native_session_id or not native_id(body['native_call_id']):
            raise BrokerError(403,'native_identity_mismatch')
        args=_arguments('cloud_request_roles' if route=='request' else 'cloud_get_role_results',body['arguments'])
        if token.encode() in _encode({k:v for k,v in body.items() if k!='capability'},REQUEST_LIMIT):
            raise BrokerError(400,'capability_in_request_data')
        if route=='request':
            return self.broker.prepare(token,native_session_id=body['native_session_id'],native_call_id=body['native_call_id'],role=args['role'],task=args['task'],context_refs=tuple(args['context_refs']))
        return self.broker.read(token,args['request_id'])

    def serve(self):
        self.state='serving'
        try:
            while not self.stop_event.is_set():
                try:
                    connection,_=self.listener.accept()
                except TimeoutError:
                    continue
                except OSError as error:
                    self.accept_failures=min(self.accept_failures+1,2**63-1)
                    self.last_accept_errno=error.errno if type(error.errno) is int else None
                    self.state='accept_degraded'
                    self.stop_event.wait(.05)  # Bounded backoff, cancellation remains responsive.
                    continue
                self.state='serving'
                with connection:
                    deadline=time.monotonic()+self.timeout
                    try:
                        first,body=_read(connection,deadline,REQUEST_LIMIT)
                        now=time.monotonic()
                        self.tokens=min(self.burst,self.tokens+(now-self.last)*self.rate);self.last=now
                        if self.tokens < 1:
                            raise BrokerError(429,'role_rate_limit')
                        self.tokens-=1
                        result=self.dispatch(first,body)
                        token=body.get('capability')
                        if type(token) is str and _HEX.fullmatch(token) and token.encode() in _encode(result,RESPONSE_LIMIT):
                            raise BrokerError(503,'unsafe_broker_response')
                        _write(connection,deadline,'HTTP/1.1 200 RoleBroker',{'result':result},response=True)
                    except Exception as error:
                        status,code=(error.status_code,error.code) if isinstance(error,BrokerError) else (503,'role_broker_unavailable')
                        if type(status) is not int or not 400 <= status <= 599 or type(code) is not str or not _CODE.fullmatch(code):
                            status,code=503,'role_broker_unavailable'
                        try:
                            _write(connection,deadline,f'HTTP/1.1 {status} RoleBroker',{'error':code},response=True)
                        except (OSError,BrokerError):
                            pass
        finally:
            self.listener.close()
            self._unlink_owned()
            self.state='stopped'

    def health(self):
        return {'state':self.state,'accept_failures':self.accept_failures,'last_accept_errno':self.last_accept_errno}

    def close(self):
        self.stop_event.set()
