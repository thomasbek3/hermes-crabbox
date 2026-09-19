"""Explicit correction admission over an authentically stopped local workflow."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from cloudworkbench import routed_remediation as remediation
from cloudworkbench.pstack_routing import BackendProfile,RoleRouter,requested_policy
from cloudworkbench.workflow_routing import EXPECTED_IDENTITIES
from cloudworkbench.store import StoreError
from tests.test_routed_stop import blocked_seed, stopped, close
from tests.test_routed_delivery import session, state


@pytest.fixture
def value(stopped,tmp_path):
    v=stopped;v.stop=close(v)
    families={'anthropic':'claude-code','openai':'openai-codex','xai':'xai-oauth'}
    profiles={name:BackendProfile(name,families[family],model,
        'chat_completions' if family=='anthropic' else 'codex_responses',effort,family,'a'*64,(effort,))
        for name,(family,model,effort) in EXPECTED_IDENTITIES.items()}
    v.router=RoleRouter(requested_policy(),profiles,ready=lambda _:True);v.parent=profiles['fable-max']
    v.pstack=tmp_path/'pstack'
    from cloudworkbench.workflow_instructions import _ROLE_REFS,_CODING_REFS
    for ref in set((_ROLE_REFS|_CODING_REFS).values()):
        path=v.pstack/ref;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('Synthetic repair instructions')
    v.base={k:session(v)[k] for k in remediation._BASE_KEYS}
    v.revisions=tmp_path/'rebound';v.revisions.mkdir(mode=0o700)
    return v


def enqueue(v,key='explicit-fix',**kwargs):
    return remediation.enqueue_remediation(v.s,v.owner,v.root,key,expected_generation=1,
        expected_stop_sha256=kwargs.pop('stop_sha',v.stop.proof_sha256),
        expected_delivery_base=kwargs.pop('base',v.base),router=v.router,parent=v.parent,
        trusted_pstack_root=v.pstack,**kwargs)


def prepare(v,root):
    return remediation.prepare_remediation_input(v.s,v.owner,root['attempt_id'],expected_generation=1,
        revision_root=v.revisions)


def test_missing_preparation_root_refuses_without_creating_state(value):
    v=value;before=state(v)
    with pytest.raises(StoreError,match='Remediation root unavailable') as error:
        remediation.prepare_remediation_input(v.s,v.owner,'missing-root',expected_generation=1,
            revision_root=v.revisions)
    assert error.value.status_code==409 and state(v)==before
    assert not list(v.revisions.iterdir()) and v.s.leases.current('account') is None


def test_admission_rebind_and_budget_preserve_source_and_session(value):
    v=value;source=state(v);created=enqueue(v)
    assert enqueue(v)==created and created['session_id']==v.session and created['attempt_id']!=v.root
    assert state(v)==source
    with v.store._connect() as db:
        new=v.store._attempt(db,created['attempt_id'])
        root=dict(db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(created['attempt_id'],)).fetchone())
        frozen=json.loads(root['frozen']);lineage=frozen['provenance']['remediation']
        old_request=db.execute('SELECT request FROM turns WHERE id=?',(v.store._attempt(db,v.root)['turn_id'],)).fetchone()[0]
        assert db.execute('SELECT request FROM turns WHERE id=?',(created['turn_id'],)).fetchone()[0]==old_request
        assert new['root_sequence']==2 and new['generation']==1 and new['parent_attempt_id']==v.root
        assert [(s['role'],s['profile']['model'],s['profile']['effort']) for s in frozen['provenance']['workflow']['steps']]==[
            ('bug_fix','grok-4.6','xhigh'),('code_review','gpt-6-astra','high'),('acceptance_verification','gpt-5.6-sol','max')]
        assert lineage['remaining_requests']==123
    with pytest.raises(StoreError,match='Registered workflow input'):
        v.s.admit_root(created['attempt_id'],accounts={'account':'owner'})
    assert v.s.leases.current('account') is None
    rebound=prepare(v,created)
    assert prepare(v,created)==rebound
    assert rebound.source_sha256==v.revision.sha256 and rebound.revision.sha256!=v.revision.sha256
    assert (rebound.revision.path/'files/code.py').read_bytes()==(v.revision.path/'files/code.py').read_bytes()
    assert state(v)==source
    admitted=v.s.admit_root(created['attempt_id'],accounts={'account':'owner'})
    assert admitted['state']=='preparing'
    with v.store._connect() as db:
        budget=dict(db.execute('SELECT * FROM inference_budget_roots WHERE root_id=?',(created['attempt_id'],)).fetchone())
        old=dict(db.execute('SELECT * FROM inference_budget_roots WHERE root_id=?',(v.root,)).fetchone())
        assert budget['deadline_us']==old['deadline_us'] and json.loads(budget['policy_json'])['root_requests']==123
    assert enqueue(v)==created


def test_stale_base_and_changed_key_payload_do_not_create_work(value):
    v=value;initial=state(v)
    with pytest.raises(StoreError,match='base changed'):enqueue(v,base={**v.base,'delivery_version':1})
    assert state(v)==initial
    created=enqueue(v)
    with pytest.raises(StoreError,match='different request'):enqueue(v,base={**v.base,'delivery_version':1})
    with pytest.raises(StoreError,match='no longer latest'):enqueue(v,key='second-key')
    assert enqueue(v)==created


def test_terminal_commit_ack_loss_returns_same_queued_root(value,monkeypatch):
    v=value;original=remediation._child_control_tx;lost=[]
    @contextmanager
    def tx(store,*,write=True):
        changed=False
        with original(store,write=write) as db:
            before=db.execute('SELECT COUNT(*) FROM workflow_roots').fetchone()[0]
            yield db
            changed=write and db.execute('SELECT COUNT(*) FROM workflow_roots').fetchone()[0]>before
        if changed and not lost:lost.append(True);raise StoreError(503,'lost remediation ack')
    monkeypatch.setattr(remediation,'_child_control_tx',tx)
    with pytest.raises(StoreError,match='lost remediation ack'):enqueue(v)
    result=enqueue(v)
    assert enqueue(v)==result and v.s.leases.current('account') is None
    with v.store._connect() as db:assert db.execute('SELECT COUNT(*) FROM workflow_roots').fetchone()[0]==2


@pytest.mark.parametrize('point',['attempt.state','workflow.remediation_admitted'])
def test_admission_event_fault_rolls_back_turn_root_and_base(value,monkeypatch,point):
    v=value;original=v.store._event
    def fail(db,sid,aid,kind,payload):
        if kind==point:raise RuntimeError('admission event fault')
        return original(db,sid,aid,kind,payload)
    monkeypatch.setattr(v.store,'_event',fail)
    with pytest.raises(RuntimeError,match='admission event fault'):enqueue(v)
    with v.store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM workflow_roots').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM turns').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM workflow_delivery_bases').fetchone()[0]==1


@pytest.mark.parametrize('change',['revoke','deadline','cancel_source','foreign_owner'])
def test_fresh_admission_requires_authority_and_remaining_lineage(value,change):
    v=value
    if change=='deadline':v.s.clock=lambda:15400
    elif change=='foreign_owner':v.owner=v.store.add_client('other','y'*40,['submit'],['demo'])
    else:
        with v.store._tx() as db:
            if change=='revoke':db.execute("UPDATE clients SET revoked_at='revoked' WHERE id=?",(v.owner['id'],))
            else:db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(v.root,))
    with pytest.raises((StoreError,ValueError)):enqueue(v)
    with v.store._connect() as db:assert db.execute('SELECT COUNT(*) FROM workflow_roots').fetchone()[0]==1


def test_concurrent_same_key_has_one_root_and_no_resource_launch(value):
    v=value;results=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(enqueue,v) for _ in range(2)]
        for future in futures:
            try:results.append(future.result())
            except StoreError as error:assert error.status_code==503
    assert results and all(r==results[0] for r in results)
    assert enqueue(v)==results[0] and v.s.leases.current('account') is None
    with v.store._connect() as db:assert db.execute('SELECT COUNT(*) FROM workflow_roots').fetchone()[0]==2


@pytest.mark.parametrize('change',['archive','scope','deadline','cancel'])
def test_prepared_queue_rechecks_authority_before_reserving(value,change):
    v=value;created=enqueue(v);prepare(v,created)
    if change=='archive':v.store.archive(v.owner,v.session,'archive-remediation')
    elif change=='deadline':v.s.clock=lambda:15400
    elif change=='cancel':v.store.cancel(v.owner,created['attempt_id'],'cancel-remediation')
    else:
        with v.store._tx() as db:db.execute("UPDATE clients SET scopes='[]' WHERE id=?",(v.owner['id'],))
    with pytest.raises(StoreError):v.s.admit_root(created['attempt_id'],accounts={'account':'owner'})
    assert v.s.leases.current('account') is None
    with v.store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_accounts WHERE root_id=?',(created['attempt_id'],)).fetchone()
        assert not db.execute('SELECT 1 FROM inference_budget_roots WHERE root_id=?',(created['attempt_id'],)).fetchone()


@pytest.mark.parametrize('reject_again',[False,True])
def test_actual_code_correction_reviews_and_acceptance_preserve_rejection(value,blocked_seed,tmp_path,monkeypatch,reject_again):
    from tests import test_six_stage_composition as six
    from cloudworkbench.role_broker import RoleBroker,ParentScope
    from cloudworkbench.routed_progression import stage_inputs
    from cloudworkbench.routed_delivery import prepare_root_delivery,finalize_root_delivery
    from cloudworkbench.routed_stop import close_stopped_workflow
    from cloudworkbench.scheduler import RootCleanupReceipt
    from cloudworkbench.provider_leases import CleanupReceipt
    v=value;original=state(v);created=enqueue(v);rebound=prepare(v,created)
    v.s.admit_root(created['attempt_id'],accounts={'account':'owner'})
    v.s.transition(created['attempt_id'],'running',expected_generation=1)
    scope=ParentScope(v.owner['id'],'demo',created['attempt_id'],created['attempt_id'],1,'native')
    broker=RoleBroker(v.store.path,authorize_parent=v.s.authorize_parent,clock=lambda:1000)
    expected=[('implement','bug_fix','grok-4.6','xhigh'),('review_code','code_review','gpt-6-astra','high'),
        ('verify','acceptance_verification','gpt-5.6-sol','max')]
    _,token=broker.issue(scope,roles=tuple(s[1] for s in expected),expires_at=2000)
    with v.store._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?',(created['attempt_id'],)).fetchone()[0])
    flow=SimpleNamespace(store=v.store,s=v.s,owner=v.owner,root=created,scope=scope,broker=broker,token=token,
        frozen=frozen,pstack=v.pstack,protected=blocked_seed[0].protected,revision=rebound.revision,
        initial=rebound.revision,stages=[])
    monkeypatch.setattr(six,'EXPECTED',expected)
    for index in range(2 if reject_again else 3):
        path=tmp_path/f'repair-stage-{index}';path.mkdir(mode=0o700)
        with monkeypatch.context() as patch:
            six.execute_stage(flow,index,path,patch,reject=reject_again and index==1)
    v.s.leases.verifier=lambda target:CleanupReceipt(target,'synthetic-inspector','terminated',1000,'a'*64)
    verifier=lambda target:RootCleanupReceipt(target,'terminated',1000,'c'*64)
    if reject_again:
        with pytest.raises(StoreError):stage_inputs(v.s,scope,'verify')
        stopped_receipt=close_stopped_workflow(v.s,created['attempt_id'],expected_generation=1,cleanup_verifier=verifier)
        assert stopped_receipt.outcome=='needs_review'
        with pytest.raises(StoreError,match='lineage limit'):
            remediation.enqueue_remediation(v.s,v.owner,created['attempt_id'],'no-second-repair',expected_generation=1,
                expected_stop_sha256=stopped_receipt.proof_sha256,expected_delivery_base=v.base,
                router=v.router,parent=v.parent,trusted_pstack_root=v.pstack)
        assert session(v)==original['session']
    else:
        delivery=prepare_root_delivery(v.s,created['attempt_id'],expected_generation=1,
            expected_delivery_version=0,expected_base_revision_sha256=None,selected_revision=flow.revision,
            storage_root=v.storage)
        result=finalize_root_delivery(v.s,delivery,cleanup_verifier=verifier)
        assert result.outcome=='verified' and session(v)['delivery_version']==1
        assert session(v)['delivered_revision_sha256']==flow.revision.sha256
    current=state(v)
    assert current['root']==original['root'] and current['attempt']==original['attempt'] and current['events']==original['events']
    assert v.s.leases.current('account') is None
    with v.store._connect() as db:
        assert db.execute('SELECT used FROM inference_budget_roots WHERE root_id=?',(v.root,)).fetchone()[0]==5
        assert db.execute('SELECT used FROM inference_budget_roots WHERE root_id=?',(created['attempt_id'],)).fetchone()[0]==len(flow.stages)


def test_preparation_commit_ack_loss_reuses_one_immutable_input(value,monkeypatch):
    v=value;created=enqueue(v);original=remediation._child_control_tx;lost=[]
    @contextmanager
    def tx(store,*,write=True):
        changed=False
        with original(store,write=write) as db:
            before=db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.remediation_input'").fetchone()[0]
            yield db
            changed=write and before==0 and db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.remediation_input'").fetchone()[0]==1
        if changed and not lost:lost.append(True);raise StoreError(503,'lost preparation ack')
    monkeypatch.setattr(remediation,'_child_control_tx',tx)
    with pytest.raises(StoreError,match='lost preparation ack'):prepare(v,created)
    result=prepare(v,created)
    assert prepare(v,created)==result and v.s.leases.current('account') is None
    with v.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.remediation_input'").fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM artifacts WHERE attempt_id=?',(created['attempt_id'],)).fetchone()[0]==1


def test_exhausted_source_budget_cannot_reset_at_new_root(value):
    v=value
    with v.store._tx() as db:db.execute('UPDATE inference_budget_roots SET used=128 WHERE root_id=?',(v.root,))
    with pytest.raises(StoreError,match='budget exhausted'):enqueue(v)
    with v.store._connect() as db:assert db.execute('SELECT COUNT(*) FROM workflow_roots').fetchone()[0]==1
