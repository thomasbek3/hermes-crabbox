from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import sqlite3
import threading

import pytest

from cloudworkbench.provider_leases import CleanupReceipt, LeaseError, ProviderLeases
from cloudworkbench.store import Store, StoreError


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'controller.db')
    principal=store.add_client('synthetic','x'*40,['submit','observe','retrieve','cancel'],['project'])
    clock=[1000.0]
    receipts=[]
    def verifier(target):
        receipts.append(target)
        return CleanupReceipt(target,'test-inspector','terminated',clock[0],hashlib.sha256(repr(target).encode()).hexdigest())
    manager=ProviderLeases(store,cleanup_verifier=verifier,inspector_id='test-inspector',clock=lambda:clock[0])
    manager.register_account('account',legacy_agent='claude',persistent_owner_id='owner')
    return store,principal,manager,clock,receipts


def create(store,principal,agent='hermes',key='task'):
    return store.create_session(principal,{'project_id':'project','goal':'synthetic','agent':agent},key)


def active(setup):
    store,principal,manager,clock,receipts=setup
    attempt=create(store,principal);store.claim_next()
    reservation=manager.reserve('account',persistent_owner_id='owner')
    grant=manager.issue_grant(reservation,attempt_id=attempt['attempt_id'],generation=attempt['generation'])
    return reservation,grant,attempt


def test_provider_first_blocks_legacy_claim_until_owner_proof(setup):
    store,principal,manager,clock,receipts=setup
    legacy=create(store,principal,'claude')
    reservation=manager.reserve('account',persistent_owner_id='owner')
    assert store.claim_next() is None
    assert store.get_attempt(legacy['attempt_id'])['reason']=='provider_account_reserved'
    assert store.claim_next() is None
    assert len(store.events(principal,legacy['session_id']))==2
    manager.cleanup_owner(reservation)
    assert store.claim_next()['id']==legacy['attempt_id']
    assert receipts[0].scope=='owner'


def test_legacy_first_blocks_provider_acquisition(setup):
    store,principal,manager,*_=setup
    attempt=create(store,principal,'claude');store.claim_next()
    with pytest.raises(LeaseError,match='legacy_account_busy'):
        manager.reserve('account',persistent_owner_id='owner')
    store.cancel(principal,attempt['attempt_id'],'cancel')
    with pytest.raises(LeaseError,match='legacy_account_busy'):
        manager.reserve('account',persistent_owner_id='owner')
    store.transition(attempt['attempt_id'],'cancelled',expected_generation=attempt['generation'])
    assert manager.reserve('account',persistent_owner_id='owner').epoch==1


@pytest.mark.parametrize('iteration',range(8))
def test_racing_legacy_claim_and_provider_reservation_have_one_winner(setup,iteration):
    store,principal,manager,*_=setup
    create(store,principal,'claude');barrier=threading.Barrier(2)
    def claim():
        barrier.wait();return ('legacy',store.claim_next())
    def reserve():
        barrier.wait()
        try:return ('provider',manager.reserve('account',persistent_owner_id='owner'))
        except LeaseError as exc:return ('blocked',exc.code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(claim),pool.submit(reserve)]
        left,right=[future.result() for future in futures]
    assert (left[1] is not None) != (right[0]=='provider')
    if right[0]=='blocked':assert right[1]=='legacy_account_busy'


def test_binding_is_immutable_free_or_busy_and_aliases_cannot_bypass(setup):
    store,principal,manager,*_=setup
    manager.register_account('account',legacy_agent='claude',persistent_owner_id='owner')
    for agent,owner in [('codex','owner'),('claude','other')]:
        with pytest.raises(LeaseError,match='immutable_account_binding'):
            manager.register_account('account',legacy_agent=agent,persistent_owner_id=owner)
    with pytest.raises(LeaseError,match='account_binding_conflict'):
        manager.register_account('alias',legacy_agent='claude',persistent_owner_id='other')
    reservation=manager.reserve('account',persistent_owner_id='owner')
    with pytest.raises(LeaseError,match='immutable_account_binding'):
        manager.register_account('account',legacy_agent='codex',persistent_owner_id='owner')


def test_grant_and_request_are_distinct_and_refresh_serializes(setup):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup)
    lease=manager.acquire_request(reservation,grant)
    assert lease.request_id!=grant and manager.authorize_request(lease)
    with pytest.raises(LeaseError,match='request_lease_busy'):
        manager.acquire_request(reservation,grant,purpose='refresh')
    manager.cleanup_request(lease)
    refresh=manager.acquire_request(reservation,grant,purpose='refresh')
    assert refresh.purpose=='refresh'
    with pytest.raises(LeaseError,match='request_lease_busy'):
        manager.acquire_request(reservation,grant)
    # A released request never frees the sole persistent owner to historical jobs.
    create(store,principal,'claude','legacy')
    assert store.claim_next(capacity=10) is None


