from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from cloudworkbench.role_broker import BrokerError, ParentScope, RoleBroker


def authorize(db, scope):
    row = db.execute('SELECT * FROM fixture_parents WHERE id=?', (scope.parent_attempt_id,)).fetchone()
    return bool(row and row['owner']==scope.owner_id and row['project']==scope.project_id
                and row['root']==scope.root_attempt_id and row['generation']==scope.generation
                and row['session']==scope.native_session_id and not row['cancelled'])


@pytest.fixture
def setup(tmp_path):
    clock=[1000.0]
    broker=RoleBroker(tmp_path/'controller.sqlite',authorize_parent=authorize,clock=lambda:clock[0])
    scope=ParentScope('owner','project','root','parent',1,'native-session')
    with broker.transaction() as db:
        db.execute('CREATE TABLE fixture_parents(id TEXT PRIMARY KEY,owner TEXT,project TEXT,root TEXT,generation INT,session TEXT,cancelled INT)')
        db.execute('INSERT INTO fixture_parents VALUES(?,?,?,?,?,?,0)',(scope.parent_attempt_id,scope.owner_id,scope.project_id,scope.root_attempt_id,scope.generation,scope.native_session_id))
        db.execute('CREATE TABLE fixture_children(id TEXT PRIMARY KEY,request_id TEXT UNIQUE)')
    grant,token=broker.issue(scope,roles=('feature','judgment'),expires_at=2000)
    return broker,scope,grant,token,clock


def prepare(broker,token,call='native-call',**changes):
    return broker.prepare(token,native_session_id='native-session',native_call_id=call,role='feature',task='Implement fixture',**changes)


def code(status,name,fn):
    with pytest.raises(BrokerError) as error:fn()
    assert (error.value.status_code,error.value.code)==(status,name)


def test_hashed_capability_durable_pending_and_response_bound(setup):
    b,s,g,t,clock=setup
    request=prepare(b,t)
    with b.transaction() as db:
        row=db.execute('SELECT * FROM role_broker_grants').fetchone()
        assert row['token_hash']==hashlib.sha256(t.encode()).hexdigest()
        assert t not in json.dumps(dict(row))
        assert db.execute('PRAGMA synchronous').fetchone()[0]==2
    fresh=RoleBroker(b.path,authorize_parent=authorize,clock=lambda:1000)
    assert prepare(fresh,t)==request
    assert b.read(t,request['id'])==request
    assert len(json.dumps(request).encode())<1024**2


def test_native_correlation_is_not_authorization(setup):
    b,s,g,t,c=setup
    code(401,'capability_invalid',lambda:prepare(b,'a'*64))
    code(401,'capability_invalid',lambda:prepare(b,'not-a-capability'))
    code(403,'native_session_mismatch',lambda:b.prepare(t,native_session_id='other',native_call_id='call',role='feature',task='x'))
    code(403,'capability_role_denied',lambda:b.prepare(t,native_session_id=s.native_session_id,native_call_id='call',role='ungranted',task='x'))
    with b.transaction() as db:assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==0


def test_payload_conflict_and_genuinely_new_call(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    code(409,'native_call_payload_conflict',lambda:b.prepare(t,native_session_id=s.native_session_id,native_call_id='native-call',role='feature',task='Changed'))
    code(409,'native_call_payload_conflict',lambda:prepare(b,t,context_refs=({'artifact_id':'a','sha256':'a'*64},)))
    assert prepare(b,t,'new-call')['id']!=r['id']


def test_concurrent_same_key_returns_one_record(setup):
    b,s,g,t,c=setup
    with ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(lambda _:prepare(b,t),range(30)))
    assert len({r['id'] for r in results})==1
    with b.transaction() as db:assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==1


def test_durable_root_limit_counts_new_calls_not_retries_or_grants(setup):
    b,s,g,t,c=setup;b.max_root_requests=2
    first=prepare(b,t);prepare(b,t,'second')
    assert prepare(b,t)==first
    _,new=b.issue(s,roles=('feature',),expires_at=2000)
    code(429,'root_request_limit',lambda:prepare(b,new,'third'))
    with b.transaction() as db:assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==2


