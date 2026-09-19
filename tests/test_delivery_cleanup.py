"""Real v3 scheduler/leases over a completed synthetic six-stage workflow.

Only Docker/provider execution is synthetic. The shared seed is immutable;
per-test SQLite copies retain this process's controller identity, not a claim
of cross-process ownership adoption. No live credentials or network.
"""
from dataclasses import replace
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from cloudworkbench.delivery_schema import record_intent
from cloudworkbench.provider_leases import CleanupReceipt, LeaseError, ProviderLeases
from cloudworkbench.routed_delivery_policy import RootDeliveryProof, collect_root_delivery_proof
from cloudworkbench.scheduler import RoleScheduler, RootCleanupReceipt
from cloudworkbench.store import Store, StoreError, encode
from tests.test_six_stage_composition import flow, execute_stage


@pytest.fixture(scope='module')
def seed(tmp_path_factory):
    path=tmp_path_factory.mktemp('delivery-cleanup-seed')
    with pytest.MonkeyPatch.context() as patch:
        migrate=Store.migrate_scheduler
        def v3(store):
            migrate(store);store.migrate_delivery()
        patch.setattr(Store,'migrate_scheduler',v3)
        value=flow.__wrapped__(path)
    for index in range(6):
        stage=path/f'stage-{index}';stage.mkdir(mode=0o700)
        with pytest.MonkeyPatch.context() as patch:
            execute_stage(value,index,stage,patch)
    proof=collect_root_delivery_proof(value.s,value.root['attempt_id'],expected_generation=1,
        selected_revision=value.revision,forbidden_values=())
    return value,proof


@pytest.fixture
def delivery(seed,tmp_path):
    original,proof=seed
    path=tmp_path/'state.db'
    with original.store._connect() as source, sqlite3.connect(path) as dest:source.backup(dest)
    store=Store(path);calls=[]
    def provider(target):
        calls.append(target)
        return CleanupReceipt(target,'synthetic-inspector','terminated',1000,'a'*64)
    leases=ProviderLeases(store,cleanup_verifier=provider,inspector_id='synthetic-inspector',clock=lambda:1000)
    leases.instance_id=original.s.leases.instance_id
    scheduler=RoleScheduler(store,leases,clock=lambda:1000)
    root=original.root['attempt_id']
    value=proof.to_dict();value['private']['database_path']=str(path)
    proof=RootDeliveryProof.from_json(encode(value));public=proof.public_record()
    artifact={'id':'delivery-'+hashlib.sha256((root+':1').encode()).hexdigest(),
        'session_id':original.root['session_id'],'attempt_id':root,'generation':1,
        'path':f'routed/{root}/1/delivery.json','storage_path':str(tmp_path/'delivery.json'),
        'sha256':'b'*64,'bytes':100,'mime':'application/json','provenance':'controller_root_delivery',
        'kind':'workspace','outcome':'verified','selected_revision_sha256':public['selected_revision_sha256'],
        'workflow_sha256':public['workflow_sha256'],'delivery_version':1}
    envelope={'schema_version':1,'root_id':root,'generation':1,'expected_delivery_version':0,
        'base_revision':None,'base_artifact':None,'base_last_artifact':None,
        'selected_revision_sha256':public['selected_revision_sha256'],'kind':'workspace',
        'workflow_proof':proof.to_dict(),'artifact':artifact}
    scheduler.transition(root,'verifying',expected_generation=1)
    return SimpleNamespace(store=store,s=scheduler,leases=leases,root=root,owner=original.owner,
        scope=original.scope,envelope=envelope,calls=calls,intent=None)


def persist(v):
    with v.store._tx() as db:
        v.intent=record_intent(db,root_id=v.root,expected_generation=1,expected_delivery_version=0,
            base_revision=None,base_artifact=None,selected_revision_sha256=v.envelope['selected_revision_sha256'],
            kind='workspace',proof=v.envelope,created_at='2026-09-18T00:00:00Z')
    return v.intent['proof_sha256']


