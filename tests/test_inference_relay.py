import hashlib
import json
import os
from pathlib import Path
import socket
import socketserver
import sqlite3
import threading
import time
import pytest
from cloudworkbench.inference_relay import (AttemptBinding,DispatchResult,InferenceRelay,WorkerDispatcher,WorkerSocketServer,RelayError,_receive_frame,_send_frame)
from cloudworkbench.inference_service import ServiceResponse

CAP='b'*64
BINDING=AttemptBinding('attempt-1',1,'a'*64)
PAYLOAD={'model':'claude-opus-4-6','messages':[{'role':'user','content':'hello'}]}
RESPONSE=ServiceResponse(200,'text/event-stream',b'data: [DONE]\n\n')

@pytest.fixture
def harness(tmp_path):
    # macOS Unix socket paths have a small kernel length cap.
    import tempfile
    root=Path(tempfile.mkdtemp(prefix='cwb-relay-',dir='/tmp')).resolve();root.chmod(0o700)
    relay_dir=root/'r';worker_dir=root/'w';relay_dir.mkdir(mode=0o700);worker_dir.mkdir(mode=0o700)
    calls=[];fences=[];live=[True]
    def execute(body,cancel):calls.append(body);return DispatchResult(RESPONSE,True)
    worker=WorkerDispatcher(journal_path=worker_dir/'worker.db',binding=BINDING,capability_sha256=hashlib.sha256(CAP.encode()).hexdigest(),authorize=lambda b:live[0],execute=execute)
    server=WorkerSocketServer(root/'socket',worker,deadline_seconds=2)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    relay=InferenceRelay(socket_path=root/'socket',journal_path=relay_dir/'relay.db',binding=BINDING,capability=CAP,fence=fences.append,deadline_seconds=2)
    yield root,relay,worker,server,calls,fences,live
    server.shutdown();server.server_close();thread.join();relay.close();worker.close()
    import shutil;shutil.rmtree(root)


def test_forward_durable_before_send_delivery_and_new_identical_prompt(harness):
    root,relay,worker,server,calls,fences,live=harness
    execute=worker.execute
    def checking(body,cancel):
        db=sqlite3.connect(root/'r/relay.db');row=db.execute('SELECT nonce,state FROM requests').fetchone();db.close()
        assert len(row[0])==64 and row[1]=='journaled'
        return execute(body,cancel)
    worker.execute=checking
    response=relay.forward(PAYLOAD,threading.Event());assert response.body==RESPONSE.body
    assert relay.forward(PAYLOAD,threading.Event()).status==409
    relay.delivery_result(response,True)
    worker.execute=execute
    response=relay.forward(PAYLOAD,threading.Event());assert response.body==RESPONSE.body;relay.delivery_result(response,True)
    assert len(calls)==2 and not fences
    assert relay.journal.db.execute('SELECT count(DISTINCT nonce) FROM requests').fetchone()[0]==2


def test_same_nonce_lost_uds_response_no_duplicate(harness):
    root,relay,worker,server,calls,fences,live=harness
    seen=[]
    class DropFirst(socketserver.BaseRequestHandler):
        def handle(self):
            request=_receive_frame(self.request,time.monotonic()+2,None,270000);seen.append(request['nonce'])
            response=worker.dispatch(request,threading.Event())
            if len(seen)>1:_send_frame(self.request,response,time.monotonic()+2,None)
    server.RequestHandlerClass=DropFirst
    response=relay.forward(PAYLOAD,threading.Event());assert response.body==RESPONSE.body
    assert len(calls)==1 and len(seen)==2 and seen[0]==seen[1]
    relay.delivery_result(response,True)


def test_http_unknown_fences_no_replay_even_new_relay(harness):
    root,relay,worker,server,calls,fences,live=harness
    response=relay.forward(PAYLOAD,threading.Event());assert response.body==RESPONSE.body;relay.delivery_result(response,False)
    assert relay.forward(PAYLOAD,threading.Event()).status==409 and len(calls)==1
    assert fences==['response_delivery_unknown']
    # Simulate a new process using its own journal handle after original releases its lock.
    relay.journal.close();os.close(relay._owner_fd)
    replacement=InferenceRelay(socket_path=root/'socket',journal_path=root/'r/relay.db',binding=BINDING,capability=CAP,fence=fences.append)
    assert replacement.forward(PAYLOAD,threading.Event()).status==409
    relay.journal=replacement.journal;relay._owner_fd=replacement._owner_fd


