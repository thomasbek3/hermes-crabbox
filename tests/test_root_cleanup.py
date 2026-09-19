from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import sqlite3
import pytest

from cloudworkbench.store import Store, StoreError
from cloudworkbench.scheduler import RoleScheduler, RootCleanupReceipt, ChildCleanupReceipt
from cloudworkbench.provider_leases import ProviderLeases, CleanupReceipt, LeaseError
from cloudworkbench.provider_dispatch import ProviderDispatch
from test_provider_dispatch import FakeRuntime


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path.resolve()/'controller.db');store.migrate_scheduler()
    owner=store.add_client('TEST root cleanup','r'*40,['submit','observe','cancel'],['project'])
    calls=[]
    def provider(target):
        calls.append(target)
        return CleanupReceipt(target,'inspector','terminated',1000,'a'*64)
    leases=ProviderLeases(store,cleanup_verifier=provider,inspector_id='inspector',clock=lambda:1000)
    accounts={'account-a':'owner-a','account-b':'owner-b'}
    for account,persistent in accounts.items():leases.register_account(account,legacy_agent=account,persistent_owner_id=persistent)
    scheduler=RoleScheduler(store,leases,clock=lambda:1000)
    frozen={'accounts':accounts,'role_plans':{'feature':[{'ready':True,'profile':{'fixture_only':True}}]},'provenance':{'fixture_only':True}}
    created=scheduler.enqueue_root(owner,{'agent':'hermes','project_id':'project','goal':'fixture'},'root',frozen=frozen)
    scheduler.admit_root(created['attempt_id'],accounts=accounts)
    scheduler.bind_runtime(created['attempt_id'],expected_generation=1,runtime_id='root-runtime-fixture')
    scheduler.transition(created['attempt_id'],'running',expected_generation=1)
    return store,owner,leases,scheduler,created,calls


def finish(setup):
    st,o,l,s,r,calls=setup
    s.transition(r['attempt_id'],'failed',expected_generation=1)


def runtime_receipt(target):
    return RootCleanupReceipt(target,'terminated',1000,'b'*64)


def release(setup,verifier=runtime_receipt):
    st,o,l,s,r,calls=setup
    return s.release_root(r['attempt_id'],expected_generation=1,verifier=verifier)


def state(setup):
    with setup[0]._connect() as db:return dict(db.execute('SELECT * FROM workflow_roots').fetchone())


def test_real_lease_cleanup_releases_capacity_and_next_work(setup):
    st,o,l,s,r,calls=setup
    queued=st.create_session(o,{'agent':'account-a','project_id':'project','goal':'legacy'},'legacy')
    finish(setup)
    assert st.claim_next() is None
    receipt=release(setup)
    assert receipt['root_id']==r['attempt_id'] and len(receipt['provider_receipt_sha256'])==2
    assert len(calls)==2 and all(t.scope=='owner' for t in calls)
    assert state(setup)['state']=='released'
    assert l.current('account-a') is None and l.current('account-b') is None
    assert st.claim_next()['id']==queued['attempt_id']


def test_live_root_child_and_uncleared_slot_refused(setup):
    st,o,l,s,r,calls=setup
    with pytest.raises(StoreError,match='terminal root'):release(setup)
    from cloudworkbench.role_broker import RoleBroker, ParentScope
    b=RoleBroker(st.path,authorize_parent=s.authorize_parent,clock=lambda:1000)
    scope=ParentScope(o['id'],'project',r['attempt_id'],r['attempt_id'],1,'native')
    _,token=b.issue(scope,roles=('feature',),expires_at=2000)
    q=b.prepare(token,native_session_id='native',native_call_id='call',role='feature',task='child')
    plan=[{'ready':True,'profile':{'fixture_only':True}}]
    s.admit_request(b,scope,q['id'],plan=plan)
    finish(setup)
    with pytest.raises(StoreError,match='terminal children'):release(setup)
    with st._connect() as db:child=db.execute("SELECT id FROM attempts WHERE execution_kind='hermes_child'").fetchone()[0]
    st.cancel(o,child,'cancel-child')
    assert release(setup)['root_id']==r['attempt_id']
    assert calls


