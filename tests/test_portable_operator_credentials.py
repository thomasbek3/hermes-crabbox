"""Portable operator setup and token refresh; all credentials and HTTP responses are synthetic."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import time
import types

import pytest
from cloudworkbench.store import Store

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provision = load('portable_provision', 'scripts/provision-delegation-client.py')
refresh = load('portable_refresh', 'integrations/omarchy-cloud/credential_refresh.py')


@pytest.fixture(autouse=True)
def restore_process_umask():
    # refresh.run owns a process in production; these tests call it in-process.
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def operator(tmp_path):
    db = tmp_path / 'state.db'
    Store(db)
    config = tmp_path / 'api.json'
    config.write_text(json.dumps({'database': str(db), 'projects': {'example-project': {}, 'other': {}}}))
    return dict(config=config, credential_dir=tmp_path/'callers', project='example-project')


def test_provision_caller_is_private_idempotent_and_project_scoped(operator):
    first = provision.provision_client('my-agent', **operator)
    second = provision.provision_client('my-agent', **operator)
    assert first == second and first['projects'] == ['example-project']
    secret_file = Path(first['credential_file'])
    secret = secret_file.read_text().strip()
    assert secret not in json.dumps(first)
    assert secret_file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='different client/project/scope'):
        provision.provision_client('my-agent', **{**operator, 'project': 'other'})
    assert secret_file.read_text().strip() == secret


def test_provision_rejects_wrong_project_without_creating_files(operator):
    with pytest.raises(ValueError, match='not configured'):
        provision.provision_client('my-agent', **{**operator, 'project': 'unknown'})
    assert not operator['credential_dir'].exists()


def test_provision_refuses_symlink_secret(operator, tmp_path):
    root = operator['credential_dir']
    root.mkdir(mode=0o700)
    destination = tmp_path / 'outside'
    destination.write_text('unchanged')
    (root/'my-agent.token').symlink_to(destination)
    with pytest.raises(OSError):
        provision.provision_client('my-agent', **operator)
    assert destination.read_text() == 'unchanged'


def test_refresh_uses_current_service_uid_gid_and_preserves_atomic_rotation(tmp_path, monkeypatch):
    uid, gid = os.geteuid(), os.getegid()
    if uid == 0:
        pytest.skip('Dedicated refresh deliberately rejects root; exercise ownership as an unprivileged service user.')
    monkeypatch.setattr(refresh.pwd, 'getpwnam', lambda name: types.SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(refresh.grp, 'getgrnam', lambda name: types.SimpleNamespace(gr_gid=gid))
    # tmp_path ancestors are OS temp directories; test protected leaf handling independently.
    monkeypatch.setattr(refresh, 'directory', lambda path: os.open(path, os.O_RDONLY | os.O_DIRECTORY))
    original = {refresh.NAMESPACE: {'auth_mode': 'oidc', 'oidc_issuer': refresh.ISSUER,
        'oidc_client_id': refresh.CLIENT_ID, 'key': 'synthetic-old-access',
        'refresh_token': 'synthetic-old-refresh',
        'expires_at': datetime.fromtimestamp(time.time()+60, timezone.utc).isoformat()}}
    auth = tmp_path / 'auth.json'
    auth.write_bytes(refresh.encode(original)); auth.chmod(0o600)
    monkeypatch.setattr(refresh, 'endpoint', lambda: 'https://auth.x.ai/token')
    seen = []
    def request(url, fields):
        seen.append((url, fields))
        return {'access_token': 'synthetic-new-access', 'refresh_token': 'synthetic-new-refresh',
                'token_type': 'bearer', 'expires_in': 10000}
    monkeypatch.setattr(refresh, 'request_json', request)
    assert refresh.run(tmp_path) == 'auth_refreshed'
    saved = refresh.decode(auth.read_bytes())[refresh.NAMESPACE]
    assert saved['key'] == 'synthetic-new-access' and saved['refresh_token'] == 'synthetic-new-refresh'
    assert auth.stat().st_mode & 0o777 == 0o600
    assert len(seen) == 1 and seen[0][1]['refresh_token'] == 'synthetic-old-refresh'
    assert not (tmp_path / refresh.INTENT).exists()
    assert refresh.run(tmp_path) == 'auth_current'
    assert len(seen) == 1


def test_refresh_rejects_wrong_service_identity_before_opening_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh.pwd, 'getpwnam', lambda name: types.SimpleNamespace(pw_uid=os.geteuid()+100))
    monkeypatch.setattr(refresh.grp, 'getgrnam', lambda name: types.SimpleNamespace(gr_gid=os.getegid()))
    with pytest.raises(refresh.RefreshError, match='dedicated_worker_required'):
        refresh.run(tmp_path)
    assert not list(tmp_path.iterdir())


def test_refresh_rejects_relative_directory():
    with pytest.raises(refresh.RefreshError, match='must_be_absolute'):
        refresh.directory(Path('relative-profile'))
