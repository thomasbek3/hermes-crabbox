from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_routed_cleanup import cleanup, driven, stage, setup, ready, request
from tests.test_routed_runtime import Docker
from cloudworkbench import routed_candidate as candidate
from cloudworkbench import routed_export as export
from cloudworkbench.routed_export_protocol import collector_program
from cloudworkbench.routed_publication import authorize_result, PublicationError
from cloudworkbench.store import StoreError
from cloudworkbench.workflow_revisions import RevisionError, verify_revision, materialize_stage


@pytest.fixture
def captured(cleanup, tmp_path, monkeypatch):
    request(cleanup)
    collector = ready(cleanup)
    collector.collect()
    spec, runtime = cleanup['spec'], cleanup['runtime']
    code = Path(spec.workspace) / 'code.py'
    code.chmod(0o600)
    code.write_bytes(b'answer = 43\n')
    stream = subprocess.run([sys.executable, '-I', '-S', '-c',
        collector_program(('code.py',), workspace=spec.workspace)],
        capture_output=True, check=True, timeout=10).stdout

    class CollectorDocker(Docker):
        def __call__(self, args, **kwargs):
            if args[0] == 'create':
                identity = super().__call__(args, **kwargs)
                obj = self.objects.pop(identity)
                obj['Id'] = 'e' * 64
                self.objects[obj['Id']] = obj
                return obj['Id']
            if args[:2] == ['start', '--attach']:
                self.calls.append(args)
                self.objects[args[-1]]['State'].update(Status='exited', Running=False, ExitCode=0)
                return stream
            return super().__call__(args, **kwargs)

    docker = CollectorDocker()
    def bounded(_runtime, args, **kwargs):
        value = docker(args, **kwargs)
        data = value if type(value) is bytes else value.encode()
        if kwargs.get('stdout_fd') is not None:
            os.write(kwargs['stdout_fd'], data)
            return b''
        return data
    monkeypatch.setattr(export, '_bounded_run', bounded)

    def authorize(_action):
        authorize_result(cleanup['scheduler'], spec, collector.quiesced)
        return True
    exported = export.export_stopped_workspace(runtime, spec, collector.quiesced.runtime_id,
        collector.quiesced.caller_cleanup, selected_paths=('code.py',), authorize=authorize,
        expected_workspace_identity=cleanup['driven'][3].materialization.source_identity)
    root = tmp_path / 'output-revisions'
    root.mkdir(mode=0o700)
    return cleanup, collector, exported, root, docker


def capture(value, **changes):
    data, collector, exported, root, _ = value
    args = dict(scheduler=data['scheduler'], runtime=data['runtime'], spec=data['spec'],
        quiesced=collector.quiesced, cleanup=collector, exported=exported, revision_root=root,
        prepared=data['driven'][3])
    return candidate.capture_exported_candidate(**{**args, **changes})


def test_export_becomes_bound_immutable_candidate_and_reviewer_copy(captured, tmp_path):
    data, collector, exported, _, docker = captured
    scheduler = data['scheduler']
    before = scheduler.store.get_attempt(data['spec'].attempt_id)
    result = capture(captured)
    assert result == capture(captured)
    assert result.verification_pass is False and not docker.objects
    assert result.export_receipt_sha256 == exported.receipt_sha256
    assert result.cleanup_sha256 == collector.collect().evidence_sha256
    assert result.input_revision_sha256 == data['driven'][3].materialization.revision_sha256
    assert result.revision.sha256 != result.input_revision_sha256
    manifest = verify_revision(scheduler.store, result.revision)
    assert manifest['binding']['attempt_id'] == data['spec'].attempt_id
    assert (result.revision.path / 'files/code.py').read_bytes() == b'answer = 43\n'
    copied = materialize_stage(scheduler.store, result.revision, result.revision.binding,
                               tmp_path / 'review-copy')
    assert copied.readonly and not (copied.source / 'code.py').stat().st_mode & 0o222
    with scheduler.store._connect() as db:
        artifacts = db.execute('SELECT * FROM artifacts').fetchall()
        assert len(artifacts) == 1 and artifacts[0]['id'] == result.artifact_id
        metadata = json.loads(artifacts[0]['metadata'])
        assert metadata['verification_pass'] is False
        assert metadata['sha256'] == result.revision.sha256
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] == before['id']
        assert len(db.execute("SELECT 1 FROM events WHERE type='artifact.created'").fetchall()) == 1
    assert scheduler.store.get_attempt(before['id']) == before