def test_runtime_unknown_keeps_reservations_without_provider_side_effect(setup):
    finish(setup)
    with pytest.raises(StoreError,match='runtime cleanup unconfirmed'):release(setup,lambda _:None)
    assert state(setup)['state']=='releasing'
    assert setup[-1]==[]
    assert setup[2].current('account-a')['state']=='held'
    assert setup[0].claim_next() is None
    assert release(setup)['root_id']==setup[4]['attempt_id']


def test_runtime_receipt_target_time_and_hash_bound(setup):
    finish(setup)
    for fn in (lambda t:replace(runtime_receipt(t),target=replace(t,generation=2)),lambda t:replace(runtime_receipt(t),observed_at=999),lambda t:replace(runtime_receipt(t),evidence_sha256='bad')):
        with pytest.raises(StoreError,match='unconfirmed'):release(setup,fn)
    assert setup[-1]==[]


def test_provider_partial_failure_retry_skips_only_proven_original_scope(setup):
    st,o,l,s,r,calls=setup;finish(setup)
    original=l.verifier
    def fail_second(target):
        if target.reservation.account_id=='account-b':raise RuntimeError('unknown')
        return original(target)
    l.verifier=fail_second
    with pytest.raises(LeaseError,match='cleanup_unconfirmed'):release(setup)
    assert state(setup)['state']=='releasing'
    assert l.current('account-a')['reason']=='root_cleanup_verified'
    assert l.current('account-b')['state']=='cleaning'
    assert len(calls)==1
    l.verifier=original
    result=release(setup,lambda _:pytest.fail('Confirmed runtime proof must replay'))
    assert result['root_id']==r['attempt_id'] and len(calls)==2
    assert state(setup)['state']=='released'


def test_released_root_replay_never_cleans_newer_account_owner(setup):
    st,o,l,s,r,calls=setup;finish(setup)
    first=release(setup)
    newer=l.reserve('account-a',persistent_owner_id='owner-a')
    result=release(setup,lambda _:pytest.fail('Released replay must not inspect'))
    assert result==first and l.current('account-a')['reservation']==newer
    assert len(calls)==2
    with st._connect() as db:
        events=db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.root_released'").fetchone()[0]
        assert events==1


def test_partial_retry_retains_cleaned_account_fence(setup):
    st,o,l,s,r,calls=setup;finish(setup);original=l.verifier
    l.verifier=lambda target:None if target.reservation.account_id=='account-b' else original(target)
    with pytest.raises(LeaseError):release(setup)
    with pytest.raises(LeaseError,match='account_reserved'):l.reserve('account-a',persistent_owner_id='owner-a')
    l.verifier=original
    release(setup)
    assert l.current('account-a') is None
    assert len(calls)==2


def test_callbacks_outside_sqlite_lock_and_root_cas_catches_drift(setup):
    st,o,l,s,r,calls=setup;finish(setup)
    def callback(target):
        with st._connect() as db:
            db.execute('PRAGMA busy_timeout=20');db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE workflow_roots SET version=version+1 WHERE root_id=?',(r['attempt_id'],));db.commit()
        return runtime_receipt(target)
    with pytest.raises(StoreError,match='target changed'):release(setup,callback)
    assert calls==[] and state(setup)['state']=='releasing'


def test_provider_callback_outside_db_and_target_rechecked_after_cleanup(setup):
    st,o,l,s,r,calls=setup;finish(setup);original=l.verifier
    def callback(target):
        with st._connect() as db:
            db.execute('PRAGMA busy_timeout=20');db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE workflow_roots SET version=version+1 WHERE root_id=?',(r['attempt_id'],));db.commit()
        return original(target)
    l.verifier=callback
    with pytest.raises(StoreError,match='target changed'):release(setup)
    assert state(setup)['state']=='releasing'
    assert l.current('account-b') is not None


