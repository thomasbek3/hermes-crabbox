"""Trusted result-phase composition; no outcome, gate, promotion or seat release."""
from dataclasses import dataclass, field, replace

from .routed_candidate import CandidateRevision, capture_exported_candidate
from .routed_cleanup import ChildCleanupEvidence, RoutedChildCleanup
from .routed_driver import QuiescedStage, _exclusive_driver, _validate_prepared
from .routed_export import (export_stopped_workspace, load_workspace_export,
                           reconcile_workspace_export, workspace_export_status)
from .routed_export_protocol import ExportLimits, normalize_selectors
from .routed_observation_store import load_observation, validate_forbidden_values
from .routed_publication import PublishedObservation, authorize_result, publish_observation
from .workflow_revisions import RevisionLimits


class ResultPhaseError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ChildResults:
    quiesced: QuiescedStage
    cleanup: ChildCleanupEvidence
    observation: PublishedObservation
    candidate: CandidateRevision | None
    verification_pass: bool = field(default=False, init=False)
    scheduler_seat_released: bool = field(default=False, init=False)


def publish_child_results(scheduler, runtime, prepared, spec, quiesced, *,
                          execution_profile, services, publication_root,
                          revision_root, selected_paths, forbidden_values,
                          export_limits=ExportLimits(), revision_limits=RevisionLimits()):
    """Resume durable results under the existing controller's current authority.

    Arguments are trusted controller policy, never model/API authority. Passing
    selected_paths=None publishes observations without exporting a workspace.
    Each artifact commits independently and remains unverified. A later failure
    can retain an earlier artifact; exact replay does not execute the caller again.
    Provider cleanup precedes reads/publication, including on cancelled children.
    """
    secrets = validate_forbidden_values(forbidden_values)
    _validate_prepared(prepared, spec, execution_profile)
    if (type(quiesced) is not QuiescedStage or type(export_limits) is not ExportLimits
            or type(revision_limits) is not RevisionLimits):
        raise ResultPhaseError('result_phase_context_invalid')
    selected = None if selected_paths is None else normalize_selectors(selected_paths)
    if selected is not None and revision_root is None:
        raise ResultPhaseError('result_revision_storage_required')
    with _exclusive_driver(runtime, spec):
        collector = RoutedChildCleanup(scheduler, runtime, spec, quiesced, services=services)
        cleanup = collector.collect()
        def authorize(action):
            if action in {'status', 'reconcile', 'cleanup_inspect', 'stop', 'remove'}:
                # Physical cleanup survives cancellation/deadlines, but requires
                # the same held owner and exact child/runtime binding.
                collector._snapshot()
            else:
                authorize_result(scheduler, spec, quiesced)
            return True

        export_state = workspace_export_status(runtime, spec, authorize=authorize)
        prior_export = export_state['state'] != 'not_started'
        reconciliation = None
        if prior_export:
            reconciliation = reconcile_workspace_export(runtime, spec, authorize=authorize,
                                                          forbidden_values=secrets)
        # Reconcile retained export operations before publication authorization:
        # a cancelled retry must still clean its exact collector, not leak it.
        # The disk snapshot is authoritative, not an in-memory observation.
        observation = load_observation(scheduler, runtime.root, spec, quiesced,
                                       forbidden_values=secrets)
        recovered = replace(quiesced, observation=observation)
        collector = RoutedChildCleanup(scheduler, runtime, spec, recovered, services=services)
        if prior_export and selected is None:
            raise ResultPhaseError('result_export_selection_changed')
        published = publish_observation(scheduler, prepared=prepared, spec=spec,
            quiesced=recovered, execution_profile=execution_profile, collector=collector,
            cleanup=cleanup, publication_root=publication_root, forbidden_values=secrets)
        candidate = None
        if selected is not None:
            if not prior_export:
                exported = export_stopped_workspace(runtime, spec, recovered.runtime_id,
                    recovered.caller_cleanup, selected_paths=selected, authorize=authorize,
                    expected_workspace_identity=prepared.materialization.source_identity,
                    limits=export_limits, forbidden_values=secrets)
            else:
                if reconciliation['bytes_recoverable'] is not True:
                    raise ResultPhaseError('result_export_unavailable_after_cleanup')
                exported = load_workspace_export(runtime, spec, authorize=authorize,
                                                   forbidden_values=secrets)
                if exported.selected_paths != selected:
                    raise ResultPhaseError('result_export_selection_changed')
            candidate = capture_exported_candidate(scheduler, runtime, spec, recovered,
                collector, exported, revision_root, prepared=prepared,
                limits=revision_limits, forbidden_values=secrets)
        authorize_result(scheduler, spec, recovered)
        return ChildResults(recovered, cleanup, published, candidate)
