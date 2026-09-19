"""Atomic root delivery over the actual six-stage local controller composition."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from cloudworkbench.provider_leases import CleanupReceipt, ProviderLeases
from cloudworkbench.routed_delivery import (DeliveryError, prepare_root_delivery, finalize_root_delivery,
    load_root_delivery, cleanup_cancelled_root_delivery)
from cloudworkbench.scheduler import RoleScheduler, RootCleanupReceipt
from cloudworkbench.store import Store, StoreError, encode
from tests.test_delivery_cleanup import seed


@pytest.fixture
def ready(seed, tmp_path):
    original, _ = seed
    path = tmp_path / 'state.db'
    with original.store._connect() as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    store = Store(path); provider_calls = []
    def provider(target):
        provider_calls.append(target)
        return CleanupReceipt(target, 'synthetic-inspector', 'terminated', 1000, 'a' * 64)
    leases = ProviderLeases(store, cleanup_verifier=provider, inspector_id='synthetic-inspector', clock=lambda: 1000)
    leases.instance_id = original.s.leases.instance_id
    scheduler = RoleScheduler(store, leases, clock=lambda: 1000)
    storage = tmp_path / 'delivery'; storage.mkdir(mode=0o700)
    runtime_calls = []
    def verifier(target):
        runtime_calls.append(target)
        return RootCleanupReceipt(target, 'terminated', 1000, 'c' * 64)
    return SimpleNamespace(store=store, s=scheduler, root=original.root['attempt_id'], owner=original.owner,
        session=original.root['session_id'], revision=original.revision, storage=storage,
        provider_calls=provider_calls, runtime_calls=runtime_calls, verifier=verifier)


def prepare(v, **kw):
    return prepare_root_delivery(v.s, v.root, expected_generation=1, expected_delivery_version=kw.pop('version', 0),
        expected_base_revision_sha256=kw.pop('base', None), selected_revision=v.revision,
        storage_root=v.storage, forbidden_values=(), **kw)


def session(v):
    with v.store._connect() as db:
        return dict(db.execute('SELECT * FROM sessions WHERE id=?', (v.session,)).fetchone())


def state(v):
    with v.store._connect() as db:
        return {'root': dict(db.execute('SELECT * FROM workflow_roots WHERE root_id=?', (v.root,)).fetchone()),
            'attempt': dict(db.execute('SELECT * FROM attempts WHERE id=?', (v.root,)).fetchone()),
            'session': session(v),
            'delivery_count': db.execute("SELECT COUNT(*) FROM artifacts WHERE json_extract(metadata,'$.provenance')='controller_root_delivery'").fetchone()[0],
            'events': [dict(r) for r in db.execute('SELECT * FROM events WHERE attempt_id=?', (v.root,))]}


def test_complete_sixstage_delivers_and_historical_replay_is_inert(ready):
    v = ready; before = session(v); p = prepare(v)
    assert prepare(v) == p
    assert session(v) == before and v.store.get_attempt(v.root)['state'] == 'verifying'
    result = finalize_root_delivery(v.s, p, cleanup_verifier=v.verifier)
    current = session(v)
    assert result.kind == 'workspace' and result.outcome == 'verified'
    assert current['delivery_version'] == result.delivery_version == 1
    assert current['delivered_revision_sha256'] == result.selected_revision_sha256 == v.revision.sha256
    assert current['delivered_artifact_id'] == current['last_delivery_artifact_id'] == result.artifact_id
    assert v.store.get_attempt(v.root)['state'] == 'completed'
    assert v.s.leases.current('account') is None
    final = state(v)
    assert load_root_delivery(v.s, v.root, expected_generation=1) == result
    assert finalize_root_delivery(v.s, p, cleanup_verifier=lambda _: pytest.fail('Replay cleanup')) == result
    assert state(v) == final and len(v.runtime_calls) == len(v.provider_calls) == 1
    with v.store._connect() as db:
        meta = json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?', (result.artifact_id,)).fetchone()[0])
        public = json.loads(db.execute("SELECT payload FROM events WHERE type='workflow.delivery'").fetchone()[0])
    assert 'storage_path' not in public
    assert str(v.storage) not in open(meta['storage_path']).read()


@pytest.mark.parametrize('point', ['artifact.created', 'attempt.state', 'workflow.delivery'])
def test_final_publication_failure_rolls_back_pointer_terminal_and_release(ready, monkeypatch, point):
    v = ready; p = prepare(v); initial = session(v)
    original = v.store._event
    def fail(db, sid, aid, kind, payload):
        if aid == v.root and kind == point:
            raise RuntimeError('injected publication failure')
        return original(db, sid, aid, kind, payload)
    with monkeypatch.context() as patch:
        patch.setattr(v.store, '_event', fail)
        with pytest.raises(RuntimeError, match='injected publication failure'):
            finalize_root_delivery(v.s, p, cleanup_verifier=v.verifier)
    assert session(v) == initial and v.store.get_attempt(v.root)['state'] == 'verifying'
    assert state(v)['delivery_count'] == 0
    assert v.s.leases.current('account')['state'] == 'cleaning'
    result = finalize_root_delivery(v.s, p, cleanup_verifier=lambda _: pytest.fail('Repeat verified cleanup'))
    assert result.outcome == 'verified' and len(v.runtime_calls) == len(v.provider_calls) == 1


def test_unknown_cleanup_keeps_pointer_and_capacity_then_reconciles_exact_target(ready):
    v = ready; p = prepare(v); seen = []
    def unknown(target):
        seen.append(target); return None
    with pytest.raises(StoreError, match='unconfirmed'):
        finalize_root_delivery(v.s, p, cleanup_verifier=unknown)
    assert session(v)['delivery_version'] == 0 and v.s.leases.current('account')['state'] == 'held'
    result = finalize_root_delivery(v.s, p, cleanup_verifier=v.verifier)
    assert result.outcome == 'verified' and seen == v.runtime_calls


@pytest.mark.parametrize('when', ['before_prepare_cleanup', 'after_prepare_cleanup'])
def test_cancellation_cleans_without_publishing_and_replays(ready, when):
    v = ready; p = prepare(v); before = session(v)
    if when == 'after_prepare_cleanup':
        v.s.prepare_delivery_cleanup(v.root, expected_generation=1, intent_sha256=p.intent_sha256, verifier=v.verifier)
    v.store.cancel(v.owner, v.root, 'cancel')
    with pytest.raises(DeliveryError, match='authority'):
        finalize_root_delivery(v.s, p, cleanup_verifier=v.verifier)
    result = cleanup_cancelled_root_delivery(v.s, p, cleanup_verifier=v.verifier)
    assert result['release_purpose'] == 'cancelled_delivery'
    assert session(v) == before and v.store.get_attempt(v.root)['state'] == 'cancelled'
    assert v.s.leases.current('account') is None and state(v)['delivery_count'] == 0
    before_replay = state(v)
    assert cleanup_cancelled_root_delivery(v.s, p, cleanup_verifier=lambda _: pytest.fail('Replay cleanup')) == result
    assert state(v) == before_replay and len(v.runtime_calls) == len(v.provider_calls) == 1


@pytest.mark.parametrize('change', ['revoke', 'project', 'archive', 'deadline', 'stale_base'])
def test_authority_change_after_physical_cleanup_cannot_promote(ready, change):
    v = ready; p = prepare(v); original = session(v)
    v.s.prepare_delivery_cleanup(v.root, expected_generation=1, intent_sha256=p.intent_sha256, verifier=v.verifier)
    with v.store._tx() as db:
        if change == 'revoke': db.execute("UPDATE clients SET revoked_at='revoked' WHERE id=?", (v.owner['id'],))
        if change == 'project': db.execute("UPDATE clients SET projects='[]' WHERE id=?", (v.owner['id'],))
        if change == 'archive': db.execute('UPDATE sessions SET archived=1 WHERE id=?', (v.session,))
        if change == 'deadline': db.execute('UPDATE workflow_roots SET deadline_at=999 WHERE root_id=?', (v.root,))
        if change == 'stale_base':
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?)', ('other-delivery', v.session, v.root, '{}'))
            db.execute("UPDATE sessions SET delivery_version=1,last_delivery_artifact_id='other-delivery' WHERE id=?", (v.session,))
    before = session(v)
    with pytest.raises(DeliveryError): finalize_root_delivery(v.s, p, cleanup_verifier=lambda _: pytest.fail('Repeat cleanup'))
    assert session(v) == before and v.store.get_attempt(v.root)['state'] == 'verifying'
    assert state(v)['delivery_count'] == 0 and v.s.leases.current('account')['state'] == 'cleaning'


def test_replay_survives_new_account_owner_and_newer_session_pointer(ready):
    v = ready; p = prepare(v); result = finalize_root_delivery(v.s, p, cleanup_verifier=v.verifier)
    owner = v.s.leases.reserve('account', persistent_owner_id='owner')
    with v.store._tx() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?)', ('newer', v.session, v.root, '{}'))
        db.execute("UPDATE sessions SET delivery_version=2,delivered_revision_sha256=?,delivered_artifact_id='newer',last_delivery_artifact_id='newer' WHERE id=?", ('b'*64, v.session))
    assert finalize_root_delivery(v.s, p, cleanup_verifier=lambda _: pytest.fail('Old cleanup')) == result
    assert v.s.leases.current('account')['reservation'] == owner and session(v)['delivery_version'] == 2


def test_concurrent_finalizers_publish_exactly_once(ready):
    v = ready; p = prepare(v)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(finalize_root_delivery, v.s, p, cleanup_verifier=v.verifier) for _ in range(2)]
        results = []
        for future in futures:
            try:
                results.append(future.result())
            except StoreError as error:
                assert error.status_code == 503
    assert results and all(result == results[0] for result in results)
    assert load_root_delivery(v.s, v.root, expected_generation=1) == results[0]
    assert session(v)['delivery_version'] == 1
    assert len(v.runtime_calls) == len(v.provider_calls) == 1


def test_prepare_conflicting_base_or_selected_revision_refused(ready):
    v = ready
    with pytest.raises(DeliveryError, match='base_changed'): prepare(v, version=1)
    p = prepare(v)
    with pytest.raises(DeliveryError, match='prepare_conflict'): prepare(v, base='f'*64)
    with pytest.raises(DeliveryError, match='intent_changed'):
        finalize_root_delivery(v.s, replace(p, intent_sha256='f'*64), cleanup_verifier=v.verifier)
    assert not v.runtime_calls


def test_cancelled_final_event_failure_rolls_back_release(ready, monkeypatch):
    v = ready; p = prepare(v); before = session(v)
    v.store.cancel(v.owner, v.root, 'cancel-event-fault')
    original = v.store._event
    def fail(db, sid, aid, kind, payload):
        if kind == 'workflow.delivery_cancelled_cleanup':
            raise RuntimeError('cancel event fault')
        return original(db, sid, aid, kind, payload)
    with monkeypatch.context() as patch:
        patch.setattr(v.store, '_event', fail)
        with pytest.raises(RuntimeError, match='cancel event fault'):
            cleanup_cancelled_root_delivery(v.s, p, cleanup_verifier=v.verifier)
    assert session(v) == before and v.store.get_attempt(v.root)['state'] == 'verifying'
    assert v.s.leases.current('account')['state'] == 'cleaning'
    assert state(v)['delivery_count'] == 0
    result = cleanup_cancelled_root_delivery(v.s, p, cleanup_verifier=lambda _: pytest.fail('Repeated cleanup'))
    assert result['release_purpose'] == 'cancelled_delivery'
    assert session(v) == before and len(v.runtime_calls) == len(v.provider_calls) == 1


def test_staged_delivery_tamper_refuses_before_cleanup(ready):
    v = ready; p = prepare(v); before = session(v)
    with v.store._connect() as db:
        envelope = json.loads(db.execute('SELECT proof_json FROM workflow_delivery_intents WHERE root_id=?', (v.root,)).fetchone()[0])
    path = Path(envelope['artifact']['storage_path'])
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'workspace', b'workspacE'))
    with pytest.raises(DeliveryError, match='artifact_changed'):
        finalize_root_delivery(v.s, p, cleanup_verifier=v.verifier)
    assert session(v) == before and not v.runtime_calls and not v.provider_calls
