from dataclasses import replace
import json

import pytest
from cloudworkbench.store import Store, StoreError
from cloudworkbench.scheduler import RoleScheduler
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.pstack_routing import BackendProfile, RoleRouter, requested_policy
from cloudworkbench.workflow_routing import EXPECTED_IDENTITIES, select_workflow
from cloudworkbench.workflow_submission import enqueue_workflow
from cloudworkbench.workflow_revisions import RevisionBinding, capture_revision
from cloudworkbench.role_broker import RoleBroker, ParentScope
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.routed_stage import prepare_child_stage


@pytest.fixture
def stage(tmp_path):
    store=Store(tmp_path/'state.db');owner=store.add_client('stage','x'*40,['submit','observe','cancel'],['demo'])
    store.migrate_scheduler()
    leases=ProviderLeases(store,cleanup_verifier=lambda _:None,inspector_id='fixture',clock=lambda:1000)
    leases.register_account('account',legacy_agent='hermes',persistent_owner_id='owner')
    scheduler=RoleScheduler(store,leases,clock=lambda:1000)
    providers={'anthropic':'claude-code','openai':'openai-codex','xai':'xai-oauth'}
    profiles={name:BackendProfile(name,providers[family],model,'chat_completions' if family=='anthropic' else 'codex_responses',
        effort,family,'a'*64,(effort,)) for name,(family,model,effort) in EXPECTED_IDENTITIES.items()}
    router=RoleRouter(requested_policy(),profiles,ready=lambda _:True)
    pstack=tmp_path/'pstack';ref=pstack/'skills/interrogate/references/reviewer-prompt.md';ref.parent.mkdir(parents=True);ref.write_text('Review the assigned code.')
    request={'project_id':'demo','agent':'hermes','goal':'Review existing code'}
    root=enqueue_workflow(scheduler,owner,request,'root',selection=select_workflow(request['goal'],allowed_workflows=['code_review'],explicit_workflow='code_review'),
        allowed_workflows=['code_review'],router=router,parent=profiles['fable-max'],accounts={'account':'owner'},trusted_pstack_root=pstack)
    scheduler.admit_root(root['attempt_id'],accounts={'account':'owner'})
    scheduler.transition(root['attempt_id'],'running',expected_generation=1)
    workspace=tmp_path/'work';workspace.mkdir();(workspace/'code.py').write_text('answer = 42\n')
    revisions=tmp_path/'revisions';revisions.mkdir()
    binding=RevisionBinding(owner['id'],'demo',root['session_id'],root['turn_id'],root['attempt_id'],1,root['attempt_id'],1)
    revision=capture_revision(store,binding,workspace,revisions,selected_paths=('code.py',),controller_attests_quiesced=True)
    broker=RoleBroker(store.path,authorize_parent=scheduler.authorize_parent,clock=lambda:1000)
    scope=ParentScope(owner['id'],'demo',root['attempt_id'],root['attempt_id'],1,'native')
    with store._connect() as db:provenance=json.loads(db.execute('SELECT frozen FROM workflow_roots').fetchone()[0])['provenance']
    if 'stage_progression_version' in provenance:
        from cloudworkbench.routed_progression import register_root_input
        register_root_input(scheduler,scope,revision)
    _,token=broker.issue(scope,roles=('code_review',),expires_at=2000)
    pending=broker.prepare(token,native_session_id='native',native_call_id='call',role='code_review',task='Review code.py for correctness')
    with store._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?',(root['attempt_id'],)).fetchone()[0])
    scheduler.admit_request(broker,scope,pending['id'],plan=frozen['role_plans']['code_review'],step_id='review_code',input_revision_sha256=revision.sha256)
    child=scheduler.claim_child(root['attempt_id'])
    return scheduler,owner,child,revision,pstack,tmp_path/'stage'


def prepare(stage,**kwargs):
    scheduler,owner,child,revision,pstack,destination=stage
    return prepare_child_stage(scheduler,child['id'],expected_generation=1,revision=kwargs.pop('revision',revision),
        destination=destination,execution_profile=kwargs.pop('execution_profile',NativeProfile('openai-codex','gpt-6-astra','high')),
        trusted_pstack_root=pstack,qualify=kwargs.pop('qualify',lambda *_:True),**kwargs)


def test_real_scheduler_child_becomes_exact_readonly_hermes_plan(stage):
    value=prepare(stage)
    assert value.attempt_id==stage[2]['id']
    assert value.materialization.readonly and value.launch.workspace_readonly
    assert value.launch.request_path=='/v1/responses'
    assert (value.materialization.source/'code.py').read_text()=='answer = 42\n'
    assert value.materialization.revision_sha256==stage[3].sha256
    receipt=json.loads(value.launch.receipt_json)
    assert receipt['role']=='code_review' and receipt['model']=='gpt-6-astra' and receipt['effort']=='high'
    assert receipt['input_revision_sha256']==stage[3].sha256
    assert stage[0].store.get_attempt(value.attempt_id)['state']=='preparing'


def test_wrong_model_profile_rejected_before_materialization(stage):
    with pytest.raises(StoreError,match='profile differs'):
        prepare(stage,execution_profile=NativeProfile('xai-oauth','grok-4.6','xhigh'))
    assert not stage[-1].exists()