def envelope(nonce='c'*64,payload=PAYLOAD):
    from cloudworkbench.inference_relay import _json
    return {'binding':BINDING.wire(),'capability':CAP,'nonce':nonce,'payload_digest':hashlib.sha256(_json(payload)).hexdigest(),'payload':payload}


def test_worker_pending_crash_never_reexecutes_and_conflict(harness):
    root,relay,worker,server,calls,fences,live=harness
    value=envelope();worker.journal.db.execute('INSERT INTO requests VALUES (?,?,?,NULL)',(value['nonce'],value['payload_digest'],'admitted'))
    result=worker.dispatch(value,threading.Event());assert result['response']['status']==503 and not calls
    with pytest.raises(RelayError,match='nonce_conflict'):worker.dispatch(envelope(payload={'different':True}),threading.Event())

@pytest.mark.parametrize('change,code',[({'capability':'d'*64},'unauthorized'),({'binding':AttemptBinding('other',1,'a'*64).wire()},'attempt_binding_mismatch'),({'payload_digest':'x'},'payload_digest_mismatch'),({'nonce':'bad'},'invalid_nonce')])
def test_worker_identity_rejects_before_execution(harness,change,code):
    root,relay,worker,server,calls,fences,live=harness
    with pytest.raises(RelayError,match=code):worker.dispatch({**envelope(),**change},threading.Event())
    assert not calls and worker.journal.db.execute('SELECT count(*) FROM requests').fetchone()[0]==0


def test_current_auth_required_even_cached(harness):
    root,relay,worker,server,calls,fences,live=harness
    worker.dispatch(envelope(),threading.Event());live[0]=False
    with pytest.raises(RelayError,match='execution_grant_unavailable'):worker.dispatch(envelope(),threading.Event())
    assert len(calls)==1


def test_cleanup_uncertain_no_success_and_cancel_discards_success(harness):
    root,relay,worker,server,calls,fences,live=harness
    worker.execute=lambda body,cancel:DispatchResult(RESPONSE,False)
    assert worker.dispatch(envelope(),threading.Event())['response']['status']==503
    def cancelled(body,cancel):cancel.set();return DispatchResult(RESPONSE,True)
    worker.execute=cancelled
    assert worker.dispatch(envelope('d'*64),threading.Event())['response']['status']==409


def test_attempt_limit_and_exclusive_relay(harness):
    root,relay,worker,server,calls,fences,live=harness
    relay.max_requests=1
    response=relay.forward(PAYLOAD,threading.Event());assert response.body==RESPONSE.body;relay.delivery_result(response,True)
    assert relay.forward(PAYLOAD,threading.Event()).status==429 and len(calls)==1
    with pytest.raises(RelayError,match='relay_already_owned'):
        InferenceRelay(socket_path=root/'socket',journal_path=root/'r/relay.db',binding=BINDING,capability=CAP,fence=fences.append)


def test_existing_socket_preserved_and_close_inode_guard(harness):
    root,relay,worker,server,calls,fences,live=harness
    with pytest.raises(RelayError,match='socket_already_exists'):WorkerSocketServer(root/'socket',worker)
    assert (root/'socket').is_socket()
    (root/'socket').unlink();(root/'socket').write_text('replacement')
    server.server_close();assert (root/'socket').read_text()=='replacement'


def test_journal_binding_and_symlink_guard(harness):
    root,relay,worker,server,calls,fences,live=harness
    with pytest.raises(RelayError,match='journal_binding_mismatch'):
        WorkerDispatcher(journal_path=root/'w/worker.db',binding=AttemptBinding('other',1,'a'*64),capability_sha256=hashlib.sha256(CAP.encode()).hexdigest(),authorize=lambda b:True,execute=lambda b,c:None)
    (root/'w/link').symlink_to(root/'w/worker.db')
    with pytest.raises(OSError):WorkerDispatcher(journal_path=root/'w/link',binding=BINDING,capability_sha256=hashlib.sha256(CAP.encode()).hexdigest(),authorize=lambda b:True,execute=lambda b,c:None)


