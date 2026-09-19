from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import time
import secrets
from types import SimpleNamespace

import pytest

from cloudworkbench.provider_auth_snapshot import AuthSnapshots
from cloudworkbench import provider_docker
from cloudworkbench.provider_dispatch import ProviderDispatch, DispatchError
from cloudworkbench.provider_docker import ProviderDocker, DockerConfig, DockerError
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.snapshot_provider_docker import SnapshotProviderDocker, OwnerSnapshotCleanupError
from test_provider_auth_snapshot import staging, setup
from test_provider_docker import FakeDocker


@pytest.fixture
def integrated(staging, setup, tmp_path, monkeypatch):
    original_manager, spec, kwargs, options = staging
    profile = tmp_path / 'profile.json'
    body = json.dumps(dict(transport='native-responses-v1', provider='openai-codex',
                          model='gpt-6-astra', effort='high')).encode()
    profile.write_bytes(body); profile.chmod(0o444)
    docker_root = tmp_path / 'docker'; docker_root.mkdir(mode=0o700)

    # macOS developer tests cannot chown root profiles or use Linux GID959.
    # Only Unix identity metadata is simulated; bytes, modes, leases, journals,
    # locks and atomic filesystem operations remain real. No live Docker here.
    identities = {(profile.stat().st_dev, profile.stat().st_ino): {'st_uid': 0}}
    original_stat, original_fstat = os.stat, os.fstat
    class Metadata:
        def __init__(self, info, fields): self.info, self.fields = info, fields
        def __getattr__(self, key): return self.fields.get(key, getattr(self.info, key))
    def metadata(info):
        fields = identities.get((info.st_dev, info.st_ino))
        return Metadata(info, fields) if fields else info
    def chown(fd, uid, gid):
        info = original_fstat(fd)
        fields = identities.setdefault((info.st_dev, info.st_ino), {})
        if uid != -1: fields['st_uid'] = uid
        if gid != -1: fields['st_gid'] = gid
    monkeypatch.setattr(os, 'fchown', chown)
    original_groups = os.getgroups()
    monkeypatch.setattr(os, 'getgroups', lambda: list(set(original_groups + [959])))
    monkeypatch.setattr(os, 'fstat', lambda fd: metadata(original_fstat(fd)))
    monkeypatch.setattr(os, 'stat', lambda *a, **kw: metadata(original_stat(*a, **kw)))

    options = {**options, 'provider_gid': 959}
    monkeypatch.setattr(provider_docker, 'time', SimpleNamespace(
        time=lambda: setup[-1][0], monotonic=time.monotonic, sleep=time.sleep))
    snapshots = AuthSnapshots(original_manager.leases, **options)
    config = DockerConfig('sha256:' + 'a' * 64, profile, hashlib.sha256(body).hexdigest(),
                          snapshots.profile.digest, snapshots.source, docker_root,
                          ('chatgpt.com',), inspector_id='synthetic-inspector')
    fake = FakeDocker()
    runtime = SnapshotProviderDocker(config, snapshots, cancel_check=lambda _: False, command=fake)
    leases = snapshots.leases
    leases.verifier = runtime.cleanup
    dispatcher = ProviderDispatch(leases, runtime)
    return runtime, dispatcher, spec, fake, kwargs


