from dataclasses import asdict, replace
import hashlib
import json
import base64
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.routed_driver import drive_prepared_child, StageDriveError
from cloudworkbench.runtime import RuntimeError
from tests.test_routed_stage import stage, prepare
from tests.test_routed_runtime import setup, RID
from tests.test_routed_collection import Reader


@pytest.fixture
def driven(stage, setup, monkeypatch):
    scheduler, owner, child, _, _, _ = stage
    prepared = prepare(stage)
    runtime, old_spec, docker = setup
    task = runtime.root.parent / 'task'
    for name, body in [('prompt.txt', prepared.launch.prompt), ('config.yaml', prepared.launch.config_json),
                       ('launch.json', json.dumps(asdict(prepared.launch)))]:
        (task / name).chmod(0o640)
        (task / name).write_text(body)
        (task / name).chmod(0o440)
    spec = runtime.prepare_caller(child['id'], child['session_id'], generation=1, plan=prepared.launch,
        workspace=prepared.materialization.source, scratch=prepared.materialization.scratch,
        task_dir=task, worker_socket_dir=old_spec.worker_socket_dir)
    result = Reader().result
    result['launch_receipt_sha256'] = hashlib.sha256(prepared.launch.receipt_json.encode()).hexdigest()
    reader = Reader(result=result)
    reader.files['relay-ready'] = json.dumps({'uid': 1001, 'pid': 10,
        'request_path': prepared.launch.request_path,
        'binding': {'attempt_id': child['id'], 'generation': 1, 'profile_digest': prepared.profile_digest}}).encode()
    command = runtime.runtime._run
    def with_file_envelopes(args, **kwargs):
        if args[0] == 'exec' and '-I' in args:
            docker.calls.append(args)
            path, offset, limit = args[-3:]
            names = {'/scratch/.cwb-observations/result.json': 'result',
                     '/scratch/.cwb-observations/events.jsonl': 'events',
                     '/run/relay/ready.json': 'relay-ready'}
            value = reader(name=names[path], offset=int(offset), max_bytes=int(limit))
            if value.get('present'):
                value = {k: v for k, v in value.items() if k != 'provenance'}
                value['data_b64'] = base64.b64encode(value.pop('data')).decode()
            return json.dumps(value)
        return command(args, **kwargs)
    monkeypatch.setattr(runtime.runtime, '_run', with_file_envelopes)
    return scheduler, owner, child, prepared, runtime, spec, docker, reader


def run(value, **kwargs):
    scheduler, _, _, prepared, runtime, spec, _, _ = value
    return drive_prepared_child(scheduler, runtime, prepared, spec,
        execution_profile=NativeProfile('openai-codex', 'gpt-6-astra', 'high'), **kwargs)


def test_one_real_scheduler_stage_binds_before_start_and_quiesces_without_acceptance(driven, monkeypatch):
    scheduler, _, child, prepared, runtime, spec, docker, _ = driven
    commands = []
    original = runtime.runtime._run
    def checked(args, **kw):
        commands.append(args[0])
        if args[0] == 'start':
            row = scheduler.read_child_launch(child['id'], expected_generation=1)
            assert row.runtime_id == RID and row.state == 'start_intent'
            assert scheduler.store.get_attempt(child['id'])['runtime_id'] == RID
        if args[0] == 'stop':
            assert scheduler.read_child_launch(child['id'], expected_generation=1).state == 'fenced'
        return original(args, **kw)
    monkeypatch.setattr(runtime.runtime, '_run', checked)
    result = run(driven)
    assert result.execution_status == 'execution_observed'
    assert result.observation.status == 'completed' and not result.verification_pass
    assert result.grant_fence_confirmed and result.caller_cleanup['caller_removed']
    assert not scheduler.store.get_attempt(child['id'])['cancel_requested']
    assert not result.provider_cleanup_qualified and not result.scheduler_seat_released
    assert not docker.objects
    with scheduler.store._connect() as db:
        root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?', (child['workflow_root_id'],)).fetchone()
        assert root['child_attempt_id'] == child['id'] and root['state'] == 'held'
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()
    assert commands.index('create') < commands.index('start') < commands.index('stop') < commands.index('rm')
    with pytest.raises(StageDriveError, match='requires_reconciliation'):
        run(driven)
    assert commands.count('create') == 1


