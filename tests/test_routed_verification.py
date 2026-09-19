"""Real controller composition with per-check runtime observations stubbed; no Docker proof."""
from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_routed_results import result_phase, run as publish_results
from tests.test_routed_cleanup import cleanup, driven, stage, setup
from cloudworkbench import routed_verification as verification
from cloudworkbench.routed_verifier_runtime import VerifierRuntime, CheckResult, VerifierRuntimeError
from cloudworkbench.routed_publication import PublicationError
from cloudworkbench.workflow_revisions import RevisionError, verify_revision


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def composed(result_phase, tmp_path, monkeypatch):
    data = result_phase[0]
    results = publish_results(result_phase)
    script = b'print("protected check")\n'
    environment = {'project_id': 'demo', 'version': 'v1', 'architecture': 'amd64',
        'base_image_digest': 'sha256:' + 'a' * 64, 'image_digest': data['runtime'].runtime.image,
        'cli_versions': {'python': '3'}, 'readiness_probes': [{'id': 'python', 'argv': ['python3', '--version']}],
        'checks': [{'id': 'check', 'description': 'Expected behavior',
            'argv': ['/usr/bin/python3', '/run/task/check.py'], 'script_id': 'protected',
            'script_name': 'check.py', 'script_sha256': digest(script)}]}
    storage = tmp_path / 'verification-storage'; storage.mkdir(mode=0o700)
    material = tmp_path / 'verification-material'; material.mkdir(mode=0o700)
    verifier = VerifierRuntime(data['runtime'].runtime, journal_root=tmp_path / 'verifier-journal')
    state = SimpleNamespace(data=data, results=results, environment=environment, scripts={'protected': script},
        storage=storage, material=material, verifier=verifier, calls=[], launched=[], saved={},
        before_run=None, before_load=None, after_run=None, exit_code=0, logs=b'check passed\n')

    def run(spec, *, authorize, forbidden_values=()):
        state.calls.append(('run', spec.digest))
        if state.before_run: state.before_run(spec)
        authorize('prepare')
        authorize('start')
        if spec.digest not in state.saved:
            state.launched.append(spec.digest)
            state.saved[spec.digest] = CheckResult(spec.digest, digest(spec.digest.encode()),
                state.exit_code, False, False, True, digest(state.logs), state.logs)
        if state.after_run: state.after_run(spec)
        authorize('publish')
        if any(secret in state.saved[spec.digest].logs for secret in forbidden_values):
            raise VerifierRuntimeError('verifier_logs_rejected')
        return state.saved[spec.digest]

    def load(spec, *, authorize, forbidden_values=()):
        state.calls.append(('load', spec.digest))
        if state.before_load: state.before_load(spec)
        authorize('load')
        value = state.saved[spec.digest]
        if any(secret in value.logs for secret in forbidden_values):
            raise VerifierRuntimeError('verifier_logs_rejected')
        return value

    def reconcile(spec, *, authorize, forbidden_values=()):
        state.calls.append(('reconcile', spec.digest))
        for action in ('reconcile', 'cleanup_inspect', 'stop', 'remove'): authorize(action)
        return {'spec_sha256': spec.digest, 'runtime_id': digest(spec.digest.encode()),
            'cleanup_confirmed': True, 'result_available': spec.digest in state.saved}

    monkeypatch.setattr(verifier, 'run', run)
    monkeypatch.setattr(verifier, 'load', load)
    monkeypatch.setattr(verifier, 'reconcile', reconcile)
    return state


def prepare(value, **changes):
    data = value.data
    args = dict(services=(data['service'],), environment=value.environment, scripts=value.scripts,
        storage_root=value.storage, material_root=value.material, forbidden_values=())
    args.update(changes)
    return verification.prepare_verification(data['scheduler'], data['runtime'], data['spec'], value.results, **args)


def execute(value, prepared, **changes):
    data = value.data
    args = dict(services=(data['service'],), forbidden_values=())
    args.update(changes)
    return verification.run_verification(data['scheduler'], data['runtime'], data['spec'],
        value.results, prepared, value.verifier, **args)


def acceptance(value, criteria):
    scheduler = value.data['scheduler']
    with scheduler.store._tx() as db:
        row = db.execute('SELECT id,request FROM turns WHERE id=?',
            (scheduler.store._attempt(db, value.data['spec'].attempt_id)['turn_id'],)).fetchone()
        request = json.loads(row['request']); request['acceptance'] = criteria
        db.execute('UPDATE turns SET request=? WHERE id=?', (json.dumps(request), row['id']))


