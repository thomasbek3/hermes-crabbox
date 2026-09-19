"""Fault receipts after real commits; no provider execution or production mutation."""
from contextlib import contextmanager

import pytest

from cloudworkbench import routed_delivery as delivery
from cloudworkbench.store import StoreError
from tests.test_delivery_cleanup import seed
from tests.test_routed_delivery import ready, prepare, session, state


def lose_delivery_commit_ack(monkeypatch, value, event_type):
    original = delivery._child_control_tx
    lost = []

    @contextmanager
    def transaction(store, *, write=True):
        committed_delivery = False
        with original(store, write=write) as db:
            before = db.execute('SELECT count(*) FROM events WHERE attempt_id=? AND type=?',
                                (value.root, event_type)).fetchone()[0]
            yield db
            after = db.execute('SELECT count(*) FROM events WHERE attempt_id=? AND type=?',
                               (value.root, event_type)).fetchone()[0]
            committed_delivery = write and not lost and before == 0 and after == 1
        # The real context has now acknowledged its COMMIT and closed the DB.
        # Only the outer delivery call loses that acknowledgement.
        if committed_delivery:
            lost.append(event_type)
            raise StoreError(503, 'Synthetic lost delivery commit acknowledgement')

    monkeypatch.setattr(delivery, '_child_control_tx', transaction)
    return lost


def no_runtime(_):
    pytest.fail('Historical retry must not call runtime cleanup')


def test_completed_delivery_lost_commit_ack_replays_inertly(ready, monkeypatch):
    value = ready
    prepared = prepare(value)
    lost = lose_delivery_commit_ack(monkeypatch, value, 'workflow.delivery')
    with pytest.raises(StoreError, match='Synthetic lost delivery commit acknowledgement'):
        delivery.finalize_root_delivery(value.s, prepared, cleanup_verifier=value.verifier)
    assert lost == ['workflow.delivery']
    assert value.store.get_attempt(value.root)['state'] == 'completed'
    assert session(value)['delivery_version'] == 1
    assert value.s.leases.current('account') is None
    assert len(value.runtime_calls) == len(value.provider_calls) == 1
    committed_state = state(value)
    receipt = delivery.load_root_delivery(value.s, value.root, expected_generation=1)
    assert delivery.finalize_root_delivery(value.s, prepared, cleanup_verifier=no_runtime) == receipt
    assert delivery.load_root_delivery(value.s, value.root, expected_generation=1) == receipt
    assert state(value) == committed_state
    assert len(value.runtime_calls) == len(value.provider_calls) == 1
    assert sum(event['type'] == 'workflow.delivery' for event in committed_state['events']) == 1
    assert committed_state['delivery_count'] == 1


def test_cancelled_delivery_lost_commit_ack_replays_inertly(ready, monkeypatch):
    value = ready
    prepared = prepare(value)
    unchanged_session = session(value)
    value.store.cancel(value.owner, value.root, 'cancel-lost-ack')
    lost = lose_delivery_commit_ack(monkeypatch, value, 'workflow.delivery_cancelled_cleanup')
    with pytest.raises(StoreError, match='Synthetic lost delivery commit acknowledgement'):
        delivery.cleanup_cancelled_root_delivery(value.s, prepared, cleanup_verifier=value.verifier)
    assert lost == ['workflow.delivery_cancelled_cleanup']
    assert value.store.get_attempt(value.root)['state'] == 'cancelled'
    assert value.s.leases.current('account') is None
    assert session(value) == unchanged_session
    assert len(value.runtime_calls) == len(value.provider_calls) == 1
    committed_state = state(value)
    receipt = delivery.cleanup_cancelled_root_delivery(value.s, prepared, cleanup_verifier=no_runtime)
    assert receipt['release_purpose'] == 'cancelled_delivery'
    assert delivery.cleanup_cancelled_root_delivery(value.s, prepared, cleanup_verifier=no_runtime) == receipt
    assert state(value) == committed_state
    assert len(value.runtime_calls) == len(value.provider_calls) == 1
    assert sum(event['type'] == 'workflow.delivery_cancelled_cleanup' for event in committed_state['events']) == 1
    assert committed_state['delivery_count'] == 0


def test_delivery_private_storage_path_scanned_after_json_decoding(ready, tmp_path):
    value = ready
    marker = 'TEST-synthetic"forbidden-marker'
    storage = tmp_path / marker
    storage.mkdir(mode=0o700)
    before = session(value)
    with pytest.raises(delivery.DeliveryError, match='delivery_secret_refused'):
        delivery.prepare_root_delivery(value.s, value.root, expected_generation=1,
            expected_delivery_version=0, expected_base_revision_sha256=None,
            selected_revision=value.revision, storage_root=storage,
            forbidden_values=(marker.encode(),))
    assert session(value) == before
    assert value.store.get_attempt(value.root)['state'] == 'running'
    assert not value.provider_calls and not value.runtime_calls
    with value.store._connect() as db:
        assert db.execute('SELECT 1 FROM workflow_delivery_intents WHERE root_id=?', (value.root,)).fetchone() is None
        assert db.execute("SELECT 1 FROM events WHERE attempt_id=? AND type='workflow.delivery_prepared'", (value.root,)).fetchone() is None
