"""Internal catalogs only: no retry authority, provider calls, or remote mutation."""
import json
import hashlib

import pytest

from cloudworkbench import workflow_routing as routing
from cloudworkbench.pstack_routing import BackendProfile, RoleRouter, requested_policy
from cloudworkbench.workflow_instructions import bind_instructions, load_stage_instructions, STAGE_BOUNDARY, _reference


NORMAL_IDS = ('feature', 'bug_fix', 'refactoring', 'perf_issue', 'hillclimb', 'investigation',
              'why', 'planning', 'plan_review', 'code_review', 'verification', 'tooling_reflection')
NORMAL_DIGEST = '2cc3f9a98ecad5b0bb105de9d75993a0b47572c7201918257cddf2c827f9e9bd'


def router(ready=lambda _: True):
    profiles = {name: BackendProfile(name, family, model, 'synthetic', effort, family, 'a'*64, (effort,))
        for name, (family, model, effort) in routing.EXPECTED_IDENTITIES.items()}
    return RoleRouter(requested_policy(), profiles, ready=ready), profiles['fable-max']


def test_normal_catalog_digest_and_jev_choices_are_unchanged():
    assert tuple(routing.WORKFLOWS) == NORMAL_IDS
    assert routing.policy_digest() == NORMAL_DIGEST
    assert routing.remediation_policy_digest() != NORMAL_DIGEST
    criteria = routing.payload('Repair the task', list(NORMAL_IDS))['questions']['workflow']['criteria']
    assert tuple(criteria) == NORMAL_IDS + ('no_match',)


@pytest.mark.parametrize('original', ['feature', 'bug_fix', 'refactoring', 'perf_issue', 'hillclimb', 'planning', 'plan_review'])
def test_plan_repair_preserves_original_coding_suffix(original):
    result = routing.remediation_workflow(original, 'plan_review', 'reject', 'needs_review')
    assert result.id == 'repair_plan_' + original
    assert [(s.id, s.role) for s in result.steps[:2]] == [('revise_plan', 'plan_revision'), ('challenge_plan', 'plan_review')]
    assert result.steps[2:] == routing.WORKFLOWS[original].steps[3:]
    assert routing.resolve_catalog(result.id, routing.remediation_policy_digest()) == result
    assert routing.remediation_workflow(result.id, 'plan_review', 'reject', 'needs_review') == result


@pytest.mark.parametrize('original,role,outcome', [
    ('feature', 'code_review', 'needs_review'), ('code_review', 'code_review', 'needs_review'),
    ('verification', 'acceptance_verification', 'rejected'),
    ('repair_code', 'acceptance_verification', 'rejected'),
    ('repair_plan_feature', 'code_review', 'needs_review'),
])
def test_code_repair_always_includes_independent_review_and_acceptance(original, role, outcome):
    result = routing.remediation_workflow(original, role, 'reject', outcome)
    assert result.id == 'repair_code'
    assert [(s.id, s.role) for s in result.steps] == [
        ('implement', 'bug_fix'), ('review_code', 'code_review'), ('verify', 'acceptance_verification')]


@pytest.mark.parametrize('values', [
    ('feature', 'code_review', None, 'needs_review'), ('feature', 'code_review', 'pass', 'unverified'),
    ('feature', 'acceptance_verification', 'reject', 'needs_review'),
    ('feature', 'planning', 'reject', 'needs_review'), ('planning', 'code_review', 'reject', 'needs_review'),
    ('repair_code', 'plan_review', 'reject', 'needs_review'),
    ('unknown', 'code_review', 'reject', 'needs_review'), ([], 'code_review', 'reject', 'needs_review'),
    ('feature', [], 'reject', 'needs_review'), ('feature', 'code_review', True, 'needs_review'),
])
def test_uncertain_unsupported_or_foreign_blocker_refused(values):
    with pytest.raises(routing.WorkflowError): routing.remediation_workflow(*values)