def test_cancel_before_capture_publishes_nothing(captured):
    data, _, _, root, _ = captured
    with data['scheduler'].store._tx() as db:
        db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (data['spec'].attempt_id,))
    with pytest.raises((StoreError, PublicationError)):
        capture(captured)
    assert not list(root.iterdir())


def test_cancellation_after_revision_keeps_unregistered_evidence(captured, monkeypatch):
    data, _, _, root, _ = captured
    original = candidate.capture_revision
    def cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        with data['scheduler'].store._tx() as db:
            db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (data['spec'].attempt_id,))
        return result
    monkeypatch.setattr(candidate, 'capture_revision', cancel)
    with pytest.raises((StoreError, PublicationError)):
        capture(captured)
    assert len(list(root.iterdir())) == 1 and not list(root.glob('.export-candidate-*'))
    with data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM artifacts').fetchone()
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()


def test_candidate_secret_scan_rejects_before_revision_or_artifact(captured):
    data, _, _, root, _ = captured
    with pytest.raises(RevisionError, match='secret_refused'):
        capture(captured, forbidden_values=(b'answer = 43',))
    assert not list(root.iterdir())
    with data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM artifacts').fetchone()


@pytest.mark.parametrize('case', ['forged_tree', 'foreign_cleanup', 'public_storage', 'overlap'])
def test_candidate_trust_boundaries(captured, case):
    data, collector, exported, root, _ = captured
    changes = {}
    if case == 'forged_tree':
        changes['exported'] = replace(exported, tree=replace(exported.tree,
            files=(replace(exported.tree.files[0], data=b'forged'),)))
    elif case == 'foreign_cleanup': changes['cleanup'] = object()
    elif case == 'public_storage': root.chmod(0o755)
    else:
        changes['revision_root'] = Path(data['spec'].scratch) / 'nested-revisions'
        changes['revision_root'].mkdir(mode=0o700)
    with pytest.raises((StoreError, export.WorkspaceExportError)):
        capture(captured, **changes)
    with data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM artifacts').fetchone()


def test_existing_artifact_conflict_not_overwritten(captured):
    result = capture(captured)
    store = captured[0]['scheduler'].store
    with store._tx() as db:
        metadata = json.loads(db.execute('SELECT metadata FROM artifacts').fetchone()[0])
        metadata['output_revision_sha256'] = 'f' * 64
        db.execute('UPDATE artifacts SET metadata=?', (json.dumps(metadata),))
    with pytest.raises(StoreError, match='conflict'):
        capture(captured)
    assert verify_revision(store, result.revision)


def test_candidate_event_failure_rolls_back_registration_and_retry_reuses_export(captured, monkeypatch):
    data, _, _, root, docker = captured
    store = data['scheduler'].store
    original = store._event
    def failed_event(db, session_id, attempt_id, kind, payload, **kwargs):
        if kind == 'workflow.candidate_published':
            raise OSError('fixture event failure')
        return original(db, session_id, attempt_id, kind, payload, **kwargs)
    monkeypatch.setattr(store, '_event', failed_event)
    with pytest.raises(OSError, match='event failure'):
        capture(captured)
    with store._connect() as db:
        assert not db.execute('SELECT 1 FROM artifacts').fetchone()
        assert not db.execute("SELECT 1 FROM events WHERE type='artifact.created'").fetchone()
    assert len(list(root.iterdir())) == 1
    monkeypatch.setattr(store, '_event', original)
    assert capture(captured).artifact_id
    assert sum(call[0] == 'create' for call in docker.calls) == 1


def test_original_materialization_identity_is_required(captured):
    data = captured[0]
    prepared = data['driven'][3]
    wrong = replace(prepared, materialization=replace(prepared.materialization, source_identity=(1, 2)))
    with pytest.raises(StoreError, match='original workspace'):
        capture(captured, prepared=wrong)
    assert not list(captured[3].iterdir())


def test_disk_export_reload_can_resume_candidate_without_container_relaunch(captured):
    data, collector, original, _, docker = captured
    def authorize(_action):
        authorize_result(data['scheduler'], data['spec'], collector.quiesced)
        return True
    loaded = export.load_workspace_export(data['runtime'], data['spec'], authorize=authorize)
    assert loaded == original
    result = capture(captured, exported=loaded)
    reloaded = export.load_workspace_export(data['runtime'], data['spec'], authorize=authorize)
    assert capture(captured, exported=reloaded) == result
    assert sum(call[0] == 'create' for call in docker.calls) == 1