def test_cancel_before_start_never_starts_and_cleans_stopped_caller(driven, monkeypatch):
    scheduler, owner, child, _, _, _, docker, _ = driven
    original = scheduler.begin_child_start
    def cancelled(*args, **kwargs):
        result = original(*args, **kwargs)
        scheduler.store.cancel(owner, child['id'], 'before-start')
        return result
    monkeypatch.setattr(scheduler, 'begin_child_start', cancelled)
    result = run(driven)
    assert result.execution_status == 'authority_unavailable'
    assert not any(c[0] == 'start' for c in docker.calls)
    assert result.caller_cleanup['caller_removed'] and result.grant_fence_confirmed


def test_cancel_during_readiness_prevents_hermes_launch(driven, monkeypatch):
    scheduler, owner, child, _, runtime, _, docker, reader = driven
    def read(_spec, _rid, **kw):
        scheduler.store.cancel(owner, child['id'], 'during-readiness')
        return reader(**kw)
    monkeypatch.setattr(runtime, 'read_caller_file', read)
    result = run(driven)
    assert result.execution_status == 'authority_unavailable'
    assert not any(c[0] == 'exec' and c[-1] == 'hermes' for c in docker.calls)
    assert result.caller_cleanup['caller_removed']


def test_grants_fenced_before_caller_stop(driven, monkeypatch):
    scheduler, _, child, _, runtime, _, _, _ = driven
    reservation = scheduler.leases.current('account')['reservation']
    grant = scheduler.leases.issue_grant(reservation, attempt_id=child['id'], generation=1)
    original = runtime.stop_caller
    def stopped(*args):
        with scheduler.store._connect() as db:
            row = db.execute('SELECT revoked_at FROM provider_execution_grants WHERE id=?', (grant,)).fetchone()
            assert row['revoked_at'] is not None
        return original(*args)
    monkeypatch.setattr(runtime, 'stop_caller', stopped)
    assert run(driven).grant_fence_confirmed


def test_missing_result_times_out_and_preserves_occupancy(driven):
    reader = driven[-1]
    reader.files.pop('result')
    now = [0.]
    def advance(seconds): now[0] += seconds
    result = run(driven, timeout_seconds=1, clock=lambda: now[0], sleep=advance)
    assert result.execution_status == 'worker_output_missing' and result.observation is None
    assert result.caller_cleanup['caller_removed'] and not result.scheduler_seat_released


def test_malformed_result_does_not_become_completion(driven):
    driven[-1].files['result'] = b'{"verification_pass":true}'
    result = run(driven)
    assert result.execution_status == 'execution_unconfirmed' and result.observation is None
    assert result.caller_cleanup['caller_removed']


def test_lost_create_response_never_retried_or_called_absence(driven):
    driven[-2].fail_create = True
    with pytest.raises(StageDriveError, match='create_unresolved'): run(driven)
    with pytest.raises(StageDriveError, match='requires_reconciliation'): run(driven)
    assert sum(c[0] == 'create' for c in driven[-2].calls) == 1
    assert RID in driven[-2].objects
    assert not any(c[0] == 'start' for c in driven[-2].calls)


def test_lost_exec_response_is_not_retried_and_exact_caller_removed(driven):
    driven[-2].fail_exec = True
    result = run(driven)
    assert result.execution_status == 'execution_unconfirmed'
    assert sum(c[0] == 'exec' for c in driven[-2].calls) == 1
    assert result.caller_cleanup['caller_removed'] and not driven[-2].objects


def test_cleanup_failure_suppresses_observation(driven, monkeypatch):
    def fail(*args): raise RuntimeError('synthetic cleanup error')
    monkeypatch.setattr(driven[4], 'remove_caller', fail)
    result = run(driven)
    assert result.cleanup_error == 'caller_cleanup_unconfirmed' and result.observation is None
    assert not result.scheduler_seat_released


def test_revocation_after_last_collection_read_discards_observation(driven, monkeypatch):
    scheduler, owner, child, _, runtime, _, _, reader = driven
    count = [0]
    def read(_spec, _rid, **kw):
        value = reader(**kw)
        if kw['name'] == 'events':
            count[0] += 1
            if count[0] == 2:
                scheduler.store.cancel(owner, child['id'], 'after-collection')
        return value
    monkeypatch.setattr(runtime, 'read_caller_file', read)
    result = run(driven)
    assert result.execution_status == 'authority_unavailable' and result.observation is None
    assert result.caller_cleanup['caller_removed']


def test_lost_start_ack_stops_exact_caller_and_never_replays(driven, monkeypatch):
    runtime, docker = driven[4], driven[-2]
    original = runtime.runtime._run
    def lost(args, **kw):
        value = original(args, **kw)
        if args[0] == 'start': raise RuntimeError('lost start ack')
        return value
    monkeypatch.setattr(runtime.runtime, '_run', lost)
    result = run(driven)
    assert result.execution_status == 'execution_unconfirmed'
    assert result.caller_cleanup['caller_removed']
    with pytest.raises(StageDriveError, match='requires_reconciliation'): run(driven)
    assert sum(c[0] == 'start' for c in docker.calls) == 1