def cancel(value):
    with value.data['scheduler'].store._tx() as db:
        db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (value.data['spec'].attempt_id,))


def protected_artifacts(value):
    with value.data['scheduler'].store._connect() as db:
        return [json.loads(r['metadata']) for r in db.execute("SELECT metadata FROM artifacts WHERE id LIKE 'verification-%'")]


def test_protected_pass_binds_exact_candidate_and_replays_without_gate_or_release(composed):
    value = composed; data = value.data
    before = data['scheduler'].store.get_attempt(data['spec'].attempt_id)
    candidate_bytes = (value.results.candidate.revision.path / 'files/code.py').read_bytes()
    prepared = prepare(value)
    assert prepared.materialization.readonly and prepared.materialization.source != Path(data['spec'].workspace)
    assert (prepared.materialization.source / 'code.py').read_bytes() == candidate_bytes
    assert prepare(value) == prepared
    first = execute(value, prepared); second = execute(value, prepared)
    assert first == second and first.outcome == 'passed' and first.remaining_criteria == ()
    assert len(value.launched) == 1
    assert (value.results.candidate.revision.path / 'files/code.py').read_bytes() == candidate_bytes
    verify_revision(data['scheduler'].store, value.results.candidate.revision)
    artifacts = protected_artifacts(value)
    assert len(artifacts) == 1
    metadata = artifacts[0]; raw = Path(metadata['storage_path']).read_bytes()
    assert digest(raw) == first.receipt_sha256 == metadata['sha256'] and len(raw) == metadata['bytes']
    assert metadata['verified_revision_sha256'] == value.results.candidate.revision.sha256
    assert metadata['input_revision_sha256'] == value.results.candidate.input_revision_sha256
    assert metadata['provenance'] == 'controller_protected_checks' and metadata['protected_checks_passed'] is True
    assert data['scheduler'].store.get_attempt(before['id']) == before
    assert data['scheduler'].leases.current('account')['reservation'] == data['reservation']
    with data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] == before['id']
        assert db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.protected_verification'").fetchone()[0] == 1


@pytest.mark.parametrize('criterion', [
    {'id': 'other', 'description': 'Expected behavior', 'mandatory': True},
    {'id': 'check', 'description': 'Different behavior', 'mandatory': True},
])
def test_authoritative_acceptance_requires_matching_id_and_description(composed, criterion):
    acceptance(composed, [criterion])
    prepared = prepare(composed)
    assert prepared.plan.acceptance[0].description == criterion['description']
    result = execute(composed, prepared)
    assert result.outcome == 'needs_review' and result.remaining_criteria == (criterion['id'],)
    assert protected_artifacts(composed)[0]['protected_checks_passed'] is False


def test_matching_mandatory_acceptance_can_pass(composed):
    acceptance(composed, [{'id': 'check', 'description': 'Expected behavior', 'mandatory': True}])
    assert execute(composed, prepare(composed)).outcome == 'passed'


@pytest.mark.parametrize('mutation', ['unregistered', 'metadata', 'candidate_bytes'])
def test_candidate_registration_or_content_drift_refused_before_execution(composed, mutation):
    candidate = composed.results.candidate
    if mutation == 'candidate_bytes':
        path = candidate.revision.path / 'files/code.py'; path.chmod(0o600); path.write_bytes(b'changed\n')
    else:
        with composed.data['scheduler'].store._tx() as db:
            if mutation == 'unregistered': db.execute('DELETE FROM artifacts WHERE id=?', (candidate.artifact_id,))
            else:
                raw = json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?', (candidate.artifact_id,)).fetchone()[0])
                raw['output_revision_sha256'] = 'f' * 64
                db.execute('UPDATE artifacts SET metadata=? WHERE id=?', (json.dumps(raw), candidate.artifact_id))
    with pytest.raises((verification.VerificationPhaseError, RevisionError)): prepare(composed)
    assert not composed.calls and not protected_artifacts(composed)


def test_protected_script_drift_is_refused_before_launch(composed):
    prepared = prepare(composed)
    path = prepared.tasks[0][1] / 'check.py'; path.chmod(0o750); path.write_bytes(b'print("changed")\n'); path.chmod(0o550)
    with pytest.raises(verification.VerificationPhaseError, match='script_changed'): execute(composed, prepared)
    assert not composed.launched and not protected_artifacts(composed)


