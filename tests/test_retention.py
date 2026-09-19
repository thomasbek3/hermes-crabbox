from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from cloudworkbench.api import create_app
from cloudworkbench.store import Store, StoreError
from cloudworkbench.retention import RetentionError, get_policy, plan_session, require_matching_plan, set_policy

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / 'state.sqlite')
    owner = store.add_client('owner', 'x'*40, ['observe', 'submit', 'cancel', 'retrieve'], ['p'])
    task = store.create_session(owner, {'project_id': 'p', 'goal': 'retention fixture', 'agent': 'fixture', 'environment_version': 'test'}, 'create')
    store.cancel(owner, task['attempt_id'], 'cancel')
    with store._tx() as db:
        for table in ('sessions', 'turns', 'attempts', 'events'):
            db.execute(f'UPDATE {table} SET created_at=?', (BASE.isoformat(),))
        db.execute('UPDATE attempts SET updated_at=?', (BASE.isoformat(),))
    return store, owner, task


def plan(state, *, days=31, **kwargs):
    store, owner, task = state
    with store._connect() as db:
        db.execute('BEGIN')
        return plan_session(db, task['session_id'], as_of=BASE+timedelta(days=days), **kwargs)


def test_default_deadline_exact_boundary_and_no_native_secret_default(state):
    before = plan(state, days=29)
    due = plan(state, days=30)
    assert before['retention_deadline'] == '2026-01-31T00:00:00+00:00'
    assert not before['eligible_for_review'] and not before['deletion_manifest']
    assert due['expired'] and due['eligible_for_review'] and due['deletion_manifest']
    assert due['dry_run'] and not due['executable'] and not due['scheduled_cleanup']
    assert due['native_state']['deadline'] is None
    assert due['secret_state']['configured_max_age_hours'] is None
    assert not due['native_state']['included_in_session_deletion_manifest']
    assert not due['secret_state']['inventory_performed']
    assert not due['shared_inputs']['included_in_deletion_manifest']


@pytest.mark.parametrize('status', ['queued', 'running', 'held', 'verifying', 'waiting_input', 'paused', 'unrecognized'])
def test_live_queued_unknown_states_never_enter_manifest(state, status):
    store, owner, task = state
    with store._tx() as db:
        db.execute('UPDATE attempts SET state=?', (status,))
    result = plan(state)
    assert result['expired'] and not result['eligible_for_review']
    assert result['deletion_manifest'] == []
    assert result['protected_attempts'][0]['state'] == status


def test_keep_cas_explicit_deadline_and_idempotency(state):
    store, owner, task = state
    response = store.set_retention(owner, task['session_id'], {'keep': True, 'deadline': BASE.isoformat()}, 0, 'keep')
    assert response['revision'] == 1
    assert store.set_retention(owner, task['session_id'], {'keep': True, 'deadline': BASE.isoformat()}, 0, 'keep') == response
    with pytest.raises(StoreError) as conflict:
        store.set_retention(owner, task['session_id'], {'keep': False}, 0, 'stale')
    assert conflict.value.status_code == 409
    result = plan(state)
    assert result['expired'] and 'keep' in result['protected_reasons']
    assert not result['deletion_manifest']
    with pytest.raises(StoreError) as replay:
        store.set_retention(owner, task['session_id'], {'keep': False}, 1, 'keep')
    assert replay.value.status_code == 409


def test_latest_activity_and_snapshot_guard(state):
    previous = plan(state)
    store, owner, task = state
    with store._tx() as db:
        db.execute('UPDATE events SET created_at=? WHERE sequence=1', ((BASE+timedelta(days=10)).isoformat(),))
    current = plan(state)
    assert current['retention_deadline'] == (BASE+timedelta(days=40)).isoformat()
    assert not current['eligible_for_review']
    with pytest.raises(RetentionError, match='snapshot changed'):
        require_matching_plan(previous, current)
    assert require_matching_plan(current, current)


def test_archive_is_not_expiry_or_purge(state):
    store, owner, task = state
    # Direct archived flag isolates archive semantics from its separate activity event.
    with store._tx() as db:
        db.execute('UPDATE sessions SET archived=1')
    result = plan(state, days=1)
    assert not result['expired']
    assert store.get_session(owner, task['session_id'])['archived'] == 1


def test_artifact_integrity_identity_without_storage_path(state):
    store, owner, task = state
    value = {'path': 'report.txt', 'sha256': hashlib.sha256(b'report').hexdigest(), 'bytes': 6, 'storage_path': '/private/controller/report'}
    with store._tx() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?)', ('artifact-1', task['session_id'], task['attempt_id'], json.dumps(value)))
    result = plan(state)
    assert result['known_artifact_bytes'] == 6
    assert result['inventory'][0]['sha256'] == value['sha256']
    assert '/private/controller' not in json.dumps(result)
    with store._tx() as db:
        value['path'] = '../../host'
        db.execute('UPDATE artifacts SET metadata=?', (json.dumps(value),))
    with pytest.raises(RetentionError, match='integrity metadata'):
        plan(state)


