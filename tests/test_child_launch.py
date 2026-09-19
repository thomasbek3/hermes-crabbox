from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3
import pytest
from test_scheduler import setup, workflow_root, pending
from cloudworkbench.scheduler import RoleScheduler
from cloudworkbench.routed_runtime import CallerSpec
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.store import StoreError


@pytest.fixture
def child_launch(setup):
    st,o,l,s,p,f=setup
    profile=NativeProfile('openai-codex','gpt-6-astra','high')
    p=[{'ready':True,'profile':{'provider':profile.provider,'model':profile.model,'effort':profile.effort,'transport':'codex_responses'}}]
    f={**f,'role_plans':{'implementation':p}}
    changed,root,b,scope,t=workflow_root((st,o,l,s,p,f))
    s.admit_request(b,scope,pending(b,t)['id'],plan=p,step_id='review',input_revision_sha256='b'*64)
    child=s.claim_child(root['attempt_id'])
    spec=CallerSpec(child['id'],child['session_id'],1,'sha256:'+'a'*64,'test','/fixture/source','/fixture/scratch','/fixture/task','/fixture/worker',True,(),(2.,4096,512))
    args={'expected_generation':1,'runtime_id':'d'*64,'caller_spec':spec,'execution_profile':profile,'input_revision_sha256':'b'*64}
    return st,o,l,s,root,child,args


def bind(value):
    st,o,l,s,root,child,args=value
    return s.bind_child_caller(child['id'],**args)


def params(binding):return {'expected_generation':binding.generation,'binding_digest':binding.binding_digest}


def test_atomic_binding_start_confirm_and_exact_replay(child_launch):
    st,o,l,s,r,c,args=child_launch
    assert s.read_child_launch(c['id'],expected_generation=1) is None
    bound=bind(child_launch)
    assert bound.state=='bound' and not bound.may_start
    assert bind(child_launch)==bound
    assert st.get_attempt(c['id'])['runtime_id']==args['runtime_id']
    with pytest.raises(StoreError,match='intent missing'):s.confirm_child_started(c['id'],**params(bound))
    launch=s.begin_child_start(c['id'],**params(bound));assert launch.may_start and launch.state=='start_intent'
    assert not s.begin_child_start(c['id'],**params(bound)).may_start
    assert s.check_child_start_authority(c['id'],**params(bound)).state=='start_intent'
    assert s.confirm_child_started(c['id'],**params(bound)).state=='started'
    assert s.confirm_child_started(c['id'],**params(bound)).state=='started'
    assert st.get_attempt(c['id'])['state']=='running'
    assert not s.begin_child_start(c['id'],**params(bound)).may_start


def test_concurrent_start_and_crash_after_intent_never_regrant(child_launch):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    def begin(_):
        try:return s.begin_child_start(c['id'],**params(bound))
        except StoreError as exc:
            assert exc.status_code==503
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        calls=list(pool.map(begin,range(8)))
    assert sum(x.may_start for x in calls if x is not None)==1
    fresh=RoleScheduler(st,l,clock=lambda:1000)
    assert fresh.read_child_launch(c['id'],expected_generation=1).state=='start_intent'
    assert not fresh.begin_child_start(c['id'],**params(bound)).may_start


@pytest.mark.parametrize('field',['runtime_id','caller_spec','input_revision_sha256','execution_profile','expected_generation'])
def test_binding_conflict_never_replaces_identity(child_launch,field):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch);changed=dict(args)
    changed[field]={'runtime_id':'e'*64,'caller_spec':replace(args['caller_spec'],scratch='/different'),
        'input_revision_sha256':'f'*64,'execution_profile':NativeProfile('openai-codex','gpt-5.6-sol','max'),'expected_generation':True}[field]
    with pytest.raises(StoreError):s.bind_child_caller(c['id'],**changed)
    assert s.read_child_launch(c['id'],expected_generation=1)==bound


