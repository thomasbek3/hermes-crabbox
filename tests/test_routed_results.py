from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_routed_cleanup import cleanup, driven, stage, setup, request, ready
from tests.test_routed_runtime import Docker
from cloudworkbench import routed_results as results
from cloudworkbench import routed_export as export
from cloudworkbench.routed_export_protocol import collector_program
from cloudworkbench.routed_publication import PublicationError
from cloudworkbench.routed_observation_store import ObservationStoreError


@pytest.fixture
def result_phase(cleanup, tmp_path, monkeypatch):
    request(cleanup)
    collector = ready(cleanup)
    spec = cleanup['spec']
    source = Path(spec.workspace) / 'code.py'
    source.chmod(0o600)
    source.write_bytes(b'answer = 43\n')
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
    published = tmp_path / 'published'; published.mkdir(mode=0o700)
    revisions = tmp_path / 'output-revisions'; revisions.mkdir(mode=0o700)
    args = dict(execution_profile=cleanup['wrapped'].executor.profile,
        services=(cleanup['service'],), publication_root=published,
        revision_root=revisions, selected_paths=('code.py',), forbidden_values=())
    return cleanup, replace(collector.quiesced, observation=None), args, docker


def run(value, **changes):
    data, quiesced, args, _ = value
    return results.publish_child_results(data['scheduler'], data['runtime'],
        data['driven'][3], data['spec'], quiesced, **{**args, **changes})


def test_result_phase_cleans_publishes_captures_and_resumes_without_reexecution(result_phase):
    data, _, _, docker = result_phase
    before = data['scheduler'].store.get_attempt(data['spec'].attempt_id)
    first = run(result_phase)
    second = run(result_phase)
    assert first == second and first.candidate is not None
    assert first.quiesced.observation.status == 'completed'
    assert first.verification_pass is False and first.scheduler_seat_released is False
    assert sum(args[0] == 'create' for args in docker.calls) == 1
    assert not docker.objects and not data['provider'].objects
    assert data['service'].receipt.resources_closed
    assert data['scheduler'].store.get_attempt(before['id']) == before
    assert data['scheduler'].leases.current('account')['reservation'] == data['reservation']
    with data['scheduler'].store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0] == 2
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()


def test_observation_only_phase_creates_no_export(result_phase):
    result = run(result_phase, selected_paths=None, revision_root=None)
    assert result.candidate is None and result.observation.artifact_id
    assert not any(args[0] == 'create' for args in result_phase[3].calls)


def test_cancelled_result_phase_still_drains_provider_but_does_not_publish(result_phase):
    data = result_phase[0]
    with data['scheduler'].store._tx() as db:
        db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (data['spec'].attempt_id,))
    with pytest.raises(PublicationError): run(result_phase)
    assert not data['provider'].objects and data['service'].receipt.resources_closed
    with data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM artifacts').fetchone()
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] == data['spec'].attempt_id


def test_in_memory_observation_cannot_override_saved_bytes(result_phase):
    data, quiesced, args, docker = result_phase
    changed = (data, replace(quiesced, observation=object()), args, docker)
    assert run(changed).quiesced.observation.status == 'completed'


def test_retry_after_candidate_publication_failure_reuses_export(result_phase, monkeypatch):
    original = results.capture_exported_candidate
    def fail(*args, **kwargs): raise OSError('synthetic publication failure')
    monkeypatch.setattr(results, 'capture_exported_candidate', fail)
    with pytest.raises(OSError): run(result_phase)
    monkeypatch.setattr(results, 'capture_exported_candidate', original)
    assert run(result_phase).candidate
    assert sum(args[0] == 'create' for args in result_phase[3].calls) == 1


def test_resume_refuses_changed_export_selection(result_phase):
    run(result_phase)
    with pytest.raises(results.ResultPhaseError, match='selection_changed'):
        run(result_phase, selected_paths=('different.py',))
    assert sum(args[0] == 'create' for args in result_phase[3].calls) == 1


def test_unrecoverable_export_never_relaunches_or_publishes_candidate(result_phase, monkeypatch):
    original = results.export_stopped_workspace
    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('synthetic result delivery failure')
    monkeypatch.setattr(results, 'export_stopped_workspace', failed)
    with pytest.raises(OSError): run(result_phase)
    monkeypatch.setattr(results, 'reconcile_workspace_export',
                        lambda *args, **kwargs: {'bytes_recoverable': False, 'collector_removed': True})
    with pytest.raises(results.ResultPhaseError, match='unavailable_after_cleanup'):
        run(result_phase)
    assert sum(args[0] == 'create' for args in result_phase[3].calls) == 1
    with result_phase[0]['scheduler'].store._connect() as db:
        kinds = [json.loads(row[0]).get('provenance') for row in db.execute('SELECT metadata FROM artifacts')]
        assert kinds == ['worker_reported']


def test_stricter_resume_secret_policy_refuses_even_private_snapshot(result_phase):
    # result.json always contains this exact launch digest, which the controller
    # can mark forbidden for this fixture without introducing any actual secret.
    data = result_phase[0]
    secret = data['driven'][3].launch.receipt_json
    import hashlib
    digest = hashlib.sha256(secret.encode()).hexdigest().encode()
    with pytest.raises(ObservationStoreError, match='secret_refused'):
        run(result_phase, forbidden_values=(digest,))
    assert not any(args[0] == 'create' for args in result_phase[3].calls)
    with data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM artifacts').fetchone()


def test_cancelled_retry_reconciles_running_collector_before_refusing_publication(result_phase, monkeypatch):
    data, _, _, docker = result_phase
    original = export._bounded_run
    failing = True
    def blocked(runtime, args, **kwargs):
        if args[:2] == ['start', '--attach']:
            docker.calls.append(args)
            docker.objects[args[-1]]['State'].update(Status='running', Running=True, ExitCode=0)
            raise export.WorkspaceExportError('export_docker_timeout')
        if args[0] == 'stop' and failing:
            raise export.WorkspaceExportError('export_docker_unavailable')
        return original(runtime, args, **kwargs)
    monkeypatch.setattr(export, '_bounded_run', blocked)
    with pytest.raises(export.WorkspaceExportError): run(result_phase)
    assert docker.objects
    failing = False
    with data['scheduler'].store._tx() as db:
        db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (data['spec'].attempt_id,))
    with pytest.raises(PublicationError): run(result_phase)
    assert not docker.objects
    assert sum(args[0] == 'create' for args in docker.calls) == 1
    assert sum(args[:2] == ['start', '--attach'] for args in docker.calls) == 1
    with data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] == data['spec'].attempt_id


def test_resume_cannot_drop_existing_export_requirement(result_phase):
    run(result_phase)
    with pytest.raises(results.ResultPhaseError, match='selection_changed'):
        run(result_phase, selected_paths=None)
    assert sum(args[0] == 'create' for args in result_phase[3].calls) == 1


def test_invalid_secret_policy_is_rejected_before_result_side_effects(result_phase):
    data = result_phase[0]
    with pytest.raises(ObservationStoreError, match='policy_invalid'):
        run(result_phase, forbidden_values=(b'',))
    assert data['provider'].objects
    with data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM artifacts').fetchone()
