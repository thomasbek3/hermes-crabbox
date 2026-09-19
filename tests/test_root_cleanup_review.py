import json
import pytest
from cloudworkbench.provider_leases import ProviderLeases, LeaseError
from cloudworkbench.scheduler import RoleScheduler
from cloudworkbench.store import StoreError
from test_root_cleanup import setup, finish, release, state, runtime_receipt


def test_restart_after_provider_cleanup_can_finalize_without_callbacks(setup, monkeypatch):
    store, _, leases, scheduler, root, calls = setup
    finish(setup)
    original = store._event
    def crash(*args, **kwargs):
        if args[3]=='workflow.root_released':raise RuntimeError('commit failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(store, '_event', crash)
    with pytest.raises(RuntimeError, match='commit failure'):release(setup)
    monkeypatch.setattr(store, '_event', original)
    fresh_leases=ProviderLeases(store, cleanup_verifier=lambda _:pytest.fail('No cleanup replay'), inspector_id='inspector', clock=lambda:1000)
    fresh=RoleScheduler(store, fresh_leases, clock=lambda:1000)
    assert fresh.release_root(root['attempt_id'], expected_generation=1, verifier=lambda _:pytest.fail('No runtime replay'))['root_id']==root['attempt_id']
    assert len(calls)==2


def test_first_runtime_binding_during_frozen_cleanup_refused(setup):
    store, principal, leases, scheduler, root, calls = setup
    from cloudworkbench.role_broker import RoleBroker, ParentScope
    broker=RoleBroker(store.path, authorize_parent=scheduler.authorize_parent, clock=lambda:1000)
    scope=ParentScope(principal['id'],'project',root['attempt_id'],root['attempt_id'],1,'native')
    _,token=broker.issue(scope,roles=('feature',),expires_at=2000)
    pending=broker.prepare(token,native_session_id='native',native_call_id='call',role='feature',task='child')
    scheduler.admit_request(broker,scope,pending['id'],plan=[{'ready':True,'profile':{'fixture_only':True}}])
    with store._connect() as db:child=db.execute("SELECT id FROM attempts WHERE execution_kind='hermes_child'").fetchone()[0]
    store.cancel(principal,child,'cancel-child')
    finish(setup)
    def verifier(target):
        with pytest.raises(StoreError,match='runtime binding frozen'):
            scheduler.bind_runtime(child,expected_generation=1,runtime_id='late-runtime')
        return runtime_receipt(target)
    release(setup,verifier)
    assert state(setup)['state']=='released'


def test_partial_foreign_proof_keeps_all_fences_and_no_callbacks(setup):
    store, _, leases, scheduler, root, calls = setup
    finish(setup)
    original=leases.verifier
    leases.verifier=lambda target:None if target.reservation.account_id=='account-b' else original(target)
    with pytest.raises(LeaseError):release(setup)
    assert leases.current('account-a')['reason']=='root_cleanup_verified'
    with pytest.raises(LeaseError,match='account_reserved'):leases.reserve('account-a',persistent_owner_id='owner-a')
    fresh_leases=ProviderLeases(store,cleanup_verifier=lambda _:pytest.fail('No foreign cleanup'),inspector_id='new-inspector',clock=lambda:1000)
    fresh=RoleScheduler(store,fresh_leases,clock=lambda:1000)
    with pytest.raises(StoreError,match='cleanup is unconfirmed'):
        fresh.release_root(root['attempt_id'],expected_generation=1,verifier=lambda _:pytest.fail('No runtime replay'))
    assert state(setup)['state']=='releasing' and len(calls)==1


def test_historical_released_provider_new_owner_untouched_on_successor_finalize(setup,monkeypatch):
    store,_,leases,_,root,calls=setup
    finish(setup)
    original=store._event
    def crash(*args,**kwargs):
        if args[3]=='workflow.root_released':raise RuntimeError('crash')
        return original(*args,**kwargs)
    monkeypatch.setattr(store,'_event',crash)
    with pytest.raises(RuntimeError):release(setup)
    monkeypatch.setattr(store,'_event',original)
    # Simulate the old implementation's release-before-root-commit state with exact proofs intact.
    with store._tx() as db:
        db.execute("UPDATE provider_reservations SET state='released',reason=NULL")
        db.execute('UPDATE provider_accounts SET active_id=NULL')
    newer=leases.reserve('account-a',persistent_owner_id='owner-a')
    fresh_leases=ProviderLeases(store,cleanup_verifier=lambda _:pytest.fail('No newer-owner cleanup'),inspector_id='rotated-inspector',clock=lambda:1000)
    fresh=RoleScheduler(store,fresh_leases,clock=lambda:1000)
    result=fresh.release_root(root['attempt_id'],expected_generation=1,verifier=lambda _:pytest.fail('No runtime replay'))
    assert result['root_id']==root['attempt_id'] and leases.current('account-a')['reservation']==newer
    assert len(calls)==2


@pytest.mark.parametrize('corrupt', ['{}','null','[]','{"attempts":{},"reservations":[]}'])
def test_persisted_cleanup_corruption_is_bounded_conflict(setup,corrupt):
    from cloudworkbench.scheduler import _cleanup_target
    with pytest.raises(StoreError,match='Invalid persisted root cleanup target') as error:_cleanup_target(corrupt)
    assert error.value.status_code==409


@pytest.mark.parametrize('table',['workflow_roots','workflow_root_cleanup','workflow_step_gates'])
def test_scheduler_reopen_rejects_table_drift(setup,table):
    store,_,leases,_,_,_=setup
    with store._tx() as db:db.execute(f'ALTER TABLE {table} ADD COLUMN unreviewed TEXT')
    with pytest.raises(StoreError,match='Scheduler table differs') as error:RoleScheduler(store,leases)
    assert error.value.status_code==503


def test_workflow_bound_grant_refuses_other_root(setup,monkeypatch):
    store,principal,leases,scheduler,root,_=setup
    with store._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?',(root['attempt_id'],)).fetchone()[0])
    second=scheduler.enqueue_root(principal,{'agent':'hermes','project_id':'project','goal':'other'},'other-root',frozen=frozen)
    reservation=leases.current('account-a')['reservation']
    # Exercise independent identity fence, even if the separate lifecycle predicate were true.
    monkeypatch.setattr(leases,'_attempt_valid',lambda *args:True)
    with pytest.raises(LeaseError,match='execution_not_authorized'):
        leases.issue_grant(reservation,attempt_id=second['attempt_id'],generation=1)
    assert leases.issue_grant(reservation,attempt_id=root['attempt_id'],generation=1)


def test_direct_owner_cleanup_cannot_drop_workflow_fence(setup):
    leases=setup[2]
    with pytest.raises(LeaseError,match='workflow_cleanup_requires_retained_fence'):
        leases.cleanup_owner(leases.current('account-a')['reservation'])
    assert leases.current('account-a')['state']=='held' and setup[-1]==[]