@pytest.mark.parametrize('change',['child_cancel','parent_cancel','root_cancel','revoke','project','scope','deadline','child_deadline','clock','parent_generation','child_generation','persistent_owner','account_owner'])
def test_authority_loss_blocks_bind_replay_and_all_launch_checks(child_launch,change):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    with st._tx() as db:
        if change=='child_cancel':db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(c['id'],))
        elif change=='parent_cancel':db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(r['attempt_id'],))
        elif change=='root_cancel':db.execute('UPDATE workflow_roots SET cancel_requested=1')
        elif change=='revoke':db.execute("UPDATE clients SET revoked_at='now'")
        elif change=='project':db.execute("UPDATE clients SET projects='[]'")
        elif change=='scope':db.execute("UPDATE clients SET scopes='[\"observe\"]'")
        elif change=='deadline':db.execute('UPDATE workflow_roots SET deadline_at=999')
        elif change=='child_deadline':s.clock=lambda:5201
        elif change=='clock':db.execute('UPDATE inference_budget_roots SET high_water_us=1001000000')
        elif change=='parent_generation':db.execute('UPDATE attempts SET generation=2 WHERE id=?',(r['attempt_id'],))
        elif change=='child_generation':db.execute('UPDATE attempts SET generation=2 WHERE id=?',(c['id'],))
        elif change=='persistent_owner':db.execute("UPDATE provider_accounts SET persistent_owner_id='replacement'")
        else:db.execute("UPDATE provider_reservations SET controller_instance_id='replacement'")
    for operation in (lambda:bind(child_launch),lambda:s.begin_child_start(c['id'],**params(bound)),
                      lambda:s.check_child_start_authority(c['id'],**params(bound)),lambda:s.confirm_child_started(c['id'],**params(bound))):
        with pytest.raises(StoreError):operation()


def test_bind_transaction_rollback_leaves_no_half_binding(child_launch,monkeypatch):
    st,o,l,s,r,c,args=child_launch
    original=st._event
    def fail(*a,**kw):raise RuntimeError('synthetic commit interruption')
    monkeypatch.setattr(st,'_event',fail)
    with pytest.raises(RuntimeError):bind(child_launch)
    assert st.get_attempt(c['id'])['runtime_id'] is None
    assert s.read_child_launch(c['id'],expected_generation=1) is None
    monkeypatch.setattr(st,'_event',original)
    assert bind(child_launch).state=='bound'


def test_fence_after_cancellation_revokes_grant_keeps_slot(child_launch):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    reservation=l.current('synthetic-account')['reservation']
    grant=l.issue_grant(reservation,attempt_id=c['id'],generation=1)
    s.begin_child_start(c['id'],**params(bound))
    with st._tx() as db:db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(r['attempt_id'],))
    fenced=s.fence_child_execution(c['id'],**params(bound))
    assert fenced.state=='fenced' and not fenced.may_start
    assert s.fence_child_execution(c['id'],**params(bound))==fenced
    with st._connect() as db:
        assert db.execute('SELECT revoked_at FROM provider_execution_grants').fetchone()[0] is not None
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0]==c['id']
        assert db.execute('SELECT active_id FROM provider_accounts').fetchone()[0]==reservation.reservation_id
    with pytest.raises(StoreError):s.check_child_start_authority(c['id'],**params(bound))


def test_fenced_launch_never_returns_authority_and_prevents_new_grants(child_launch):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    s.fence_child_execution(c['id'],**params(bound))
    for operation in (lambda:s.begin_child_start(c['id'],**params(bound)),lambda:s.check_child_start_authority(c['id'],**params(bound)),lambda:s.confirm_child_started(c['id'],**params(bound))):
        with pytest.raises(StoreError):operation()
    assert s.read_child_launch(c['id'],expected_generation=1).state=='fenced'
    assert st.get_attempt(c['id'])['cancel_requested']==0
    with pytest.raises(sqlite3.IntegrityError,match='child_execution_fenced'):l.issue_grant(l.current('synthetic-account')['reservation'],attempt_id=c['id'],generation=1)


