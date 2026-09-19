"""Qualified admission through real scheduler, stage and protected-plan composition."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from cloudworkbench import hermes_adapter as adapter
from cloudworkbench import routed_caller
from cloudworkbench.environments import EnvironmentRegistry
from cloudworkbench.workflow_submission import enqueue_qualified_workflow
from cloudworkbench.workflow_routing import WorkflowError
from cloudworkbench.routed_stage import prepare_child_stage
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.stage_contracts import VERSION
from cloudworkbench.routed_verification import prepare_qualified_verification, VerificationPhaseError
from tests.test_environments import passed
from tests import test_routed_stage as stage_tests
from tests.test_routed_verification import composed, result_phase, cleanup, driven, setup


@pytest.fixture
def qualified_stage(tmp_path, monkeypatch):
    registry = EnvironmentRegistry(tmp_path/'environments.db')
    manifest = {'project_id':'demo','version':'v1','architecture':'amd64',
        'base_image_digest':'sha256:'+'a'*64,'image_digest':'sha256:'+'a'*64,
        'cli_versions':{'python':'3'},'readiness_probes':[{'id':'python','argv':['python3','--version']}],
        'checks':[]}
    registry.register(manifest); registry.qualify('demo','v1',passed)
    def qualified(scheduler, principal, request, key, **workflow):
        return enqueue_qualified_workflow(scheduler,principal,{**request,'environment_version':'v1'},key,
            registry=registry,allowed_versions=('v1',),script_sources={},forbidden_values=(),**workflow)
    monkeypatch.setattr(stage_tests,'enqueue_workflow',qualified)
    base = stage_tests.stage.__wrapped__(tmp_path)
    return SimpleNamespace(base=base,registry=registry,manifest=manifest)


@pytest.fixture
def stage(qualified_stage):
    return qualified_stage.base


def prepare(value):
    scheduler,owner,child,revision,pstack,destination=value.base
    return prepare_child_stage(scheduler,child['id'],expected_generation=1,revision=revision,
        destination=destination,execution_profile=NativeProfile('openai-codex','gpt-6-astra','high'),
        trusted_pstack_root=pstack,qualify=lambda *_:True)


def test_qualified_admission_reaches_bound_stage_prompt_without_registry(qualified_stage):
    value=qualified_stage; s,_,child,revision,*_=value.base
    frozen=s.child_assignment(child['id'],expected_generation=1)['workflow_snapshot']['provenance']
    assert frozen['stage_contract_version']==VERSION
    assert frozen['environment']['manifest']['version']=='v1'
    value.registry.path.rename(value.registry.path.with_suffix('.offline'))
    prepared=prepare(value);receipt=json.loads(prepared.launch.receipt_json)
    assert receipt['qualified_environment']['manifest_sha256']==frozen['environment']['manifest_sha256']
    assert receipt['stage_contract']['role']=='code_review'
    assert receipt['stage_contract']['input_revision_sha256']==revision.sha256
    assert receipt['stage_contract']['context_refs']==[]
    assert receipt['stage_context_sha256']
    assert 'Required final answer contract' in prepared.launch.prompt
    assert 'untrusted_prior_stage_context' in prepared.launch.prompt
    assert prepared.materialization.readonly


def test_frozen_environment_is_not_replaced_by_new_active_version(qualified_stage):
    value=qualified_stage
    value.registry.register({**value.manifest,'version':'v2','image_digest':'sha256:'+'b'*64})
    value.registry.qualify('demo','v2',passed);value.registry.activate('demo','v2',expected_active=None)
    receipt=json.loads(prepare(value).launch.receipt_json)
    assert receipt['qualified_environment']['version']=='v1'
    assert receipt['qualified_environment']['image_digest']==value.manifest['image_digest']


def test_qualified_verifier_uses_frozen_checks_not_ambient_composed_fixture(composed):
    d=composed.data
    prepared=prepare_qualified_verification(d['scheduler'],d['runtime'],d['spec'],composed.results,
        allowed_versions=('v1',),script_sources={},forbidden_values=(),services=(d['service'],),
        storage_root=composed.storage,material_root=composed.material)
    assert prepared.plan.checks==()
    assert composed.environment['checks']  # Unrelated fixture config cannot replace frozen checks.
    with pytest.raises(VerificationPhaseError,match='environment_override'):
        prepare_qualified_verification(d['scheduler'],d['runtime'],d['spec'],composed.results,
            allowed_versions=('v1',),script_sources={},forbidden_values=(),environment={})


@pytest.mark.parametrize('profile',[
    {'network_profile':'internet'}, {'secret_refs':['private-key']},
    {'startup_commands':[{'id':'start','argv':['python3','server.py']}]},
])
def test_unsupported_environment_profile_refused_before_admission(tmp_path, profile):
    registry=EnvironmentRegistry(tmp_path/'registry.db')
    manifest={'project_id':'demo','version':'v1','architecture':'amd64',
        'base_image_digest':'sha256:'+'a'*64,'image_digest':'sha256:'+'a'*64,
        'cli_versions':{'python':'3'},'readiness_probes':[{'id':'python','argv':['python3','--version']}],
        'checks':[],**profile}
    registry.register(manifest);registry.qualify('demo','v1',passed)
    with pytest.raises(WorkflowError,match='runtime_profile_unsupported'):
        enqueue_qualified_workflow(None,None,
            {'project_id':'demo','agent':'hermes','goal':'Review code','environment_version':'v1'},
            'key',registry=registry,allowed_versions=('v1',),script_sources={},forbidden_values=())


def test_private_policy_override_rejected_before_registry_or_admission():
    with pytest.raises(WorkflowError,match='policy_override'):
        enqueue_qualified_workflow(None,None,{},'key',registry=None,
            allowed_versions=(),script_sources={},forbidden_values=(),_stage_contract_version='other')


def test_modified_context_text_refused_before_prompt_construction(qualified_stage,monkeypatch):
    from cloudworkbench import routed_context
    original=routed_context.load_stage_context
    monkeypatch.setattr(routed_context,'load_stage_context',
        lambda *a,**kw:replace(original(*a,**kw),text='Altered context with unchanged digest'))
    with pytest.raises(adapter.HermesAdapterError,match='context_binding'):
        prepare(qualified_stage)
    assert not qualified_stage.base[-1].exists()


@pytest.mark.parametrize('text',[123,'x'*(adapter.MAX_TEXT+1),'{}'+' '*(adapter.MAX_TEXT-2)+'EXTRA'])
def test_structured_final_answer_is_never_silently_clipped(text):
    parser=adapter.JsonlParser(strict_result=True)
    parser.feed(json.dumps({'type':'system','subtype':'init'}).encode()+b'\n')
    with pytest.raises(adapter.HermesAdapterError,match='structured_result'):
        parser.feed(json.dumps({'type':'result','exit_code':0,'text':text}).encode()+b'\n')
    with pytest.raises(adapter.HermesAdapterError,match='parser_closed'):parser.finish()


def test_exact_limit_structured_final_answer_remains_complete():
    parser=adapter.JsonlParser(strict_result=True)
    parser.feed(b'{"type":"system","subtype":"init"}\n')
    text='x'*adapter.MAX_TEXT
    result=parser.feed(json.dumps({'type':'result','exit_code':0,'text':text}).encode()+b'\n')
    parser.finish();assert result[-1]['payload']['summary']==text


def test_caller_enforces_contract_bound_before_success(qualified_stage,tmp_path):
    prepared=prepare(qualified_stage);plan=prepared.launch
    code="import json; print(json.dumps({'type':'system','subtype':'init'})); print(json.dumps({'type':'result','exit_code':0,'text':'x'*16385}))"
    plan=replace(plan,argv=(sys.executable,'-c',code,'--run-budget','10'))
    output=tmp_path/'observed';output.mkdir()
    assert routed_caller.supervise_hermes(plan,{'PATH':'/usr/bin:/bin'},'f'*64,output,cwd=tmp_path)==1
    assert json.loads((output/'result.json').read_text())['status']=='failed'


@pytest.mark.parametrize('change',[{'image_digest':'sha256:'+'b'*64},{'resources':{}}])
def test_wrong_environment_refused_before_caller_preparation(setup,change):
    from cloudworkbench.hermes_adapter import RoutedLaunchPlan
    from cloudworkbench.runtime import RuntimeError
    routed,spec,docker=setup
    raw=json.loads((Path(spec.task_dir)/'launch.json').read_text())
    for name in ('argv','required_readonly_paths','required_empty_directories'):
        raw[name]=tuple(raw[name])
    raw['environment']=tuple(tuple(v) for v in raw['environment'])
    plan=RoutedLaunchPlan(**raw);receipt=json.loads(plan.receipt_json)
    receipt['qualified_environment']={'image_digest':routed.runtime.image,
        'resources':{k:getattr(routed.runtime,k) for k in ('cpus','memory_mib','pids','workspace_mib')},**change}
    plan=replace(plan,receipt_json=json.dumps(receipt))
    with pytest.raises(RuntimeError,match='qualified environment runtime mismatch'):
        routed.prepare_caller(spec.attempt_id,spec.session_id,generation=1,plan=plan,
            workspace='/absent',scratch='/absent',task_dir='/absent',worker_socket_dir='/absent')
    assert not docker.calls