def test_cancel_revokes_access_but_holds_all_leases(setup):
    store,principal,manager,clock,_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant)
    store.cancel(principal,attempt['attempt_id'],'cancel')
    with pytest.raises(LeaseError,match='grant_revoked'):manager.authorize_request(lease)
    with pytest.raises(LeaseError,match='grant_revoked'):manager.acquire_request(reservation,grant)
    with pytest.raises(LeaseError,match='account_reserved'):
        manager.reserve('account',persistent_owner_id='owner')
    clock[0]+=10000
    with pytest.raises(LeaseError,match='account_reserved'):
        manager.reserve('account',persistent_owner_id='owner')
    manager.cleanup_owner(reservation)
    assert manager.reserve('account',persistent_owner_id='owner').epoch==2


@pytest.mark.parametrize('change',['terminal','fence','client_revoke'])
def test_attempt_authority_changes_invalidate_grants(setup,change):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant)
    if change=='terminal':store.transition(attempt['attempt_id'],'failed',expected_generation=attempt['generation'])
    elif change=='fence':store.fence_attempt(attempt['attempt_id'],attempt['generation'])
    else:
        with store._tx() as db:db.execute('UPDATE clients SET revoked_at=? WHERE id=?',('revoked',principal['id']))
    with pytest.raises(LeaseError):manager.authorize_request(lease)
    with pytest.raises(LeaseError):manager.acquire_request(reservation,grant)
    assert manager.current('account')['reservation']==reservation


def test_expiry_never_releases_owner_or_request_and_clock_rollback_cannot_revive(setup):
    store,principal,manager,clock,_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant,ttl_seconds=5)
    clock[0]+=6
    with pytest.raises(LeaseError,match='request_expired'):manager.authorize_request(lease)
    clock[0]-=6
    with pytest.raises(LeaseError,match='request_requires_reconciliation'):manager.authorize_request(lease)
    with pytest.raises(LeaseError,match='request_lease_busy'):manager.acquire_request(reservation,grant)
    assert manager.current('account')['reservation']==reservation


def test_grant_expiration_is_persisted_on_denial(setup):
    store,principal,manager,clock,_=setup
    reservation,_,attempt=active(setup)
    grant=manager.issue_grant(reservation,attempt_id=attempt['attempt_id'],generation=attempt['generation'],ttl_seconds=1)
    clock[0]+=2
    with pytest.raises(LeaseError,match='grant_expired'):manager.acquire_request(reservation,grant)
    clock[0]-=2
    with pytest.raises(LeaseError,match='grant_revoked'):manager.acquire_request(reservation,grant)


def test_restart_cannot_adopt_old_owner_or_grants_without_cleanup(setup):
    store,principal,manager,clock,_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant)
    restarted=ProviderLeases(Store(store.path),cleanup_verifier=manager.verifier,inspector_id='test-inspector',clock=lambda:clock[0])
    assert restarted.current('account')['requires_reconciliation']
    with pytest.raises(LeaseError,match='owner_requires_reconciliation'):restarted.acquire_request(reservation,grant)
    with pytest.raises(LeaseError,match='request_requires_reconciliation'):restarted.authorize_request(lease)
    with pytest.raises(LeaseError,match='account_reserved'):
        restarted.reserve('account',persistent_owner_id='owner')
    restarted.cleanup_owner(reservation)
    assert restarted.reserve('account',persistent_owner_id='owner').epoch==2


@pytest.mark.parametrize('response',[True,False,None,{'cleaned':True},'cleaned'])
def test_no_model_boolean_or_dict_cleanup_proof(setup,response):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant)
    manager.verifier=lambda target:response
    with pytest.raises(LeaseError,match='cleanup_unconfirmed'):manager.cleanup_owner(reservation)
    assert manager.current('account')['state']=='cleaning'
    with pytest.raises(LeaseError):manager.authorize_request(lease)
    with pytest.raises(LeaseError,match='account_reserved'):
        manager.reserve('account',persistent_owner_id='owner')


def test_failed_request_cleanup_blocks_refresh_and_restart(setup):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant)
    def broken(target):raise OSError('untrusted details not returned')
    manager.verifier=broken
    with pytest.raises(LeaseError,match='cleanup_unconfirmed'):manager.cleanup_request(lease)
    with pytest.raises(LeaseError,match='request_lease_busy'):manager.acquire_request(reservation,grant,purpose='refresh')


