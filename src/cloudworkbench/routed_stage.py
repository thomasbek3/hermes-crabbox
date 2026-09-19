"""Bind one admitted scheduler child to its exact revision and routed Hermes plan.

Preparation is not runtime admission, a provider qualification, or an outcome gate.
The controller must recheck authority when it records create intent and starts work.
"""
from dataclasses import dataclass, replace
from contextlib import closing
import json

from .hermes_adapter import RoutedLaunchPlan, build_routed_launch
from .inference_transport import PinnedCLI
from .native_responses import NativeProfile
from .provider_protocol import provider_profile_digest
from .store import StoreError
from .workflow_instructions import load_stage_instructions
from .workflow_revisions import (RevisionBinding, StageMaterialization, materialize_stage,
    discard_unlaunched_materialization)

_CODING = frozenset({'feature','bug_fix','refactoring','perf_issue','hillclimb'})


@dataclass(frozen=True)
class PreparedStage:
    attempt_id: str
    generation: int
    workflow_digest: str
    role_request_id: str
    profile_digest: str
    materialization: StageMaterialization
    launch: RoutedLaunchPlan


@dataclass(frozen=True)
class PlannedStage:
    consumer: RevisionBinding
    assigned_role: str
    workflow_digest: str
    role_request_id: str
    profile_digest: str
    input_revision_sha256: str
    readonly: bool
    launch: RoutedLaunchPlan


def _authority(scheduler, child_id, generation):
    with closing(scheduler.store._connect()) as db:
        db.execute('BEGIN')
        child = scheduler.store._attempt(db, child_id)
        root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(child.get('workflow_root_id'),)).fetchone()
        parent = scheduler.store._attempt(db,child.get('workflow_parent_id')) if child.get('workflow_parent_id') else None
        if (child.get('execution_kind') != 'hermes_child' or type(generation) is not int
                or child['generation'] != generation or child['state'] != 'preparing'
                or child['cancel_requested'] or not root or root['state'] != 'held'
                or root['cancel_requested'] or root['child_attempt_id'] != child_id
                or not parent or parent['cancel_requested'] or parent['generation'] != child['workflow_parent_generation']
                or parent['state'] != 'running' or not scheduler._owner_current(db,root['root_id'])
                or not scheduler.leases._attempt_valid(db,child_id,generation)
                or not scheduler.leases._attempt_valid(db,parent['id'],parent['generation'])):
            raise StoreError(409,'Routed stage authority unavailable')
        session = db.execute('SELECT owner_id,project_id FROM sessions WHERE id=?',(child['session_id'],)).fetchone()
        return RevisionBinding(session['owner_id'],session['project_id'],child['session_id'],child['turn_id'],
            root['root_id'],root['generation'],child_id,generation)