def test_new_controller_cannot_adopt_unreleased_root(setup):
    st,o,l,s,r,calls=setup;finish(setup)
    fresh_leases=ProviderLeases(st,cleanup_verifier=l.verifier,inspector_id='inspector',clock=lambda:1000)
    fresh=RoleScheduler(st,fresh_leases,clock=lambda:1000)
    with pytest.raises(StoreError,match='current owner'):
        fresh.release_root(r['attempt_id'],expected_generation=1,verifier=runtime_receipt)
    assert state(setup)['state']=='held' and calls==[]


def test_concurrent_releases_same_receipt_one_inspection(setup):
    st,o,l,s,r,calls=setup;finish(setup);observations=[]
    def check(target):observations.append(target);return runtime_receipt(target)
    with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(lambda _:release(setup,check),range(6)))
    assert all(r==results[0] for r in results)
    assert len(observations)==1 and len(calls)==2


def test_routed_runtime_binding_immutable_legacy_unchanged(setup):
    st,o,l,s,r,calls=setup
    assert s.bind_runtime(r['attempt_id'],expected_generation=1,runtime_id='root-runtime-fixture')['runtime_id']=='root-runtime-fixture'
    with pytest.raises(sqlite3.IntegrityError,match='immutable_workflow_runtime'):
        s.bind_runtime(r['attempt_id'],expected_generation=1,runtime_id='wrong-runtime')
    with pytest.raises(StoreError,match='Stale'):
        s.bind_runtime(r['attempt_id'],expected_generation=2,runtime_id='root-runtime-fixture')


def test_existing_provider_dispatch_cleanup_proof_is_used(setup):
    st,o,l,s,r,calls=setup
    runtime=FakeRuntime();l.verifier=runtime.cleanup;l.inspector_id='synthetic-inspector'
    owner=l.current('account-a')['reservation']
    grant=l.issue_grant(owner,attempt_id=r['attempt_id'],generation=1)
    d=ProviderDispatch(l,runtime)
    spec=d.admit(owner,grant,request_nonce='a'*64,payload=b'fixture',profile_digest='b'*64)
    d.launch(spec.lease.request_id,b'fixture')
    assert len(runtime.objects)==4
    finish(setup);release(setup)
    assert runtime.objects=={}
    assert d.read(spec.lease.request_id)['state']=='cleaned'


def test_unknown_provider_create_prevents_root_release(setup):
    st,o,l,s,r,calls=setup
    runtime=FakeRuntime();l.verifier=runtime.cleanup;l.inspector_id='synthetic-inspector'
    owner=l.current('account-a')['reservation']
    grant=l.issue_grant(owner,attempt_id=r['attempt_id'],generation=1)
    d=ProviderDispatch(l,runtime)
    spec=d.admit(owner,grant,request_nonce='a'*64,payload=b'fixture',profile_digest='b'*64)
    with st._tx() as db:db.execute("UPDATE provider_dispatch SET state='create_intent',operation='create',uncertain=1 WHERE request_id=?",(spec.lease.request_id,))
    finish(setup)
    with pytest.raises(LeaseError,match='dispatch_outcome_unknown'):release(setup)
    assert state(setup)['state']=='releasing'
    assert l.current('account-a')['state']=='held'
    assert runtime.events==[]


