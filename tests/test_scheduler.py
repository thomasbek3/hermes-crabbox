from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3
import pytest
from cloudworkbench.store import Store, StoreError
from cloudworkbench.scheduler import RoleScheduler, ChildCleanupReceipt
from cloudworkbench.provider_leases import ProviderLeases, LeaseError
from cloudworkbench.role_broker import RoleBroker, ParentScope, BrokerError


def request(agent='hermes'):
    return {'agent':agent,'goal':'synthetic scheduler qualification','project_id':'demo'}

@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path.resolve()/'state.db')
    owner=store.add_client('TEST scheduler','x'*40,['submit','observe','cancel'],['demo'])
    store.migrate_scheduler()
    leases=ProviderLeases(store,cleanup_verifier=lambda target:None,inspector_id='fixture',clock=lambda:1000)
    leases.register_account('synthetic-account',legacy_agent='hermes',persistent_owner_id='test-owner')
    scheduler=RoleScheduler(store,leases,clock=lambda:1000)
    plan=[{'ready':True,'profile':{'profile_id':'fixture','provider':'fixture','model':'fake','effort':'high','qualification_reference_verified':False}}]*2
    frozen={'accounts':{'synthetic-account':'test-owner'},'role_plans':{'implementation':plan},'provenance':{'fixture_only':True}}
    return store,owner,leases,scheduler,plan,frozen


def root(setup,key='root'):
    st,o,l,s,p,f=setup
    created=s.enqueue_root(o,request(),key,frozen=f)
    assert s.admit_root(created['attempt_id'],accounts=f['accounts'])['state']=='preparing'
    s.transition(created['attempt_id'],'running',expected_generation=1)
    return created


def broker(setup,created):
    st,o,l,s,p,f=setup
    b=RoleBroker(st.path,authorize_parent=s.authorize_parent,clock=lambda:1000)
    scope=ParentScope(o['id'],'demo',created['attempt_id'],created['attempt_id'],1,'native-root')
    grant,token=b.issue(scope,roles=('implementation',),expires_at=2000)
    return b,scope,token


def pending(b,token,call='call'):
    return b.prepare(token,native_session_id='native-root',native_call_id=call,role='implementation',task='Test task')


def test_migration_preserves_history_and_legacy_claim(tmp_path):
    st=Store(tmp_path/'state.db');o=st.add_client('test','a'*40,['submit','observe'],['demo'])
    created=st.create_session(o,request('claude'),'legacy')
    before=st.get_attempt(created['attempt_id'])
    with st._connect() as db: events=[tuple(x) for x in db.execute('SELECT * FROM events')]
    st.migrate_scheduler();st=Store(st.path)
    after=st.get_attempt(created['attempt_id'])
    assert all(after[k]==v for k,v in before.items())
    assert after['execution_kind']=='legacy' and after['root_sequence']==1
    with st._connect() as db: assert events==[tuple(x) for x in db.execute('SELECT * FROM events')]
    assert st.claim_next()['id']==created['attempt_id']
    assert len(st.active_attempts())==1


def test_migration_refuses_live_without_partial_ddl(tmp_path):
    st=Store(tmp_path/'state.db');o=st.add_client('test','a'*40,['submit'],['demo'])
    st.create_session(o,request('claude'),'legacy');st.claim_next()
    with pytest.raises(StoreError):st.migrate_scheduler()
    with st._connect() as db:
        assert db.execute('SELECT version FROM schema_version').fetchone()[0]==1
        assert 'execution_kind' not in {r[1] for r in db.execute('PRAGMA table_info(attempts)')}


def test_migration_reopen_does_not_recreate_broad_indexes(setup):
    st,*_=setup
    st.migrate_scheduler();Store(st.path)
    with st._tx() as db: db.execute('DROP INDEX one_live_child')
    with pytest.raises(StoreError,match='schema differs'):Store(st.path)