def test_foreign_controller_can_observe_but_not_start_or_fence(child_launch):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    other=ProviderLeases(st,cleanup_verifier=lambda _:None,inspector_id='foreign',clock=lambda:1000)
    fresh=RoleScheduler(st,other,clock=lambda:1000)
    assert fresh.read_child_launch(c['id'],expected_generation=1)==bound
    with pytest.raises(StoreError):fresh.begin_child_start(c['id'],**params(bound))
    with pytest.raises(StoreError):fresh.fence_child_execution(c['id'],**params(bound))
    assert s.read_child_launch(c['id'],expected_generation=1)==bound


def test_schema_binding_cannot_mutate_or_skip_start_intent(child_launch):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    for sql in ("UPDATE workflow_child_launch SET binding='{}'", "UPDATE workflow_child_launch SET state='started'", "DELETE FROM workflow_child_launch"):
        with st._tx() as db,pytest.raises(sqlite3.IntegrityError,match='immutable'):db.execute(sql)
    with st._tx() as db:db.execute('DROP TRIGGER workflow_child_launch_immutable')
    with pytest.raises(StoreError,match='guard differs'):RoleScheduler(st,l)


def test_successful_observation_advances_clock_watermark_without_charge(child_launch):
    st,o,l,s,r,c,args=child_launch;s.clock=lambda:1100;bound=bind(child_launch)
    s.clock=lambda:1050
    with pytest.raises(StoreError,match='budget'):s.begin_child_start(c['id'],**params(bound))
    with st._connect() as db:
        row=db.execute('SELECT high_water_us,used FROM inference_budget_roots').fetchone()
        assert tuple(row)==(1100000000,0)


@pytest.mark.parametrize('mutation',['child_generation','parent_generation','persistent_owner','controller_owner'])
def test_teardown_never_revokes_replacement_ownership(child_launch,mutation):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    reservation=l.current('synthetic-account')['reservation']
    l.issue_grant(reservation,attempt_id=c['id'],generation=1)
    with st._tx() as db:
        if mutation=='child_generation':db.execute('UPDATE attempts SET generation=2 WHERE id=?',(c['id'],))
        elif mutation=='parent_generation':db.execute('UPDATE attempts SET generation=2 WHERE id=?',(r['attempt_id'],))
        elif mutation=='persistent_owner':db.execute("UPDATE provider_accounts SET persistent_owner_id='new-owner'")
        else:db.execute("UPDATE provider_reservations SET controller_instance_id='new-controller'")
    with pytest.raises(StoreError):s.fence_child_execution(c['id'],**params(bound))
    with st._connect() as db:
        assert db.execute('SELECT revoked_at FROM provider_execution_grants').fetchone()[0] is None
        assert db.execute('SELECT state FROM workflow_child_launch').fetchone()[0]=='bound'


def test_boolean_spec_generation_rejected(child_launch):
    st,o,l,s,r,c,args=child_launch;args={**args,'caller_spec':replace(args['caller_spec'],generation=True)}
    with pytest.raises(StoreError):s.bind_child_caller(c['id'],**args)


@pytest.mark.parametrize('loss',['deadline','client_revocation'])
def test_execution_fence_allowed_after_nonownership_authority_loss(child_launch,loss):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    if loss=='deadline':s.clock=lambda:20000
    else:
        with st._tx() as db:db.execute("UPDATE clients SET revoked_at='now'")
    assert s.fence_child_execution(c['id'],**params(bound)).state=='fenced'
    assert not st.get_attempt(c['id'])['cancel_requested']


def test_delete_cannot_erase_start_replay_fence(child_launch):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    assert s.begin_child_start(c['id'],**params(bound)).may_start
    with st._tx() as db,pytest.raises(sqlite3.IntegrityError,match='immutable_child_launch'):
        db.execute('DELETE FROM workflow_child_launch WHERE child_id=?',(c['id'],))
    assert not s.begin_child_start(c['id'],**params(bound)).may_start