def receipt(target):return RootCleanupReceipt(target,'terminated',1000,'c'*64)


def prepare(v,verifier=receipt):
    return v.s.prepare_delivery_cleanup(v.root,expected_generation=1,
        intent_sha256=v.intent['proof_sha256'],verifier=verifier)


def commit(v,db,*,cancelled=False):
    method=v.s.commit_cancelled_delivery_cleanup if cancelled else v.s.commit_delivery_cleanup
    return method(db,v.root,expected_generation=1,intent_sha256=v.intent['proof_sha256'])


def snapshot(v):
    with v.store._connect() as db:
        return (dict(db.execute('SELECT * FROM workflow_roots').fetchone()),
            [dict(r) for r in db.execute('SELECT * FROM provider_accounts')],
            [dict(r) for r in db.execute('SELECT * FROM provider_reservations')],
            [dict(r) for r in db.execute('SELECT * FROM workflow_root_cleanup')])


def cancel(v):
    v.store.cancel(v.owner,v.root,'cancel-delivery')
    # Terminal cancellation is a trusted controller reconciliation step, after
    # the public cancellation request. It does not confer delivery authority.
    with v.store._tx() as db:
        db.execute("UPDATE attempts SET state='cancelled' WHERE id=?",(v.root,))


def test_prepare_retains_fences_commit_is_same_transaction(delivery):
    v=delivery;persist(v);proof=prepare(v)
    assert proof['fences_retained'] is True and len(v.calls)==1
    state=snapshot(v);assert state[0]['state']=='releasing' and state[1][0]['active_id']
    assert state[2][0]['state']=='cleaning'
    with pytest.raises(LeaseError,match='account_reserved'):v.leases.reserve('account',persistent_owner_id='owner')
    with v.store._tx() as db:
        result=commit(v,db)
        db.execute("UPDATE attempts SET state='completed',outcome='verified' WHERE id=?",(v.root,))
    assert result['fences_retained'] is False and result['release_purpose']=='delivery'
    assert snapshot(v)[0]['state']=='released' and v.leases.current('account') is None
    with v.store._connect() as db:
        assert db.execute('SELECT delivery_version FROM sessions').fetchone()[0]==0
    with pytest.raises(StoreError):
        v.s.release_root(v.root,expected_generation=1,verifier=lambda _:pytest.fail('No cleanup replay'))


def test_transaction_rollback_retains_all_prepared_fences(delivery):
    v=delivery;persist(v);prepare(v);before=snapshot(v)
    with pytest.raises(RuntimeError,match='delivery publication'):
        with v.store._tx() as db:
            commit(v,db)
            raise RuntimeError('delivery publication failed')
    assert snapshot(v)==before and len(v.calls)==1
    with v.store._tx() as db:assert not commit(v,db)['fences_retained']


def test_runtime_unknown_retries_exact_target_only(delivery):
    v=delivery;persist(v);targets=[]
    def unknown(target):targets.append(target);raise TimeoutError
    with pytest.raises(StoreError,match='runtime cleanup unconfirmed'):prepare(v,unknown)
    assert not v.calls and snapshot(v)[0]['state']=='releasing'
    def reconcile(target):targets.append(target);return receipt(target)
    prepare(v,reconcile)
    assert len(targets)==2 and targets[0]==targets[1]
    assert prepare(v,lambda _:pytest.fail('Do not repeat a durable proof'))['fences_retained']


def test_provider_unknown_keeps_fence_and_durable_runtime_proof(delivery):
    v=delivery;persist(v);original=v.leases.verifier
    v.leases.verifier=lambda _:None
    with pytest.raises(LeaseError,match='cleanup_unconfirmed'):prepare(v)
    assert snapshot(v)[0]['state']=='releasing' and v.leases.current('account') is not None
    v.leases.verifier=original
    assert prepare(v,lambda _:pytest.fail('Runtime already proven'))['fences_retained']