def test_failed_grant_fence_still_stops_caller_but_never_returns_observation(driven, monkeypatch):
    def fail(*args, **kwargs): raise RuntimeError('lost database')
    monkeypatch.setattr(driven[0], 'fence_child_execution', fail)
    result = run(driven)
    assert result.caller_cleanup['caller_removed'] and result.observation is None
    assert result.cleanup_error == 'grant_fence_unconfirmed'
    assert not result.grant_fence_confirmed and not result.provider_cleanup_qualified


@pytest.mark.parametrize('mutate', [
    lambda v: v.update(uid=1000),
    lambda v: v['binding'].update(generation=True),
    lambda v: v['binding'].update(profile_digest='f'*64),
    lambda v: v.update(request_path='/v1/chat/completions'),
])
def test_wrong_relay_identity_never_launches_hermes(driven, mutate):
    reader, docker = driven[-1], driven[-2]
    value = json.loads(reader.files['relay-ready'])
    mutate(value)
    reader.files['relay-ready'] = json.dumps(value).encode()
    result = run(driven)
    assert result.execution_status == 'invalid_relay_ready'
    assert not any(c[0] == 'exec' and c[-1] == 'hermes' for c in docker.calls)
    assert result.caller_cleanup['caller_removed']


def test_spec_revision_path_mismatch_rejected_before_rpc(driven):
    changed = list(driven)
    changed[5] = replace(driven[5], workspace=driven[5].scratch)
    with pytest.raises(StageDriveError, match='binding_changed'): run(changed)
    assert not driven[-2].calls


def test_changed_materialized_input_rejected_before_rpc(driven):
    source = driven[3].materialization.source / 'code.py'
    source.chmod(0o660)
    source.write_text('changed input\n')
    with pytest.raises(StageDriveError, match='materialization_changed'): run(driven)
    assert not driven[-2].calls


def test_input_changed_during_relay_readiness_never_launches_hermes(driven, monkeypatch):
    runtime, reader, prepared = driven[4], driven[-1], driven[3]
    def read(_spec, _rid, **kwargs):
        source = prepared.materialization.source / 'code.py'
        source.chmod(0o660)
        source.write_text('modified while stopped\n')
        return reader(**kwargs)
    monkeypatch.setattr(runtime, 'read_caller_file', read)
    result = run(driven)
    assert result.execution_status == 'materialization_changed'
    assert not any(c[0] == 'exec' and c[-1] == 'hermes' for c in driven[-2].calls)
    assert result.caller_cleanup['caller_removed']


def test_direct_runtime_stop_fences_later_driver_launch(driven, monkeypatch):
    runtime, reader = driven[4], driven[-1]
    def stop_after_ready(spec, rid, **kwargs):
        value = reader(**kwargs)
        runtime.stop_caller(spec, rid)
        return value
    monkeypatch.setattr(runtime, 'read_caller_file', stop_after_ready)
    result = run(driven)
    assert result.execution_status == 'execution_unconfirmed' and result.observation is None
    assert not any(c[0] == 'exec' and c[-1] == 'hermes' for c in driven[-2].calls)
    assert result.caller_cleanup['caller_removed']


def test_silent_worker_wait_bound_to_actual_launch_budget(driven):
    reader = driven[-1]
    reader.files.pop('result')
    reader.files.pop('events')
    now = [0.]
    # Fast-forward idle time; SQLite contention has separate real-clock tests.
    def advance(seconds): now[0] += 10 * seconds
    result = run(driven, clock=lambda: now[0], sleep=advance, poll_seconds=1)
    assert result.execution_status == 'worker_output_missing'
    assert now[0] == 330  # Actual frozen300-second plan plus30-second output grace.
    assert result.caller_cleanup['caller_removed']


def test_parallel_driver_cannot_stop_active_driver(driven, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = driven[4].create_caller
    def wait(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(driven[4], 'create_caller', wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(run, driven)
        assert entered.wait(5)
        try:
            with pytest.raises(StageDriveError, match='driver_busy'): run(driven)
            assert not driven[-2].calls
        finally:
            release.set()
        assert active.result().caller_cleanup['caller_removed']


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf'), 5000])
def test_invalid_deadline_before_rpc(driven, value):
    with pytest.raises(StageDriveError, match='deadline'): run(driven, timeout_seconds=value)
    assert not driven[-2].calls
