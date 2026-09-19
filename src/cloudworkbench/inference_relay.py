"""D9 relay plumbing; no provider credentials, Docker access or lease authority.

The worker callback must enforce current grants and prove outer cleanup. Socket
write completion is not client consumption. Unknown HTTP delivery fences the
attempt; no HTTP request is silently replayed under a previous nonce.
"""
from dataclasses import dataclass
import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import select
import socket
import socketserver
import sqlite3
import stat
import struct
import threading
import time

from .inference_service import InferenceService, ServiceResponse

REQUEST_CAP = 262144
RESPONSE_CAP = 1048576
WIRE_CAP = 4 * RESPONSE_CAP // 3 + 4096
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_STATUSES = {200,400,401,403,409,413,422,429,502,503,504}
_ERRORS = {'unauthorized':401,'attempt_binding_mismatch':403,'execution_grant_unavailable':403,
           'invalid_nonce':400,'invalid_request':400,'payload_digest_mismatch':400,'nonce_conflict':409,
           'body_limit':413,'invalid_json':400,'relay_timeout':504,'request_cancelled':409}


class RelayError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AttemptBinding:
    attempt_id: str
    generation: int
    profile_digest: str

    def __post_init__(self):
        if (not isinstance(self.attempt_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',self.attempt_id)
                or type(self.generation) is not int or not 1 <= self.generation <= 2**31-1
                or not isinstance(self.profile_digest,str) or not _HEX.fullmatch(self.profile_digest)):
            raise RelayError('invalid_binding')

    def wire(self):
        return {'attempt_id':self.attempt_id,'generation':self.generation,'profile_digest':self.profile_digest}


def _binding_matches(value,binding):
    if not isinstance(value,dict) or set(value)!={'attempt_id','generation','profile_digest'}:return False
    try:return AttemptBinding(**value)==binding
    except RelayError:return False


@dataclass(frozen=True)
class DispatchContext:
    """Authenticated worker identity; never supplied as model transcript fields."""
    binding: AttemptBinding
    request_nonce: str
    payload_digest: str

    def __post_init__(self):
        if (type(self.binding) is not AttemptBinding or not isinstance(self.request_nonce,str)
                or not _HEX.fullmatch(self.request_nonce) or not isinstance(self.payload_digest,str)
                or not _HEX.fullmatch(self.payload_digest)):
            raise RelayError('invalid_dispatch_context')


@dataclass(frozen=True)
class DispatchResult:
    response: ServiceResponse
    outer_cleanup_confirmed: bool


def _json(value, maximum=WIRE_CAP):
    try:data=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    except (ValueError,TypeError,UnicodeError,RecursionError):raise RelayError('invalid_json') from None
    if len(data)>maximum:raise RelayError('body_limit')
    return data


def _parse(data):
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise RelayError('invalid_json')
            value[key]=item
        return value
    try:
        value=json.loads(data,object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _json(value)
        return value
    except RelayError:raise
    except (ValueError,UnicodeError,RecursionError):raise RelayError('invalid_json') from None


def _failure(code,status=503):
    return ServiceResponse(status,'application/json',_json({'error':{'code':code}}))


def _response(value):
    if (type(value) is not ServiceResponse or type(value.status) is not int or value.status not in _STATUSES
            or value.content_type not in ('application/json','text/event-stream')
            or type(value.body) is not bytes or len(value.body)>RESPONSE_CAP):
        raise RelayError('invalid_worker_response')
    return {'status':value.status,'content_type':value.content_type,'body':base64.b64encode(value.body).decode()}


def _response_from(value):
    if not isinstance(value,dict) or set(value)!={'status','content_type','body'} or not isinstance(value['body'],str):
        raise RelayError('invalid_worker_response')
    try:body=base64.b64decode(value['body'],validate=True)
    except ValueError:raise RelayError('invalid_worker_response') from None
    result=ServiceResponse(value['status'],value['content_type'],body);_response(result)
    return result


def _private_parent(path):
    if type(path) is not Path and not isinstance(path,Path):raise RelayError('unsafe_journal')
    if not path.is_absolute() or path.parent != path.parent.resolve():raise RelayError('unsafe_journal')
    info=path.parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise RelayError('unsafe_journal')


def _private_file(path):
    _private_parent(path)
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600:
            raise RelayError('unsafe_journal')
    except BaseException:os.close(fd);raise
    return fd


class _Journal:
    def __init__(self,path,binding):
        if type(binding) is not AttemptBinding:raise RelayError('invalid_binding')
        self.path=path;self.binding=binding
        fd=_private_file(path);os.fsync(fd);os.close(fd)
        self.db=sqlite3.connect(path,timeout=1,check_same_thread=False,isolation_level=None)
        self.lock=threading.Lock()
        self.db.execute('PRAGMA journal_mode=DELETE');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding BLOB NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS requests (nonce TEXT PRIMARY KEY,payload_digest TEXT NOT NULL,state TEXT NOT NULL,response BLOB)')
        encoded=_json(binding.wire())
        self.db.execute('INSERT OR IGNORE INTO identity VALUES (1,?)',(encoded,))
        if self.db.execute('SELECT binding FROM identity').fetchone()[0]!=encoded:
            self.db.close();raise RelayError('journal_binding_mismatch')
        directory=os.open(path.parent,os.O_RDONLY);os.fsync(directory);os.close(directory)

    def close(self):self.db.close()

    def begin(self):self.db.execute('BEGIN IMMEDIATE')


def _remaining(deadline,cancel):
    if cancel is not None and cancel.is_set():raise RelayError('request_cancelled')
    left=deadline-time.monotonic()
    if left<=0:raise RelayError('relay_timeout')
    return min(left,.1)


def _send(sock,data,deadline,cancel):
    view=memoryview(data)
    while view:
        sock.settimeout(_remaining(deadline,cancel))
        try:sent=sock.send(view)
        except socket.timeout:continue
        if sent==0:raise ConnectionError()
        view=view[sent:]


def _recv(sock,count,deadline,cancel):
    result=bytearray()
    while len(result)<count:
        sock.settimeout(_remaining(deadline,cancel))
        try:piece=sock.recv(count-len(result))
        except socket.timeout:continue
        if not piece:raise ConnectionError()
        result.extend(piece)
    return bytes(result)


def _receive_frame(sock,deadline,cancel,maximum):
    size=struct.unpack('!I',_recv(sock,4,deadline,cancel))[0]
    if not 0<size<=maximum:raise RelayError('body_limit')
    return _parse(_recv(sock,size,deadline,cancel))


def _send_frame(sock,value,deadline,cancel):
    data=_json(value);_send(sock,struct.pack('!I',len(data))+data,deadline,cancel)


class InferenceRelay:
    """One per attempt. Supply forward and delivery to the loopback HTTP service."""
    def __init__(self,*,socket_path,journal_path,binding,capability, fence, deadline_seconds=180,max_requests=32,max_retries=2):
        if (not isinstance(capability,str) or not _HEX.fullmatch(capability) or not callable(fence)
                or type(deadline_seconds) not in (int,float) or not 0<deadline_seconds<=180
                or type(max_requests) is not int or not 1<=max_requests<=32
                or type(max_retries) is not int or not 0<=max_retries<=2):raise RelayError('invalid_relay_config')
        if not isinstance(socket_path,Path) or not socket_path.is_absolute():raise RelayError('invalid_socket_path')
        self._owner_fd=_private_file(journal_path.with_suffix(journal_path.suffix+'.lock'))
        try:fcntl.flock(self._owner_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:os.close(self._owner_fd);raise RelayError('relay_already_owned') from None
        try:self.journal=_Journal(journal_path,binding)
        except BaseException:os.close(self._owner_fd);raise
        self.socket_path=socket_path;self.binding=binding;self._capability=capability;self.fence=fence
        self.deadline_seconds=deadline_seconds;self.max_requests=max_requests;self.max_retries=max_retries
        self._pending=None;self._guard=threading.Lock();self._fenced=False
        if self.journal.db.execute("SELECT 1 FROM requests WHERE state NOT IN ('http_write_completed','not_sent') LIMIT 1").fetchone():
            self._unknown()

    def _unknown(self):
        already_fenced=self._fenced
        self._fenced=True
        self.journal.db.execute("UPDATE requests SET state='response_delivery_unknown' WHERE state NOT IN ('http_write_completed','not_sent')")
        if not already_fenced:self.fence('response_delivery_unknown')

    def _not_sent(self):
        self.journal.db.execute("UPDATE requests SET state='not_sent' WHERE nonce=?",(self._pending,))
        self._pending=None

    def _pause(self,retry,deadline,cancel):
        end=min(deadline,time.monotonic()+(.25 if retry==0 else 1.0))
        while time.monotonic()<end:
            duration=min(_remaining(deadline,cancel),end-time.monotonic())
            if duration>0:
                if cancel is None:time.sleep(duration)
                else:cancel.wait(duration)
        _remaining(deadline,cancel)

    def forward(self,body,cancel):
        if not self._guard.acquire(False):return _failure('relay_busy',409)
        transmit_started=False
        try:
            if self._fenced:return _failure('response_delivery_unknown',409)
            if self._pending is not None:return _failure('http_delivery_pending',409)
            if cancel is not None and cancel.is_set():return _failure('request_cancelled',409)
            if not isinstance(body,dict):return _failure('invalid_request',400)
            raw=_json(body,REQUEST_CAP);digest=hashlib.sha256(raw).hexdigest();nonce=secrets.token_hex(32)
            self.journal.begin()
            try:
                if self.journal.db.execute('SELECT count(*) FROM requests').fetchone()[0]>=self.max_requests:
                    self.journal.db.rollback();return _failure('attempt_request_limit',429)
                self.journal.db.execute('INSERT INTO requests VALUES (?,?,?,NULL)',(nonce,digest,'journaled'))
                self.journal.db.commit()
            except BaseException:self.journal.db.rollback();raise
            self._pending=nonce
            envelope={'binding':self.binding.wire(),'capability':self._capability,'nonce':nonce,'payload_digest':digest,'payload':body}
            deadline=time.monotonic()+self.deadline_seconds
            for retry in range(self.max_retries+1):
                try:
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                        sock.settimeout(_remaining(deadline,cancel));sock.connect(str(self.socket_path))
                        transmit_started=True
                        _send_frame(sock,envelope,deadline,cancel)
                        wire=_receive_frame(sock,deadline,cancel,WIRE_CAP)
                    if isinstance(wire,dict) and set(wire)=={'error','status'} and isinstance(wire['error'],str) and type(wire['status']) is int and wire['status']==_ERRORS.get(wire['error']):
                        response=_failure(wire['error'],wire['status'])
                        self.journal.db.execute("UPDATE requests SET state='awaiting_http_write',response=? WHERE nonce=?",(_json(_response(response)),nonce))
                        return ServiceResponse(response.status,response.content_type,response.body,nonce)
                    if (not isinstance(wire,dict) or set(wire)!={'binding','nonce','payload_digest','response'}
                            or not _binding_matches(wire['binding'],self.binding) or wire['nonce']!=nonce or wire['payload_digest']!=digest):
                        raise RelayError('worker_binding_mismatch')
                    response=_response_from(wire['response'])
                    self.journal.db.execute("UPDATE requests SET state='awaiting_http_write',response=? WHERE nonce=?",(_json(_response(response)),nonce))
                    return ServiceResponse(response.status,response.content_type,response.body,nonce)
                except (OSError,ConnectionError):
                    if retry==self.max_retries:break
                    self._pause(retry,deadline,cancel)
            if transmit_started:self._unknown()
            else:self._not_sent()
            return _failure('response_delivery_unknown' if transmit_started else 'worker_unavailable')
        except RelayError as exc:
            if self._pending is not None:
                if transmit_started:self._unknown()
                else:self._not_sent()
            return _failure(exc.code,_ERRORS.get(exc.code,503))
        finally:self._guard.release()

    def delivery_result(self,response,response_sent):
        with self._guard:
            if (type(response) is not ServiceResponse or response.delivery_token is None
                    or response.delivery_token!=self._pending):return
            if response_sent is True and not self._fenced:
                self.journal.db.execute("UPDATE requests SET state='http_write_completed' WHERE nonce=?",(self._pending,))
                self._pending=None
            else:self._unknown()

    def close(self):
        self.journal.close();os.close(self._owner_fd)


class WorkerDispatcher:
    """Bounded synthetic plumbing; execute must own grants/leases and outer cleanup."""
    def __init__(self,*,journal_path,binding,capability_sha256,authorize,execute=None,execute_request=None,max_requests=32):
        if (not isinstance(capability_sha256,str) or not _HEX.fullmatch(capability_sha256) or not callable(authorize)
                or (execute is None)==(execute_request is None)
                or (execute is not None and not callable(execute))
                or (execute_request is not None and not callable(execute_request)) or type(max_requests) is not int or not 1<=max_requests<=32):raise RelayError('invalid_worker_config')
        self.journal=_Journal(journal_path,binding);self.binding=binding;self.capability_sha256=capability_sha256
        self.authorize=authorize;self.execute=execute;self.execute_request=execute_request;self.max_requests=max_requests

    def dispatch(self,request,cancel):
        if ((self.execute is None)==(self.execute_request is None)
                or (self.execute is not None and not callable(self.execute))
                or (self.execute_request is not None and not callable(self.execute_request))):
            raise RelayError('invalid_worker_config')
        if (not isinstance(request,dict) or set(request)!={'binding','capability','nonce','payload_digest','payload'}
                or not isinstance(request['capability'],str) or not _HEX.fullmatch(request['capability'])
                or not hmac.compare_digest(hashlib.sha256(request['capability'].encode()).hexdigest(),self.capability_sha256)):
            raise RelayError('unauthorized')
        if not _binding_matches(request['binding'],self.binding):
            supplied=request['binding']
            if (isinstance(supplied,dict) and supplied.get('attempt_id')==self.binding.attempt_id
                    and type(supplied.get('generation')) is int and supplied['generation']==self.binding.generation
                    and isinstance(request['nonce'],str) and _HEX.fullmatch(request['nonce'])):
                with self.journal.lock:
                    if self.journal.db.execute('SELECT 1 FROM requests WHERE nonce=?',(request['nonce'],)).fetchone():
                        raise RelayError('nonce_conflict')
            raise RelayError('attempt_binding_mismatch')
        if not isinstance(request['nonce'],str) or not _HEX.fullmatch(request['nonce']):raise RelayError('invalid_nonce')
        if not isinstance(request['payload'],dict):raise RelayError('invalid_request')
        digest=hashlib.sha256(_json(request['payload'],REQUEST_CAP)).hexdigest()
        if request['payload_digest']!=digest:raise RelayError('payload_digest_mismatch')
        if self.authorize(self.binding) is not True:raise RelayError('execution_grant_unavailable')
        nonce=request['nonce'];journal=self.journal
        with journal.lock:
            journal.begin()
            try:
                row=journal.db.execute('SELECT payload_digest,state,response FROM requests WHERE nonce=?',(nonce,)).fetchone()
                if row:
                    journal.db.commit()
                    if row[0]!=digest:raise RelayError('nonce_conflict')
                    if row[1]!='cached':response=_failure('dispatch_outcome_unknown')
                    else:response=_response_from(_parse(row[2]))
                    return self._wire(nonce,digest,response)
                if journal.db.execute('SELECT count(*) FROM requests').fetchone()[0]>=self.max_requests:
                    journal.db.rollback();return self._wire(nonce,digest,_failure('attempt_request_limit',429))
                journal.db.execute('INSERT INTO requests VALUES (?,?,?,NULL)',(nonce,digest,'admitted'))
                journal.db.commit()
            except BaseException:journal.db.rollback();raise
        try:
            if cancel.is_set() or self.authorize(self.binding) is not True:raise RelayError('execution_grant_unavailable')
            context=DispatchContext(self.binding,nonce,digest)
            result=(self.execute_request(context,request['payload'],cancel) if self.execute_request is not None
                    else self.execute(request['payload'],cancel))
            if type(result) is not DispatchResult or result.outer_cleanup_confirmed is not True:
                response=_failure('outer_cleanup_unconfirmed')
            else:
                response=result.response;_response(response)
            if cancel.is_set() or self.authorize(self.binding) is not True:response=_failure('request_cancelled',409)
        except Exception:response=_failure('dispatch_failed')
        with journal.lock:
            journal.db.execute("UPDATE requests SET state='cached',response=? WHERE nonce=?",(_json(_response(response)),nonce))
        return self._wire(nonce,digest,response)

    def _wire(self,nonce,digest,response):
        return {'binding':self.binding.wire(),'nonce':nonce,'payload_digest':digest,'response':_response(response)}

    def close(self):self.journal.close()


class _SocketHandler(socketserver.BaseRequestHandler):
    def handle(self):
        deadline=time.monotonic()+self.server.deadline_seconds
        cancel=threading.Event();done=threading.Event();watcher=None
        try:
            request=_receive_frame(self.request,min(deadline,time.monotonic()+5),cancel,REQUEST_CAP+4096)
            def watch():
                while not done.wait(.05):
                    if time.monotonic()>=deadline:cancel.set();return
                    try:
                        if select.select([self.request],[],[],0)[0]:cancel.set();return
                    except OSError:cancel.set();return
            watcher=threading.Thread(target=watch,daemon=True);watcher.start()
            response=self.server.dispatcher.dispatch(request,cancel)
            done.set();watcher.join(1)
            _send_frame(self.request,response,deadline,None)
        except RelayError as exc:
            try:_send_frame(self.request,{'error':exc.code if exc.code in _ERRORS else 'invalid_request','status':_ERRORS.get(exc.code,400)},deadline,None)
            except (RelayError,OSError):pass
        except OSError:pass
        finally:
            done.set()
            if watcher:watcher.join(1)


class WorkerSocketServer(socketserver.UnixStreamServer):
    request_queue_size=8
    def __init__(self,path,dispatcher,*,socket_gid=None,deadline_seconds=180):
        if not isinstance(path,Path) or not path.is_absolute() or path.parent!=path.parent.resolve():raise RelayError('unsafe_socket')
        info=path.parent.stat()
        if info.st_uid!=os.geteuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode&0o022:raise RelayError('unsafe_socket')
        if not 0<deadline_seconds<=180:raise RelayError('invalid_deadline')
        if socket_gid is not None and (type(socket_gid) is not int or socket_gid<0):raise RelayError('invalid_socket_group')
        if os.path.lexists(path):raise RelayError('socket_already_exists')
        self.dispatcher=dispatcher;self.deadline_seconds=deadline_seconds;self.path=path;self._identity=None
        super().__init__(str(path),_SocketHandler)
        info=path.lstat();self._identity=(info.st_dev,info.st_ino)
        try:
            os.chmod(path,0o600 if socket_gid is None else 0o660)
            if socket_gid is not None:os.chown(path,-1,socket_gid)
        except BaseException:self.server_close();raise

    def handle_error(self,*args):pass

    def server_close(self):
        super().server_close()
        try:
            info=self.path.lstat()
            if self._identity is not None and (info.st_dev,info.st_ino)==self._identity and stat.S_ISSOCK(info.st_mode):self.path.unlink()
        except FileNotFoundError:pass


def make_loopback_service(relay,*,address=('127.0.0.1',0),capability_sha256,authorize,execution_timeout=190,**kwargs):
    """Safe composition with correlated delivery and explicit ordered deadlines."""
    if type(relay) is not InferenceRelay or address[0] != '127.0.0.1':
        raise RelayError('invalid_loopback_service')
    if not relay.deadline_seconds < execution_timeout <= 190:
        raise RelayError('invalid_deadline_order')
    if set(kwargs)&{'execute','delivery','delivery_result'}:raise RelayError('reserved_service_callback')
    return InferenceService(address,capability_sha256=capability_sha256,authorize=authorize,
        execute=relay.forward,delivery_result=relay.delivery_result,execution_timeout=execution_timeout,**kwargs)