def test_unqualified_profile_never_materialized(stage):
    with pytest.raises(StoreError,match='qualification unavailable'):prepare(stage,qualify=lambda *_:False)
    assert not stage[-1].exists()


def test_assigned_revision_cannot_be_substituted(stage):
    with pytest.raises(StoreError,match='revision differs'):prepare(stage,revision=replace(stage[3],sha256='f'*64))
    assert not stage[-1].exists()


def test_cancelled_stage_refused(stage):
    scheduler,owner,child,*_=stage
    scheduler.store.cancel(owner,child['id'],'cancel')
    with pytest.raises(StoreError,match='authority unavailable'):prepare(stage)
    assert not stage[-1].exists()


def test_revoked_owner_refused_before_materialization(stage):
    scheduler,owner,*_=stage
    with scheduler.store._tx() as db:db.execute("UPDATE clients SET revoked_at='revoked' WHERE id=?",(owner['id'],))
    with pytest.raises(StoreError,match='authority unavailable'):prepare(stage)
    assert not stage[-1].exists()


def test_cancel_during_preparation_never_returns_prepared_stage(stage):
    scheduler,owner,child,*_=stage
    def qualify(*args):
        scheduler.store.cancel(owner,child['id'],'cancel-during-prepare')
        return True
    with pytest.raises(StoreError,match='authority unavailable'):prepare(stage,qualify=qualify)
    assert not stage[-1].exists()
    assert scheduler.store.get_attempt(child['id'])['cancel_requested']


def test_cancel_after_publication_discards_only_unlaunched_copy(stage,monkeypatch):
    from cloudworkbench import routed_stage
    scheduler,owner,child,*_=stage
    original=routed_stage.materialize_stage
    def publish_then_cancel(*args,**kwargs):
        value=original(*args,**kwargs)
        assert value.source.exists()
        scheduler.store.cancel(owner,child['id'],'cancel-after-publication')
        return value
    monkeypatch.setattr(routed_stage,'materialize_stage',publish_then_cancel)
    with pytest.raises(StoreError,match='authority unavailable'):prepare(stage)
    assert not stage[-1].exists()
    assert stage[3].path.exists()


def test_authority_read_closes_its_sqlite_connection(stage,monkeypatch):
    from cloudworkbench.routed_stage import _authority
    scheduler,_,child,*_=stage
    connect=scheduler.store._connect
    closed=[]
    class Connection:
        def __init__(self):self.inner=connect()
        def __getattr__(self,name):return getattr(self.inner,name)
        def close(self):self.inner.close();closed.append(True)
    monkeypatch.setattr(scheduler.store,'_connect',Connection)
    assert _authority(scheduler,child['id'],1).attempt_id==child['id']
    assert closed==[True]


def test_read_only_plan_can_be_provisioned_before_materialization(stage):
    from cloudworkbench.routed_stage import plan_child_stage
    scheduler,_,child,revision,pstack,destination=stage
    before=set(destination.parent.iterdir())
    planned=plan_child_stage(scheduler,child['id'],expected_generation=1,revision=revision,
        execution_profile=NativeProfile('openai-codex','gpt-6-astra','high'),trusted_pstack_root=pstack,qualify=lambda *_:True)
    assert set(destination.parent.iterdir())==before
    assert planned.consumer.attempt_id==child['id'] and planned.assigned_role=='code_review'
    assert planned.readonly and planned.input_revision_sha256==revision.sha256
    prepared=prepare(stage,expected_plan=planned)
    assert prepared.launch==planned.launch and prepared.profile_digest==planned.profile_digest
    assert prepared.role_request_id==planned.role_request_id and prepared.workflow_digest==planned.workflow_digest


def test_changed_provisioned_plan_refused_before_copy(stage):
    from cloudworkbench.routed_stage import plan_child_stage
    scheduler,_,child,revision,pstack,destination=stage
    planned=plan_child_stage(scheduler,child['id'],expected_generation=1,revision=revision,
        execution_profile=NativeProfile('openai-codex','gpt-6-astra','high'),trusted_pstack_root=pstack,qualify=lambda *_:True)
    with pytest.raises(StoreError,match='Provisioned routed plan differs'):
        prepare(stage,expected_plan=replace(planned,role_request_id='another-request'))
    assert not destination.exists()


def test_cleanup_failure_preserves_original_authority_error(stage,monkeypatch):
    from cloudworkbench import routed_stage
    from cloudworkbench.workflow_revisions import RevisionError
    scheduler,owner,child,*_=stage
    original=routed_stage.materialize_stage
    def publish_then_cancel(*args,**kwargs):
        value=original(*args,**kwargs)
        scheduler.store.cancel(owner,child['id'],'cancel-after-publication')
        return value
    def failed_cleanup(*args,**kwargs):raise RevisionError('private detail must not escape')
    monkeypatch.setattr(routed_stage,'materialize_stage',publish_then_cancel)
    monkeypatch.setattr(routed_stage,'discard_unlaunched_materialization',failed_cleanup)
    with pytest.raises(StoreError,match='authority unavailable') as caught:prepare(stage)
    assert caught.value.cleanup_failure=='unlaunched_materialization_retained'
    assert 'reconciliation' in caught.value.__notes__[0]
    assert 'private detail' not in repr(caught.value) and stage[-1].exists()