def test_prepare_retry_refuses_changed_environment_plan(composed):
    prepared = prepare(composed)
    changed = copy.deepcopy(composed.environment); changed['checks'][0]['description'] = 'Changed requirement'
    with pytest.raises(verification.VerificationPhaseError, match='plan_changed'): prepare(composed, environment=changed)
    assert prepare(composed) == prepared and not composed.launched


def test_stronger_secret_policy_refuses_script_before_new_launch(composed):
    prepared = prepare(composed)
    with pytest.raises(verification.VerificationPhaseError, match='known_secret_refused'):
        execute(composed, prepared, forbidden_values=(b'protected check',))
    assert not composed.launched and not protected_artifacts(composed)


def test_stronger_secret_policy_rechecks_saved_runtime_logs(composed):
    prepared = prepare(composed); first = execute(composed, prepared)
    with pytest.raises(VerifierRuntimeError, match='logs_rejected'):
        execute(composed, prepared, forbidden_values=(b'check passed',))
    assert len(composed.launched) == 1 and protected_artifacts(composed)[0]['id'] == first.artifact_id


def test_cancellation_after_check_before_publication_never_publishes(composed):
    prepared = prepare(composed)
    composed.after_run = lambda _: cancel(composed)
    with pytest.raises(PublicationError): execute(composed, prepared)
    assert len(composed.launched) == 1 and not protected_artifacts(composed)


def test_empty_check_set_needs_review_without_runtime_execution(composed):
    composed.environment['checks'] = []; composed.scripts = {}
    result = execute(composed, prepare(composed))
    assert result.outcome == 'needs_review' and not composed.launched
    assert not composed.calls


def test_authoritative_acceptance_change_after_prepare_refused(composed):
    prepared = prepare(composed)
    acceptance(composed, [{'id': 'check', 'description': 'new requirement', 'mandatory': True}])
    with pytest.raises(verification.VerificationPhaseError, match='acceptance_changed'): execute(composed, prepared)
    assert not composed.launched and not protected_artifacts(composed)


def test_runtime_replay_mismatch_never_registers_protected_artifact(composed):
    prepared = prepare(composed)
    def changed(spec):
        composed.saved[spec.digest] = replace(composed.saved[spec.digest], logs_sha256='f' * 64)
    composed.before_load = changed
    with pytest.raises(verification.VerificationPhaseError, match='runtime_receipt_changed'): execute(composed, prepared)
    assert not protected_artifacts(composed)


def test_nonzero_exit_is_rejected_not_success(composed):
    composed.exit_code = 1
    assert execute(composed, prepare(composed)).outcome == 'rejected'
    assert protected_artifacts(composed)[0]['protected_checks_passed'] is False


def test_cancelled_retry_reconciles_before_denied_result_authority(composed):
    prepared = prepare(composed)
    execute(composed, prepared)
    prior = copy.deepcopy(protected_artifacts(composed))
    composed.calls.clear(); cancel(composed)
    with pytest.raises(PublicationError): execute(composed, prepared)
    assert [kind for kind, _ in composed.calls] == ['reconcile']
    assert len(composed.launched) == 1 and protected_artifacts(composed) == prior
    with composed.data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] == composed.data['spec'].attempt_id


def test_explicit_cleanup_after_cancel_is_permitted_without_new_execution(composed):
    prepared = prepare(composed); cancel(composed)
    value = verification.reconcile_verification(composed.data['scheduler'], composed.data['runtime'],
        composed.data['spec'], composed.results, prepared, composed.verifier,
        services=(composed.data['service'],), forbidden_values=())
    assert len(value) == 1 and value[0]['cleanup_confirmed'] is True
    assert [kind for kind, _ in composed.calls] == ['reconcile']
    assert not composed.launched and not protected_artifacts(composed)


def test_reconciliation_failure_blocks_new_execution_and_publication(composed, monkeypatch):
    prepared = prepare(composed)
    def failed(spec, *, authorize, forbidden_values=()):
        authorize('cleanup_inspect')
        raise VerifierRuntimeError('verifier_stop_unconfirmed')
    monkeypatch.setattr(composed.verifier, 'reconcile', failed)
    with pytest.raises(VerifierRuntimeError, match='stop_unconfirmed'): execute(composed, prepared)
    assert not composed.launched and not protected_artifacts(composed)