@pytest.mark.parametrize('failure',['boolean','generation','runtime','future','hash'])
def test_runtime_proof_requires_exact_identity_and_bounded_observation(delivery,failure):
    v=delivery;persist(v)
    def invalid(target):
        value=receipt(target)
        if failure=='boolean':return True
        if failure=='generation':return replace(value,target=replace(target,generation=2))
        if failure=='runtime':
            attempts=tuple(replace(a,runtime_id='foreign') if a.attempt_id==target.root_id else a for a in target.attempts)
            return replace(value,target=replace(target,attempts=attempts))
        if failure=='future':return replace(value,observed_at=1001)
        return replace(value,evidence_sha256='invalid')
    with pytest.raises(StoreError,match='runtime cleanup unconfirmed'):prepare(v,invalid)
    assert not v.calls and snapshot(v)[0]['state']=='releasing'


def test_commit_without_cleanup_does_not_release(delivery):
    v=delivery;persist(v)
    with v.store._tx() as db:
        with pytest.raises(StoreError,match='not prepared'):commit(v,db)
    assert snapshot(v)[0]['state']=='held' and v.leases.current('account') is not None


def test_wrong_intent_cannot_reuse_prepared_cleanup(delivery):
    v=delivery;persist(v);prepare(v);before=snapshot(v)
    with v.store._tx() as db:
        with pytest.raises(StoreError,match='intent binding'):
            v.s.commit_delivery_cleanup(db,v.root,expected_generation=1,intent_sha256='f'*64)
    assert snapshot(v)==before


@pytest.mark.parametrize('timing',['before','after'])
def test_cancellation_cleans_without_promotion_and_can_release(delivery,timing):
    v=delivery;persist(v)
    if timing=='before':cancel(v)
    else:prepare(v);cancel(v)
    prepare(v)
    with v.store._tx() as db:
        with pytest.raises(StoreError,match='promotion authority'):commit(v,db)
        result=commit(v,db,cancelled=True)
    assert result['release_purpose']=='cancelled_delivery' and v.leases.current('account') is None
    assert v.store.get_attempt(v.root)['state']=='cancelled'
    with v.store._connect() as db:assert db.execute('SELECT delivery_version FROM sessions').fetchone()[0]==0


def test_cancelled_commit_cannot_release_verifying_root(delivery):
    v=delivery;persist(v);prepare(v)
    v.store.cancel(v.owner,v.root,'request-without-terminalization')
    with v.store._tx() as db:
        with pytest.raises(StoreError,match='promotion authority'):commit(v,db,cancelled=True)
    assert snapshot(v)[0]['state']=='releasing'


def test_cancelled_cleanup_commit_rollback_retains_fences(delivery):
    v=delivery;persist(v);prepare(v);cancel(v);before=snapshot(v)
    with pytest.raises(RuntimeError,match='cancel receipt'):
        with v.store._tx() as db:
            commit(v,db,cancelled=True)
            raise RuntimeError('cancel receipt publication failed')
    assert snapshot(v)==before
    with v.store._tx() as db:assert commit(v,db,cancelled=True)['release_purpose']=='cancelled_delivery'


def test_cancellation_during_unknown_cleanup_uses_same_frozen_target(delivery):
    v=delivery;persist(v);targets=[]
    def unknown(target):targets.append(target);return None
    with pytest.raises(StoreError,match='unconfirmed'):prepare(v,unknown)
    cancel(v)
    def reconcile(target):targets.append(target);return receipt(target)
    prepare(v,reconcile)
    assert targets[0]==targets[1]
    assert next(a.state for a in targets[1].attempts if a.attempt_id==v.root)=='verifying'
    with v.store._tx() as db:assert commit(v,db,cancelled=True)['release_purpose']=='cancelled_delivery'