def test_absolute_timeout_no_worker_bytes_and_fence(harness):
    root,relay,worker,server,calls,fences,live=harness
    class Stall(socketserver.BaseRequestHandler):
        def handle(self):time.sleep(.25)
    server.RequestHandlerClass=Stall;relay.deadline_seconds=.12
    start=time.monotonic();response=relay.forward(PAYLOAD,threading.Event())
    assert response.status==504 and time.monotonic()-start<.5
    assert fences==['response_delivery_unknown'] and not calls

def test_oversize_before_journal_no_credentials_persisted(harness):
    root,relay,worker,server,calls,fences,live=harness
    assert relay.forward({'text':'x'*262145},threading.Event()).status==413
    assert relay.journal.db.execute('SELECT count(*) FROM requests').fetchone()[0]==0
    response=relay.forward(PAYLOAD,threading.Event());assert response.body==RESPONSE.body;relay.delivery_result(response,True)
    assert CAP.encode() not in (root/'r/relay.db').read_bytes()
    assert CAP.encode() not in (root/'w/worker.db').read_bytes()


def test_cached_response_survives_worker_restart(harness):
    root,relay,worker,server,calls,fences,live=harness
    first=worker.dispatch(envelope(),threading.Event())
    replacement=WorkerDispatcher(journal_path=root/'w/worker.db',binding=BINDING,capability_sha256=hashlib.sha256(CAP.encode()).hexdigest(),authorize=lambda b:True,execute=lambda b,c:pytest.fail('must not re-execute'))
    try:assert replacement.dispatch(envelope(),threading.Event())==first
    finally:replacement.close()
    assert len(calls)==1


def test_concurrent_duplicate_has_single_execution(harness):
    root,relay,worker,server,calls,fences,live=harness
    entered=threading.Event();release=threading.Event();out=[]
    def execute(body,cancel):calls.append(body);entered.set();release.wait(2);return DispatchResult(RESPONSE,True)
    worker.execute=execute
    thread=threading.Thread(target=lambda:out.append(worker.dispatch(envelope(),threading.Event())))
    thread.start();assert entered.wait(1)
    assert worker.dispatch(envelope(),threading.Event())['response']['status']==503
    release.set();thread.join(2)
    assert len(calls)==1 and out[0]['response']['status']==200


def test_invalid_worker_response_fences(harness):
    root,relay,worker,server,calls,fences,live=harness
    class Bad(socketserver.BaseRequestHandler):
        def handle(self):
            _receive_frame(self.request,time.monotonic()+1,None,270000)
            _send_frame(self.request,{'binding':{},'nonce':'x','payload_digest':'x','response':{}},time.monotonic()+1,None)
    server.RequestHandlerClass=Bad
    assert relay.forward(PAYLOAD,threading.Event()).status==503
    assert fences==['response_delivery_unknown'] and not calls


def test_hardlink_journal_rejected(harness):
    root,relay,worker,server,calls,fences,live=harness
    os.link(root/'w/worker.db',root/'w/hardlink')
    try:
        with pytest.raises(RelayError,match='unsafe_journal'):
            WorkerDispatcher(journal_path=root/'w/hardlink',binding=BINDING,capability_sha256=hashlib.sha256(CAP.encode()).hexdigest(),authorize=lambda b:True,execute=lambda b,c:None)
    finally:(root/'w/hardlink').unlink()


def test_worker_uncaught_callback_is_cached_fixed_failure(harness):
    root,relay,worker,server,calls,fences,live=harness
    def failed(body,cancel):calls.append(body);raise RuntimeError('private secret detail')
    worker.execute=failed
    first=worker.dispatch(envelope(),threading.Event())
    assert first['response']['status']==503 and b'private secret' not in json.dumps(first).encode()
    assert worker.dispatch(envelope(),threading.Event())==first and len(calls)==1

def test_correlated_delivery_ignores_rejected_second_call(harness):
    root,relay,worker,server,calls,fences,live=harness
    first=relay.forward(PAYLOAD,threading.Event())
    rejected=relay.forward(PAYLOAD,threading.Event());assert rejected.status==409
    relay.delivery_result(rejected,True)
    assert relay.journal.db.execute('SELECT state FROM requests').fetchone()[0]=='awaiting_http_write'
    relay.delivery_result(first,False)
    assert fences==['response_delivery_unknown']