def test_script_changed_between_preflight_and_start_is_refused(composed):
    prepared = prepare(composed)
    def mutate(_):
        path = prepared.tasks[0][1] / 'check.py'; path.chmod(0o750)
        path.write_bytes(b'print("race")\n'); path.chmod(0o550)
    composed.before_run = mutate
    with pytest.raises(verification.VerificationPhaseError, match='script_changed'): execute(composed, prepared)
    assert not composed.launched and not protected_artifacts(composed)


def test_receipt_contains_frozen_plan_and_controller_assessment(composed):
    prepared = prepare(composed); result = execute(composed, prepared)
    raw = Path(protected_artifacts(composed)[0]['storage_path']).read_bytes()
    body = json.loads(raw)
    assert set(body) == {'schema_version', 'plan', 'assessment'} and body['schema_version'] == 1
    assert body['plan'] == prepared.plan.to_dict()
    assert body['assessment']['outcome'] == result.outcome == 'passed'
    assert body['assessment']['plan_sha256'] == prepared.plan.digest
    assert body['assessment']['records'][0]['check_id'] == 'check'
    assert body['assessment']['records'][0]['exit_code'] == 0
    assert body['assessment']['records'][0]['cleanup_confirmed'] is True
    assert digest(raw) == result.receipt_sha256
    assert b'protected check' not in raw


@pytest.mark.parametrize('crash_point', ['before_materialization', 'after_materialization', 'after_checks'])
def test_partial_preparation_retry_recovers_exact_published_components(composed, monkeypatch, crash_point):
    materialize = verification.materialize_stage
    publish = verification._publish_exclusive
    published_material = []
    def interrupted_materialize(*args, **kwargs):
        if crash_point == 'before_materialization': raise OSError('synthetic preparation interruption')
        value = materialize(*args, **kwargs); published_material.append(value)
        if crash_point == 'after_materialization': raise OSError('synthetic preparation interruption')
        return value
    def interrupted_publish(source, destination):
        value = publish(source, destination)
        if crash_point == 'after_checks' and Path(destination).name.endswith('-checks'):
            raise OSError('synthetic preparation interruption')
        return value
    monkeypatch.setattr(verification, 'materialize_stage', interrupted_materialize)
    monkeypatch.setattr(verification, '_publish_exclusive', interrupted_publish)
    with pytest.raises(OSError, match='synthetic preparation interruption'): prepare(composed)
    assert not composed.launched and not protected_artifacts(composed)
    identities = {str(path): (path.stat().st_dev, path.stat().st_ino)
        for path in composed.material.iterdir() if path.name.startswith('verification-')}
    monkeypatch.setattr(verification, 'materialize_stage', materialize)
    monkeypatch.setattr(verification, '_publish_exclusive', publish)
    prepared = prepare(composed)
    for name, identity in identities.items():
        assert (Path(name).stat().st_dev, Path(name).stat().st_ino) == identity
    if published_material:
        assert prepared.materialization == published_material[0]
    assert prepare(composed) == prepared
    assert execute(composed, prepared).outcome == 'passed'
    assert len(composed.launched) == 1


def test_partial_preparation_refuses_modified_published_material(composed, monkeypatch):
    materialize = verification.materialize_stage
    captured = []
    def interrupted(*args, **kwargs):
        value = materialize(*args, **kwargs); captured.append(value)
        raise OSError('synthetic preparation interruption')
    monkeypatch.setattr(verification, 'materialize_stage', interrupted)
    with pytest.raises(OSError): prepare(composed)
    monkeypatch.setattr(verification, 'materialize_stage', materialize)
    path = captured[0].source / 'code.py'; path.chmod(0o600); path.write_bytes(b'foreign replacement\n')
    with pytest.raises((verification.VerificationPhaseError, RevisionError)): prepare(composed)
    assert path.read_bytes() == b'foreign replacement\n'
    assert not composed.launched and not protected_artifacts(composed)


def test_protected_artifact_public_event_excludes_private_storage_path(composed):
    result = execute(composed, prepare(composed))
    metadata = protected_artifacts(composed)[0]
    with composed.data['scheduler'].store._connect() as db:
        events = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE type='artifact.created' AND json_extract(payload,'$.id')=?",
            (result.artifact_id,))]
    assert len(events) == 1 and events[0]['sha256'] == result.receipt_sha256
    assert Path(metadata['storage_path']).is_file()
    assert 'storage_path' not in events[0]
    assert str(composed.storage) not in json.dumps(events)


