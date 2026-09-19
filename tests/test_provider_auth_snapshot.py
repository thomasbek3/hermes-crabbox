from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import base64
import json
import os
from pathlib import Path
import stat
import threading
import time

import pytest

from cloudworkbench import provider_auth_snapshot as module
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.provider_auth_snapshot import AuthSnapshots, SnapshotError
from cloudworkbench.provider_leases import LeaseError, ProviderLeases
from test_provider_dispatch import setup, admit

PROFILE = NativeProfile('openai-codex', 'gpt-6-astra', 'high')


@pytest.fixture
def staging(setup, tmp_path):
    store, principal, attempt, leases, owner, grant, runtime, dispatcher, clock = setup
    source_root = tmp_path / 'dedicated'
    root = tmp_path / 'snapshots'
    source_root.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    claims = base64.urlsafe_b64encode(json.dumps({'exp': time.time() + 3600}).encode()).decode().rstrip('=')
    value = {'auth_mode': 'chatgpt', 'tokens': {
        'access_token': 'header.' + claims + '.signature',
        'refresh_token': 'synthetic-refresh-only', 'account_id': 'synthetic-account'}}
    source = source_root / 'auth.json'
    source.write_text(json.dumps(value))
    source.chmod(0o600)
    config = dict(source=source, dedicated_root=source_root, destination_root=root,
                  profile=PROFILE, source_uid=os.geteuid(), provider_gid=os.getegid(),
                  account_id='account', persistent_owner_id='logical-owner')
    manager = AuthSnapshots(leases, **config)
    spec = admit(setup, profile=PROFILE.digest)
    kwargs = dict(attempt_id=attempt['attempt_id'], generation=attempt['generation'])
    return manager, spec, kwargs, config


def test_publish_mount_cleanup_and_no_secret_receipt(staging, setup):
    manager, spec, kwargs, config = staging
    before = manager.source.read_bytes()
    snapshot = manager.prepare(spec.lease, **kwargs)
    assert snapshot.path.read_bytes() == before
    assert manager.mount_path(spec.lease, **kwargs) == snapshot.path
    assert stat.S_IMODE(snapshot.path.stat().st_mode) == 0o440
    assert snapshot.path.stat().st_gid == config['provider_gid']
    assert stat.S_IMODE(snapshot.path.parent.stat().st_mode) & 0o777 == 0o700
    assert manager.source.read_bytes() == before
    receipt = (snapshot.path.parent / 'binding.json').read_text()
    assert 'synthetic-refresh-only' not in receipt and 'header.' not in receipt
    assert 'synthetic-refresh-only' not in repr(snapshot)
    with pytest.raises(SnapshotError, match='cleanup_unconfirmed'):
        manager.cleanup(spec.lease, **kwargs)
    setup[7].cleanup(spec.lease.request_id)
    manager.cleanup(spec.lease, **kwargs)
    manager.cleanup(spec.lease, **kwargs)
    assert not snapshot.path.parent.exists() and manager.source.exists()


def test_native_source_replaced_after_snapshot_does_not_change_mount(staging):
    manager, spec, kwargs, config = staging
    snapshot = manager.prepare(spec.lease, **kwargs)
    original = snapshot.path.read_bytes()
    manager.source.write_text('new-login-do-not-reuse-implicitly')
    assert manager.mount_path(spec.lease, **kwargs).read_bytes() == original
    with pytest.raises(SnapshotError, match='already_exists'):
        manager.prepare(spec.lease, **kwargs)


@pytest.mark.parametrize('case', ['symlink', 'parent_symlink', 'hardlink', 'group', 'world', 'fifo', 'empty', 'large'])
def test_unsafe_source_refuses_without_publication(staging, case):
    manager, spec, kwargs, config = staging
    source = manager.source
    if case == 'symlink':
        target = source.with_name('target'); source.rename(target); source.symlink_to(target)
    elif case == 'parent_symlink':
        old = manager.dedicated_root.with_name('renamed'); manager.dedicated_root.rename(old)
        manager.dedicated_root.symlink_to(old)
    elif case == 'hardlink': os.link(source, source.with_name('alias'))
    elif case == 'group': source.chmod(0o640)
    elif case == 'world': source.chmod(0o604)
    elif case == 'fifo': source.unlink(); os.mkfifo(source)
    elif case == 'empty': source.write_bytes(b'')
    else: source.write_bytes(b'x' * 65537)
    with pytest.raises(SnapshotError): manager.prepare(spec.lease, **kwargs)
    assert not list(manager.root.iterdir())


