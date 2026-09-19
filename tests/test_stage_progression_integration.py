"""Actual finalization/release to next-stage admission and complete plan context."""
import json
import pytest
from cloudworkbench.routed_progression import VERSION,register_root_input,stage_inputs
from cloudworkbench.role_broker import RoleBroker,ParentScope
from cloudworkbench.routed_stage import prepare_child_stage
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.store import StoreError
from tests import test_routed_stage_decision as finalizer
from tests.test_routed_stage_decision import (
    setup,driven,cleanup,result_phase,completed_input,complete,release,
)


@pytest.fixture
def stage(tmp_path,monkeypatch):
    original_enqueue=finalizer.enqueue_workflow
    original_capture=finalizer.capture_revision
    def capture(store,binding,*args,**kwargs):
        revision=original_capture(store,binding,*args,**kwargs)
        # The existing make_stage helper captures its input before it returns
        # the scheduler. Its enqueue wrapper supplies this controller instance.
        scope=ParentScope(binding.owner_id,binding.project_id,binding.root_attempt_id,
            binding.root_attempt_id,binding.root_generation,'native')
        register_root_input(holder['scheduler'],scope,revision)
        return revision
    holder={}
    def enqueue(scheduler,*args,**kw):
        holder['scheduler']=scheduler
        return original_enqueue(scheduler,*args,**kw,_stage_progression_version=VERSION)
    monkeypatch.setattr(finalizer,'enqueue_workflow',enqueue)
    monkeypatch.setattr(finalizer,'capture_revision',capture)
    return finalizer.make_stage(tmp_path,role='planning')


def target(stage):
    value,_=stage;s,owner,child,*_=value
    scope=ParentScope(owner['id'],'demo',child['workflow_root_id'],child['workflow_root_id'],1,'native')
    return s,scope


def admit_review(stage,**changes):
    s,scope=target(stage);selected=stage_inputs(s,scope,'challenge_plan');selected.update(changes)
    broker=RoleBroker(s.store.path,authorize_parent=s.authorize_parent,clock=lambda:1000)
    _,token=broker.issue(scope,roles=('plan_review',),expires_at=2000)
    pending=broker.prepare(token,native_session_id='native',native_call_id='review',role='plan_review',
        task='Challenge the prior plan.',context_refs=tuple(selected['context_refs']))
    with s.store._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots').fetchone()[0])
    s.admit_request(broker,scope,pending['id'],plan=frozen['role_plans']['plan_review'],
        step_id='challenge_plan',input_revision_sha256=selected['input_revision_sha256'])
    return s.claim_child(scope.root_attempt_id)


def test_real_finalized_plan_reaches_exact_next_review_prompt(completed_input,stage,tmp_path):
    result=complete(completed_input);assert result.outcome=='unverified'
    release(completed_input,result)
    next_child=admit_review(stage)
    s,_,_,revision,pstack,_=stage[0]
    with s.store._connect() as db:
        binding=json.loads(db.execute('SELECT binding FROM workflow_child_launch WHERE child_id=?',
            (completed_input.data['spec'].attempt_id,)).fetchone()[0])
    assert binding['execution_profile']['path']==str(stage[1].path)
    assert binding['execution_profile']['native_model']=='claude-fable-5-1'
    assert binding['execution_profile']['effort']=='max'
    prepared=prepare_child_stage(s,next_child['id'],expected_generation=1,revision=revision,
        destination=tmp_path/'review-stage',execution_profile=NativeProfile('openai-codex','gpt-6-astra','high'),
        trusted_pstack_root=pstack,qualify=lambda *_:True)
    receipt=json.loads(prepared.launch.receipt_json)
    assert receipt['model']=='gpt-6-astra' and receipt['effort']=='high'
    assert receipt['stage_contract']['context_refs']==[{'artifact_id':result.artifact_id,'sha256':result.sha256}]
    assert 'Inspect code.py.' in prepared.launch.prompt
    assert 'Run the protected criterion.' in prepared.launch.prompt
    assert 'stage_contract' in prepared.launch.prompt and prepared.materialization.readonly


def test_passed_stage_without_release_cannot_admit_next(completed_input,stage):
    complete(completed_input)
    with pytest.raises(StoreError,match='cleanup release required'):admit_review(stage)


@pytest.mark.parametrize('changes',[{'input_revision_sha256':'f'*64},{'context_refs':[]}])
def test_real_finalized_output_cannot_be_substituted(completed_input,stage,changes):
    result=complete(completed_input);release(completed_input,result)
    with pytest.raises(StoreError,match='differs'):admit_review(stage,**changes)


@pytest.mark.parametrize('driven',['needs_review'],indirect=True)
def test_incomplete_plan_does_not_advance(completed_input,stage):
    result=complete(completed_input);assert result.gate is None
    release(completed_input,result)
    with pytest.raises(StoreError,match='output chain changed'):admit_review(stage)