def test_cancelled_reconciliation_allows_log_collection_but_not_new_launch(composed, monkeypatch):
    prepared = prepare(composed); cancel(composed)
    observed = []
    def reconcile(spec, *, authorize, forbidden_values=()):
        for action in ('reconcile', 'cleanup_inspect', 'cleanup_logs', 'stop', 'remove'):
            assert authorize(action) is True; observed.append(action)
        for action in ('prepare', 'create', 'start', 'load'):
            with pytest.raises(verification.VerificationPhaseError, match='cleanup_only'): authorize(action)
        return {'cleanup_confirmed': True, 'result_available': False, 'aborted': True}
    monkeypatch.setattr(composed.verifier, 'reconcile', reconcile)
    with pytest.raises(PublicationError): execute(composed, prepared)
    assert observed == ['reconcile', 'cleanup_inspect', 'cleanup_logs', 'stop', 'remove']
    assert not composed.launched and not protected_artifacts(composed)


@pytest.fixture
def phase_clock(monkeypatch):
    value = SimpleNamespace(wall=10000.0, monotonic=100.0)
    monkeypatch.setattr(verification, '_wall_time', lambda: value.wall, raising=False)
    monkeypatch.setattr(verification, '_monotonic', lambda: value.monotonic, raising=False)
    return value


def test_total_phase_budget_is_not_reset_by_retry(composed, monkeypatch, phase_clock):
    prepared = prepare(composed, limits=verification.VerificationLimits(max_seconds=10))
    def interrupted(_): raise OSError('synthetic executor interruption')
    composed.before_run = interrupted
    with pytest.raises(OSError, match='executor interruption'): execute(composed, prepared)
    composed.before_run = None; composed.calls.clear()
    phase_clock.wall += 11; phase_clock.monotonic += 11
    with pytest.raises(verification.VerificationPhaseError, match='budget|deadline'): execute(composed, prepared)
    assert [kind for kind, _ in composed.calls] == ['reconcile']
    assert not composed.launched and not protected_artifacts(composed)
    phase_clock.wall += 1
    with pytest.raises(verification.VerificationPhaseError, match='budget|deadline'): execute(composed, prepared)
    assert not composed.launched


def test_phase_budget_allows_retry_only_within_original_deadline(composed, phase_clock):
    prepared = prepare(composed, limits=verification.VerificationLimits(max_seconds=10))
    def interrupted(_): raise OSError('synthetic executor interruption')
    composed.before_run = interrupted
    with pytest.raises(OSError): execute(composed, prepared)
    composed.before_run = None
    phase_clock.wall += 4; phase_clock.monotonic += 4
    assert execute(composed, prepared).outcome == 'passed'
    assert len(composed.launched) == 1


def test_wall_clock_rollback_blocks_pending_verification_durably(composed, phase_clock):
    prepared = prepare(composed, limits=verification.VerificationLimits(max_seconds=10))
    def interrupted(_): raise OSError('synthetic executor interruption')
    composed.before_run = interrupted
    with pytest.raises(OSError): execute(composed, prepared)
    composed.before_run = None; phase_clock.wall -= 1
    with pytest.raises(verification.VerificationPhaseError, match='clock|budget'): execute(composed, prepared)
    phase_clock.wall += 2
    with pytest.raises(verification.VerificationPhaseError, match='clock|budget'): execute(composed, prepared)
    assert not composed.launched and not protected_artifacts(composed)


def test_monotonic_budget_prevents_success_if_wall_clock_stalls(composed, phase_clock):
    prepared = prepare(composed, limits=verification.VerificationLimits(max_seconds=10))
    def elapsed(_): phase_clock.monotonic += 11
    composed.after_run = elapsed
    with pytest.raises(verification.VerificationPhaseError, match='budget|deadline'): execute(composed, prepared)
    assert len(composed.launched) == 1 and not protected_artifacts(composed)
    composed.after_run = None; composed.calls.clear()
    with pytest.raises(verification.VerificationPhaseError, match='budget|deadline'): execute(composed, prepared)
    assert [kind for kind, _ in composed.calls] == ['reconcile']
    assert len(composed.launched) == 1 and not protected_artifacts(composed)


def test_completed_receipt_replays_after_phase_expiry_by_load_only(composed, phase_clock):
    prepared = prepare(composed, limits=verification.VerificationLimits(max_seconds=10))
    first = execute(composed, prepared); before = protected_artifacts(composed)
    composed.calls.clear(); phase_clock.wall += 100; phase_clock.monotonic += 100
    second = execute(composed, prepared)
    assert second == first and protected_artifacts(composed) == before
    assert [kind for kind, _ in composed.calls] == ['reconcile', 'load']
    assert len(composed.launched) == 1
    with composed.data['scheduler'].store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.protected_verification'").fetchone()[0] == 1


