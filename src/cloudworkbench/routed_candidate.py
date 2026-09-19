"""Capture an exported workspace candidate without granting a workflow outcome."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .artifacts import _open_directory, _remove_staging
from .inference_budget import _stamp
from .routed_cleanup import RoutedChildCleanup
from .routed_export import WorkspaceExport, validate_workspace_export
from .routed_stage import PreparedStage
from .routed_publication import (
    authorize_result, result_authority, read_cleanup_evidence, validate_cleanup_membership,
)
from .scheduler import _child_control_tx
from .store import StoreError, encode
from .workflow_revisions import (
    RevisionBinding, RevisionLimits, WorkspaceRevision, _write_tree,
    capture_revision, verify_revision,
)


@dataclass(frozen=True)
class CandidateRevision:
    revision: WorkspaceRevision
    artifact_id: str
    input_revision_sha256: str
    export_receipt_sha256: str
    cleanup_sha256: str
    verification_pass: bool = field(default=False, init=False)


def _private_root(path, spec):
    path = Path(os.path.abspath(path))
    descriptor = _open_directory(path)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise StoreError(409, 'Candidate storage must be controller private')
    finally:
        os.close(descriptor)
    for value in (spec.workspace, spec.scratch, spec.task_dir, spec.worker_socket_dir):
        source = Path(value)
        if path == source or source in path.parents or path in source.parents:
            raise StoreError(409, 'Candidate storage overlaps caller material')
    return path


def capture_exported_candidate(scheduler, runtime, spec, quiesced, cleanup, exported,
                               revision_root, *, prepared, limits=RevisionLimits(), forbidden_values=()):
    """Trusted controller entry point. Call after exporter cleanup is confirmed.

    A candidate is immutable retained evidence, not a delivered session revision.
    Cancellation after capture can leave an unregistered immutable revision for
    reconciliation; it cannot register the candidate or advance the workflow.
    """
    if (type(cleanup) is not RoutedChildCleanup or cleanup.scheduler is not scheduler
            or cleanup.runtime is not runtime or cleanup.spec != spec
            or cleanup.quiesced != quiesced or type(exported) is not WorkspaceExport
            or type(prepared) is not PreparedStage
            or type(limits) is not RevisionLimits):
        raise StoreError(409, 'Candidate controller binding mismatch')
    if (prepared.attempt_id != spec.attempt_id or prepared.generation != spec.generation
            or str(prepared.materialization.source) != spec.workspace
            or str(prepared.materialization.scratch) != spec.scratch
            or exported.workspace_identity != prepared.materialization.source_identity):
        raise StoreError(409, 'Candidate original workspace binding mismatch')
    root = _private_root(revision_root, spec)
    runtime._private(runtime.root, directory=True)
    if root == runtime.root or root in runtime.root.parents:
        raise StoreError(409, 'Candidate storage overlaps private runtime journals')
    with _child_control_tx(scheduler.store) as db:
        authority = result_authority(scheduler, db, spec, quiesced)
    assignment = authority['assignment']
    binding = RevisionBinding(assignment['owner_id'], assignment['project_id'],
        assignment['session_id'], assignment['turn_id'], assignment['root_id'],
        assignment['root_generation'], spec.attempt_id, spec.generation)
    if (prepared.materialization.consumer != binding
            or prepared.materialization.revision_sha256 != assignment['input_revision_sha256']
            or prepared.profile_digest != authority['binding'].profile_digest):
        raise StoreError(409, 'Candidate original revision binding mismatch')
    evidence = cleanup.collect()
    proof = read_cleanup_evidence(cleanup, evidence)

    def authorized(_action):
        authorize_result(scheduler, spec, quiesced)
        return True

    validate_workspace_export(runtime, spec, exported, authorize=authorized)
    staging = Path(tempfile.mkdtemp(prefix='.export-candidate-', dir=runtime.root))
    try:
        files = {item.path: (item.data, item.executable) for item in exported.tree.files}
        _write_tree(staging / 'files', files, exported.tree.directories, readonly=True)
        authorize_result(scheduler, spec, quiesced)
        revision = capture_revision(scheduler.store, binding, staging / 'files', root,
            selected_paths=exported.selected_paths, controller_attests_quiesced=True,
            limits=limits, forbidden_values=forbidden_values)
    finally:
        _remove_staging(staging)
    verify_revision(scheduler.store, revision, limits=limits)
    validate_workspace_export(runtime, spec, exported, authorize=authorized)
    if cleanup.collect() != evidence:
        raise StoreError(409, 'Candidate cleanup evidence changed')
    manifest = revision.path / 'manifest.json'
    artifact_id = hashlib.sha256(('routed-candidate:' + spec.attempt_id + ':' +
                                  str(spec.generation)).encode()).hexdigest()
    metadata = {
        'id': artifact_id, 'session_id': spec.session_id, 'attempt_id': spec.attempt_id,
        'path': f'routed/{spec.attempt_id}/{spec.generation}/candidate-manifest.json',
        'storage_path': str(manifest), 'bytes': manifest.stat().st_size,
        'sha256': revision.sha256, 'mime': 'application/json', 'generation': spec.generation,
        'provenance': 'controller_captured_candidate', 'verification_pass': False,
        'input_revision_sha256': assignment['input_revision_sha256'],
        'output_revision_sha256': revision.sha256,
        'export_receipt_sha256': exported.receipt_sha256,
        'cleanup_sha256': evidence.evidence_sha256,
        'launch_binding_sha256': quiesced.binding_digest,
    }
    event = {'artifact_id': artifact_id, 'generation': spec.generation,
             'input_revision_sha256': assignment['input_revision_sha256'],
             'output_revision_sha256': revision.sha256, 'verification_pass': False}
    with _child_control_tx(scheduler.store) as db:
        current = result_authority(scheduler, db, spec, quiesced)
        if current['assignment'] != assignment:
            raise StoreError(409, 'Candidate assignment changed')
        validate_cleanup_membership(db, proof, current['binding'], current['root'],
                                    current['accounts'], scheduler)
        existing = db.execute('''SELECT * FROM artifacts WHERE id=? OR
            (attempt_id=? AND json_extract(metadata, '$.path')=?)''',
            (artifact_id, spec.attempt_id, metadata['path'])).fetchall()
        if existing:
            if (len(existing) != 1 or existing[0]['id'] != artifact_id
                    or existing[0]['session_id'] != spec.session_id
                    or existing[0]['attempt_id'] != spec.attempt_id
                    or json.loads(existing[0]['metadata']) != metadata):
                raise StoreError(409, 'Candidate publication conflict')
            prior_events = db.execute('''SELECT payload FROM events WHERE attempt_id=?
                AND type='workflow.candidate_published' AND json_extract(payload, '$.artifact_id')=?''',
                (spec.attempt_id, artifact_id)).fetchall()
            if len(prior_events) != 1 or json.loads(prior_events[0][0]) != event:
                raise StoreError(409, 'Candidate publication record incomplete')
        else:
            db.execute('INSERT INTO artifacts(id,session_id,attempt_id,metadata) VALUES(?,?,?,?)',
                       (artifact_id, spec.session_id, spec.attempt_id, encode(metadata)))
            scheduler.store._event(db, spec.session_id, spec.attempt_id, 'artifact.created',
                                  {key: value for key, value in metadata.items() if key != 'storage_path'})
            scheduler.store._event(db, spec.session_id, spec.attempt_id,
                                  'workflow.candidate_published', event)
        db.execute('UPDATE inference_budget_roots SET high_water_us=? WHERE root_id=? AND high_water_us<?',
            (_stamp(current['observed_at']), current['root']['root_id'], _stamp(current['observed_at'])))
    return CandidateRevision(revision, artifact_id, assignment['input_revision_sha256'],
                             exported.receipt_sha256, evidence.evidence_sha256)