def test_concurrent_capacity_is_transactional(setup):
    b,s,g,t,c=setup;b.max_root_requests=3
    def submit(n):
        try:return prepare(b,t,str(n))['id']
        except BrokerError as e:return e.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(submit,range(16)))
    assert sum(isinstance(v,str) for v in results)==3 and results.count(429)==13


@pytest.mark.parametrize('mutation',['cancel','generation','owner','project','root','native'])
def test_parent_fence_rechecked_for_prepare_read_and_admission(setup,mutation):
    b,s,g,t,c=setup;r=prepare(b,t)
    column={'cancel':'cancelled','generation':'generation','owner':'owner','project':'project','root':'root','native':'session'}[mutation]
    value=1 if mutation=='cancel' else 2 if mutation=='generation' else 'changed'
    with b.transaction() as db:db.execute(f'UPDATE fixture_parents SET {column}=?',(value,))
    code(403,'parent_scope_denied',lambda:prepare(b,t,'new'))
    code(403,'parent_scope_denied',lambda:b.read(t,r['id']))
    with b.transaction() as db:
        code(403,'parent_scope_denied',lambda:b.admit_in_transaction(db,s,r['id'],lambda *_:pytest.fail('callback called')))


@pytest.mark.parametrize('expired',[False,True])
def test_revocation_and_exact_expiry_prevent_new_work(setup,expired):
    b,s,g,t,c=setup;r=prepare(b,t)
    if expired:c[0]=2000
    else:b.revoke(g)
    code(401,'capability_invalid',lambda:prepare(b,t))
    code(401,'capability_invalid',lambda:b.read(t,r['id']))
    with b.transaction() as db:code(401,'capability_invalid',lambda:b.admit_in_transaction(db,s,r['id'],lambda *_:pytest.fail('callback called')))