def test_partial_preparation_refuses_identical_bytes_with_replaced_source_inode(composed, monkeypatch):
    import shutil
    materialize = verification.materialize_stage; captured = []
    def interrupted(*args, **kwargs):
        value = materialize(*args, **kwargs); captured.append(value)
        raise OSError('synthetic preparation interruption')
    monkeypatch.setattr(verification, 'materialize_stage', interrupted)
    with pytest.raises(OSError): prepare(composed)
    monkeypatch.setattr(verification, 'materialize_stage', materialize)
    source = captured[0].source; moved = source.with_name('original-preserved')
    source.rename(moved); shutil.copytree(moved, source)
    assert (source / 'code.py').read_bytes() == (moved / 'code.py').read_bytes()
    assert (source.stat().st_dev, source.stat().st_ino) != captured[0].source_identity
    with pytest.raises((verification.VerificationPhaseError, RevisionError)): prepare(composed)
    assert source.exists() and moved.exists() and not composed.launched
    assert not protected_artifacts(composed)


def test_missing_pinned_stage_after_publish_error_remains_explicitly_unresolved(composed, monkeypatch):
    from cloudworkbench import workflow_revisions
    publish = workflow_revisions._publish
    def failed_publish(source, destination):
        raise OSError('synthetic rename failure')
    monkeypatch.setattr(workflow_revisions, '_publish', failed_publish)
    with pytest.raises(RevisionError, match='revision_filesystem_error'): prepare(composed)
    intent, = composed.storage.glob('verification-*/material-intent.json')
    original = intent.read_bytes()
    assert not list(composed.material.glob('.materialize-*'))
    monkeypatch.setattr(workflow_revisions, '_publish', publish)
    with pytest.raises(verification.VerificationPhaseError, match='material_publication_unresolved'):
        prepare(composed)
    assert intent.read_bytes() == original
    assert not composed.launched and not protected_artifacts(composed)


@pytest.mark.parametrize('unsupported_gid', [True, False, 999, 1001, 0, -1, '1000', 1000.0])
def test_unsupported_verifier_group_refused_before_preparation_writes(composed, unsupported_gid):
    before = composed.data['scheduler'].store.get_attempt(composed.data['spec'].attempt_id)
    candidate_bytes = (composed.results.candidate.revision.path / 'files/code.py').read_bytes()
    assert list(composed.storage.iterdir()) == [] and list(composed.material.iterdir()) == []
    with pytest.raises(verification.VerificationPhaseError, match='verification_tool_identity_unsupported'):
        prepare(composed, required_tool_gid=unsupported_gid)
    assert list(composed.storage.iterdir()) == [] and list(composed.material.iterdir()) == []
    assert (composed.results.candidate.revision.path / 'files/code.py').read_bytes() == candidate_bytes
    assert composed.data['scheduler'].store.get_attempt(before['id']) == before
    assert not composed.calls and not composed.launched and not protected_artifacts(composed)


@pytest.mark.parametrize('replay', [False, True])
@pytest.mark.parametrize('action', ['unknown', 'cleanup_log', '', None, {}, ['start']])
def test_unknown_runtime_action_refused_before_budget_or_result_authority(composed, monkeypatch, replay, action):
    prepared = prepare(composed)
    if replay: execute(composed, prepared)
    previous = copy.deepcopy(protected_artifacts(composed)); launches = list(composed.launched)
    boundary_calls = []
    original_authority = verification.authorize_result
    original_check = verification._PhaseBudget.check
    entered = False
    def authority(*args, **kwargs):
        if entered: boundary_calls.append('result_authority')
        return original_authority(*args, **kwargs)
    def check(value):
        if entered: boundary_calls.append('budget')
        return original_check(value)
    def invalid_action(spec, *, authorize, forbidden_values=()):
        nonlocal entered
        entered = True
        try: authorize(action)
        finally: entered = False
        raise AssertionError('unknown action was accepted')
    monkeypatch.setattr(verification, 'authorize_result', authority)
    monkeypatch.setattr(verification._PhaseBudget, 'check', check)
    monkeypatch.setattr(composed.verifier, 'load' if replay else 'run', invalid_action)
    with pytest.raises(verification.VerificationPhaseError, match='verification_action_unsupported'):
        execute(composed, prepared)
    assert boundary_calls == []
    assert composed.launched == launches and protected_artifacts(composed) == previous