@pytest.mark.parametrize('identity,digest', [
    ('feature', '0'*64), ('feature', True), ('feature', []),
    ('feature', 'remediation'), ('repair_code', NORMAL_DIGEST),
    ('repair_plan_not_real', None), ('repair_plan_repair_plan_feature', None),
])
def test_catalog_requires_exact_approved_id_and_matching_version(identity, digest):
    with pytest.raises(routing.WorkflowError): routing.resolve_catalog(identity, digest)


@pytest.mark.parametrize('identity', ['repair_code', 'repair_plan_feature', 'repair_plan_plan_review'])
def test_internal_catalog_cannot_be_selected_by_jev_or_explicit_normal_selection(identity):
    with pytest.raises(routing.WorkflowError): routing.payload('Repair task', [identity])
    with pytest.raises(routing.WorkflowError):
        routing.select_workflow('Repair task', allowed_workflows=['feature'], explicit_workflow=identity)


def test_resolution_retains_blocked_steps_and_exact_seats_without_fallback():
    routes, parent = router(ready=lambda profile: profile.profile_id != 'astra-high')
    plan = routing.resolve_remediation('feature', 'plan_review', 'reject', 'needs_review', router=routes, parent=parent)
    assert [s['profile']['profile_id'] for s in plan['steps']] == ['fable-max', 'astra-high', 'grok-xhigh', 'astra-high', 'sol-max']
    assert [s['profile']['effort'] for s in plan['steps']] == ['max', 'high', 'xhigh', 'high', 'max']
    assert not plan['ready'] and not plan['dispatch_performed']
    assert plan['failure_rules']['automatic_retry'] is False
    assert plan['workflow_policy_sha256'] == routing.remediation_policy_digest()
    plan['failure_rules']['automatic_retry'] = True
    assert routing.FAILURE_RULES['automatic_retry'] is False


def test_mislabelled_remediation_model_is_refused():
    fake = BackendProfile('astra-high', 'xai', 'grok-4.6', 'synthetic', 'high', 'xai', 'a'*64, ('high',))
    routes = RoleRouter({'plan_revision': ['astra-high']}, {'astra-high': fake}, ready=lambda _: True)
    with pytest.raises(routing.WorkflowError, match='approved_role_policy_mismatch'):
        routing.resolve_remediation('planning', 'plan_review', 'reject', 'needs_review', router=routes, parent=fake)


@pytest.mark.parametrize('identity', ['repair_code', 'repair_plan_feature', 'repair_plan_planning', 'repair_plan_plan_review'])
def test_internal_stage_instructions_are_bound_and_checked(tmp_path, identity):
    workflow = routing.resolve_catalog(identity)
    for step in workflow.steps:
        path = tmp_path / _reference(step.role)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Pinned role guidance: ' + step.role)
    refs = bind_instructions(identity, tmp_path)
    for step in workflow.steps:
        loaded = load_stage_instructions(identity, step.id, refs, tmp_path, frozen_boundary=STAGE_BOUNDARY)
        assert loaded.role == step.role and loaded.text == 'Pinned role guidance: ' + step.role
    (tmp_path / refs[0]['relative_path']).write_text('Changed after freeze')
    with pytest.raises(routing.WorkflowError, match='pstack_reference_changed'):
        load_stage_instructions(identity, workflow.steps[0].id, refs, tmp_path, frozen_boundary=STAGE_BOUNDARY)