def plan_child_stage(scheduler, child_id, *, expected_generation, revision,
                     execution_profile, trusted_pstack_root, qualify, max_turns=32,
                     run_budget_seconds=300, forbidden_values=()):
    """Read-only planning; caller still needs fresh runtime/start authority."""
    binding = _authority(scheduler,child_id,expected_generation)
    assignment = scheduler.child_assignment(child_id,expected_generation=expected_generation)
    frozen = assignment['profile']
    if type(execution_profile) is NativeProfile:
        effective = (execution_profile.provider,execution_profile.model,execution_profile.effort,'codex_responses')
    elif type(execution_profile) is PinnedCLI:
        effective = ('claude-code',execution_profile.native_model,execution_profile.effort,'chat_completions')
    else:
        raise StoreError(409,'Unsupported routed execution profile')
    if (frozen.get('provider'),frozen.get('model'),frozen.get('effort'),frozen.get('transport')) != effective:
        raise StoreError(409,'Routed execution profile differs from admitted profile')
    digest = provider_profile_digest(execution_profile)
    if not callable(qualify) or qualify(dict(frozen),digest) is not True:
        raise StoreError(503,'Routed backend qualification unavailable')
    if assignment['input_revision_sha256'] != revision.sha256:
        raise StoreError(409,'Assigned stage revision differs')
    provenance = assignment['workflow_snapshot']['provenance']
    contract = context = None
    if 'stage_contract_version' in provenance:
        from .stage_contracts import VERSION, stage_contract
        from .routed_context import load_stage_context
        if provenance['stage_contract_version'] != VERSION:
            raise StoreError(409,'Frozen stage contract version unsupported')
        context = load_stage_context(scheduler, child_id, expected_generation=expected_generation,
                                     forbidden_values=forbidden_values)
        contract = stage_contract(assignment['role'], revision.sha256, assignment['context_refs'])
    elif assignment['context_refs']:
        raise StoreError(503,'Routed context requires a frozen stage contract')
    instructions = load_stage_instructions(provenance['workflow']['workflow'],assignment['workflow_step_id'],
        provenance['pstack_references'],trusted_pstack_root,frozen_boundary=provenance['stage_boundary'])
    if instructions.role != assignment['role']:
        raise StoreError(409,'Assigned stage role differs')
    readonly = assignment['role'] not in _CODING
    launch = build_routed_launch(execution_profile,instructions,task=assignment['task'],
        input_revision_sha256=revision.sha256,workspace_readonly=readonly,
        max_turns=max_turns,run_budget_seconds=run_budget_seconds,
        stage_contract=contract,stage_context=context)
    if 'environment' in provenance:
        from .routed_environment import validate_routed_environment_snapshot
        request = assignment['attempt']['request']
        environment = validate_routed_environment_snapshot(provenance['environment'],
            project_id=request['project_id'], allowed_versions=(request['environment_version'],),
            forbidden_values=forbidden_values)
        manifest = environment['manifest']
        receipt = json.loads(launch.receipt_json)
        receipt['qualified_environment'] = {
            'project_id':manifest['project_id'], 'version':manifest['version'],
            'snapshot_sha256':environment['snapshot_sha256'],
            'manifest_sha256':environment['manifest_sha256'],
            'image_digest':manifest['image_digest'], 'resources':manifest['resources']}
        launch = replace(launch, receipt_json=json.dumps(receipt,sort_keys=True,separators=(',',':')))
    if _authority(scheduler,child_id,expected_generation) != binding:
        raise StoreError(409,'Routed planning authority changed')
    return PlannedStage(binding,assignment['role'],assignment['workflow_digest'],
        assignment['role_request_id'],digest,revision.sha256,readonly,launch)


def prepare_child_stage(scheduler, child_id, *, expected_generation, revision, destination,
                        execution_profile, trusted_pstack_root, qualify, max_turns=32,
                        run_budget_seconds=300, required_tool_gid=None, expected_plan=None,
                        forbidden_values=()):
    """Use the same plan seam, then publish only an exact authorized materialization."""
    planned = plan_child_stage(scheduler,child_id,expected_generation=expected_generation,
        revision=revision,execution_profile=execution_profile,trusted_pstack_root=trusted_pstack_root,
        qualify=qualify,max_turns=max_turns,run_budget_seconds=run_budget_seconds,
        forbidden_values=forbidden_values)
    if expected_plan is not None and (type(expected_plan) is not PlannedStage or expected_plan != planned):
        raise StoreError(409,'Provisioned routed plan differs')
    binding,readonly,launch = planned.consumer,planned.readonly,planned.launch
    materialization = materialize_stage(scheduler.store,revision,binding,destination,readonly=readonly,
        required_tool_gid=required_tool_gid,
        before_publish=lambda consumer: consumer == binding and
            _authority(scheduler,child_id,expected_generation) == binding)
    try:
        if _authority(scheduler,child_id,expected_generation) != binding:
            raise StoreError(409,'Routed stage authority changed')
    except Exception as authority_error:
        # This function has not created a runtime; only its exact returned copy
        # may be discarded. Modified/replaced material is retained for recovery.
        try:
            discard_unlaunched_materialization(materialization,controller_attests_unlaunched=True)
        except Exception:
            authority_error.cleanup_failure = 'unlaunched_materialization_retained'
            authority_error.add_note('Unlaunched materialization cleanup failed; exact material retained for reconciliation.')
        raise
    return PreparedStage(child_id,expected_generation,planned.workflow_digest,
        planned.role_request_id,planned.profile_digest,materialization,launch)