def test_cross_scope_guessed_ids_and_action_scope(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    other=replace(s,owner_id='other',parent_attempt_id='other-parent',native_session_id='other-session')
    with b.transaction() as db:db.execute('INSERT INTO fixture_parents VALUES(?,?,?,?,?,?,0)',(other.parent_attempt_id,other.owner_id,other.project_id,other.root_attempt_id,other.generation,other.native_session_id))
    _,other_token=b.issue(other,roles=('feature',),expires_at=2000)
    code(404,'role_request_not_found',lambda:b.read(other_token,r['id']))
    _,observe=b.issue(s,roles=('feature',),expires_at=2000,actions=('cloud_get_role_results',))
    code(403,'capability_action_denied',lambda:prepare(b,observe))
    assert b.read(observe,r['id'])==r
    _,submit=b.issue(s,roles=('feature',),expires_at=2000,actions=('cloud_request_roles',))
    code(403,'capability_action_denied',lambda:b.read(submit,r['id']))


def test_native_session_rebind_refused_even_after_grant_revoked(setup):
    b,s,g,t,c=setup;b.revoke(g)
    with b.transaction() as db:db.execute('UPDATE fixture_parents SET session=?',('new-session',))
    code(409,'parent_identity_already_bound',lambda:b.issue(replace(s,native_session_id='new-session'),roles=('feature',),expires_at=2000))


def test_authorization_exception_fails_closed_without_pending(setup):
    b,s,g,t,c=setup
    def fail(*args):raise RuntimeError('sensitive internal diagnostic')
    b.authorize_parent=fail
    code(503,'parent_authorization_unavailable',lambda:prepare(b,t))
    with b.transaction() as db:assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==0


def create_child(db,record):
    db.execute('INSERT INTO fixture_children VALUES(?,?)',('child-'+record['id'],record['id']))
    return 'child-'+record['id']


def test_child_mapping_and_link_commit_atomically_and_replay(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    with b.transaction() as db:linked=b.admit_in_transaction(db,s,r['id'],create_child)
    with b.transaction() as db:
        assert b.admit_in_transaction(db,s,r['id'],lambda *_:pytest.fail('duplicate child'))==linked
        assert db.execute('SELECT COUNT(*) FROM fixture_children').fetchone()[0]==1
    assert b.read(t,r['id'])['state']=='linked'


def test_admission_callback_failure_rolls_back_child_and_link(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    with pytest.raises(RuntimeError):
        with b.transaction() as db:
            def fail(db,row):create_child(db,row);raise RuntimeError('simulated crash')
            b.admit_in_transaction(db,s,r['id'],fail)
    assert b.read(t,r['id'])['state']=='pending'
    with b.transaction() as db:assert db.execute('SELECT COUNT(*) FROM fixture_children').fetchone()[0]==0


def crash_child(path,scope,request_id):
    b=RoleBroker(path,authorize_parent=authorize,clock=lambda:1000)
    with b.transaction() as db:
        def crash(db,row):create_child(db,row);os._exit(29)
        b.admit_in_transaction(db,scope,request_id,crash)


def test_actual_process_death_before_commit_rolls_back_mapping(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    process=multiprocessing.get_context('spawn').Process(target=crash_child,args=(b.path,s,r['id']))
    process.start();process.join(15);assert process.exitcode==29
    with b.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM fixture_children').fetchone()[0]==0
        assert b.admit_in_transaction(db,s,r['id'],create_child)['state']=='linked'


def test_concurrent_admission_creates_one_mapping(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    def admit(_):
        with b.transaction() as db:return b.admit_in_transaction(db,s,r['id'],create_child)
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(admit,range(12)))
    assert len({x['controller_request_id'] for x in results})==1
    with b.transaction() as db:assert db.execute('SELECT COUNT(*) FROM fixture_children').fetchone()[0]==1


@pytest.mark.parametrize('kwargs,expected',[
    ({'native_call_id':''},'native_identity_unavailable'),
    ({'native_call_id':'\n'},'native_identity_unavailable'),
    ({'task':'\ud800'},'invalid_role_request'),
    ({'task':'x'*32769},'role_request_exceeds_bound'),
    ({'context_refs':({'artifact_id':'x','sha256':'invalid'},)},'invalid_context_reference'),
    ({'context_refs':tuple({'artifact_id':'x','sha256':'a'*64} for _ in range(33))},'role_request_exceeds_bound'),
])
def test_bounded_untrusted_request_fields(setup,kwargs,expected):
    b,s,g,t,c=setup
    fields=dict(native_session_id='native-session',native_call_id='call',role='feature',task='x',context_refs=());fields.update(kwargs)
    code(400,expected,lambda:b.prepare(t,**fields))


def test_same_database_transaction_required(setup,tmp_path):
    b,s,g,t,c=setup;r=prepare(b,t)
    foreign=sqlite3.connect(tmp_path/'other.sqlite');foreign.execute('BEGIN IMMEDIATE')
    code(500,'controller_database_mismatch',lambda:b.admit_in_transaction(foreign,s,r['id'],create_child));foreign.close()
    with b._connect() as db:code(500,'controller_transaction_required',lambda:b.admit_in_transaction(db,s,r['id'],create_child))


def test_narrow_read_capability_cannot_observe_other_role(setup):
    b,s,g,t,c=setup;r=b.prepare(t,native_session_id=s.native_session_id,native_call_id='judge',role='judgment',task='Review')
    _,narrow=b.issue(s,roles=('feature',),expires_at=2000,actions=('cloud_get_role_results',))
    code(403,'capability_role_denied',lambda:b.read(narrow,r['id']))


def test_generation_change_never_adopts_old_native_request(setup):
    b,s,g,t,c=setup;first=prepare(b,t)
    with b.transaction() as db:db.execute('UPDATE fixture_parents SET generation=2')
    next_scope=replace(s,generation=2)
    _,next_token=b.issue(next_scope,roles=('feature',),expires_at=2000)
    later=prepare(b,next_token)
    assert later['request_key']!=first['request_key'] and later['id']!=first['id']
    code(404,'role_request_not_found',lambda:b.read(next_token,first['id']))


def test_caught_callback_failure_does_not_commit_partial_child(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    with b.transaction() as db:
        def invalid(db,row):create_child(db,row);return 'invalid mapping with spaces'
        code(500,'invalid_controller_mapping',lambda:b.admit_in_transaction(db,s,r['id'],invalid))
        assert db.execute('SELECT COUNT(*) FROM fixture_children').fetchone()[0]==0
    assert b.read(t,r['id'])['state']=='pending'


def test_read_snapshot_does_not_wait_for_writer_and_sees_committed_fence(setup):
    b,s,g,t,c=setup;r=prepare(b,t);b.busy_timeout_ms=20
    with b.transaction() as writer:
        writer.execute('UPDATE fixture_parents SET cancelled=1')
        assert b.read(t,r['id'])==r # WAL snapshot before this writer commits.
    code(403,'parent_scope_denied',lambda:b.read(t,r['id']))


def test_busy_error_is_fixed_and_diagnostic_contains_no_raw_message(setup):
    b,s,g,t,c=setup;diagnostics=[];b.diagnostic_hook=diagnostics.append;b.busy_timeout_ms=20
    with b.transaction():code(503,'storage_unavailable',lambda:prepare(b,t))
    assert diagnostics and diagnostics[-1]['code']=='storage_unavailable'
    assert set(diagnostics[-1]) <= {'code','exception_type','sqlite_errorcode'}
    with b.transaction() as db:assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==0


def test_deferred_snapshot_conflict_returns_fixed_503(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    with b._connect() as db:
        db.execute('BEGIN');db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()
        with b.transaction() as other:other.execute('UPDATE role_broker_grants SET expires_at=expires_at+1')
        code(503,'storage_unavailable',lambda:b.admit_in_transaction(db,s,r['id'],create_child))
        db.rollback()
    with b.transaction() as db:assert db.execute('SELECT COUNT(*) FROM fixture_children').fetchone()[0]==0


@pytest.mark.parametrize('expired',[False,True])
def test_rotated_capability_observes_blocked_then_explicitly_rejected_pending(setup,expired):
    b,s,g,t,c=setup;r=prepare(b,t)
    if expired:c[0]=2000;reason='capability_expired'
    else:b.revoke(g);reason='capability_revoked'
    _,new=b.issue(s,roles=('feature',),expires_at=3000)
    retry=prepare(b,new)
    assert retry['id']==r['id'] and retry['state']=='blocked' and retry['reason']==reason
    assert b.read(new,r['id'])==retry
    rejected=b.reject(s,r['id'],reason)
    assert rejected['state']=='rejected' and b.read(new,r['id'])==rejected
    assert b.reject(s,r['id'],reason)==rejected
    with b.transaction() as db:
        assert b.admit_in_transaction(db,s,r['id'],lambda *_:pytest.fail('rejected callback'))==rejected
        assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==1
    code(409,'role_request_already_resolved',lambda:b.reject(s,r['id'],'operator_rejected'))


def test_controller_can_reject_cancelled_parent_but_not_another_scope(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    with b.transaction() as db:db.execute('UPDATE fixture_parents SET cancelled=1')
    code(404,'role_request_not_found',lambda:b.reject(replace(s,owner_id='other'),r['id'],'parent_cancelled'))
    assert b.reject(s,r['id'],'parent_cancelled')['state']=='rejected'


def test_rejection_does_not_refund_root_quota_or_change_linked_history(setup):
    b,s,g,t,c=setup;b.max_root_requests=1;r=prepare(b,t)
    b.reject(s,r['id'],'operator_rejected')
    code(429,'root_request_limit',lambda:prepare(b,t,'new'))
    b.max_root_requests=2;r2=prepare(b,t,'new')
    with b.transaction() as db:b.admit_in_transaction(db,s,r2['id'],create_child)
    code(409,'role_request_already_resolved',lambda:b.reject(s,r2['id'],'operator_rejected'))


def test_private_and_explicit_shared_database_permissions(tmp_path):
    private=RoleBroker(tmp_path/'new-private'/'state.sqlite',authorize_parent=authorize)
    assert private.path.stat().st_mode & 0o777==0o600
    assert private.path.parent.stat().st_mode & 0o777==0o700
    with private._connect() as db:assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    unsafe=tmp_path/'unsafe.sqlite';unsafe.touch();unsafe.chmod(0o644)
    code(500,'unsafe_broker_permissions',lambda:RoleBroker(unsafe,authorize_parent=authorize))
    assert unsafe.stat().st_mode & 0o777==0o644 # Refuse rather than silently mutate existing permissions.
    shared=tmp_path/'shared.sqlite';shared.touch();shared.chmod(0o660)
    code(500,'unsafe_broker_permissions',lambda:RoleBroker(shared,authorize_parent=authorize))
    RoleBroker(shared,authorize_parent=authorize,shared_group=True)
    assert shared.stat().st_mode & 0o777==0o660


def test_refuses_non_wal_mode(tmp_path,monkeypatch):
    from contextlib import contextmanager
    class RefuseWal:
        def execute(self,*args):return self
        def fetchone(self):return ('delete',)
    @contextmanager
    def connection(self):yield RefuseWal()
    monkeypatch.setattr(RoleBroker,'_connect',connection)
    code(503,'wal_unavailable',lambda:RoleBroker(tmp_path/'db',authorize_parent=authorize))


@pytest.mark.parametrize('changes,error',[
    ({'roles':('feature','feature')},'invalid_role_scope'),({'roles':()},'invalid_role_scope'),
    ({'actions':()},'invalid_action_scope'),({'actions':('admin',)},'invalid_action_scope'),
    ({'expires_at':1000},'invalid_capability_expiry'),({'expires_at':87401},'invalid_capability_expiry'),
    ({'expires_at':float('nan')},'invalid_capability_expiry')])
def test_issuance_validation_codes(setup,changes,error):
    b,s,g,t,c=setup;kwargs={'roles':('feature',),'expires_at':2000};kwargs.update(changes)
    code(400,error,lambda:b.issue(s,**kwargs))


def test_configuration_clock_and_hardlink_refusal(tmp_path,setup):
    code(500,'invalid_broker_configuration',lambda:RoleBroker(tmp_path/'db',authorize_parent=None))
    code(500,'invalid_broker_configuration',lambda:RoleBroker(tmp_path/'db',authorize_parent=authorize,busy_timeout_ms=0))
    target=tmp_path/'target';target.touch();target.chmod(0o600);alias=tmp_path/'alias';alias.hardlink_to(target)
    code(500,'unsafe_broker_database',lambda:RoleBroker(alias,authorize_parent=authorize))
    b,s,g,t,c=setup;b.clock=lambda:float('nan')
    code(503,'clock_unavailable',lambda:prepare(b,t))


def test_authorization_failure_diagnostic_omits_secret_message(setup):
    b,s,g,t,c=setup;seen=[];b.diagnostic_hook=seen.append
    def fail(*_):raise RuntimeError('synthetic-sensitive-value')
    b.authorize_parent=fail
    code(503,'parent_authorization_unavailable',lambda:prepare(b,t))
    assert seen==[{'code':'parent_authorization_unavailable','exception_type':'RuntimeError'}]


def test_callback_rollback_reports_ended_transaction(setup):
    b,s,g,t,c=setup;r=prepare(b,t)
    with b.transaction() as db:
        def rollback(db,row):db.rollback();return 'mapping'
        code(500,'controller_callback_ended_transaction',lambda:b.admit_in_transaction(db,s,r['id'],rollback))
    assert b.read(t,r['id'])['state']=='pending'