@pytest.mark.parametrize('case,code', [('json', 'auth_invalid'), ('expired', 'auth_expired'), ('unknown', 'auth_schema_unsupported')])
def test_native_validation_uses_existing_parser_and_scrubs_failed_stage(staging, case, code):
    manager, spec, kwargs, config = staging
    if case == 'json': manager.source.write_text('synthetic-secret-invalid-json')
    elif case == 'unknown': manager.source.write_text('{"unsupported":"synthetic-secret"}')
    else:
        data = json.loads(manager.source.read_text())
        claims = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip('=')
        data['tokens']['access_token'] = 'header.' + claims + '.signature'
        manager.source.write_text(json.dumps(data))
    with pytest.raises(SnapshotError) as caught: manager.prepare(spec.lease, **kwargs)
    assert str(caught.value) == code and not list(manager.root.iterdir())


def test_grok_observed_schema_supported(staging):
    from datetime import datetime, timezone, timedelta
    manager, spec, kwargs, config = staging
    config['profile'] = NativeProfile('xai-oauth', 'grok-4.6', 'xhigh')
    manager = AuthSnapshots(manager.leases, **config)
    with manager.leases.store._tx() as db:
        db.execute('UPDATE provider_dispatch SET profile_digest=? WHERE request_id=?',
                   (manager.profile.digest, spec.lease.request_id))
    issuer, client = 'https://auth.x.ai', 'b1a00492-073a-47ea-816f-4c329264a828'
    manager.source.write_text(json.dumps({issuer + '::' + client: {
        'key': 'synthetic-access', 'refresh_token': 'synthetic-refresh',
        'auth_mode': 'oidc', 'oidc_issuer': issuer, 'oidc_client_id': client,
        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}))
    assert manager.prepare(spec.lease, **kwargs).path.is_file()


@pytest.mark.parametrize('case', ['generation', 'attempt', 'account', 'owner', 'epoch', 'controller', 'grant', 'request', 'refresh'])
def test_stale_or_other_identity_cannot_read_source(staging, monkeypatch, case):
    manager, spec, kwargs, config = staging
    lease = spec.lease
    if case == 'generation': kwargs['generation'] += 1
    elif case == 'attempt': kwargs['attempt_id'] = 'different-attempt'
    elif case in ('account', 'owner', 'epoch', 'controller'):
        key = {'account': 'account_id', 'owner': 'persistent_owner_id', 'epoch': 'epoch', 'controller': 'controller_instance_id'}[case]
        lease = replace(lease, reservation=replace(lease.reservation, **{key: 2 if key == 'epoch' else 'different'}))
    elif case == 'refresh': lease = replace(lease, purpose='refresh')
    else: lease = replace(lease, **{case + '_id': 'different'})
    monkeypatch.setattr(manager, '_source_bytes', lambda: pytest.fail('unauthorized source read'))
    with pytest.raises(LeaseError): manager.prepare(lease, **kwargs)
    assert not list(manager.root.iterdir())


@pytest.mark.parametrize('case', ['profile', 'cancel', 'uncertain', 'running', 'expiry', 'revoked'])
def test_dispatch_or_grant_fence_blocks_mount(staging, setup, case):
    manager, spec, kwargs, config = staging
    snapshot = manager.prepare(spec.lease, **kwargs)
    with manager.leases.store._tx() as db:
        if case == 'profile': db.execute("UPDATE provider_dispatch SET profile_digest=?", ('f' * 64,))
        elif case == 'cancel': db.execute('UPDATE provider_dispatch SET cancel_requested=1')
        elif case == 'uncertain': db.execute('UPDATE provider_dispatch SET uncertain=1')
        elif case == 'running': db.execute("UPDATE provider_dispatch SET state='running'")
        elif case == 'revoked': db.execute('UPDATE provider_execution_grants SET revoked_at=1000')
    if case == 'expiry': setup[-1][0] += 3601
    with pytest.raises(LeaseError): manager.mount_path(spec.lease, **kwargs)
    assert snapshot.path.exists()


def test_cancellation_during_copy_prevents_publication(staging, setup, monkeypatch):
    manager, spec, kwargs, config = staging
    original = module._read_native_auth
    def cancel_after_validation(*args):
        result = original(*args)
        manager.leases.revoke_grant(spec.lease.grant_id)
        return result
    monkeypatch.setattr(module, '_read_native_auth', cancel_after_validation)
    with pytest.raises(LeaseError): manager.prepare(spec.lease, **kwargs)
    assert not list(manager.root.iterdir())


def test_two_concurrent_stagers_have_one_publication(staging):
    manager, spec, kwargs, config = staging
    barrier = threading.Barrier(2)
    def stage():
        barrier.wait()
        try: return manager.prepare(spec.lease, **kwargs)
        except SnapshotError as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: stage(), range(2)))
    assert sum(isinstance(v, module.AuthSnapshot) for v in results) == 1
    assert 'snapshot_already_exists' in results
    assert len(list(manager.root.iterdir())) == 1


