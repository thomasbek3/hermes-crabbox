from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from tests.test_routed_driver import driven, stage, setup, run
from cloudworkbench import routed_observation_store as snapshots
from cloudworkbench import routed_cleanup
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.routed_publication import PublicationError
from cloudworkbench.scheduler import RoleScheduler
from cloudworkbench.store import Store, StoreError


def snapshot_path(driven):
    return snapshots._location(driven[4].root, driven[5])


def test_driver_persists_exact_bytes_before_stop_and_loader_uses_only_disk(driven, monkeypatch, tmp_path):
    scheduler, _, _, _, runtime, spec, docker, reader = driven
    original = runtime.stop_caller
    seen = []
    def stop(*args):
        folder = snapshot_path(driven)
        assert set(p.name for p in folder.iterdir()) == snapshots._FILES
        assert (folder/'result.json').read_bytes() == reader.files['result']
        assert (folder/'events.jsonl').read_bytes() == reader.files['events']
        assert folder.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir())
        seen.append(folder)
        return original(*args)
    monkeypatch.setattr(runtime, 'stop_caller', stop)
    outcome = run(driven)
    assert len(seen) == 1 and outcome.execution_status == 'execution_observed'
    expected = outcome.observation
    context = replace(outcome, observation=None)
    copied = tmp_path/'retained-private-snapshots';copied.mkdir(mode=0o700)
    shutil.copytree(seen[0], copied/seen[0].name)
    reader.files.clear()
    commands = list(docker.calls)
    # New Store/scheduler handles re-read the file without carrying raw bytes;
    # current controller ownership remains intentionally required.
    reopened = RoleScheduler(Store(scheduler.store.path), scheduler.leases, clock=scheduler.clock)
    loaded = snapshots.load_observation(reopened, copied, spec, context)
    assert loaded == expected and not loaded.verification_pass
    assert docker.calls == commands and not docker.objects
    with scheduler.store._connect() as db:
        assert not db.execute('SELECT 1 FROM artifacts').fetchone()
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()


@pytest.mark.parametrize('mutation', ['truncate','symlink','hardlink','mode','manifest','extra','directory_link'])
def test_snapshot_tampering_refuses_recovery(driven, tmp_path, mutation):
    outcome=run(driven);folder=snapshot_path(driven)
    if mutation=='truncate': (folder/'events.jsonl').write_bytes(b'')
    elif mutation=='symlink':
        path=folder/'events.jsonl';other=tmp_path/'events';path.rename(other);path.symlink_to(other)
    elif mutation=='hardlink': os.link(folder/'result.json',tmp_path/'result-link')
    elif mutation=='mode': (folder/'manifest.json').chmod(0o644)
    elif mutation=='manifest':
        path=folder/'manifest.json';body=json.loads(path.read_bytes());body['binding']['runtime_id']='f'*64
        path.write_text(json.dumps(body))
    elif mutation=='extra': (folder/'unexpected').touch()
    else:
        other=tmp_path/'moved';folder.rename(other);folder.symlink_to(other,target_is_directory=True)
    with pytest.raises((ValueError,OSError)):
        snapshots.load_observation(driven[0],driven[4].root,driven[5],replace(outcome,observation=None))


@pytest.mark.parametrize('mutation',['spec','runtime','generation','launch_digest','cleanup'])
def test_recovery_refuses_context_substitution(driven, mutation):
    outcome=run(driven);spec=driven[5]
    if mutation=='spec': spec=replace(spec,image='sha256:'+'f'*64)
    elif mutation=='runtime': outcome=replace(outcome,runtime_id='f'*64)
    elif mutation=='generation': outcome=replace(outcome,generation=2)
    elif mutation=='launch_digest': outcome=replace(outcome,binding_digest='f'*64)
    else: outcome=replace(outcome,grant_fence_confirmed=False)
    with pytest.raises((ValueError,StoreError)):
        snapshots.load_observation(driven[0],driven[4].root,spec,outcome)


def test_new_controller_cannot_adopt_snapshot_ownership(driven):
    outcome=run(driven);scheduler=driven[0]
    leases=ProviderLeases(scheduler.store,cleanup_verifier=lambda _:None,inspector_id='new',clock=scheduler.clock)
    successor=RoleScheduler(scheduler.store,leases,clock=scheduler.clock)
    with pytest.raises(PublicationError,match='authority_unavailable'):
        snapshots.load_observation(successor,driven[4].root,driven[5],replace(outcome,observation=None))


def test_cancel_after_save_refuses_reload_without_losing_private_bytes(driven):
    outcome=run(driven);scheduler=driven[0]
    with scheduler.store._tx() as db:
        db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(driven[5].attempt_id,))
    with pytest.raises(PublicationError):
        snapshots.load_observation(scheduler,driven[4].root,driven[5],outcome)
    assert (snapshot_path(driven)/'result.json').exists()


def test_atomic_directory_publish_failure_still_fences_and_removes_caller(driven,monkeypatch):
    def failed(*args): raise OSError('synthetic private publication failure')
    monkeypatch.setattr(routed_cleanup,'_publish_exclusive',failed)
    outcome=run(driven)
    assert outcome.execution_status=='observation_persistence_failed'
    assert outcome.observation is None and outcome.grant_fence_confirmed
    assert outcome.caller_cleanup['caller_removed'] and not driven[6].objects
    assert not snapshot_path(driven).exists()
    assert not list(driven[4].root.glob('.observation-*.pending'))