def test_terminal_claimed_child_must_clear_exact_occupancy(setup):
    st,o,l,s,r,calls=setup
    from cloudworkbench.role_broker import RoleBroker, ParentScope
    b=RoleBroker(st.path,authorize_parent=s.authorize_parent,clock=lambda:1000)
    scope=ParentScope(o['id'],'project',r['attempt_id'],r['attempt_id'],1,'native')
    _,token=b.issue(scope,roles=('feature',),expires_at=2000)
    q=b.prepare(token,native_session_id='native',native_call_id='call',role='feature',task='child')
    s.admit_request(b,scope,q['id'],plan=[{'ready':True,'profile':{'fixture_only':True}}])
    child=s.claim_child(r['attempt_id']);s.bind_runtime(child['id'],expected_generation=1,runtime_id='child-runtime')
    finish(setup);s.transition(child['id'],'cancelled',expected_generation=1)
    with pytest.raises(StoreError,match='cleared child occupancy'):release(setup)
    s.release_child(child['id'],expected_generation=1,verifier=lambda target:ChildCleanupReceipt(target,'confirmed_stopped','c'*64))
    def check(target):
        assert {a.runtime_id for a in target.attempts}=={'root-runtime-fixture','child-runtime'}
        return runtime_receipt(target)
    release(setup,check)


def test_released_root_reopens_idempotently_with_new_controller(setup):
    st,o,l,s,r,calls=setup;finish(setup);receipt=release(setup)
    fresh_store=Store(st.path)
    fresh_leases=ProviderLeases(fresh_store,cleanup_verifier=lambda _:pytest.fail('No replay cleanup'),inspector_id='inspector',clock=lambda:1000)
    fresh=RoleScheduler(fresh_store,fresh_leases,clock=lambda:1000)
    assert fresh.release_root(r['attempt_id'],expected_generation=1,verifier=lambda _:pytest.fail('No replay runtime inspection'))==receipt
    with pytest.raises(StoreError):fresh.release_root(r['attempt_id'],expected_generation=2,verifier=runtime_receipt)


def test_next_root_can_reuse_accounts_after_confirmed_release(setup):
    st,o,l,s,r,calls=setup;finish(setup);release(setup)
    with st._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots').fetchone()[0])
    second=s.enqueue_root(o,{'agent':'hermes','project_id':'project','goal':'second'},'root2',frozen=frozen)
    assert s.admit_root(second['attempt_id'],accounts=frozen['accounts'])['state']=='preparing'
    assert l.current('account-a')['reservation'].epoch==2


def test_runtime_receipt_durable_before_provider_crash(setup,monkeypatch):
    st,o,l,s,r,calls=setup;finish(setup);checks=[];original=l.cleanup_owner
    def crash(_,**kwargs):raise KeyboardInterrupt('simulated process death')
    monkeypatch.setattr(l,'cleanup_owner',crash)
    def check(target):checks.append(target);return runtime_receipt(target)
    with pytest.raises(KeyboardInterrupt):release(setup,check)
    assert state(setup)['state']=='releasing'
    monkeypatch.setattr(l,'cleanup_owner',original)
    assert release(setup,check)['root_id']==r['attempt_id']
    assert len(checks)==1


def test_failure_after_external_cleanup_before_release_commit_replays(setup,monkeypatch):
    st,o,l,s,r,calls=setup;finish(setup);original=st._event
    def fail_final(*args,**kwargs):
        if args[3]=='workflow.root_released':raise RuntimeError('simulated commit boundary')
        return original(*args,**kwargs)
    monkeypatch.setattr(st,'_event',fail_final)
    with pytest.raises(RuntimeError,match='commit boundary'):release(setup)
    assert state(setup)['state']=='releasing'
    assert l.current('account-a')['reason']=='root_cleanup_verified' and l.current('account-b')['reason']=='root_cleanup_verified'
    assert st.claim_next() is None
    with st._connect() as db:
        assert db.execute('SELECT release_receipt FROM workflow_root_cleanup').fetchone()[0] is None
    monkeypatch.setattr(st,'_event',original)
    assert release(setup,lambda _:pytest.fail('Runtime cleanup already durable'))['root_id']==r['attempt_id']
    assert len(calls)==2 and state(setup)['state']=='released'