def test_inventory_budget_fails_closed(state):
    with pytest.raises(RetentionError, match='budget'):
        plan(state, max_items=3)


@pytest.mark.parametrize('change', [{'keep': 1}, {'retention_days': True}, {'retention_days': 0}, {'deadline': '2026-01-01'}, {'native_hours': 720}, {'secret_hours': 720}, {'purge': True}, {}])
def test_invalid_policy_never_mutates(state, change):
    store, owner, task = state
    with store._tx() as db:
        with pytest.raises(RetentionError):
            set_policy(db, task['session_id'], change, expected_revision=0)
        assert get_policy(db, task['session_id'])['revision'] == 0


def test_separate_native_secret_controls_do_not_authorize_deletion(state):
    store, owner, task = state
    with store._tx() as db:
        set_policy(db, task['session_id'], {'native_hours': 48, 'secret_hours': 4}, expected_revision=0, now=BASE)
    result = plan(state, days=3)
    assert result['native_state']['eligible_for_separate_review']
    assert not result['eligible_for_review']
    assert not result['secret_state']['policy_adopted']
    assert not result['deletion_manifest']


def test_api_owner_scope_cas_idempotency_and_purge_stays_disabled(state):
    store, owner, task = state
    other = store.add_client('other', 'y'*40, ['observe', 'submit'], ['p'])
    observer = store.add_client('observer', 'z'*40, ['observe'], ['p'])
    client = TestClient(create_app(store))
    url = '/v1/sessions/' + task['session_id'] + '/retention'
    headers = {'Authorization': 'Bearer ' + 'x'*40, 'Idempotency-Key': 'retention-control'}
    assert client.get(url).status_code == 401
    assert client.get(url, headers={'Authorization': 'Bearer '+'y'*40}).status_code == 404
    assert client.get(url, headers=headers).json()['policy']['revision'] == 0
    assert client.get(url+'/dry-run', headers=headers).json()['dry_run']
    assert client.patch(url, headers={'Authorization': 'Bearer '+'z'*40, 'Idempotency-Key': 'deny'}, json={'keep': True, 'expected_revision': 0}).status_code == 403
    assert client.patch(url, headers={'Authorization': headers['Authorization']}, json={'keep': True, 'expected_revision': 0}).status_code == 422
    changed = client.patch(url, headers=headers, json={'keep': True, 'expected_revision': 0})
    assert changed.status_code == 200 and changed.json()['keep']
    assert client.patch(url, headers=headers, json={'keep': True, 'expected_revision': 0}).json() == changed.json()
    assert client.patch(url, headers={**headers, 'Idempotency-Key': 'stale'}, json={'keep': False, 'expected_revision': 0}).status_code == 409
    assert client.delete(url.removesuffix('/retention'), headers=headers).status_code == 501
    assert store.get_session(owner, task['session_id'])['attempts'][0]['state'] == 'cancelled'


def test_byte_bound_and_absent_snapshot_guards_fail_closed(state):
    with pytest.raises(RetentionError, match='byte budget'):
        plan(state, max_manifest_bytes=1024)
    fake = {'format': 'cloud-workbench-retention-plan-v1', 'session_id': 'id'}
    with pytest.raises(RetentionError, match='snapshot changed'):
        require_matching_plan(fake, fake)


def test_project_allowlist_still_required_for_owner(state):
    store, owner, task = state
    restricted = {**owner, 'projects': []}
    with pytest.raises(StoreError) as denied:
        store.get_retention(restricted, task['session_id'])
    assert denied.value.status_code == 404
    with pytest.raises(StoreError) as denied:
        store.set_retention(restricted, task['session_id'], {'keep': True}, 0, 'denied')
    assert denied.value.status_code == 404


def test_retention_cookie_patch_requires_origin_and_csrf(state):
    store, owner, task = state
    origin = 'https://workbench.example'
    client = TestClient(create_app(store, {'dashboard_origin': origin}), base_url=origin)
    login = client.post('/auth/session', headers={'Origin': origin, 'Authorization': 'Bearer ' + 'x'*40})
    assert login.status_code == 200
    url = '/v1/sessions/' + task['session_id'] + '/retention'
    body = {'expected_revision': 0, 'keep': True}
    headers = {'Idempotency-Key': 'cookie-control'}
    assert client.patch(url, headers=headers, json=body).status_code == 403
    assert client.patch(url, headers={**headers, 'Origin': origin}, json=body).status_code == 403
    assert client.patch(url, headers={**headers, 'Origin': 'https://evil.example', 'X-CSRF-Token': login.json()['csrf']}, json=body).status_code == 403
    assert client.patch(url, headers={**headers, 'Origin': origin, 'X-CSRF-Token': login.json()['csrf']}, json=body).status_code == 200


def test_concurrent_policy_cas_allows_one_writer(state):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store, owner, task = state
    barrier = Barrier(2)
    def update(index):
        barrier.wait(timeout=5)
        try:
            return store.set_retention(owner, task['session_id'], {'keep': bool(index)}, 0, 'concurrent-' + str(index))['revision']
        except StoreError as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, (0, 1))) == [1, 409]
    assert store.get_retention(owner, task['session_id'])['policy']['revision'] == 1