def test_legacy_workers_never_claim_or_monitor_roots_or_children(setup):
    st,o,l,s,p,f=setup
    created=s.enqueue_root(o,request(),'root',frozen=f)
    assert st.claim_next() is None
    s.admit_root(created['attempt_id'],accounts=f['accounts'])
    assert st.active_attempts()==[]
    assert len(st.active_attempts(execution_kind='hermes_root'))==1
    with pytest.raises(StoreError,match='typed'):st.add_message(o,created['session_id'],'more','message')


def test_root_reserves_two_slots_against_legacy_and_other_root(setup):
    st,o,l,s,p,f=setup
    first=root(setup)
    other=s.enqueue_root(o,request(),'other',frozen=f)
    st.create_session(o,request('claude'),'legacy')
    assert s.admit_root(other['attempt_id'],accounts=f['accounts']) is None
    assert st.claim_next(capacity=100) is None
    s.transition(first['attempt_id'],'failed',expected_generation=1)
    assert st.claim_next() is None  # terminal state cannot prove cleanup


def test_legacy_live_delays_root(setup):
    st,o,l,s,p,f=setup
    st.create_session(o,request('claude'),'legacy');st.claim_next()
    created=s.enqueue_root(o,request(),'root',frozen=f)
    assert s.admit_root(created['attempt_id'],accounts=f['accounts']) is None


def test_same_account_root_guard_not_self_blocked(setup):
    st,o,l,s,p,f=setup
    created=root(setup)
    assert st.get_attempt(created['attempt_id'])['state']=='running'
    with st._tx() as db:
        assert db.execute('SELECT active_id FROM provider_accounts').fetchone()[0]


def test_accounts_all_or_none_rollback(setup):
    st,o,l,s,p,f=setup
    f={**f,'accounts':{**f['accounts'],'zz-missing':'missing'}}
    created=s.enqueue_root(o,request(),'root',frozen=f)
    with pytest.raises(LeaseError):s.admit_root(created['attempt_id'],accounts=f['accounts'])
    with st._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM provider_reservations').fetchone()[0]==0
        assert db.execute('SELECT active_id FROM provider_accounts').fetchone()[0] is None
    assert st.get_attempt(created['attempt_id'])['state']=='queued'


def test_reservation_transaction_requires_actual_lock(setup):
    st,o,l,s,p,f=setup
    with st._tx() as db,pytest.raises(LeaseError,match='locked_account'):
        l.reserve_in_transaction(db,'synthetic-account',persistent_owner_id='test-owner')