@pytest.mark.parametrize('field,value',[('epoch',2),('controller_instance_id','other-controller'),('persistent_owner_id','other'),('account_id','other')])
def test_stale_identity_release_refused(setup,field,value):
    *_,manager,clock,receipts=setup
    reservation,_,_=active(setup)
    with pytest.raises(LeaseError,match='stale_reservation'):manager.cleanup_owner(replace(reservation,**{field:value}))
    assert not receipts and manager.current('account')['reservation']==reservation


def test_old_receipt_cannot_release_retried_cleanup_same_clock(setup):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant)
    original=manager.verifier;captured=[]
    def record_then_fail(target):
        captured.append(original(target));return True
    manager.verifier=record_then_fail
    with pytest.raises(LeaseError):manager.cleanup_request(lease)
    manager.verifier=lambda target:captured[0]
    with pytest.raises(LeaseError,match='cleanup_unconfirmed'):manager.cleanup_request(lease)
    assert manager.current('account')['reservation']==reservation


def test_verifier_runs_outside_db_lock_and_rechecks_identity(setup):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant)
    original=manager.verifier
    def inspect_then_concurrent_fence(target):
        with sqlite3.connect(store.path,timeout=.1) as db:
            db.execute('BEGIN IMMEDIATE');db.rollback()
        manager.quarantine(reservation)
        return original(target)
    manager.verifier=inspect_then_concurrent_fence
    with pytest.raises(LeaseError,match='owner_requires_reconciliation'):manager.cleanup_request(lease)
    assert manager.current('account')['state']=='quarantined'


def test_release_receipt_is_persisted_and_old_owner_cannot_clear_new_one(setup):
    store,principal,manager,*_=setup
    reservation,_,_=active(setup);manager.cleanup_owner(reservation)
    new=manager.reserve('account',persistent_owner_id='owner')
    with pytest.raises(LeaseError,match='stale_reservation'):manager.cleanup_owner(reservation)
    assert manager.current('account')['reservation']==new
    with store._connect() as db:
        assert 'test-inspector' in db.execute('SELECT cleanup_receipt FROM provider_reservations WHERE id=?',(reservation.reservation_id,)).fetchone()[0]


def test_migration_preserves_old_jobs_versions_and_default_claim_behavior(tmp_path):
    store=Store(tmp_path/'old.db');principal=store.add_client('synthetic','x'*40,['submit'],['project'])
    attempt=create(store,principal,'claude')
    with store._connect() as db:
        for trigger in ('provider_guard_legacy_insert','provider_guard_legacy_update'):
            db.execute('DROP TRIGGER '+trigger)
        for table in ('provider_dispatch','provider_schema_version','provider_request_leases','provider_execution_grants','provider_reservations','provider_accounts'):
            db.execute('DROP TABLE '+table)
    reopened=Store(store.path)
    assert reopened.get_attempt(attempt['attempt_id'])['state']=='queued'
    assert reopened.claim_next()['id']==attempt['attempt_id']
    with reopened._connect() as db:assert [r[0] for r in db.execute('SELECT version FROM schema_version')]==[1]


def test_manual_transition_cannot_bypass_reserved_legacy_account(setup):
    store,principal,manager,*_=setup
    attempt=create(store,principal,'claude')
    manager.reserve('account',persistent_owner_id='owner')
    with pytest.raises(StoreError,match='Provider account is reserved'):
        store.transition(attempt['attempt_id'],'preparing',expected_generation=attempt['generation'])


def test_required_verifier_and_generation_validation(setup):
    store,principal,manager,*_=setup
    with pytest.raises(LeaseError,match='trusted_verifier_required'):ProviderLeases(store,cleanup_verifier=True,inspector_id='x')
    reservation,grant,attempt=active(setup)
    with pytest.raises(LeaseError,match='invalid_reservation'):manager.cleanup_owner(replace(reservation,epoch=True))
    with pytest.raises(LeaseError,match='execution_not_authorized'):
        manager.issue_grant(reservation,attempt_id=attempt['attempt_id'],generation=attempt['generation']+1)


def test_old_sql_writer_cannot_bypass_account_reservation(setup):
    store,principal,manager,*_=setup
    legacy=create(store,principal,'claude')
    manager.reserve('account',persistent_owner_id='owner')
    with store._connect() as db:
        with pytest.raises(sqlite3.IntegrityError,match='provider_account_reserved'):
            db.execute("UPDATE attempts SET state='preparing' WHERE id=?",(legacy['attempt_id'],))
        with pytest.raises(sqlite3.IntegrityError,match='provider_account_reserved'):
            db.execute("""INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at)
                SELECT 'raw-attempt',session_id,turn_id,agent,generation+1,'running',created_at,updated_at FROM attempts WHERE id=?""",(legacy['attempt_id'],))
    assert store.get_attempt(legacy['attempt_id'])['state']=='queued'