def test_admitted_dispatch_stages_before_rpc_and_removes_after_release(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    snapshots = runtime.snapshots
    path = snapshots.expected_path(spec.lease, **kwargs)
    original = snapshots.source.read_bytes()
    assert not path.exists()
    observed = []
    def hook(args):
        if args[0] == 'version':
            observed.append(path.read_bytes() == original)
            assert snapshots.leases._lock_local.held
    fake.hook = hook
    resources = dispatch.launch(spec.lease.request_id, b'payload')
    assert observed == [True]
    mounts = fake.objects[resources.provider_id]['Mounts']
    secret = next(m for m in mounts if m['Destination'] == '/run/secrets/native-auth.json')
    assert secret['Source'] == str(path) and secret['RW'] is False
    assert all(str(snapshots.source) not in arg for command in fake.commands for arg in command)
    assert all('synthetic-refresh-only' not in arg for command in fake.commands for arg in command)
    assert fake.objects[resources.gateway_id]['Mounts'] == []
    dispatch.collect(spec.lease.request_id)
    unrelated = snapshots.root / 'unrelated'; unrelated.mkdir(); (unrelated / 'keep').touch()
    dispatch.cleanup(spec.lease.request_id)
    assert not fake.objects and not path.parent.exists()
    assert snapshots.source.read_bytes() == original and (unrelated / 'keep').exists()
    dispatch.cleanup(spec.lease.request_id)
    assert dispatch.deliver(spec.lease.request_id) == fake.response


@pytest.mark.parametrize('target', ['source', 'snapshot'])
def test_missing_credential_does_not_block_inspect_collect_cleanup(integrated, target):
    runtime, dispatch, spec, fake, kwargs = integrated
    dispatch.launch(spec.lease.request_id, b'payload')
    path = runtime.snapshots.expected_path(spec.lease, **kwargs)
    (runtime.snapshots.source if target == 'source' else path).unlink()
    dispatch.collect(spec.lease.request_id)
    dispatch.cleanup(spec.lease.request_id)
    assert not fake.objects and not path.parent.exists()


def test_corrupt_snapshot_cannot_block_physical_cleanup_and_error_is_safe(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    dispatch.launch(spec.lease.request_id, b'payload')
    path = runtime.snapshots.expected_path(spec.lease, **kwargs)
    path.chmod(0o600); path.write_text('unknown-external-content'); path.chmod(0o440)
    dispatch.collect(spec.lease.request_id)
    with pytest.raises(DispatchError, match='post_cleanup_material_pending'):
        dispatch.cleanup(spec.lease.request_id)
    assert not fake.objects and dispatch.read(spec.lease.request_id)['state'] == 'cleaned'
    with runtime.snapshots.leases.store._connect() as db:
        assert db.execute('SELECT state FROM provider_request_leases WHERE id=?',
                          (spec.lease.request_id,)).fetchone()[0] == 'released'
    count = len(fake.commands)
    with pytest.raises(DispatchError, match='post_cleanup_material_pending'):
        dispatch.cleanup(spec.lease.request_id)
    assert len(fake.commands) == count and path.read_text() == 'unknown-external-content'


def test_snapshot_schema_failure_has_durable_no_rpc_reconciliation(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    runtime.snapshots.source.write_text('unsupported-synthetic-auth')
    with pytest.raises(DispatchError, match='create_outcome_unknown'):
        dispatch.launch(spec.lease.request_id, b'payload')
    assert fake.commands == []
    journal = runtime._load(spec)
    assert journal['resources'] == {} and journal['pending'] is None
    dispatch.reconcile(spec.lease.request_id)
    dispatch.cleanup(spec.lease.request_id)
    assert dispatch.read(spec.lease.request_id)['state'] == 'cleaned'
    assert not fake.commands and not list(runtime.snapshots.root.iterdir())


def test_unknown_docker_create_keeps_private_snapshot_and_lease(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    def fail(args):
        if args[:2] == ['network', 'create']: raise TimeoutError('unknown')
    fake.hook = fail
    with pytest.raises(DispatchError): dispatch.launch(spec.lease.request_id, b'payload')
    with pytest.raises(DispatchError, match='operation_still_unknown'): dispatch.reconcile(spec.lease.request_id)
    with pytest.raises(DispatchError, match='operation_still_unknown'): dispatch.cleanup(spec.lease.request_id)
    assert runtime.snapshots.expected_path(spec.lease, **kwargs).exists()
    assert runtime.snapshots.leases.active_request(spec.lease.reservation) is not None


def test_snapshot_removed_between_create_and_start_refuses_execution_but_cleans(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    path = runtime.snapshots.expected_path(spec.lease, **kwargs)
    def remove_before_start(args):
        if args[0] == 'create' and spec.provider_name in args: path.unlink()
    fake.hook = remove_before_start
    with pytest.raises(DispatchError, match='start_outcome_unknown'):
        dispatch.launch(spec.lease.request_id, b'payload')
    assert not any(args[0] == 'start' for args in fake.commands)
    dispatch.reconcile(spec.lease.request_id)
    dispatch.cleanup(spec.lease.request_id)
    assert not fake.objects and not path.parent.exists()


def test_restart_cleanup_uses_exact_journal_without_reauth(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    dispatch.launch(spec.lease.request_id, b'payload')
    path = runtime.snapshots.expected_path(spec.lease, **kwargs)
    runtime.snapshots.source.unlink()
    runtime.config.profile_path.chmod(0o600); runtime.config.profile_path.write_bytes(b'corrupt')
    rebuilt = SnapshotProviderDocker(runtime.config, runtime.snapshots,
                                     cancel_check=lambda _: False, command=fake)
    leases = runtime.snapshots.leases
    leases.verifier = rebuilt.cleanup
    dispatcher = ProviderDispatch(leases, rebuilt)
    dispatcher.cleanup(spec.lease.request_id)
    assert not fake.objects and not path.exists()


def test_hook_retry_after_durable_release_does_not_repeat_docker_cleanup(integrated, monkeypatch):
    runtime, dispatch, spec, fake, kwargs = integrated
    dispatch.launch(spec.lease.request_id, b'payload')
    with monkeypatch.context() as patch:
        patch.setattr(runtime.snapshots, 'cleanup', lambda *a, **kw: (_ for _ in ()).throw(OSError('secret-looking message')))
        with pytest.raises(DispatchError) as error: dispatch.cleanup(spec.lease.request_id)
        assert str(error.value) == 'post_cleanup_material_pending'
    assert not fake.objects
    count = len(fake.commands)
    dispatch.cleanup(spec.lease.request_id)
    assert len(fake.commands) == count
    assert not runtime.snapshots.expected_path(spec.lease, **kwargs).exists()


def test_recovery_only_adapter_denies_all_execution_entries(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    runtime.snapshots.source.unlink()
    recovery = ProviderDocker(runtime.config, cancel_check=lambda _: False, command=fake,
                              recovery_only=True, credential_target='/run/secrets/native-auth.json')
    for method, args in ((recovery.create, (spec, b'payload')),
                         (recovery.create_prepared, (spec, b'payload')),
                         (recovery.start, (spec, None))):
        with pytest.raises(DockerError, match='recovery_adapter_cannot_execute'):
            method(*args, deadline=time.monotonic() + 1)
    assert fake.commands == []


def test_prepared_journal_cannot_replay_after_docker_effects(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    dispatch.launch(spec.lease.request_id, b'payload')
    config = replace(runtime.config, credential_path=runtime.snapshots.expected_path(spec.lease, **kwargs))
    execution = ProviderDocker(config, cancel_check=lambda _: False, command=fake)
    count = len(fake.commands)
    with pytest.raises(DockerError, match='journal_not_pristine'):
        execution.create_prepared(spec, b'payload', deadline=time.monotonic() + 1)
    assert len(fake.commands) == count


def test_incompatible_snapshot_config_refuses(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    with pytest.raises(DockerError, match='snapshot_configuration_mismatch'):
        SnapshotProviderDocker(replace(runtime.config, profile_digest='b' * 64), runtime.snapshots,
                               cancel_check=lambda _: False, command=fake)


def test_dispatch_refuses_different_lease_manager_before_lock_or_admission(integrated, monkeypatch):
    runtime, dispatch, spec, fake, kwargs = integrated
    first = runtime.snapshots.leases
    second = ProviderLeases(first.store, cleanup_verifier=runtime.cleanup,
                            inspector_id=runtime.config.inspector_id)
    with first.store._connect() as db:
        before = db.execute('SELECT COUNT(*) FROM provider_dispatch').fetchone()[0]
    monkeypatch.setattr(second, 'account_lock', lambda *a, **kw: pytest.fail('must refuse before locking'))
    with first.account_lock(spec.lease.reservation.account_id):
        with pytest.raises(DockerError, match='snapshot_lease_manager_mismatch'):
            ProviderDispatch(second, runtime)
    assert fake.commands == [] and not list(runtime.snapshots.root.iterdir())
    with first.store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM provider_dispatch').fetchone()[0] == before
    assert ProviderDispatch(first, runtime).leases is first


def test_explicit_owner_hook_requires_durable_proof_and_preserves_new_owner(integrated):
    runtime, dispatch, spec, fake, kwargs = integrated
    leases = runtime.snapshots.leases
    dispatch.launch(spec.lease.request_id, b'payload')
    old_path = runtime.snapshots.expected_path(spec.lease, **kwargs)
    targets = []
    def verifier(target):
        targets.append(target)
        with pytest.raises(DockerError, match='owner_cleanup_unconfirmed'):
            runtime.after_owner_cleanup(target)
        return runtime.cleanup(target)
    leases.verifier = verifier
    leases.cleanup_owner(spec.lease.reservation)
    assert not fake.objects and old_path.exists()  # explicit hook is not wired into owner cleanup
    leases.verifier = runtime.cleanup
    owner = leases.reserve('account', persistent_owner_id='logical-owner')
    grant = leases.issue_grant(owner, attempt_id=spec.attempt_id, generation=spec.generation)
    newer = dispatch.admit(owner, grant, request_nonce=secrets.token_hex(32), payload=b'new',
                           profile_digest=runtime.config.profile_digest)
    dispatch.launch(newer.lease.request_id, b'new')
    new_path = runtime.snapshots.expected_path(newer.lease, **kwargs)
    objects = set(fake.objects)
    assert runtime.after_owner_cleanup(targets[-1]) == (spec.lease.request_id,)
    assert not old_path.exists() and new_path.exists() and set(fake.objects) == objects
    assert leases.active_request(owner) == newer.lease
    assert runtime.after_owner_cleanup(targets[-1]) == (spec.lease.request_id,)
    with pytest.raises(DockerError, match='owner_cleanup_unconfirmed'):
        runtime.after_owner_cleanup(replace(targets[-1], reservation=owner))


def test_owner_hook_continues_only_exact_rows_and_reports_typed_pending(integrated):
    runtime, dispatch, first, fake, kwargs = integrated
    leases = runtime.snapshots.leases
    dispatch.launch(first.lease.request_id, b'payload')
    first_path = runtime.snapshots.expected_path(first.lease, **kwargs)
    leases.cleanup_request(first.lease)  # deliberately retain material for owner sweep
    second = dispatch.admit(first.lease.reservation, first.lease.grant_id,
                            request_nonce=secrets.token_hex(32), payload=b'second',
                            profile_digest=runtime.config.profile_digest)
    dispatch.launch(second.lease.request_id, b'second')
    second_path = runtime.snapshots.expected_path(second.lease, **kwargs)
    targets = []
    def verifier(target):
        targets.append(target)
        return runtime.cleanup(target)
    leases.verifier = verifier; leases.cleanup_owner(first.lease.reservation)
    # The first SQL row must fail, proving the later exact row still cleans.
    rows = sorted([(first.lease.request_id, first_path), (second.lease.request_id, second_path)])
    rows[0][1].parent.chmod(0o750)
    with pytest.raises(OwnerSnapshotCleanupError) as error:
        runtime.after_owner_cleanup(targets[-1])
    assert str(error.value) == 'snapshot_owner_material_pending'
    assert error.value.pending_request_ids == (rows[0][0],)
    assert error.value.checked_request_ids == (rows[1][0],)
    assert rows[0][1].exists() and not rows[1][1].exists() and not fake.objects


def test_recovery_cannot_skip_real_pending_snapshot_material(integrated,monkeypatch):
    from cloudworkbench.provider_recovery import recover_request
    from cloudworkbench.inference_relay import AttemptBinding
    runtime,dispatch,spec,fake,kwargs=integrated
    dispatch.launch(spec.lease.request_id,b'payload')
    path=runtime.snapshots.expected_path(spec.lease,**kwargs)
    binding=AttemptBinding(spec.attempt_id,spec.generation,spec.profile_digest)
    def recover():return recover_request(dispatch,spec.lease.request_id,reservation=spec.lease.reservation,grant_id=spec.lease.grant_id,binding=binding)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.snapshots,'cleanup',lambda *a,**kw:(_ for _ in ()).throw(OSError('synthetic pending material')))
        with pytest.raises(DispatchError):dispatch.cleanup(spec.lease.request_id)
        assert path.exists() and not fake.objects
        count=len(fake.commands)
        with pytest.raises(DispatchError,match='post_cleanup_material_pending'):recover()
        assert path.exists() and len(fake.commands)==count
    assert recover().cleanup_confirmed
    assert not path.exists() and len(fake.commands)==count
    assert dispatch.leases.current(spec.lease.reservation.account_id)['state']=='held'
