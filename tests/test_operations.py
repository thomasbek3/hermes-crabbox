import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from cloudworkbench.operations import OperationsError, backup_state, restore_state
from cloudworkbench.store import Store


@pytest.fixture
def populated(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    components = {name: source / name for name in ('artifacts', 'inputs', 'results', 'workspaces')}
    for root in components.values():
        root.mkdir()
    store = Store(source / 'state.sqlite')
    principal = store.add_client('owner', 't' * 40, ['observe', 'retrieve', 'submit', 'cancel'], ['project'])
    task = store.create_session(principal, {'project_id': 'project', 'goal': 'Synthetic portable backup rehearsal', 'agent': 'fixture', 'environment_version': 'test-v1'}, 'create')
    aid, sid = task['attempt_id'], task['session_id']
    blob = bytes(range(256)) * 1024
    artifact = components['artifacts'] / 'report.bin'
    artifact.write_bytes(blob)
    store.register_artifact(aid, {'path': 'report.bin', 'storage_path': str(artifact), 'sha256': hashlib.sha256(blob).hexdigest(), 'bytes': len(blob), 'mime': 'application/octet-stream'}, expected_generation=task['generation'])
    item = store.reserve_input(principal, {'name': 'input.txt', 'mime': 'text/plain'}, 'input')
    input_file = components['inputs'] / 'input.txt'
    input_file.write_bytes(b'supplied input')
    store.finalize_input(principal, item['id'], {'storage_path': str(input_file), 'sha256': hashlib.sha256(b'supplied input').hexdigest(), 'bytes': len(b'supplied input')}, key='finalize')
    store.cancel(principal, aid, 'cancel')
    store.archive(principal, sid, 'archive')
    workspace = components['workspaces'] / sid
    (workspace / 'work' / '.git').mkdir(parents=True)
    (workspace / 'work' / '.git' / 'HEAD').write_text('ref: refs/heads/test\n')
    (workspace / 'work' / 'answer.py').write_text('print(42)\n')
    (workspace / 'work' / '.venv').mkdir()
    (workspace / 'work' / '.venv' / 'python').symlink_to('/outside/python')
    (workspace / 'native').mkdir()
    (workspace / 'native' / 'public-event.json').write_text('{"type":"completed"}')
    (workspace / 'native' / '.credentials.json').write_bytes(b'NEVER_BACKUP_AUTH_TOKEN')
    (components['workspaces'] / '.volumes').mkdir()
    (components['workspaces'] / '.volumes' / 'unused.img').write_text('raw backing data excluded')
    (components['results'] / 'result.json').write_text('{"outcome":"unverified"}')
    destination = tmp_path / 'backup'
    restore = tmp_path / 'restore'
    restore.mkdir(mode=0o700)
    return store, principal, task, components, destination, restore, blob


def make_backup(populated, **kwargs):
    store, principal, task, components, destination, restore, blob = populated
    return backup_state(store.path, components, destination, quiesced=True, max_bytes=10 * 1024**2, reserve_bytes=0, **kwargs)


def test_consistent_snapshot_offline_restore_preserves_data_not_credentials(populated):
    store, owner, task, components, destination, restore, blob = populated
    before = store.get_session(owner, task['session_id'])
    manifest = make_backup(populated, forbidden_values=(b'NEVER_BACKUP_AUTH_TOKEN',))
    assert manifest['encrypted'] is False and manifest['clients_disabled'] == 1
    assert manifest['components']['workspaces']['omitted_count'] == 3
    assert {entry['path'] for entry in manifest['components']['workspaces']['omissions']} == {'.volumes', task['session_id'] + '/work/.venv', task['session_id'] + '/native/.credentials.json'}
    assert manifest['components']['workspaces']['complete_workspace'] is False
    assert store.authenticate('t' * 40) == owner
    assert store.get_session(owner, task['session_id']) == before
    assert not any(b'NEVER_BACKUP_AUTH_TOKEN' in path.read_bytes() for path in destination.rglob('*') if path.is_file())
    receipt = restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    assert receipt['credentials_restored'] is False
    assert receipt['services_started'] is False
    assert receipt['attempt_states_changed'] is False
    assert receipt['clients_revoked'] == 1
    assert (restore / 'components/artifacts/report.bin').read_bytes() == blob
    assert (restore / 'components/workspaces' / task['session_id'] / 'work/.git/HEAD').exists()
    assert (restore / 'components/workspaces' / task['session_id'] / 'native/public-event.json').exists()
    with sqlite3.connect(restore / 'database.sqlite') as db:
        token_hash, revoked_at = db.execute('SELECT token_hash,revoked_at FROM clients').fetchone()
        assert token_hash.startswith('backup-disabled:') and revoked_at
        assert db.execute('SELECT state FROM attempts').fetchone()[0] == 'cancelled'
        assert db.execute('SELECT archived FROM sessions').fetchone()[0] == 1
        artifact = json.loads(db.execute('SELECT metadata FROM artifacts').fetchone()[0])
        assert artifact['storage_path'] == str(restore / 'components/artifacts/report.bin')
        assert db.execute('SELECT storage_path FROM inputs').fetchone()[0] == str(restore / 'components/inputs/input.txt')
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert stat.S_IMODE((restore / 'database.sqlite').stat().st_mode) == 0o400
    assert stat.S_IMODE(restore.stat().st_mode) == 0o500


def test_sqlite_backup_includes_uncheckpointed_wal(populated):
    store, owner, task, components, destination, restore, blob = populated
    connection = sqlite3.connect(store.path)
    try:
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute("UPDATE sessions SET goal='persisted only in WAL' WHERE id=?", (task['session_id'],))
        connection.commit()
        assert Path(str(store.path) + '-wal').exists()
        make_backup(populated)
        with sqlite3.connect(destination / 'database.sqlite') as db:
            assert db.execute('SELECT goal FROM sessions').fetchone()[0] == 'persisted only in WAL'
    finally:
        connection.close()


def test_backup_requires_quiescence_and_no_queued_or_live_attempts(populated):
    store, owner, task, components, destination, restore, blob = populated
    with pytest.raises(OperationsError, match='quiescence'):
        backup_state(store.path, components, destination)
    store.add_message(owner, task['session_id'], 'new queued task', 'queued')
    with pytest.raises(OperationsError, match='all attempts closed'):
        make_backup(populated)
    assert not destination.exists()
    assert not list(destination.parent.glob('.cwb-backup-*'))


def test_backup_rejects_missing_referenced_file(populated):
    store, owner, task, components, destination, restore, blob = populated
    (components['artifacts'] / 'report.bin').unlink()
    with pytest.raises(OperationsError, match='omitted or missing'):
        make_backup(populated)
    assert not destination.exists()


@pytest.mark.parametrize('entry', ['symlink', 'hardlink', 'fifo'])
def test_backup_rejects_unsafe_included_files(populated, entry):
    store, owner, task, components, destination, restore, blob = populated
    target = components['results'] / 'unsafe'
    if entry == 'symlink':
        target.symlink_to(store.path)
    elif entry == 'hardlink':
        os.link(store.path, target)
    else:
        os.mkfifo(target)
    with pytest.raises(OperationsError):
        make_backup(populated)
    assert not destination.exists()


def test_backup_rejects_known_secret_even_inside_database(populated):
    store, owner, task, components, destination, restore, blob = populated
    with store._connect() as db:
        db.execute("UPDATE sessions SET goal='sensitive_canary' WHERE id=?", (task['session_id'],))
        db.commit()
    with pytest.raises(OperationsError, match='protected credential'):
        make_backup(populated, forbidden_values=(b'sensitive_canary',))
    assert not destination.exists()


def test_restore_rejects_modified_bytes_and_keeps_empty_target(populated):
    store, owner, task, components, destination, restore, blob = populated
    make_backup(populated)
    artifact = destination / 'components/artifacts/report.bin'
    artifact.chmod(0o600)
    artifact.write_bytes(b'tampered')
    with pytest.raises(OperationsError, match='integrity mismatch'):
        restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    assert list(restore.iterdir()) == []
    assert not list(restore.parent.glob('.cwb-restore-*'))


@pytest.mark.parametrize('bad_path', ['../outside', '/etc/passwd', 'components/../outside', 'components\\outside'])
def test_restore_rejects_manifest_traversal(populated, bad_path):
    store, owner, task, components, destination, restore, blob = populated
    make_backup(populated)
    path = destination / 'manifest.json'
    document = json.loads(path.read_text())
    document['files'][0]['path'] = bad_path
    path.chmod(0o600)
    path.write_text(json.dumps(document))
    with pytest.raises(OperationsError, match='unsafe bundle path'):
        restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    assert list(restore.iterdir()) == []


def test_restore_never_replaces_nonempty_directory(populated):
    store, owner, task, components, destination, restore, blob = populated
    make_backup(populated)
    (restore / 'important').write_text('keep')
    with pytest.raises(OperationsError, match='empty'):
        restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    assert (restore / 'important').read_text() == 'keep'


def test_restore_rejects_extra_file_and_symlink_target(populated):
    store, owner, task, components, destination, restore, blob = populated
    make_backup(populated)
    destination.chmod(0o700)
    (destination / 'unexpected').write_text('not manifested')
    with pytest.raises(OperationsError, match='match manifest'):
        restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    linked = restore.parent / 'linked-restore'
    linked.symlink_to(restore)
    with pytest.raises(OSError):
        restore_state(destination, linked, max_bytes=10 * 1024**2, reserve_bytes=0)


def test_backup_limits_and_existing_destination(populated):
    store, owner, task, components, destination, restore, blob = populated
    with pytest.raises(OperationsError, match='byte limit'):
        backup_state(store.path, components, destination, quiesced=True, max_bytes=128, reserve_bytes=0)
    destination.mkdir()
    (destination / 'important').write_text('keep')
    with pytest.raises(OperationsError, match='already exists'):
        make_backup(populated)
    assert (destination / 'important').read_text() == 'keep'


@pytest.mark.parametrize('component,file', [('artifacts', 'report.bin'), ('inputs', 'input.txt')])
def test_backup_rejects_referenced_source_content_inconsistent_with_database(populated, component, file):
    store, owner, task, components, destination, restore, blob = populated
    (components[component] / file).write_bytes(b'corrupted source')
    with pytest.raises(OperationsError, match='database integrity metadata'):
        make_backup(populated)
    assert not destination.exists()


def test_restore_crosschecks_database_hashes_even_if_file_manifest_was_rewritten(populated):
    store, owner, task, components, destination, restore, blob = populated
    make_backup(populated)
    artifact = destination / 'components/artifacts/report.bin'
    artifact.chmod(0o600)
    artifact.write_bytes(b'corrupted but self-consistent file manifest')
    manifest_path = destination / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for item in manifest['files']:
        if item['path'] == 'components/artifacts/report.bin':
            item['sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            item['bytes'] = artifact.stat().st_size
    manifest['bytes'] = sum(item['bytes'] for item in manifest['files'])
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(OperationsError, match='database integrity metadata'):
        restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    assert list(restore.iterdir()) == []


def test_scratch_space_reserve_and_nonfinite_duration_fail_closed(populated, monkeypatch):
    import cloudworkbench.operations as operations
    from collections import namedtuple
    store, owner, task, components, destination, restore, blob = populated
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(operations.shutil, 'disk_usage', lambda path: usage(10000, 0, 2500))
    with pytest.raises(OperationsError, match='free-space reserve'):
        backup_state(store.path, components, destination, quiesced=True, max_bytes=1000, reserve_bytes=0)
    with pytest.raises(OperationsError, match='invalid operation limits'):
        backup_state(store.path, components, destination, quiesced=True, max_seconds=float('nan'))
    assert not destination.exists()


def test_write_fence_rejects_second_connection_update_during_sqlite_copy(populated, monkeypatch):
    import cloudworkbench.operations as operations
    store, owner, task, components, destination, restore, blob = populated
    original_connect = sqlite3.connect
    probed = []
    class ProbedConnection(sqlite3.Connection):
        def backup(self, target, **kwargs):
            original_progress = kwargs.get('progress')
            def progress(status, remaining, total):
                if not probed:
                    with original_connect(store.path, timeout=0.01) as concurrent:
                        with pytest.raises(sqlite3.OperationalError, match='locked'):
                            concurrent.execute("UPDATE sessions SET goal='must not enter snapshot'")
                    probed.append(True)
                if original_progress:
                    original_progress(status, remaining, total)
            return super().backup(target, **{**kwargs, 'progress': progress})
    def connect(*args, **kwargs):
        return original_connect(*args, **{**kwargs, 'factory': ProbedConnection})
    monkeypatch.setattr(operations.sqlite3, 'connect', connect)
    make_backup(populated)
    assert probed == [True]
    with original_connect(destination / 'database.sqlite') as copied:
        assert copied.execute('SELECT goal FROM sessions').fetchone()[0] != 'must not enter snapshot'
    # Writes can resume after the completed backup releases its fence.
    with original_connect(store.path, timeout=0.01) as resumed:
        resumed.execute("UPDATE sessions SET goal='explicit later change'")
        resumed.commit()


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_post_freeze_rename_failure_removes_private_staging(populated, monkeypatch, operation):
    import cloudworkbench.operations as operations
    store, owner, task, components, destination, restore, blob = populated
    if operation == 'restore':
        make_backup(populated)
    original_rename = os.rename
    injected = []
    def fail_publication(source, target, *args, **kwargs):
        if (operation == 'backup' and Path(source).name.startswith('.cwb-backup-')) or (operation == 'restore' and Path(source).name == 'payload'):
            assert (Path(source) / 'database.sqlite').stat().st_mode & 0o200 == 0
            injected.append(True)
            raise OSError('synthetic final publication failure')
        return original_rename(source, target, *args, **kwargs)
    monkeypatch.setattr(operations.os, 'rename', fail_publication)
    with pytest.raises(OperationsError):
        if operation == 'backup':
            make_backup(populated)
        else:
            restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    assert injected == [True]
    assert not list(destination.parent.glob('.cwb-backup-*'))
    assert not list(destination.parent.glob('.cwb-restore-*'))
    if operation == 'backup':
        assert not destination.exists()
    assert list(restore.iterdir()) == []


def test_broad_credential_name_omission_is_visible_not_claimed_complete(populated):
    store, owner, task, components, destination, restore, blob = populated
    auth = components['workspaces'] / task['session_id'] / 'work/src/auth'
    auth.mkdir(parents=True)
    (auth / 'handler.py').write_text('def authenticate(): pass\n')
    manifest = make_backup(populated)
    report = manifest['components']['workspaces']
    omitted = {entry['path'] for entry in report['omissions']}
    assert task['session_id'] + '/work/src/auth' in omitted
    assert report['complete_workspace'] is False
    assert report['omissions_truncated'] is False
    restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    assert not (restore / 'components/workspaces' / task['session_id'] / 'work/src/auth').exists()


def test_zero_client_empty_state_roundtrip(tmp_path):
    store = Store(tmp_path / 'empty.sqlite')
    source = tmp_path / 'artifacts'
    source.mkdir()
    destination, target = tmp_path / 'bundle', tmp_path / 'isolated'
    target.mkdir(mode=0o700)
    manifest = backup_state(store.path, {'artifacts': source}, destination, quiesced=True, max_bytes=1024**2, reserve_bytes=0)
    assert manifest['clients_disabled'] == 0
    receipt = restore_state(destination, target, max_bytes=1024**2, reserve_bytes=0)
    assert receipt['clients_revoked'] == 0


def test_repository_baselines_roundtrip_when_selected(populated):
    store, owner, task, components, destination, restore, blob = populated
    baseline = store.path.parent / 'baselines'
    baseline.mkdir()
    components['baselines'] = baseline
    content = b'{"commit":"synthetic","files":{"a.py":{"contents":"cHJpbnQoMSk="}}}'
    (baseline / (task['session_id'] + '.json')).write_bytes(content)
    (baseline / (task['session_id'] + '.ready.json')).write_text('{"ready":true}')
    manifest = make_backup(populated)
    assert manifest['components']['baselines']['exported_file_count'] == 2
    restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    assert (restore / 'components/baselines' / (task['session_id'] + '.json')).read_bytes() == content
    assert (restore / 'components/baselines' / (task['session_id'] + '.ready.json')).is_file()


def test_attempt_credential_capsules_and_quarantine_state_never_export(populated):
    store, owner, task, components, destination, restore, blob = populated
    workspace = components['workspaces'] / task['session_id']
    protected = (workspace / 'tasks/.credentials', workspace / 'credential-state', workspace / 'credential_state')
    for directory in protected:
        directory.mkdir(parents=True)
        (directory / 'synthetic.token').write_bytes(b'EXCLUDED_ATTEMPT_SECRET')
    manifest = make_backup(populated, forbidden_values=(b'EXCLUDED_ATTEMPT_SECRET',))
    paths = {row['path'] for row in manifest['components']['workspaces']['omissions']}
    assert task['session_id'] + '/tasks/.credentials' in paths
    assert task['session_id'] + '/credential-state' in paths
    assert task['session_id'] + '/credential_state' in paths
    assert not any(b'EXCLUDED_ATTEMPT_SECRET' in p.read_bytes() for p in destination.rglob('*') if p.is_file())
    restore_state(destination, restore, max_bytes=10 * 1024**2, reserve_bytes=0)
    assert not list((restore / 'components/workspaces').rglob('synthetic.token'))