def test_pre_cancel_and_never_connected_are_not_delivery_unknown(harness):
    root,relay,worker,server,calls,fences,live=harness
    cancel=threading.Event();cancel.set()
    assert relay.forward(PAYLOAD,cancel).status==409
    assert relay.journal.db.execute('SELECT count(*) FROM requests').fetchone()[0]==0
    relay.socket_path=root/'absent';relay.deadline_seconds=.15
    assert relay.forward(PAYLOAD,threading.Event()).status==504
    assert relay.journal.db.execute('SELECT state FROM requests').fetchone()[0]=='not_sent'
    assert not fences and not calls
    relay.socket_path=root/'socket';relay.deadline_seconds=2
    response=relay.forward(PAYLOAD,threading.Event());assert response.status==200
    relay.delivery_result(response,True)


def test_transient_missing_socket_recovers_after_bounded_backoff(harness):
    root,relay,worker,server,calls,fences,live=harness
    (root/'socket').rename(root/'holding')
    timer=threading.Timer(.1,lambda:(root/'holding').rename(root/'socket'));timer.start()
    try:
        start=time.monotonic();response=relay.forward(PAYLOAD,threading.Event())
    finally:timer.join()
    assert response.status==200 and .2<time.monotonic()-start<1.5
    relay.delivery_result(response,True);assert len(calls)==1 and not fences

@pytest.mark.parametrize('state',['journaled','awaiting_http_write'])
def test_restart_pending_states_fence(harness,state):
    root,relay,worker,server,calls,fences,live=harness
    relay.journal.db.execute('INSERT INTO requests VALUES (?,?,?,NULL)',('d'*64,'e'*64,state))
    relay.journal.close();os.close(relay._owner_fd)
    replacement=InferenceRelay(socket_path=root/'socket',journal_path=root/'r/relay.db',binding=BINDING,capability=CAP,fence=fences.append)
    assert fences==['response_delivery_unknown']
    relay.journal=replacement.journal;relay._owner_fd=replacement._owner_fd


def test_strict_binding_rejects_float_generation(harness):
    root,relay,worker,server,calls,fences,live=harness
    value=envelope();value['binding']['generation']=1.0
    with pytest.raises(RelayError,match='attempt_binding_mismatch'):worker.dispatch(value,threading.Event())
    assert not calls


def test_hostile_oversized_worker_length_and_partial_send_fence(harness,monkeypatch):
    import struct
    import cloudworkbench.inference_relay as module
    root,relay,worker,server,calls,fences,live=harness
    class Oversize(socketserver.BaseRequestHandler):
        def handle(self):
            _receive_frame(self.request,time.monotonic()+1,None,270000)
            self.request.sendall(struct.pack('!I',module.WIRE_CAP+1))
    server.RequestHandlerClass=Oversize
    assert relay.forward(PAYLOAD,threading.Event()).status==413
    assert fences==['response_delivery_unknown']


def test_partial_write_is_uncertain_not_not_sent(harness,monkeypatch):
    import cloudworkbench.inference_relay as module
    root,relay,worker,server,calls,fences,live=harness
    relay.max_retries=0
    def partial(sock,value,deadline,cancel):sock.sendall(b'\x00\x00');raise ConnectionError()
    monkeypatch.setattr(module,'_send_frame',partial)
    assert relay.forward(PAYLOAD,threading.Event()).status==503
    assert fences==['response_delivery_unknown']


def test_safe_factory_deadlines_and_actual_http_write_failure(harness):
    from http.client import HTTPConnection
    from cloudworkbench.inference_relay import make_loopback_service
    from cloudworkbench.inference_service import _Handler
    root,relay,worker,server,calls,fences,live=harness
    with pytest.raises(RelayError,match='invalid_deadline_order'):
        make_loopback_service(relay,capability_sha256=hashlib.sha256(CAP.encode()).hexdigest(),authorize=lambda:True,execution_timeout=1)
    http=make_loopback_service(relay,capability_sha256=hashlib.sha256(CAP.encode()).hexdigest(),authorize=lambda:True,execution_timeout=3)
    class Lost(_Handler):
        def _reply(self,status,body,content_type='application/json'):
            if status==200:
                self.connection.shutdown(socket.SHUT_RDWR);raise ConnectionError()
            return super()._reply(status,body,content_type)
    http.RequestHandlerClass=Lost
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    connection=HTTPConnection(*http.server_address,timeout=2)
    try:
        connection.request('POST','/v1/chat/completions',body=json.dumps(PAYLOAD),headers={'Authorization':'Bearer '+CAP,'Content-Type':'application/json'})
        with pytest.raises(Exception):connection.getresponse()
        deadline=time.monotonic()+1
        while not fences and time.monotonic()<deadline:time.sleep(.01)
        assert fences==['response_delivery_unknown'] and len(calls)==1
        assert relay.journal.db.execute('SELECT state FROM requests').fetchone()[0]=='response_delivery_unknown'
    finally:connection.close();http.shutdown();http.server_close();thread.join()


