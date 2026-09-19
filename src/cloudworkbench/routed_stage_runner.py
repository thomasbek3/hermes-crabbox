"""One-shot trusted composition of an already prepared routed child."""
from dataclasses import dataclass, field
import json

from .inference_relay import AttemptBinding
from .provider_dispatch import ProviderDispatch
from .routed_driver import drive_prepared_child, _validate_prepared
from .routed_environment import load_routed_environment
from .routed_observation_store import validate_forbidden_values
from .routed_results import publish_child_results
from .routed_stage_decision import (CODING_ROLES, CommittedStage, complete_stage,
    release_stage_child)
from .routed_decision import CommittedDecision, complete_task_verification, release_decided_child
from .routed_verification import (prepare_qualified_verification, run_verification,
    _private_root, _identity, _disjoint)
from .routed_verification_policy import VerificationLimits
from .routed_verifier_runtime import VerifierRuntime
from .stage_contracts import ROLES
from .store import encode
from .supervised_executor import SupervisedProviderExecutor
from .worker_service import WorkerService
from .workflow_revisions import WorkspaceRevision, verify_revision


class StageRunError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value:
        raise StageRunError(code)


@dataclass(frozen=True)
class CompletedStage:
    decision: CommittedStage | CommittedDecision
    carried_revision: WorkspaceRevision
    release_receipt_json: str = field(repr=False)

    @property
    def release_receipt(self):
        return json.loads(self.release_receipt_json)


def _services(scheduler, prepared, services):
    _require(type(services) is tuple and 1 <= len(services) <= 32
        and all(type(service) is WorkerService for service in services), 'stage_services_invalid')
    expected = AttemptBinding(prepared.attempt_id, prepared.generation, prepared.profile_digest)
    grants = set()
    for service in services:
        wrapped = service.dispatcher.execute_request
        _require(type(wrapped) is SupervisedProviderExecutor, 'stage_services_invalid')
        executor = wrapped.executor
        _require(service.binding == expected and executor.binding == expected
            and type(executor.dispatch) is ProviderDispatch
            and executor.dispatch.leases is scheduler.leases
            and callable(getattr(executor.dispatch.runtime, 'after_request_cleanup', None))
            and executor.grant_id not in grants, 'stage_service_binding_changed')
        receipt = service.receipt
        _require(receipt.started and not receipt.stop_requested and not receipt.thread_stopped
            and not receipt.resources_closed and receipt.error is None, 'stage_service_unavailable')
        grants.add(executor.grant_id)


def run_prepared_stage(scheduler, runtime, prepared, spec, *, execution_profile,
                       services, input_revision, publication_root, revision_root,
                       decision_root, selected_paths, forbidden_values,
                       allowed_environment_versions=(), script_sources=None, verifier=None,
                       verification_material_root=None, required_tool_gid=None,
                       verification_limits=VerificationLimits(), timeout_seconds=3600,
                       relay_timeout_seconds=20, poll_seconds=.25):
    """Run once, publish retained evidence, decide, then release this child only.

    All handles and policies are controller-owned. This function issues no grants,
    provisions no directories, and never retries or recreates a launched child.
    On exception the caller retains its original runtime/service handles and the
    existing phase journals for explicit reconciliation. Underlying phases retain
    their exact cleanup behavior; there is no generic close/discard fallback.
    """
    secrets = validate_forbidden_values(forbidden_values)
    _validate_prepared(prepared, spec, execution_profile)
    _require(type(input_revision) is WorkspaceRevision
        and input_revision.sha256 == prepared.materialization.revision_sha256,
        'stage_input_revision_changed')
    verify_revision(scheduler.store, input_revision)
    assignment = scheduler.child_assignment(spec.attempt_id, expected_generation=spec.generation)
    role = assignment['role']
    _require(role in ROLES and assignment['input_revision_sha256'] == input_revision.sha256
        and assignment['workflow_digest'] == prepared.workflow_digest
        and assignment['role_request_id'] == prepared.role_request_id
        and prepared.materialization.readonly == (role not in CODING_ROLES), 'stage_assignment_changed')
    _services(scheduler, prepared, services)
    from .routed_export_protocol import normalize_selectors
    selected_paths = normalize_selectors(selected_paths)
    roots = tuple(_private_root(path) for path in (publication_root, revision_root, decision_root))
    for index, root in enumerate(roots):
        _disjoint(root, (input_revision.path, prepared.materialization.source,
            prepared.materialization.scratch, *roots[:index]))
    publication_root, revision_root, decision_root = roots
    task = role == 'acceptance_verification'
    if task:
        _require(type(verifier) is VerifierRuntime and verifier.base is runtime.runtime
            and verification_material_root is not None
            and type(verification_limits) is VerificationLimits,
            'stage_verifier_configuration_required')
        _require(required_tool_gid is None or type(required_tool_gid) is int and required_tool_gid == 1000,
            'stage_verifier_tool_identity_unsupported')
        _identity(verification_material_root)
        _disjoint(verification_material_root, (*roots, input_revision.path,
            prepared.materialization.source, prepared.materialization.scratch))
        snapshot = assignment['workflow_snapshot']['provenance'].get('environment')
        resolved = load_routed_environment(snapshot, project_id=input_revision.binding.project_id,
            allowed_versions=allowed_environment_versions, script_sources=script_sources,
            forbidden_values=secrets)
        _require(resolved.manifest['version'] == assignment['attempt']['request']['environment_version'],
            'stage_environment_changed')
    quiesced = drive_prepared_child(scheduler, runtime, prepared, spec,
        execution_profile=execution_profile, timeout_seconds=timeout_seconds,
        relay_timeout_seconds=relay_timeout_seconds, poll_seconds=poll_seconds,
        forbidden_values=secrets)
    results = publish_child_results(scheduler, runtime, prepared, spec, quiesced,
        execution_profile=execution_profile, services=services, publication_root=publication_root,
        revision_root=revision_root, selected_paths=selected_paths, forbidden_values=secrets)
    _require(results.candidate is not None, 'stage_candidate_required')
    common = dict(input_revision=input_revision, services=services, forbidden_values=secrets)
    if task:
        verification = prepare_qualified_verification(scheduler, runtime, spec, results,
            allowed_versions=allowed_environment_versions, script_sources=script_sources,
            forbidden_values=secrets, services=services, storage_root=decision_root,
            material_root=verification_material_root, required_tool_gid=required_tool_gid,
            limits=verification_limits)
        run_verification(scheduler, runtime, spec, results, verification, verifier,
            services=services, forbidden_values=secrets)
        decision = complete_task_verification(scheduler, runtime, spec, results,
            verification, verifier, **common)
    else:
        decision = complete_stage(scheduler, runtime, spec, results,
            storage_root=decision_root, **common)
    carried = results.candidate.revision if role in CODING_ROLES else input_revision
    _require(decision.carried_revision_sha256 == carried.sha256, 'stage_carried_revision_changed')
    if task:
        release = release_decided_child(scheduler, runtime, spec, results, verification,
            verifier, decision_sha256=decision.sha256, **common)
    else:
        release = release_stage_child(scheduler, runtime, spec, results,
            decision_sha256=decision.sha256, **common)
    return CompletedStage(decision, carried, encode(release))