def test_postpublish_cancel_keeps_snapshot_but_returns_no_observation(driven,monkeypatch):
    original=routed_cleanup._publish_exclusive
    def cancel(source,target):
        original(source,target)
        with driven[0].store._tx() as db:
            db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(driven[5].attempt_id,))
    monkeypatch.setattr(routed_cleanup,'_publish_exclusive',cancel)
    outcome=run(driven)
    assert outcome.execution_status=='authority_unavailable' and outcome.observation is None
    assert outcome.caller_cleanup['caller_removed'] and outcome.grant_fence_confirmed
    assert (snapshot_path(driven)/'manifest.json').is_file()


def test_load_refuses_cancel_during_file_read(driven,monkeypatch):
    outcome=run(driven);original=snapshots._load
    def cancelled(*args):
        value=original(*args)
        with driven[0].store._tx() as db:
            db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(driven[5].attempt_id,))
        return value
    monkeypatch.setattr(snapshots,'_load',cancelled)
    with pytest.raises(PublicationError):
        snapshots.load_observation(driven[0],driven[4].root,driven[5],outcome)


def test_save_replay_is_exact_and_conflicting_bytes_do_not_replace(driven,monkeypatch):
    original=snapshots.save_observation;receipts=[]
    def repeated(*args, **kwargs):
        receipt=original(*args, **kwargs);assert original(*args, **kwargs)==receipt;receipts.append(receipt)
        changed=replace(args[-1],stderr_bytes=args[-1].stderr_bytes+1)
        with pytest.raises(ValueError): original(*args[:-1],changed, **kwargs)
        return receipt
    monkeypatch.setattr(snapshots,'save_observation',repeated)
    outcome=run(driven)
    assert outcome.execution_status=='execution_observed' and len(receipts)==1
    assert snapshots.load_observation(driven[0],driven[4].root,driven[5],outcome)==outcome.observation


@pytest.mark.parametrize('escaped', [False, True])
def test_secret_refusal_writes_no_private_output_and_still_cleans(driven, monkeypatch, escaped):
    raw = driven[-1].files['events']
    if escaped:
        raw = raw.replace(b'done', b'\\u0064\\u006f\\u006e\\u0065')
        assert b'done' not in raw
        driven[-1].files['events'] = raw
    def unexpected_staging(*args, **kwargs):
        pytest.fail('secret reached private staging')
    monkeypatch.setattr(snapshots.tempfile, 'mkdtemp', unexpected_staging)
    outcome = run(driven, forbidden_values=(b'done',))
    assert outcome.execution_status == 'observation_persistence_failed'
    assert outcome.observation is None and outcome.grant_fence_confirmed
    assert outcome.caller_cleanup['caller_removed'] and not driven[6].objects
    assert not snapshot_path(driven).exists()
    assert not list(driven[4].root.glob('.observation-*.pending'))


@pytest.mark.parametrize('policy', [None, b'secret', 'secret', {b'secret'}, (b'',),
    ('secret',), (bytearray(b'secret'),), (b'x' * 4097,), (b'x',) * 65,
    (b'x' * 4096,) * 17])
def test_invalid_policy_refused_before_any_context_or_early_exit(policy):
    from cloudworkbench.routed_driver import drive_prepared_child
    for invoke in (
        lambda: snapshots.save_observation(None,None,None,None,None,None,forbidden_values=policy),
        lambda: snapshots.load_observation(None,None,None,None,forbidden_values=policy),
        lambda: drive_prepared_child(None,None,None,None,execution_profile=None,forbidden_values=policy),
    ):
        with pytest.raises(snapshots.ObservationStoreError) as exc:
            invoke()
        assert str(exc.value) == 'observation_secret_policy_invalid'


@pytest.mark.parametrize('raw', [
    b'{"key":"prefix-\\u0073ecret-suffix"}',
    b'{"\\u0073ecret":"value"}',
    b'{"nested":[{"value":"\\u0073ecret"}]}',
    b'{"value":"caf\\u00e9-secret"}',
])
def test_decoded_keys_nested_fields_and_unicode_are_scanned(raw):
    with pytest.raises(snapshots.ObservationStoreError) as exc:
        snapshots.scan_output_secrets(raw,b'',forbidden_values=(b'secret',))
    assert str(exc.value) == 'observation_secret_refused'
    assert 'prefix' not in str(exc.value)


def test_recovery_stricter_policy_refuses_without_modifying_snapshot(driven):
    outcome = run(driven, forbidden_values=(b'absent-private-value',))
    folder = snapshot_path(driven)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    with pytest.raises(snapshots.ObservationStoreError, match='^observation_secret_refused$'):
        snapshots.load_observation(driven[0],driven[4].root,driven[5],outcome,
                                   forbidden_values=(b'done',))
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert snapshots.load_observation(driven[0],driven[4].root,driven[5],outcome,
                                     forbidden_values=(b'absent-private-value',)) == outcome.observation


def test_mutable_controller_policy_is_frozen_before_launch(driven, monkeypatch):
    policy = [b'done']
    create = driven[4].create_caller
    def changed(*args):
        policy.clear()
        return create(*args)
    monkeypatch.setattr(driven[4], 'create_caller', changed)
    outcome = run(driven, forbidden_values=policy)
    assert outcome.execution_status == 'observation_persistence_failed'
    assert not snapshot_path(driven).exists()


def test_allowed_snapshot_preserves_original_formatting_and_bytes(driven):
    reader=driven[-1]
    reader.files['result'] = json.dumps(reader.result, indent=2).encode()+b'\n'
    outcome=run(driven,forbidden_values=(b'absent-private-value',))
    assert outcome.execution_status == 'execution_observed'
    folder=snapshot_path(driven)
    assert (folder/'result.json').read_bytes() == reader.files['result']
    assert (folder/'events.jsonl').read_bytes() == reader.files['events']