def test_old_sql_writer_other_agents_and_nonlive_states_unaffected(setup):
    store,principal,manager,*_=setup
    legacy=create(store,principal,'claude');other=create(store,principal,'codex','other')
    manager.reserve('account',persistent_owner_id='owner')
    with store._connect() as db:
        db.execute("UPDATE attempts SET reason='still queued' WHERE id=?",(legacy['attempt_id'],))
        db.execute("UPDATE attempts SET state='cancelled' WHERE id=?",(legacy['attempt_id'],))
        db.execute("UPDATE attempts SET state='preparing' WHERE id=?",(other['attempt_id'],))
        with pytest.raises(sqlite3.IntegrityError,match='provider_account_reserved'):
            db.execute("UPDATE attempts SET agent='claude' WHERE id=?",(other['attempt_id'],))
    assert store.get_attempt(legacy['attempt_id'])['state']=='cancelled'
    assert store.get_attempt(other['attempt_id'])['agent']=='codex'


@pytest.mark.parametrize('field,value',[('inspector_id','unknown'),('outcome','looks_ok'),('observed_at',999.0),('observed_at',1001.0),('evidence_sha256','not-a-hash')])
def test_invalid_inspection_receipt_retains_owner(setup,field,value):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup)
    original=manager.verifier
    manager.verifier=lambda target:replace(original(target),**{field:value})
    with pytest.raises(LeaseError,match='cleanup_unconfirmed'):manager.cleanup_owner(reservation)
    assert manager.current('account')['reservation']==reservation


def test_simultaneous_request_and_refresh_have_one_winner(setup):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup);barrier=threading.Barrier(2)
    def take(purpose):
        barrier.wait()
        try:return manager.acquire_request(reservation,grant,purpose=purpose)
        except LeaseError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(take,['inference','refresh']))
    assert sum(value=='request_lease_busy' for value in results)==1
    assert sum(not isinstance(value,str) for value in results)==1


def test_lost_request_object_recovers_from_durable_lease(setup):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup)
    lease=manager.acquire_request(reservation,grant);identity=lease.request_id
    del lease
    recovered=manager.active_request(reservation)
    assert recovered.request_id==identity
    manager.cleanup_request(recovered)
    assert manager.active_request(reservation) is None
    assert manager.acquire_request(reservation,grant,purpose='refresh').purpose=='refresh'


def test_concurrent_cleanup_request_rechecks_nonce(setup):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant)
    original=manager.verifier;barrier=threading.Barrier(2)
    def slow(target):
        import time
        time.sleep(.03)
        return original(target)
    manager.verifier=slow
    def clean(_):
        barrier.wait(timeout=2)
        try:manager.cleanup_request(lease);return 'released'
        except LeaseError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(clean,range(2)))
    assert results.count('released')==1
    assert set(results)<={'released','cleanup_identity_changed','stale_request_lease'}
    assert manager.active_request(reservation) is None


def test_owner_cleanup_in_progress_denies_new_request(setup):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup)
    entered=threading.Event();proceed=threading.Event();original=manager.verifier
    def slow(target):
        entered.set()
        assert proceed.wait(timeout=2)
        return original(target)
    manager.verifier=slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(manager.cleanup_owner,reservation)
        try:
            assert entered.wait(timeout=2)
            with pytest.raises(LeaseError,match='owner_requires_reconciliation'):
                manager.acquire_request(reservation,grant)
        finally:proceed.set()
        future.result()
    assert manager.current('account') is None


def test_quarantine_marks_cleaning_request_and_keeps_it_recoverable(setup):
    store,principal,manager,*_=setup
    reservation,grant,attempt=active(setup);lease=manager.acquire_request(reservation,grant)
    manager.verifier=lambda target:True
    with pytest.raises(LeaseError):manager.cleanup_request(lease)
    manager.quarantine(reservation)
    with store._connect() as db:
        row=db.execute('SELECT state,reason FROM provider_request_leases WHERE id=?',(lease.request_id,)).fetchone()
        assert tuple(row)==('quarantined','reconciliation_required')
    assert manager.active_request(reservation)==lease
    with pytest.raises(LeaseError,match='owner_requires_reconciliation'):manager.cleanup_request(lease)
