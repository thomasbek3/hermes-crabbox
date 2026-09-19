"""Default result selection across legacy and routed attempt identities."""
import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from cloudworkbench.api import create_app
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.result_bundle import BundleError, authorized_snapshot, build_bundle
from cloudworkbench.role_broker import RoleBroker, ParentScope
from cloudworkbench.scheduler import RoleScheduler
from cloudworkbench.store import Store, now, uid


def result(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return json.loads(archive.read('result.json'))


@pytest.mark.parametrize('reverse', [False, True])
def test_bundle_uses_root_sequence_not_generation_or_child_position(tmp_path, reverse):
    attempts = [
        {'id': 'old', 'generation': 9, 'root_sequence': 1, 'execution_kind': 'legacy', 'state': 'failed'},
        {'id': 'new', 'generation': 1, 'root_sequence': 2, 'execution_kind': 'hermes_root', 'state': 'failed'},
        {'id': 'child', 'generation': 1, 'root_sequence': None, 'execution_kind': 'hermes_child', 'state': 'queued'},
    ]
    if reverse:
        attempts.reverse()
    session = {'id': 'session', 'attempts': attempts}
    assert result(build_bundle(session, [], tmp_path))['attempt_id'] == 'new'
    next(a for a in attempts if a['id'] == 'new')['state'] = 'queued'
    with pytest.raises(BundleError, match='not terminal'):
        build_bundle(session, [], tmp_path)


def test_legacy_bundle_still_uses_generation(tmp_path):
    attempts = [{'id': 'new', 'generation': 2, 'state': 'failed'},
                {'id': 'old', 'generation': 1, 'state': 'failed'}]
    assert result(build_bundle({'id': 'session', 'attempts': attempts}, [], tmp_path))['attempt_id'] == 'new'


@pytest.mark.parametrize('attempts', [
    [{'id': 'child', 'generation': 1, 'root_sequence': None, 'execution_kind': 'hermes_child', 'state': 'failed'}],
    [{'id': 'a', 'generation': 1, 'root_sequence': None, 'execution_kind': 'hermes_root', 'state': 'failed'}],
    [{'id': 'a', 'generation': 1, 'root_sequence': 1, 'state': 'failed'},
     {'id': 'b', 'generation': 1, 'root_sequence': 1, 'state': 'failed'}],
])
def test_missing_or_ambiguous_root_fails_closed(tmp_path, attempts):
    with pytest.raises(BundleError):
        build_bundle({'id': 'session', 'attempts': attempts}, [], tmp_path)


@pytest.fixture
def routed(tmp_path):
    store = Store(tmp_path / 'state.db')
    owner = store.add_client('owner', 'a' * 40, ['submit', 'observe', 'retrieve', 'cancel'], ['demo'])
    store.add_client('other', 'b' * 40, ['observe', 'retrieve'], ['demo'])
    store.migrate_scheduler()
    leases = ProviderLeases(store, cleanup_verifier=lambda _: None, inspector_id='fixture', clock=lambda: 1000)
    leases.register_account('synthetic', legacy_agent='hermes', persistent_owner_id='owner')
    scheduler = RoleScheduler(store, leases, clock=lambda: 1000)
    plan = [{'ready': True, 'profile': {'profile_id': 'fixture', 'provider': 'fixture',
        'model': 'fake', 'effort': 'high', 'qualification_reference_verified': False}}]
    frozen = {'accounts': {'synthetic': 'owner'}, 'role_plans': {'implementation': plan},
              'provenance': {'fixture_only': True}}
    created = scheduler.enqueue_root(owner, {'agent': 'hermes', 'goal': 'test', 'project_id': 'demo'}, 'root', frozen=frozen)
    scheduler.admit_root(created['attempt_id'], accounts=frozen['accounts'])
    scheduler.transition(created['attempt_id'], 'running', expected_generation=1)
    broker = RoleBroker(store.path, authorize_parent=scheduler.authorize_parent, clock=lambda: 1000)
    scope = ParentScope(owner['id'], 'demo', created['attempt_id'], created['attempt_id'], 1, 'native')
    _, token = broker.issue(scope, roles=('implementation',), expires_at=2000)
    pending = broker.prepare(token, native_session_id='native', native_call_id='call', role='implementation', task='test')
    scheduler.admit_request(broker, scope, pending['id'], plan=plan)
    child = scheduler.claim_child(created['attempt_id'])
    artifacts = tmp_path / 'artifacts'; artifacts.mkdir()
    client = TestClient(create_app(store, {'projects': {}, 'artifact_root': artifacts}))
    return store, owner, client, artifacts, created, child, scheduler


def delivery(store, root, attempt_id, label, scheduler=None):
    content = ('patch-' + label).encode()
    path = root / label; path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    metadata = {'delivery': {'base_commit': 'a' * 40, 'patch_sha256': digest, 'complete_text_patch': True}}
    if scheduler is not None:
        scheduler.transition(attempt_id, 'failed', expected_generation=1, result=metadata)
    else:
        # Finalized history fixture, not a routed execution/transition test.
        with store._tx() as db:
            db.execute("UPDATE attempts SET state='failed',result=?,updated_at=? WHERE id=?",
                       (json.dumps(metadata), now(), attempt_id))
    return store.register_artifact(attempt_id, {'path': '@delivery/changes.patch', 'storage_path': str(path),
        'bytes': len(content), 'sha256': digest}, expected_generation=1)


def headers(token='a' * 40):
    return {'Authorization': 'Bearer ' + token}


def test_actual_scheduler_child_never_replaces_root_bundle_or_diff(routed):
    store, owner, client, root, created, child, scheduler = routed
    child_artifact = delivery(store, root, child['id'], 'child', scheduler)
    url = '/v1/sessions/' + created['session_id']
    # A finished child cannot make an active root downloadable.
    assert client.get(url + '/bundle', headers=headers()).status_code == 409
    assert client.get(url + '/diff', headers=headers()).status_code == 409
    root_artifact = delivery(store, root, created['attempt_id'], 'root', scheduler)
    downloaded = client.get(url + '/bundle', headers=headers())
    assert downloaded.status_code == 200
    assert result(downloaded.content)['attempt_id'] == created['attempt_id']
    patch = client.get(url + '/diff', headers=headers()).json()
    assert patch['attempt_id'] == created['attempt_id'] and patch['patch_artifact_id'] == root_artifact['id']
    for suffix in ('/bundle', '/diff'):
        assert client.get(url + suffix, headers=headers('b' * 40)).status_code == 404
    # Explicit authorized child artifact retrieval retains its existing contract.
    child_url = '/v1/artifacts/' + child_artifact['id'] + '/content'
    assert client.get(child_url, headers=headers()).content == b'patch-child'
    assert client.get(child_url, headers=headers('b' * 40)).status_code == 404


def next_queued_root(store, created):
    # Fixture for later root history, using migrated identity guards. It never
    # starts execution or claims to test scheduler follow-up admission.
    attempt_id, turn_id, stamp = uid(), uid(), now()
    with store._tx() as db:
        db.execute('INSERT INTO turns VALUES(?,?,?,?,?)',
            (turn_id, created['session_id'], 2, json.dumps({'agent': 'hermes', 'goal': 'next', 'project_id': 'demo'}), stamp))
        db.execute("""INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at,
            execution_kind,workflow_root_id,root_sequence) VALUES(?,?,?,'hermes',1,'queued',?,?,'hermes_root',?,2)""",
            (attempt_id, created['session_id'], turn_id, stamp, stamp, attempt_id))
    return attempt_id


def test_latest_root_snapshot_and_diff_do_not_fall_back_to_old_root(routed):
    store, owner, client, root, created, child, scheduler = routed
    delivery(store, root, child['id'], 'child', scheduler)
    delivery(store, root, created['attempt_id'], 'old', scheduler)
    old_snapshot, old_artifacts = authorized_snapshot(store, owner, created['session_id'])
    latest = next_queued_root(store, created)
    url = '/v1/sessions/' + created['session_id']
    assert client.get(url + '/bundle', headers=headers()).status_code == 409
    assert client.get(url + '/diff', headers=headers()).status_code == 409
    latest_artifact = delivery(store, root, latest, 'new')
    snapshot, artifacts = authorized_snapshot(store, owner, created['session_id'])
    assert snapshot['attempts'][0]['id'] == latest
    assert [item['id'] for item in artifacts] == [latest_artifact['id']]
    assert result(client.get(url + '/bundle', headers=headers()).content)['attempt_id'] == latest
    assert client.get(url + '/diff', headers=headers()).json()['patch_artifact_id'] == latest_artifact['id']
    # Previously authorized in-memory snapshot remains pinned to its own root.
    assert result(build_bundle(old_snapshot, old_artifacts, root))['attempt_id'] == created['attempt_id']


def test_running_root_with_delivery_metadata_still_has_no_default_diff(routed):
    store, owner, client, root, created, child, scheduler = routed
    content = b'not-terminal-patch'
    path = root / 'in-progress'; path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    scheduler.transition(created['attempt_id'], 'running', expected_generation=1,
        result={'delivery': {'patch_sha256': digest, 'complete_text_patch': True}})
    store.register_artifact(created['attempt_id'], {'path': '@delivery/changes.patch',
        'storage_path': str(path), 'bytes': len(content), 'sha256': digest}, expected_generation=1)
    route = '/v1/sessions/' + created['session_id']
    assert client.get(route + '/diff', headers=headers()).status_code == 409
    assert client.get(route + '/bundle', headers=headers()).status_code == 409


@pytest.mark.parametrize('malformed', [None, {}, {'generation': 1, 'execution_kind': 'hermes_supervisor', 'root_sequence': 2}])
def test_selector_malformed_projection_is_a_controlled_error(malformed):
    from cloudworkbench.result_bundle import latest_root_attempt
    with pytest.raises(BundleError):
        latest_root_attempt([{'id': 'old', 'generation': 1, 'state': 'failed'}, malformed])


def test_migrated_legacy_history_keeps_latest_bundle_and_diff(tmp_path):
    store = Store(tmp_path / 'legacy.db')
    owner = store.add_client('owner', 'a' * 40, ['submit', 'observe', 'retrieve'], ['demo'])
    created = store.create_session(owner, {'agent': 'claude', 'goal': 'first', 'project_id': 'demo'}, 'first')
    store.claim_next()
    store.transition(created['attempt_id'], 'failed', expected_generation=1)
    latest = store.add_message(owner, created['session_id'], 'second', 'second')
    store.claim_next()
    content = b'legacy-patch'; path = tmp_path / 'patch'; path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    store.transition(latest['attempt_id'], 'failed', expected_generation=2,
        result={'delivery': {'patch_sha256': digest, 'complete_text_patch': True}})
    artifact = store.register_artifact(latest['attempt_id'], {'path': '@delivery/changes.patch',
        'storage_path': str(path), 'bytes': len(content), 'sha256': digest}, expected_generation=2)
    client = TestClient(create_app(store, {'projects': {}, 'artifact_root': tmp_path}))
    route = '/v1/sessions/' + created['session_id']
    for migrated in (False, True):
        if migrated:
            store.migrate_scheduler()
            with store._connect() as db:
                rows = db.execute('SELECT execution_kind,root_sequence,generation FROM attempts ORDER BY root_sequence').fetchall()
            assert [tuple(row) for row in rows] == [('legacy', 1, 1), ('legacy', 2, 2)]
        response = client.get(route + '/bundle', headers=headers())
        assert response.status_code == 200
        assert result(response.content)['attempt_id'] == latest['attempt_id']
        response = client.get(route + '/diff', headers=headers())
        assert response.status_code == 200 and response.json()['patch_artifact_id'] == artifact['id']


def test_schema_rejects_unknown_null_kinds_and_duplicate_root_sequences(routed):
    import sqlite3
    store, owner, client, root, created, child, scheduler = routed
    for kind, sequence in [('hermes_supervisor', 2), (None, 2), ('hermes_root', 1)]:
        expected = r'attempts.session_id, attempts.root_sequence' if sequence == 1 else r'invalid_workflow_identity|CHECK|NOT NULL'
        with pytest.raises(sqlite3.IntegrityError, match=expected), store._tx() as db:
            attempt_id, turn_id = uid(), uid()
            db.execute('INSERT INTO turns VALUES(?,?,?,?,?)',
                       (turn_id, created['session_id'], 2, '{}', now()))
            db.execute("""INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at,
                execution_kind,workflow_root_id,root_sequence) VALUES(?,?,?,'hermes',1,'queued',?,?,?,?,?)""",
                (attempt_id, created['session_id'], turn_id, now(), now(), kind, attempt_id, sequence))