def test_refresh_cannot_acquire_while_snapshot_inference_lease_held(staging):
    manager, spec, kwargs, config = staging
    manager.prepare(spec.lease, **kwargs)
    with pytest.raises(LeaseError, match='request_lease_busy'):
        manager.leases.acquire_request(spec.lease.reservation, spec.lease.grant_id, purpose='refresh')


@pytest.mark.parametrize('case', ['mode', 'replace', 'hardlink', 'receipt', 'extra'])
def test_tamper_refuses_mount_or_cleanup_without_deleting_unknown(staging, setup, case):
    manager, spec, kwargs, config = staging
    snapshot = manager.prepare(spec.lease, **kwargs)
    if case == 'mode': snapshot.path.chmod(0o640)
    elif case == 'replace':
        snapshot.path.unlink(); snapshot.path.write_text('not-original'); snapshot.path.chmod(0o440)
    elif case == 'hardlink': os.link(snapshot.path, snapshot.path.with_name('alias'))
    elif case == 'receipt': (snapshot.path.parent / 'binding.json').write_text('{}')
    else: (snapshot.path.parent / 'unexpected').write_text('preserve-me')
    if case != 'extra':
        with pytest.raises(SnapshotError): manager.mount_path(spec.lease, **kwargs)
    setup[7].cleanup(spec.lease.request_id)
    with pytest.raises(SnapshotError): manager.cleanup(spec.lease, **kwargs)
    assert snapshot.path.exists()


def test_new_controller_refuses_mount_but_can_cleanup_released_old_snapshot(staging, setup):
    manager, spec, kwargs, config = staging
    snapshot = manager.prepare(spec.lease, **kwargs)
    restarted = ProviderLeases(setup[0], cleanup_verifier=setup[6].cleanup,
                              inspector_id='synthetic-inspector', clock=lambda: 1000.0)
    other = AuthSnapshots(restarted, **config)
    with pytest.raises(LeaseError): other.mount_path(spec.lease, **kwargs)
    with pytest.raises(SnapshotError): other.cleanup(spec.lease, **kwargs)
    setup[7].cleanup(spec.lease.request_id)
    other.cleanup(spec.lease, **kwargs)
    assert not snapshot.path.exists()


def test_setgid_private_roots_supported_and_group_access_refused(staging):
    manager, spec, kwargs, config = staging
    manager.root.chmod(0o2700); manager.dedicated_root.chmod(0o2700)
    assert manager.prepare(spec.lease, **kwargs).path.is_file()
    manager.root.chmod(0o2750)
    with pytest.raises(SnapshotError, match='unsafe_snapshot_directory'):
        manager.mount_path(spec.lease, **kwargs)


def test_partial_write_failure_cleans_only_new_stage(staging, monkeypatch):
    manager, spec, kwargs, config = staging
    unrelated = manager.root / 'unrelated'; unrelated.mkdir(); (unrelated / 'keep').touch()
    original = module.os.write
    def fail(fd, data):
        if bytes(data).startswith(b'{"binding"'): raise OSError('secret-looking-error-suppressed')
        return original(fd, data)
    monkeypatch.setattr(module.os, 'write', fail)
    with pytest.raises(SnapshotError) as caught: manager.prepare(spec.lease, **kwargs)
    assert str(caught.value) == 'snapshot_io_failed'
    assert list(manager.root.iterdir()) == [unrelated] and (unrelated / 'keep').exists()


def test_cleanup_retry_after_secret_unlink(staging, setup, monkeypatch):
    manager, spec, kwargs, config = staging
    snapshot = manager.prepare(spec.lease, **kwargs)
    setup[7].cleanup(spec.lease.request_id)
    original = module.os.unlink
    def fail_receipt(path, **options):
        if path == 'binding.json': raise OSError('synthetic interruption')
        return original(path, **options)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'unlink', fail_receipt)
        with pytest.raises(SnapshotError, match='cleanup_failed'):
            manager.cleanup(spec.lease, **kwargs)
    assert not snapshot.path.exists()
    assert (snapshot.path.parent / 'binding.json').exists()
    manager.cleanup(spec.lease, **kwargs)
    assert not snapshot.path.parent.exists()