@pytest.mark.parametrize('metadata', ['not json', '[]', '42', 'null'])
def test_malformed_artifact_shape_api_returns_controlled_422(state, metadata):
    store, owner, task = state
    with store._tx() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?)', ('bad-artifact', task['session_id'], task['attempt_id'], metadata))
    client = TestClient(create_app(store))
    result = client.get('/v1/sessions/' + task['session_id'] + '/retention', headers={'Authorization': 'Bearer ' + 'x'*40})
    assert result.status_code == 422
    assert result.json()['detail'].startswith('Artifact integrity metadata invalid')


def test_missing_new_policy_keys_inherit_defaults_and_remain_editable(state):
    store, owner, task = state
    with store._tx() as db:
        db.execute('INSERT INTO session_retention VALUES(?,?,?,?)', (task['session_id'], '{"keep":false,"retention_days":30}', 4, BASE.isoformat()))
    assert store.get_retention(owner, task['session_id'])['policy']['secret_hours'] is None
    assert store.set_retention(owner, task['session_id'], {'keep': True}, 4, 'upgrade-policy')['revision'] == 5


@pytest.mark.parametrize('path', ['credentials/x', 'nested/.credentials/attempt.token', 'native/state', 'credential-state/claude.json', 'shared_inputs/source'])
def test_sensitive_artifact_paths_fail_closed_without_reflection(state, path):
    store, owner, task = state
    metadata = {'path': path, 'sha256': '0'*64, 'bytes': 1}
    with store._tx() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?)', ('private-artifact', task['session_id'], task['attempt_id'], json.dumps(metadata)))
    with pytest.raises(RetentionError) as failure:
        plan(state)
    assert path not in str(failure.value)


def test_read_operations_do_not_extend_activity_but_archive_does(state):
    store, owner, task = state
    artifact = store.register_artifact(task['attempt_id'], {'path': 'read-only.txt', 'storage_path': '/not-read-by-this-test', 'sha256': '0'*64, 'bytes': 0}, expected_generation=task['generation'])
    before = store.get_retention(owner, task['session_id'])['last_activity']
    store.get_artifact(owner, artifact['id'])
    store.get_session(owner, task['session_id'])
    store.list_artifacts(owner, task['session_id'])
    store.events(owner, task['session_id'])
    assert store.get_retention(owner, task['session_id'])['last_activity'] == before
    store.archive(owner, task['session_id'], 'archive-via-api')
    assert store.get_retention(owner, task['session_id'])['last_activity'] > before


def test_clear_deadline_restores_rolling_and_failed_cas_key_can_retry(state):
    store, owner, task = state
    store.set_retention(owner, task['session_id'], {'deadline': BASE.isoformat()}, 0, 'deadline')
    with pytest.raises(StoreError):
        store.set_retention(owner, task['session_id'], {'deadline': None}, 0, 'retry-cas')
    store.set_retention(owner, task['session_id'], {'deadline': None}, 1, 'retry-cas')
    result = store.get_retention(owner, task['session_id'])
    assert datetime.fromisoformat(result['retention_deadline']) - datetime.fromisoformat(result['last_activity']) == timedelta(days=30)


@pytest.mark.parametrize('deadline', ['9999-12-31T00:00:00Z', '0001-01-01T00:00:00Z', '0001-01-01T00:00:00+14:00'])
def test_extreme_explicit_deadlines_controlled_rejection(state, deadline):
    store, owner, task = state
    with pytest.raises(StoreError) as failure:
        store.set_retention(owner, task['session_id'], {'deadline': deadline}, 0, 'extreme')
    assert failure.value.status_code == 422


def test_explicit_deadline_cannot_precede_configured_native_policy(state):
    store, owner, task = state
    with store._tx() as db, pytest.raises(RetentionError, match='native review deadline'):
        set_policy(db, task['session_id'], {'deadline': (BASE+timedelta(hours=1)).isoformat(), 'native_hours': 48}, expected_revision=0, now=BASE)


def test_sqlite_unsupported_activity_format_does_not_silently_shorten_expiry(state):
    store, owner, task = state
    with store._tx() as db:
        db.execute("UPDATE events SET created_at='20260130T000000+0000' WHERE sequence=1")
    with pytest.raises(RetentionError, match='unsupported'):
        plan(state)


def test_activity_native_deadline_conflict_protects_fixed_deadline(state):
    store, owner, task = state
    with store._tx() as db:
        set_policy(db, task['session_id'], {'deadline': (BASE+timedelta(days=3)).isoformat(), 'native_hours': 48}, expected_revision=0, now=BASE)
        db.execute('UPDATE events SET created_at=? WHERE sequence=1', ((BASE+timedelta(days=2)).isoformat(),))
    result = plan(state, days=5)
    assert result['expired'] and not result['deletion_manifest']
    assert 'native_deadline_conflict' in result['protected_reasons']
