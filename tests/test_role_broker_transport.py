import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import threading
import time
from dataclasses import replace
import pytest
from cloudworkbench.role_broker import BrokerError,ParentScope,RoleBroker
from cloudworkbench.role_broker_transport import RoleClient,RoleSocketServer,_read,_write,REQUEST_LIMIT


def authorize(db,scope):
    row=db.execute('SELECT active,generation FROM fixture_parent WHERE id=?',(scope.parent_attempt_id,)).fetchone()
    return row is not None and row['active']==1 and row['generation']==scope.generation


def plugin():
    spec=importlib.util.spec_from_file_location('cloud_roles_test',Path(__file__).parents[1]/'plugins/cloud_roles/__init__.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


@pytest.fixture
def endpoint(tmp_path):
    # macOS UDS pathname limit requires a short private temp path.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='cwr-',dir='/tmp') as root:
        root=Path(root);clock=[1000]
        broker=RoleBroker(root/'state.db',authorize_parent=authorize,clock=lambda:clock[0])
        scope=ParentScope('owner','project','root','parent',1,'native-session')
        with broker.transaction() as db:
            db.execute('CREATE TABLE fixture_parent(id TEXT PRIMARY KEY,active INT,generation INT)')
            db.execute('INSERT INTO fixture_parent VALUES(?,1,1)',(scope.parent_attempt_id,))
        grant,token=broker.issue(scope,roles=('feature',),expires_at=2000)
        server=RoleSocketServer(root/'socket',broker,scope,hashlib.sha256(token.encode()).hexdigest(),timeout=.3,burst=100,requests_per_second=100)
        thread=threading.Thread(target=server.serve);thread.start()
        client=RoleClient(token,scope.native_session_id,str(root/'socket'),timeout=1)
        yield broker,scope,grant,client,server,clock,root
        server.close();thread.join(3);assert not thread.is_alive()


def request(client,call='native-call',**updates):
    args={'role':'feature','task':'Synthetic task'};args.update(updates)
    return client.call('cloud_request_roles',args,session_id='native-session',tool_call_id=call)


def error(status,code,fn):
    with pytest.raises(BrokerError) as caught:fn()
    assert (caught.value.status_code,caught.value.code)==(status,code)


def test_socket_durable_retry_payload_conflict_and_read(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    first=request(client);assert first['state']=='pending'
    assert request(client)==first
    assert RoleClient(client.capability,client.native_session_id,client.socket_path).call('cloud_get_role_results',{'request_id':first['id']},session_id=scope.native_session_id,tool_call_id='read-call')==first
    error(409,'native_call_payload_conflict',lambda:request(client,task='Changed'))
    assert request(client,'new-call')['id']!=first['id']
    fresh=RoleBroker(broker.path,authorize_parent=authorize,clock=lambda:1000)
    assert fresh.read(client.capability,first['id'])==first
    assert client.capability not in repr(client)


def test_lost_response_retry_has_one_durable_row(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    committed=threading.Event();original=server.dispatch
    def dispatch(first,body):
        result=original(first,body);committed.set();return result
    server.dispatch=dispatch
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.connect(client.socket_path)
        _write(connection,time.monotonic()+1,'POST /v1/request HTTP/1.1',{'capability':client.capability,'native_session_id':scope.native_session_id,'native_call_id':'native-call','arguments':{'role':'feature','task':'Synthetic task'}})
        assert committed.wait(2)
        # Drop receipt before reading it; same native identity must recover, not relaunch.
    result=request(client)
    with broker.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==1
        assert db.execute('SELECT id FROM role_broker_pending').fetchone()[0]==result['id']


def test_cancel_generation_expiry_revoke(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    first=request(client)
    with broker.transaction() as db:db.execute('UPDATE fixture_parent SET active=0')
    error(403,'parent_scope_denied',lambda:request(client))
    error(403,'parent_scope_denied',lambda:client.call('cloud_get_role_results',{'request_id':first['id']},session_id=scope.native_session_id,tool_call_id='read'))
    with broker.transaction() as db:db.execute('UPDATE fixture_parent SET active=1,generation=2')
    error(403,'parent_scope_denied',lambda:request(client))
    with broker.transaction() as db:db.execute('UPDATE fixture_parent SET generation=1')
    clock[0]=2000
    error(401,'capability_invalid',lambda:request(client))
    clock[0]=1000;broker.revoke(grant)
    error(401,'capability_invalid',lambda:request(client))


def test_endpoint_does_not_accept_another_valid_grant(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    _,other=broker.issue(scope,roles=('feature',),expires_at=2000)
    error(401,'capability_invalid',lambda:request(replace(client,capability=other)))
    error(500,'endpoint_scope_mismatch',lambda:RoleSocketServer(root/'wrong',broker,replace(scope,owner_id='another'),hashlib.sha256(client.capability.encode()).hexdigest()))
    assert not (root/'wrong').exists()


@pytest.mark.parametrize('extra',[{'session_id':'forged'},{'tool_call_id':'forged'},{'capability':'forged'},{'owner_id':'owner'},{'socket_path':'/elsewhere'}])
def test_plugin_rejects_model_identity_fields(endpoint,extra):
    broker,scope,grant,client,server,clock,root=endpoint
    handler=plugin().make_handlers(client)['cloud_request_roles']
    result=json.loads(handler({'role':'feature','task':'x',**extra},session_id=scope.native_session_id,tool_call_id='native'))
    assert result=={'error':'invalid_role_arguments','status':400}
    with broker.transaction() as db:assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==0


def test_plugin_native_ids_and_pending_only(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    handlers=plugin().make_handlers(client)
    assert json.loads(handlers['cloud_request_roles']({'role':'feature','task':'x'}))['error']=='native_identity_mismatch'
    value=json.loads(handlers['cloud_request_roles']({'role':'feature','task':'x'},session_id=scope.native_session_id,tool_call_id='native',task_id='ignored'))
    assert value['result']['state']=='pending' and value['result']['controller_request_id'] is None
    assert json.loads(handlers['cloud_get_role_results']({'request_id':value['result']['id']},session_id=scope.native_session_id,tool_call_id='read'))==value
    assert json.loads(handlers['cloud_get_role_results']({'request_id':value['result']['id'],'cursor':'anything'},session_id=scope.native_session_id,tool_call_id='read'))['error']=='invalid_role_arguments'


def raw(client,data):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as c:
        c.settimeout(2);c.connect(client.socket_path);c.sendall(data)
        return _read(c,time.monotonic()+2,1024**2,response=True)


@pytest.mark.parametrize('data,expected',[
    (b'POST /v1/request HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 999999\r\nConnection: close\r\n\r\n','message_exceeds_bound'),
    (b'POST /v1/request HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}','invalid_http_framing'),
    (b'POST /v1/request HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n','invalid_http_framing'),
])
def test_rejects_bad_framing_before_body(endpoint,data,expected):
    assert raw(endpoint[3],data)[1]=={'error':expected}


def test_duplicate_json_and_overlong_headers(endpoint):
    client=endpoint[3];body=b'{"capability":1,"capability":2}'
    header=f'POST /v1/request HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()
    assert raw(client,header+body)[1]=={'error':'invalid_json'}
    assert raw(client,b'x'*2048)[1]=={'error':'header_exceeds_bound'}


def test_absolute_header_deadline_and_shutdown(endpoint):
    client=endpoint[3]
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as c:
        c.connect(client.socket_path);start=time.monotonic();c.sendall(b'POST ')
        time.sleep(.18);c.sendall(b'/v1/request ')
        assert c.recv(1)==b''
        assert time.monotonic()-start < .6
    assert request(client)['state']=='pending'


def test_fixed_internal_error_and_response_size(endpoint):
    server=endpoint[4];client=endpoint[3];original=server.dispatch
    def fail(*args):raise ValueError('SENSITIVE-DO-NOT-RETURN')
    server.dispatch=fail
    error(503,'role_broker_unavailable',lambda:request(client))
    server.dispatch=lambda *args:{'oversized':'x'*(1024**2)}
    error(413,'message_exceeds_bound',lambda:request(client))
    server.dispatch=original
    error(400,'role_request_exceeds_bound',lambda:request(client,task='x'*REQUEST_LIMIT))


def test_per_attempt_rate_limit(endpoint):
    server=endpoint[4];client=endpoint[3];server.tokens=0;server.last=time.monotonic();server.rate=1
    error(429,'role_rate_limit',lambda:request(client))


def test_private_config_and_refusal_without_mutation(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    path=root/'client.json';path.write_text(json.dumps({'capability':client.capability,'native_session_id':scope.native_session_id}));path.chmod(0o400)
    loaded=RoleClient.from_file(path,socket_path=client.socket_path,expected_owner_uid=os.getuid());assert request(loaded)['state']=='pending'
    path.chmod(0o600)
    error(500,'client_configuration_unavailable',lambda:RoleClient.from_file(path))
    assert path.stat().st_mode & 0o777==0o600
    alias=root/'alias';alias.symlink_to(path)
    error(500,'client_configuration_unavailable',lambda:RoleClient.from_file(alias))
    assert path.is_file()
    error(500,'endpoint_scope_mismatch',lambda:RoleSocketServer(root/'unused',broker,scope,'a'*64))
    with pytest.raises(OSError):RoleSocketServer(root/'socket',broker,scope,hashlib.sha256(client.capability.encode()).hexdigest())
    assert request(client)['state']=='pending'


def test_absolute_body_deadline(endpoint):
    client=endpoint[3]
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as c:
        c.connect(client.socket_path);start=time.monotonic()
        c.sendall(b'POST /v1/request HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{')
        time.sleep(.18);c.sendall(b' ')
        assert c.recv(1)==b''
        assert time.monotonic()-start < .6
    assert request(client)['state']=='pending'


def test_response_write_has_absolute_deadline():
    left,right=socket.socketpair()
    try:
        left.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024)
        start=time.monotonic()
        with pytest.raises(TimeoutError):
            _write(left,start+.1,'HTTP/1.1 200 RoleBroker',{'result':{'payload':'x'*900000}},response=True)
        assert time.monotonic()-start < .5
    finally:left.close();right.close()


def test_controller_atomic_admit_visible_over_socket(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    first=request(client)
    calls=[]
    def create(db,record):
        calls.append(record['id'])
        db.execute('CREATE TABLE fixture_mapping(id TEXT PRIMARY KEY)')
        db.execute("INSERT INTO fixture_mapping VALUES('synthetic-controller-request')")
        return 'synthetic-controller-request'
    with broker.transaction() as db:broker.admit_in_transaction(db,scope,first['id'],create)
    with broker.transaction() as db:broker.admit_in_transaction(db,scope,first['id'],create)
    result=client.call('cloud_get_role_results',{'request_id':first['id']},session_id=scope.native_session_id,tool_call_id='read')
    assert result['state']=='linked' and result['controller_request_id']=='synthetic-controller-request'
    assert len(calls)==1


def test_oversize_arguments_refused_before_serializing_or_connecting(endpoint,monkeypatch):
    client=endpoint[3]
    def forbidden(*args,**kwargs):raise AssertionError('oversize data must be refused before serialization')
    monkeypatch.setattr('cloudworkbench.role_broker_transport.json.dumps',forbidden)
    error(400,'role_request_exceeds_bound',lambda:request(client,task='x'*(1024**2)))
    error(400,'role_request_exceeds_bound',lambda:request(client,context_refs=[{}]*100000))
    error(400,'invalid_request_id',lambda:client.call('cloud_get_role_results',{'request_id':'x'*(1024**2)},session_id='native-session',tool_call_id='read'))


def test_known_attempt_capability_cannot_be_persisted_as_model_text_or_call_id(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    error(400,'capability_in_request_data',lambda:request(client,task=client.capability))
    error(400,'capability_in_request_data',lambda:request(client,call=client.capability))
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as c:
        c.connect(client.socket_path)
        _write(c,time.monotonic()+1,'POST /v1/request HTTP/1.1',{'capability':client.capability,'native_session_id':scope.native_session_id,'native_call_id':'native','arguments':{'role':'feature','task':client.capability}})
        assert _read(c,time.monotonic()+1,1024**2,response=True)[1]=={'error':'capability_in_request_data'}
    with broker.transaction() as db:assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==0
    server.dispatch=lambda *args:{'task':client.capability}
    error(503,'unsafe_broker_response',lambda:request(client))


def test_lost_reply_reports_retryable_unknown_outcome_not_bad_task(endpoint,monkeypatch):
    import cloudworkbench.role_broker_transport as transport
    broker,scope,grant,client,server,clock,root=endpoint
    original=transport._write;drop=[True]
    def write(sock,deadline,first,value,**kwargs):
        if kwargs.get('response') and first.startswith('HTTP/1.1 200') and drop[0]:
            drop[0]=False;sock.shutdown(socket.SHUT_RDWR);raise OSError('synthetic disconnect')
        return original(sock,deadline,first,value,**kwargs)
    monkeypatch.setattr(transport,'_write',write)
    error(503,'role_broker_unavailable',lambda:request(client))
    result=request(client)
    with broker.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==1
        assert db.execute('SELECT id FROM role_broker_pending').fetchone()[0]==result['id']


def test_explicit_trusted_config_owner_policy(endpoint,monkeypatch):
    broker,scope,grant,client,server,clock,root=endpoint
    config=root/'different-owner.json';config.write_text(json.dumps({'capability':client.capability,'native_session_id':scope.native_session_id}));config.chmod(0o440)
    original=os.fstat;other_uid=os.getuid()+10001
    def stat_other(fd):
        values=list(original(fd));values[4]=other_uid;return os.stat_result(values)
    monkeypatch.setattr('cloudworkbench.role_broker_transport.os.fstat',stat_other)
    error(500,'client_configuration_unavailable',lambda:RoleClient.from_file(config))
    assert RoleClient.from_file(config,expected_owner_uid=other_uid).native_session_id==scope.native_session_id
    error(500,'invalid_client_configuration',lambda:RoleClient.from_file(config,expected_owner_uid=True))


def test_accept_failure_is_observable_and_recovers(endpoint):
    import errno
    server=endpoint[4];client=endpoint[3];listener=server.listener;failed=threading.Event()
    class Flaky:
        def accept(self):
            if not failed.is_set():
                failed.set();raise OSError(errno.EMFILE,'sensitive synthetic diagnostic')
            return listener.accept()
        def close(self):return listener.close()
    server.listener=Flaky()
    assert failed.wait(1)
    assert request(client)['state']=='pending'
    assert server.health()=={'state':'serving','accept_failures':1,'last_accept_errno':errno.EMFILE}


def test_large_valid_rate_limited_request_gets_fixed_429(endpoint):
    server=endpoint[4];client=endpoint[3];server.tokens=0;server.last=time.monotonic();server.rate=1
    error(429,'role_rate_limit',lambda:request(client,task='\x01'*32000))


def test_context_references_traverse_socket_and_bind_conflict(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    refs=[{'artifact_id':'artifact-'+str(i),'sha256':'a'*64} for i in range(32)]
    first=request(client,context_refs=refs)
    assert first['payload']['arguments']['context_refs']==refs
    assert request(client,context_refs=refs)==first
    changed=[dict(x) for x in refs];changed[0]['sha256']='b'*64
    error(409,'native_call_payload_conflict',lambda:request(client,context_refs=changed))
    error(400,'invalid_context_reference',lambda:request(client,'bad-context',context_refs=['not-a-record']))
    error(400,'role_request_exceeds_bound',lambda:request(client,'many-context',context_refs=refs+[refs[0]]))
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as c:
        c.connect(client.socket_path)
        _write(c,time.monotonic()+1,'POST /v1/request HTTP/1.1',{'capability':client.capability,'native_session_id':scope.native_session_id,'native_call_id':'bad-wire-context','arguments':{'role':'feature','task':'x','context_refs':['bad']}})
        assert _read(c,time.monotonic()+1,1024**2,response=True)[1]=={'error':'invalid_context_reference'}


def test_fresh_native_id_after_lost_reply_is_explicitly_a_new_request(endpoint):
    broker,scope,grant,client,server,clock,root=endpoint
    first=request(client,'first-call')
    second=request(client,'model-retry-new-call')
    assert second['id']!=first['id']
    error(409,'native_call_payload_conflict',lambda:request(client,'first-call',task='Different later turn'))
    with broker.transaction() as db:assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==2