def test_atomic_broker_link_serial_children_and_cleanup(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    value=s.admit_request(b,scope,q['id'],plan=p)
    assert value['state']=='linked'
    assert s.admit_request(b,scope,q['id'],plan=p)==value
    with st._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM workflow_seats').fetchone()[0]==2
    with ThreadPoolExecutor(max_workers=8) as pool: claims=list(pool.map(lambda _:s.claim_child(created['attempt_id']),range(8)))
    child=next(c for c in claims if c)
    assert sum(c is not None for c in claims)==1
    assert child['generation']==1 and child['workflow_parent_generation']==1
    assert st.get_session(o,created['session_id'])['active_attempt_id']==created['attempt_id']
    assert len(st.active_attempts(execution_kind='hermes_child'))==1
    assert st.active_attempts()==[]
    s.transition(child['id'],'failed',expected_generation=1)
    assert s.claim_child(created['attempt_id']) is None
    with pytest.raises(StoreError):s.release_child(child['id'],expected_generation=1,verifier=lambda _:None)
    assert s.claim_child(created['attempt_id']) is None
    s.release_child(child['id'],expected_generation=1,verifier=lambda target:ChildCleanupReceipt(target,'confirmed_stopped','a'*64))
    assert s.claim_child(created['attempt_id'])['id']!=child['id']


def test_callback_crash_rolls_back_children_and_broker_link(setup,monkeypatch):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    original=st._event
    def fail(*args):raise RuntimeError('synthetic crash')
    monkeypatch.setattr(st,'_event',fail)
    with pytest.raises(RuntimeError):s.admit_request(b,scope,q['id'],plan=p)
    with st._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM workflow_requests').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==1
    assert b.read(t,q['id'])['state']=='pending'
    monkeypatch.setattr(st,'_event',original)
    assert s.admit_request(b,scope,q['id'],plan=p)['state']=='linked'


def test_concurrent_retries_single_panel_after_restart(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    with ThreadPoolExecutor(max_workers=6) as pool: values=list(pool.map(lambda _:s.admit_request(b,scope,q['id'],plan=p),range(6)))
    assert len({v['controller_request_id'] for v in values})==1
    fresh=RoleScheduler(Store(st.path),l,clock=lambda:1000)
    assert fresh.admit_request(b,scope,q['id'],plan=p)==values[0]
    with st._connect() as db:assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==3


def test_leaf_cannot_create_pending_even_with_scope_forgery(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p);child=s.claim_child(created['attempt_id'])
    with pytest.raises(BrokerError): b.issue(replace(scope,parent_attempt_id=child['id']),roles=('implementation',),expires_at=2000)
    with st._connect() as db:assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==1


def test_cancelled_parent_rejects_child_start_and_admission(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p)
    st.cancel(o,created['attempt_id'],'cancel')
    assert s.claim_child(created['attempt_id']) is None
    with pytest.raises(BrokerError): pending(b,t,'other')
    assert st.claim_next() is None


def test_immutable_kind_and_direct_start_sql_guards(setup):
    st,o,l,s,p,f=setup
    created=s.enqueue_root(o,request(),'root',frozen=f)
    with st._tx() as db,pytest.raises(sqlite3.IntegrityError,match='immutable'):
        db.execute("UPDATE attempts SET execution_kind='legacy' WHERE id=?",(created['attempt_id'],))
    with st._tx() as db,pytest.raises(sqlite3.IntegrityError,match='workflow_admission'):
        db.execute("UPDATE attempts SET state='preparing' WHERE id=?",(created['attempt_id'],))


def test_frozen_account_and_plan_drift_denied(setup):
    st,o,l,s,p,f=setup
    created=s.enqueue_root(o,request(),'root',frozen=f)
    with pytest.raises(StoreError,match='account mapping'):s.admit_root(created['attempt_id'],accounts={'wrong':'wrong'})
    s.admit_root(created['attempt_id'],accounts=f['accounts'])
    b,scope,t=broker(setup,created);q=pending(b,t)
    with pytest.raises(StoreError,match='role plan'):s.admit_request(b,scope,q['id'],plan=p[:1])
    assert b.read(t,q['id'])['state']=='pending'


def test_public_queue_limit_does_not_count_materialized_children(setup):
    st,o,l,s,p,f=setup
    st.policy['max_pending_attempts']=2
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p)
    st.create_session(o,request('claude'),'second')
    with pytest.raises(StoreError,match='capacity'):st.create_session(o,request('codex'),'third')


def test_generation_fence_requires_typed_reconciliation(setup):
    st,*_=setup
    created=root(setup)
    with pytest.raises(StoreError,match='reconciliation'):st.fence_attempt(created['attempt_id'],1)


def test_root_deadline_and_clock_rollback_block_child(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p)
    s.clock=lambda:999
    assert s.claim_child(created['attempt_id']) is None
    s.clock=lambda:20000
    assert s.claim_child(created['attempt_id']) is None


def test_root_seat_quota_concurrent_requests_no_partial_plan(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created)
    requests=[pending(b,t,'call'+str(i)) for i in range(20)]
    def admit(q):
        try: return s.admit_request(b,scope,q['id'],plan=p)
        except StoreError as exc:
            assert exc.status_code==429
            return None
    with ThreadPoolExecutor(max_workers=8) as pool: outcomes=list(pool.map(admit,requests))
    assert sum(x is not None for x in outcomes)==16
    with st._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM workflow_seats').fetchone()[0]==32
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==33
        assert db.execute('SELECT COUNT(*) FROM role_broker_pending WHERE controller_request_id IS NULL').fetchone()[0]==4


def test_sql_start_second_child_rejected_before_scheduler(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p);s.claim_child(created['attempt_id'])
    with st._tx() as db,pytest.raises(sqlite3.IntegrityError,match='workflow_admission'):
        db.execute("UPDATE attempts SET state='preparing' WHERE execution_kind='hermes_child' AND state='queued'")


def test_cleanup_receipt_cas_rejects_state_drift(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p);child=s.claim_child(created['attempt_id'])
    s.transition(child['id'],'failed',expected_generation=1)
    def changed(target):
        with st._tx() as db:db.execute('UPDATE workflow_roots SET version=version+1')
        return ChildCleanupReceipt(target,'confirmed_stopped','a'*64)
    with pytest.raises(StoreError,match='reservation changed'):s.release_child(child['id'],expected_generation=1,verifier=changed)
    assert s.claim_child(created['attempt_id']) is None


def test_expired_broker_capability_cannot_link_children(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    b.clock=lambda:2001
    with pytest.raises(BrokerError):s.admit_request(b,scope,q['id'],plan=p)
    with st._connect() as db:assert db.execute('SELECT COUNT(*) FROM workflow_seats').fetchone()[0]==0


def test_migration_rollback_if_final_guard_validation_fails(tmp_path,monkeypatch):
    from cloudworkbench import scheduler
    st=Store(tmp_path/'state.db')
    with monkeypatch.context() as m:
        m.setattr(scheduler,'verify_schema',lambda _:(_ for _ in ()).throw(StoreError(503,'synthetic')))
        with pytest.raises(StoreError):st.migrate_scheduler()
    with st._connect() as db:
        assert db.execute('SELECT version FROM schema_version').fetchone()[0]==1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_roots'").fetchone()
        assert 'execution_kind' not in {r[1] for r in db.execute('PRAGMA table_info(attempts)')}
    st.migrate_scheduler()


def test_revoked_owner_cannot_admit_root(setup):
    st,o,l,s,p,f=setup
    created=s.enqueue_root(o,request(),'root',frozen=f)
    with st._tx() as db:db.execute("UPDATE clients SET revoked_at='revoked' WHERE id=?",(o['id'],))
    with pytest.raises(StoreError,match='authority revoked'):s.admit_root(created['attempt_id'],accounts=f['accounts'])
    assert l.current('synthetic-account') is None


def test_new_controller_cannot_silently_adopt_root_accounts(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p)
    new_leases=ProviderLeases(st,cleanup_verifier=lambda _:None,inspector_id='new-controller',clock=lambda:1000)
    fresh=RoleScheduler(st,new_leases,clock=lambda:1000)
    assert fresh.claim_child(created['attempt_id']) is None
    with st._connect() as db:assert not fresh.authorize_parent(db,scope)
    assert st.claim_next() is None


def test_child_assignment_uses_role_task_and_frozen_profile(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p);child=s.claim_child(created['attempt_id'])
    assignment=s.child_assignment(child['id'],expected_generation=1)
    assert assignment['task']=='Test task'
    assert assignment['task']!=child['request']['goal']
    assert assignment['profile']==p[0]['profile']
    assert assignment['workflow_snapshot']==f
    with pytest.raises(StoreError):s.child_assignment(child['id'],expected_generation=2)
    with st._tx() as db,pytest.raises(sqlite3.IntegrityError,match='immutable'):
        db.execute("UPDATE workflow_seats SET profile='{}'")


def test_restart_cannot_transition_or_release_old_routed_attempts(setup):
    st,o,l,s,p,f=setup
    created=root(setup);b,scope,t=broker(setup,created);q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p);child=s.claim_child(created['attempt_id'])
    fresh_leases=ProviderLeases(st,cleanup_verifier=lambda _:None,inspector_id='fresh',clock=lambda:1000)
    fresh=RoleScheduler(Store(st.path),fresh_leases,clock=lambda:1000)
    for aid in (created['attempt_id'],child['id']):
        with pytest.raises(StoreError,match='ownership'):st.transition(aid,'running',expected_generation=1)
        with pytest.raises(StoreError,match='ownership'):fresh.transition(aid,'running',expected_generation=1)
    s.transition(child['id'],'failed',expected_generation=1)
    with pytest.raises(StoreError):fresh.release_child(child['id'],expected_generation=1,verifier=lambda target:ChildCleanupReceipt(target,'confirmed_stopped','a'*64))
    assert fresh.claim_child(created['attempt_id']) is None


def test_exact_provider_guard_rejects_substring_stub(setup):
    st,*_=setup
    with st._tx() as db:
        db.execute('DROP TRIGGER provider_guard_legacy_update')
        db.execute("CREATE TRIGGER provider_guard_legacy_update BEFORE UPDATE OF state ON attempts WHEN 0 AND NEW.execution_kind='legacy' BEGIN SELECT 1; END")
    with pytest.raises(StoreError,match='Provider legacy guard'):Store(st.path)


def test_migrate_actual_previous_store_source(tmp_path):
    import types
    from pathlib import Path
    previous=Path(__file__).parents[1]/'evidence/role-child-scheduler-review-snapshot/src/cloudworkbench/store.py'
    module=types.ModuleType('cloudworkbench._previous_store')
    module.__package__='cloudworkbench'
    exec(compile(previous.read_text(),str(previous),'exec'),module.__dict__)
    old=module.Store(tmp_path/'legacy.db');owner=old.add_client('TEST old','o'*40,['submit','observe'],['demo'])
    created=old.create_session(owner,request('claude'),'old');before=old.get_attempt(created['attempt_id'])
    new=Store(old.path);new.migrate_scheduler()
    assert all(new.get_attempt(created['attempt_id'])[k]==v for k,v in before.items())
    with pytest.raises(module.StoreError,match='Unsupported database schema'):module.Store(old.path)


def test_migration_conflict_reports_bounded_session_identity(tmp_path):
    st=Store(tmp_path/'state.db');o=st.add_client('test','b'*40,['submit'],['demo'])
    first=st.create_session(o,request('claude'),'one');st.cancel({**o,'scopes':['cancel']},first['attempt_id'],'cancel')
    second=st.add_message(o,first['session_id'],'next','two')
    with st._tx() as db:db.execute('UPDATE attempts SET generation=1 WHERE id=?',(second['attempt_id'],))
    with pytest.raises(StoreError,match=first['session_id']):st.migrate_scheduler()


def workflow_root(setup):
    st,o,l,s,p,f=setup
    p=p[:1]
    f={**f,'role_plans':{'implementation':p},'provenance':{'workflow':{'ready':True,'steps':[
        {'id':'review','role':'implementation','ready':True,'profile':p[0]['profile']},
        {'id':'build','role':'implementation','ready':True,'profile':p[0]['profile']}]}}}
    changed=(st,o,l,s,p,f)
    created=root(changed);b,scope,t=broker(changed,created)
    return changed,created,b,scope,t


def completed_step(st,s,child):
    s.transition(child['id'],'running',expected_generation=1)
    s.transition(child['id'],'verifying',expected_generation=1)
    s.transition(child['id'],'completed',expected_generation=1,outcome='verified')
    return st.register_artifact(child['id'],{'path':'review.json','sha256':'a'*64,'reviewed_revision_sha256':'b'*64},expected_generation=1)


def gate(s,child,artifact,decision='pass',**changes):
    return s.record_step_gate(child['id'],expected_generation=1,decision=decision,revision_sha256='b'*64,
        reviewed_artifact_id=artifact['id'],reviewed_artifact_sha256='a'*64,evidence_sha256=changes.get('evidence_sha256','c'*64))


def test_workflow_step_order_requires_exact_gate_and_cleanup(setup):
    (st,o,l,s,p,f),created,b,scope,t=workflow_root(setup)
    q=pending(b,t)
    with pytest.raises(StoreError,match='Prior workflow'):s.admit_request(b,scope,q['id'],plan=p,step_id='build',input_revision_sha256='b'*64)
    with pytest.raises(StoreError,match='Explicit frozen'):s.admit_request(b,scope,q['id'],plan=p,input_revision_sha256='b'*64)
    first=s.admit_request(b,scope,q['id'],plan=p,step_id='review',input_revision_sha256='b'*64)
    assert s.admit_request(b,scope,q['id'],plan=p,step_id='review',input_revision_sha256='b'*64)==first
    child=s.claim_child(created['attempt_id']);artifact=completed_step(st,s,child)
    second=pending(b,t,'second')
    with pytest.raises(StoreError,match='passing gate'):s.admit_request(b,scope,second['id'],plan=p,step_id='build',input_revision_sha256='b'*64)
    receipt=gate(s,child,artifact)
    assert gate(s,child,artifact)==receipt
    with pytest.raises(StoreError,match='immutable'):gate(s,child,artifact,evidence_sha256='d'*64)
    s.admit_request(b,scope,second['id'],plan=p,step_id='build',input_revision_sha256='b'*64)
    assert s.claim_child(created['attempt_id']) is None
    s.release_child(child['id'],expected_generation=1,verifier=lambda target:ChildCleanupReceipt(target,'confirmed_stopped','e'*64))
    next_child=s.claim_child(created['attempt_id'])
    assert next_child['id']!=child['id']
    assert s.child_assignment(next_child['id'],expected_generation=1)['task']=='Test task'


def test_rejected_review_blocks_later_step_durably(setup):
    (st,o,l,s,p,f),created,b,scope,t=workflow_root(setup)
    q=pending(b,t);s.admit_request(b,scope,q['id'],plan=p,step_id='review',input_revision_sha256='b'*64)
    child=s.claim_child(created['attempt_id']);artifact=completed_step(st,s,child)
    gate(s,child,artifact,'reject')
    s.release_child(child['id'],expected_generation=1,verifier=lambda target:ChildCleanupReceipt(target,'confirmed_stopped','e'*64))
    q2=pending(b,t,'second')
    fresh=RoleScheduler(Store(st.path),l,clock=lambda:1000)
    with pytest.raises(StoreError,match='passing gate'):fresh.admit_request(b,scope,q2['id'],plan=p,step_id='build',input_revision_sha256='b'*64)
    with pytest.raises(StoreError,match='immutable'):gate(s,child,artifact,'pass')
    assert fresh.claim_child(created['attempt_id']) is None


def test_sibling_review_artifact_cannot_authorize_child(setup):
    (st,o,l,s,p,f),created,b,scope,t=workflow_root(setup)
    q=pending(b,t);s.admit_request(b,scope,q['id'],plan=p,step_id='review',input_revision_sha256='b'*64)
    child=s.claim_child(created['attempt_id']);artifact=completed_step(st,s,child);gate(s,child,artifact)
    s.release_child(child['id'],expected_generation=1,verifier=lambda target:ChildCleanupReceipt(target,'confirmed_stopped','e'*64))
    s.admit_request(b,scope,pending(b,t,'second')['id'],plan=p,step_id='build',input_revision_sha256='b'*64)
    next_child=s.claim_child(created['attempt_id']);own_artifact=completed_step(st,s,next_child)
    with pytest.raises(StoreError,match='artifact binding'):gate(s,next_child,artifact)
    with pytest.raises(StoreError,match='assigned revision'):
        s.record_step_gate(next_child['id'],expected_generation=1,decision='pass',revision_sha256='f'*64,reviewed_artifact_id=own_artifact['id'],reviewed_artifact_sha256='a'*64,evidence_sha256='c'*64)
    assert gate(s,next_child,own_artifact)['decision']=='pass'


def test_unverified_prose_pass_cannot_advance(setup):
    (st,o,l,s,p,f),created,b,scope,t=workflow_root(setup)
    s.admit_request(b,scope,pending(b,t)['id'],plan=p,step_id='review',input_revision_sha256='b'*64)
    child=s.claim_child(created['attempt_id'])
    artifact=st.register_artifact(child['id'],{'path':'review.json','sha256':'a'*64,'reviewed_revision_sha256':'b'*64},expected_generation=1)
    s.transition(child['id'],'running',expected_generation=1)
    s.transition(child['id'],'verifying',expected_generation=1)
    s.transition(child['id'],'completed',expected_generation=1,outcome='needs_review',result={'summary':'PASS'})
    with pytest.raises(StoreError,match='verified child'):gate(s,child,artifact)


def test_same_native_call_cannot_link_later_step(setup):
    (st,o,l,s,p,f),created,b,scope,t=workflow_root(setup)
    q=pending(b,t);s.admit_request(b,scope,q['id'],plan=p,step_id='review',input_revision_sha256='b'*64)
    child=s.claim_child(created['attempt_id']);gate(s,child,completed_step(st,s,child))
    with pytest.raises(StoreError,match='another step'):s.admit_request(b,scope,q['id'],plan=p,step_id='build',input_revision_sha256='b'*64)


def test_root_completion_requires_all_steps_and_cleanup(setup):
    (st,o,l,s,p,f),created,b,scope,t=workflow_root(setup)
    s.transition(created['attempt_id'],'verifying',expected_generation=1)
    def complete():return s.transition(created['attempt_id'],'completed',expected_generation=1,outcome='verified')
    with pytest.raises(StoreError,match='all passing'):complete()
    for index,step_id in enumerate(('review','build')):
        s.admit_request(b,scope,pending(b,t,'stage'+str(index))['id'],plan=p,step_id=step_id,input_revision_sha256='b'*64)
        child=s.claim_child(created['attempt_id'])
        assignment=s.child_assignment(child['id'],expected_generation=1)
        assert assignment['workflow_step_id']==step_id
        assert assignment['workflow_step_ordinal']==index
        gate(s,child,completed_step(st,s,child))
        with pytest.raises(StoreError,match='child cleanup'):complete()
        s.release_child(child['id'],expected_generation=1,verifier=lambda target:ChildCleanupReceipt(target,'confirmed_stopped','e'*64))
    assert complete()['state']=='completed'
    assert st.claim_next() is None
    with st._connect() as db:
        assert db.execute('SELECT state FROM workflow_roots').fetchone()[0]=='held'


def test_assigned_revision_frozen_before_launch_and_gate_matches(setup):
    (st,o,l,s,p,f),created,b,scope,t=workflow_root(setup)
    q=pending(b,t)
    s.admit_request(b,scope,q['id'],plan=p,step_id='review',input_revision_sha256='b'*64)
    with pytest.raises(StoreError,match='immutable'):
        s.admit_request(b,scope,q['id'],plan=p,step_id='review',input_revision_sha256='f'*64)
    child=s.claim_child(created['attempt_id']);completed_step(st,s,child)
    assert s.child_assignment(child['id'],expected_generation=1)['input_revision_sha256']=='b'*64
    different=st.register_artifact(child['id'],{'path':'other-revision.json','sha256':'a'*64,'reviewed_revision_sha256':'f'*64},expected_generation=1)
    with pytest.raises(StoreError,match='assigned revision'):
        s.record_step_gate(child['id'],expected_generation=1,decision='pass',revision_sha256='f'*64,reviewed_artifact_id=different['id'],reviewed_artifact_sha256='a'*64,evidence_sha256='c'*64)


def test_incomplete_workflow_can_fail_or_cancel(setup):
    (st,o,l,s,p,f),created,b,scope,t=workflow_root(setup)
    st.cancel(o,created['attempt_id'],'cancel')
    assert s.transition(created['attempt_id'],'cancelled',expected_generation=1)['state']=='cancelled'