@pytest.mark.parametrize('replace_inode', [False, True])
def test_source_write_during_read_fails_closed(staging, monkeypatch, replace_inode):
    manager, spec, kwargs, config = staging
    original = module.os.read
    changed = False
    def race(fd, amount):
        nonlocal changed
        result = original(fd, amount)
        if not changed:
            changed = True
            if replace_inode:
                replacement = manager.source.with_name('new')
                replacement.write_bytes(manager.source.read_bytes()); replacement.chmod(0o600)
                replacement.replace(manager.source)
            else:
                manager.source.write_text('source-was-changed')
        return result
    monkeypatch.setattr(module.os, 'read', race)
    with pytest.raises(SnapshotError, match='auth_source_changed'):
        manager.prepare(spec.lease, **kwargs)
    assert not list(manager.root.iterdir())


def test_other_generation_cannot_clean_released_snapshot(staging, setup):
    manager, spec, kwargs, config = staging
    snapshot = manager.prepare(spec.lease, **kwargs)
    setup[7].cleanup(spec.lease.request_id)
    with pytest.raises(SnapshotError, match='cleanup_unconfirmed'):
        manager.cleanup(spec.lease, **{**kwargs, 'generation': kwargs['generation'] + 1})
    assert snapshot.path.exists()


def test_changed_destination_symlink_refuses(staging):
    manager, spec, kwargs, config = staging
    previous = manager.root.with_name('original')
    manager.root.rename(previous); manager.root.symlink_to(previous)
    with pytest.raises(SnapshotError): manager.prepare(spec.lease, **kwargs)
    assert not list(previous.iterdir())


def test_pending_directory_open_failure_leaves_no_orphan(staging, monkeypatch):
    manager, spec, kwargs, config = staging
    original = module.os.open
    def fail_pending(path, flags, *args, **options):
        if isinstance(path, str) and path.startswith('.pending-'):
            raise OSError(24, 'synthetic descriptor exhaustion')
        return original(path, flags, *args, **options)
    monkeypatch.setattr(module.os, 'open', fail_pending)
    with pytest.raises(SnapshotError, match='snapshot_io_failed'):
        manager.prepare(spec.lease, **kwargs)
    assert not list(manager.root.iterdir())


def test_constructor_requires_actual_provider_group_membership(staging, monkeypatch):
    manager, spec, kwargs, config = staging
    monkeypatch.setattr(module.os, 'getgroups', lambda: [])
    other_gid = os.getegid() + 100001
    with pytest.raises(SnapshotError, match='provider_group_unavailable'):
        AuthSnapshots(manager.leases, **{**config, 'provider_gid': other_gid})


@pytest.mark.parametrize('access', ['primary', 'supplementary', 'root'])
def test_membership_preflight_supports_primary_supplementary_and_root(staging, monkeypatch, access):
    manager, spec, kwargs, config = staging
    gid = os.getegid() if access == 'primary' else os.getegid() + 100001
    monkeypatch.setattr(module.os, 'getgroups', lambda: [gid] if access == 'supplementary' else [])
    if access == 'root':
        monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
        config = {**config, 'source_uid': 0}
    assert AuthSnapshots(manager.leases, **{**config, 'provider_gid': gid}).gid == gid


def test_invalid_utf8_jwt_maps_to_fixed_auth_error(staging):
    manager, spec, kwargs, config = staging
    value = json.loads(manager.source.read_text())
    value['tokens']['access_token'] = 'header.' + base64.urlsafe_b64encode(b'\xff\xfe').decode().rstrip('=') + '.signature'
    manager.source.write_text(json.dumps(value))
    with pytest.raises(SnapshotError) as error: manager.prepare(spec.lease, **kwargs)
    assert str(error.value) == 'auth_invalid' and error.value.__suppress_context__
    assert not list(manager.root.iterdir())


@pytest.mark.parametrize('path', ['/home/example-operator/auth.json', '/Users/example-operator/auth.json', '/root/auth.json'])
def test_personal_paths_rejected_even_explicitly_configured(staging, path):
    manager, spec, kwargs, config = staging
    config.update(source=Path(path), dedicated_root=Path(path).parent)
    with pytest.raises(SnapshotError, match='personal_home_forbidden'):
        AuthSnapshots(manager.leases, **config)