def test_reused_nonce_different_profile_conflicts(harness):
    root,relay,worker,server,calls,fences,live=harness
    worker.dispatch(envelope(),threading.Event())
    changed=envelope();changed['binding']['profile_digest']='d'*64
    with pytest.raises(RelayError,match='nonce_conflict'):worker.dispatch(changed,threading.Event())
    assert len(calls)==1


def test_authenticated_context_preserves_nonce_and_cached_retry(harness):
    from dataclasses import FrozenInstanceError
    root,relay,worker,server,calls,fences,live=harness
    seen=[];bodies=[];frozen=[]
    def execute_request(context, body, cancel):
        seen.append(context);bodies.append(dict(body))
        try:
            context.request_nonce = 'd'*64
        except FrozenInstanceError:
            frozen.append(True)
        else:
            frozen.append(False)
        return DispatchResult(RESPONSE, True)
    worker.execute=None;worker.execute_request=execute_request
    request=envelope()
    first=worker.dispatch(request,threading.Event())
    second=worker.dispatch(request,threading.Event())
    assert first==second and len(seen)==1
    assert first['response']['status']==200
    assert bodies==[PAYLOAD] and 'nonce' not in bodies[0] and frozen==[True]
    assert seen[0].binding==BINDING
    assert seen[0].request_nonce==request['nonce']
    assert seen[0].payload_digest==request['payload_digest']


@pytest.mark.parametrize('callbacks', [{}, {'execute':lambda a,b:None,'execute_request':lambda a,b,c:None}, {'execute_request':False}])
def test_callback_selection_is_exclusive(tmp_path, callbacks):
    with pytest.raises(RelayError,match='invalid_worker_config'):
        WorkerDispatcher(journal_path=tmp_path/'worker.db',binding=BINDING,
                         capability_sha256=hashlib.sha256(CAP.encode()).hexdigest(),
                         authorize=lambda b:True,**callbacks)
    assert not (tmp_path/'worker.db').exists()


def test_context_callback_not_called_when_authorization_fails(harness):
    root,relay,worker,server,calls,fences,live=harness
    worker.execute=None
    worker.execute_request=lambda *args:calls.append(args)
    live[0]=False
    with pytest.raises(RelayError,match='execution_grant_unavailable'):
        worker.dispatch(envelope(),threading.Event())
    assert not calls


def test_context_invalid_fields_and_mutated_callback_config(harness):
    from cloudworkbench.inference_relay import DispatchContext
    with pytest.raises(RelayError,match='invalid_dispatch_context'):
        DispatchContext(BINDING,'x','y')
    root,relay,worker,server,calls,fences,live=harness
    worker.execute_request=lambda *args:DispatchResult(RESPONSE,True)
    with pytest.raises(RelayError,match='invalid_worker_config'):
        worker.dispatch(envelope(),threading.Event())
    assert not calls


@pytest.mark.parametrize('revoke_at', [2,3])
def test_context_revocation_at_admission_and_after_execution(harness,revoke_at):
    root,relay,worker,server,calls,fences,live=harness
    checks=[]
    def authorize(binding):
        checks.append(binding)
        return len(checks)<revoke_at
    worker.authorize=authorize
    worker.execute=None
    def execute_request(context,payload,cancel):
        calls.append(context)
        return DispatchResult(RESPONSE,True)
    worker.execute_request=execute_request
    result=worker.dispatch(envelope(),threading.Event())
    assert result['response']['status']!=200
    assert len(calls)==(0 if revoke_at==2 else 1)