@pytest.fixture(scope='module', params=[False, True], ids=['pass', 'rejected'])
def finalized_repair(request, tmp_path_factory):
    from tests import test_six_stage_composition as six
    from cloudworkbench import workflow_submission
    root = tmp_path_factory.mktemp('repair-catalog')
    with pytest.MonkeyPatch.context() as patch:
        expected = [('revise_plan', 'plan_revision', 'claude-fable-5-1', 'max'),
                    ('challenge_plan', 'plan_review', 'gpt-6-astra', 'high')]
        patch.setattr(six, 'EXPECTED', expected)
        def resolve(_selection, _summary, *, allowed_workflows, router, parent):
            return routing.resolve_remediation('planning', 'plan_review', 'reject', 'needs_review', router=router, parent=parent)
        patch.setattr(workflow_submission, 'resolve_workflow', resolve)
        patch.setattr(workflow_submission, 'bind_instructions',
            lambda _workflow, path: bind_instructions('repair_plan_planning', path))
        flow = six.flow.__wrapped__(root)
        for index in range(2):
            stage = root / ('stage-' + str(index)); stage.mkdir(mode=0o700)
            with pytest.MonkeyPatch.context() as boundary:
                six.execute_stage(flow, index, stage, boundary, reject=request.param and index == 1)
    return flow, request.param


def test_real_internal_catalog_finalization_and_strict_policy_roundtrip(finalized_repair):
    from cloudworkbench import routed_delivery_policy, routed_stop_policy
    flow, rejected = finalized_repair
    if rejected:
        proof = routed_stop_policy.collect_stopped_workflow_proof(flow.s, flow.root['attempt_id'], expected_generation=1)
        assert proof.outcome == 'needs_review'
        assert proof.public_record()['blocker']['gate'] == 'reject'
        assert routed_stop_policy.StoppedWorkflowProof.from_json(proof.canonical_json) == proof
    else:
        proof = routed_delivery_policy.collect_root_delivery_proof(flow.s, flow.root['attempt_id'], expected_generation=1,
            selected_revision=flow.revision)
        assert proof.kind == 'report' and proof.outcome == 'unverified'
        assert routed_delivery_policy.RootDeliveryProof.from_json(proof.canonical_json) == proof
    assert proof.public_record()['workflow'] == 'repair_plan_planning'
    assert len(proof.public_record()['decision_refs']) == 2
    assert flow.revision == flow.initial


def test_internal_public_proof_cannot_claim_an_unapproved_or_normal_sequence(finalized_repair):
    from cloudworkbench import routed_delivery_policy, routed_stop_policy
    flow, rejected = finalized_repair
    if rejected:
        proof = routed_stop_policy.collect_stopped_workflow_proof(flow.s, flow.root['attempt_id'], expected_generation=1)
        parse = routed_stop_policy.StoppedWorkflowProof.from_json
    else:
        proof = routed_delivery_policy.collect_root_delivery_proof(flow.s, flow.root['attempt_id'], expected_generation=1,
            selected_revision=flow.revision)
        parse = routed_delivery_policy.RootDeliveryProof.from_json
    for identity in ('invented-repair', 'planning', 'repair_code'):
        value = proof.to_dict(); value['public']['workflow'] = identity
        with pytest.raises(ValueError): parse(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False))


@pytest.mark.parametrize('digest', [None, NORMAL_DIGEST, 'f'*64])
def test_collectors_require_frozen_internal_policy_digest(finalized_repair, monkeypatch, digest):
    from cloudworkbench import routed_delivery_policy, routed_stop_policy
    from cloudworkbench.store import encode
    flow, rejected = finalized_repair
    module = routed_stop_policy if rejected else routed_delivery_policy
    snapshot = module._snapshot
    def changed(db, root):
        value = snapshot(db, root)
        row = value['workflow_roots']['rows'][0]
        frozen = json.loads(row['frozen'])
        frozen['provenance']['workflow']['workflow_policy_sha256'] = digest
        row['frozen'] = encode(frozen)
        row['frozen_digest'] = hashlib.sha256(row['frozen'].encode()).hexdigest()
        return value
    monkeypatch.setattr(module, '_snapshot', changed)
    with pytest.raises(ValueError, match='policy_changed'):
        if rejected:
            module.collect_stopped_workflow_proof(flow.s, flow.root['attempt_id'], expected_generation=1)
        else:
            module.collect_root_delivery_proof(flow.s, flow.root['attempt_id'], expected_generation=1,
                selected_revision=flow.revision)