@pytest.mark.parametrize('field,value',[
    ('schema_version',True),('generation',True),('root_id','other'),('expected_delivery_version',True),
    ('base_last_artifact','other'),('base_revision','f'*64),('base_artifact','other'),('extra',1),
])
def test_exact_envelope_echo_refuses_before_cleanup(delivery,field,value):
    v=delivery;v.envelope[field]=value;persist(v)
    with pytest.raises(StoreError,match='semantic binding'):prepare(v)
    assert not v.calls and not snapshot(v)[3]


@pytest.mark.parametrize('field,value',[('kind','report'),('root_id','other'),('generation',True),
    ('selected_revision_sha256','f'*64)])
def test_public_proof_echo_refuses(delivery,field,value):
    v=delivery;v.envelope['workflow_proof']['public'][field]=value;persist(v)
    with pytest.raises(StoreError,match='semantic binding'):prepare(v)
    assert not v.calls


def test_artifact_other_scope_refuses(delivery):
    v=delivery;v.envelope['artifact']['attempt_id']='other';persist(v)
    with pytest.raises(StoreError,match='semantic binding'):prepare(v)
    assert not v.calls


@pytest.mark.parametrize('mutation',['release_hash','reorder','missing','child'])
def test_ordered_release_membership_refuses(delivery,mutation):
    v=delivery;refs=v.envelope['workflow_proof']['public']['decision_refs']
    if mutation=='release_hash':refs[0]['release_sha256']='e'*64
    elif mutation=='reorder':refs.reverse()
    elif mutation=='missing':refs.pop()
    else:refs[0]['child_id']=v.root
    persist(v)
    with pytest.raises((StoreError,ValueError)):prepare(v)
    assert not v.calls and not snapshot(v)[3]


@pytest.mark.parametrize('mutation',['pending','release_event','runtime','extra_child'])
def test_live_database_membership_drift_refuses(delivery,mutation):
    v=delivery;persist(v)
    child=v.envelope['workflow_proof']['public']['decision_refs'][0]['child_id']
    with v.store._tx() as db:
        if mutation=='pending':db.execute("UPDATE attempts SET state='queued' WHERE id=?",(child,))
        elif mutation=='release_event':db.execute("DELETE FROM events WHERE attempt_id=? AND type='workflow.stage_child_released'",(child,))
        elif mutation=='runtime':db.execute('UPDATE workflow_roots SET child_attempt_id=?',(child,))
        else:
            with pytest.raises(sqlite3.IntegrityError,match='immutable_workflow_identity'):
                db.execute("UPDATE attempts SET workflow_root_id=NULL WHERE id=?",(child,))
            return
    with pytest.raises((StoreError,ValueError)):prepare(v)
    assert not v.calls and not snapshot(v)[3]


def test_intent_fences_broker_before_cleanup(delivery):
    v=delivery
    with v.store._tx() as db:assert v.s.authorize_parent(db,v.scope)
    persist(v)
    with v.store._tx() as db:assert not v.s.authorize_parent(db,v.scope)
    assert snapshot(v)[0]['state']=='held'


def test_callback_outside_db_and_identity_change_refuses(delivery):
    v=delivery;persist(v)
    def changed(target):
        with v.store._tx() as db:db.execute('UPDATE workflow_roots SET version=version+1')
        return receipt(target)
    with pytest.raises(StoreError,match='target changed'):prepare(v,changed)
    assert not v.calls


def test_current_controller_required(delivery):
    v=delivery;persist(v);v.leases.instance_id='other-instance'
    with pytest.raises(StoreError):prepare(v)
    assert not v.calls


def test_commit_requires_this_database_transaction(delivery,tmp_path):
    v=delivery;persist(v);prepare(v)
    with v.store._connect() as db:
        with pytest.raises(StoreError,match='transaction required'):commit(v,db)
    with sqlite3.connect(tmp_path/'other.db') as db:
        db.execute('BEGIN')
        with pytest.raises(StoreError,match='transaction required'):commit(v,db)
