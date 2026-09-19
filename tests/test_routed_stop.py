"""Stopped qualified workflows preserve review evidence and never publish."""
from contextlib import contextmanager
import json

import pytest

from cloudworkbench import routed_stop as stopping
from cloudworkbench.store import Store, StoreError
from tests import test_six_stage_composition as six
from tests.test_routed_delivery import ready as delivery_ready, session, state


@pytest.fixture(scope='module')
def blocked_seed(tmp_path_factory):
    root = tmp_path_factory.mktemp('stopped-workflow-seed')
    with pytest.MonkeyPatch.context() as patch:
        migrate = Store.migrate_scheduler
        def v3(store):
            migrate(store); store.migrate_delivery()
        patch.setattr(Store, 'migrate_scheduler', v3)
        value = six.flow.__wrapped__(root)
    for index in range(5):
        stage = root / f'stage-{index}'; stage.mkdir(mode=0o700)
        with pytest.MonkeyPatch.context() as patch:
            six.execute_stage(value, index, stage, patch, reject=index == 4)
    return value, None


@pytest.fixture
def stopped(blocked_seed, tmp_path):
    return delivery_ready.__wrapped__(blocked_seed, tmp_path)


def close(v, **kwargs):
    return stopping.close_stopped_workflow(v.s, v.root, expected_generation=1,
        cleanup_verifier=kwargs.pop('verifier', v.verifier), **kwargs)


def child_evidence(v):
    with v.store._connect() as db:
        return [tuple(row) for row in db.execute('SELECT id,state,outcome,result FROM attempts WHERE workflow_root_id=? AND id!=? ORDER BY id', (v.root,v.root))]


def test_rejected_review_closes_root_without_publication_and_replays(stopped):
    v = stopped; before = session(v); children = child_evidence(v)
    receipt = close(v)
    assert receipt.outcome == 'needs_review'
    assert v.store.get_attempt(v.root)['state'] == 'completed'
    assert session(v) == before and child_evidence(v) == children
    assert state(v)['delivery_count'] == 0 and v.s.leases.current('account') is None
    assert str(v.store.path) not in json.dumps(v.store.get_attempt(v.root)['result'])
    after = state(v)
    assert stopping.load_stopped_workflow(v.s,v.root,expected_generation=1) == receipt
    assert close(v, verifier=lambda _: pytest.fail('Historical runtime callback')) == receipt
    assert state(v) == after and len(v.runtime_calls) == len(v.provider_calls) == 1


def test_unknown_cleanup_retains_capacity_and_reconciles_exact_target(stopped):
    v = stopped; seen = []; before = session(v)
    def unknown(target):
        seen.append(target); return None
    with pytest.raises(StoreError, match='cleanup unconfirmed'):
        close(v, verifier=unknown)
    assert v.store.get_attempt(v.root)['state'] == 'completed'
    assert v.store.get_attempt(v.root)['outcome'] == 'needs_review'
    assert session(v) == before and v.s.leases.current('account')['state'] == 'held'
    receipt = close(v)
    assert receipt.outcome == 'needs_review' and seen == v.runtime_calls
    assert len(v.runtime_calls) == len(v.provider_calls) == 1


def test_record_event_failure_rolls_back_terminal_and_does_not_cleanup(stopped, monkeypatch):
    v = stopped; before = state(v); original = v.store._event
    def fail(db,sid,aid,kind,payload):
        if kind == 'workflow.stopped': raise RuntimeError('stop record fault')
        return original(db,sid,aid,kind,payload)
    monkeypatch.setattr(v.store,'_event',fail)
    with pytest.raises(RuntimeError,match='stop record fault'): close(v)
    assert state(v) == before and not v.runtime_calls and not v.provider_calls


def test_lost_terminal_commit_ack_does_not_repeat_stages(stopped, monkeypatch):
    v = stopped; original = stopping._child_control_tx; lost = []
    @contextmanager
    def tx(store,*,write=True):
        changed=False
        with original(store,write=write) as db:
            before=db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.stopped'").fetchone()[0]
            yield db
            changed=write and before==0 and db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.stopped'").fetchone()[0]==1
        if changed and not lost:
            lost.append(True);raise StoreError(503,'lost stop commit ack')
    monkeypatch.setattr(stopping,'_child_control_tx',tx)
    children=child_evidence(v)
    with pytest.raises(StoreError,match='lost stop commit ack'):close(v)
    assert not v.runtime_calls and not v.provider_calls
    assert close(v).outcome=='needs_review'
    assert child_evidence(v)==children and len(v.runtime_calls)==len(v.provider_calls)==1


def test_released_replay_does_not_touch_new_account_owner(stopped):
    v=stopped;receipt=close(v)
    owner=v.s.leases.reserve('account',persistent_owner_id='owner')
    assert close(v,verifier=lambda _:pytest.fail('New owner must be untouched'))==receipt
    assert v.s.leases.current('account')['reservation']==owner


def test_release_event_failure_reuses_proven_cleanup_on_retry(stopped,monkeypatch):
    v=stopped;before=session(v);children=child_evidence(v);original=v.store._event
    def fail(db,sid,aid,kind,payload):
        if kind=='workflow.root_released':raise RuntimeError('release commit fault')
        return original(db,sid,aid,kind,payload)
    with monkeypatch.context() as patch:
        patch.setattr(v.store,'_event',fail)
        with pytest.raises(RuntimeError,match='release commit fault'):close(v)
    assert v.store.get_attempt(v.root)['state']=='completed'
    assert v.s.leases.current('account')['state']=='cleaning'
    assert session(v)==before and child_evidence(v)==children
    assert close(v,verifier=lambda _:pytest.fail('Repeat proven cleanup')).outcome=='needs_review'
    assert len(v.runtime_calls)==len(v.provider_calls)==1


@pytest.mark.parametrize('change',['cancel','revoke','deadline'])
def test_stopped_outcome_requires_current_authority(stopped,change):
    v=stopped
    if change=='cancel':v.store.cancel(v.owner,v.root,'cancel-stop')
    else:
        with v.store._tx() as db:
            if change=='revoke':db.execute("UPDATE clients SET revoked_at='revoked' WHERE id=?",(v.owner['id'],))
            else:db.execute('UPDATE workflow_roots SET deadline_at=999 WHERE root_id=?',(v.root,))
    before=state(v)
    with pytest.raises(ValueError):close(v)
    assert state(v)==before and not v.runtime_calls and not v.provider_calls


@pytest.mark.parametrize('case',['stage_needs_review','task_rejected','task_needs_review'])
def test_actual_nonpassing_outcomes_close_with_exact_semantics(tmp_path,monkeypatch,case):
    from tests.test_routed_stop_policy import build_stopped
    source=tmp_path/'source';source.mkdir()
    migrate=Store.migrate_scheduler
    def v3(store):
        migrate(store);store.migrate_delivery()
    with monkeypatch.context() as patch:
        patch.setattr(Store,'migrate_scheduler',v3)
        flow=build_stopped(source,case)
    destination=tmp_path/'controller';destination.mkdir()
    v=delivery_ready.__wrapped__((flow,None),destination)
    before=session(v);children=child_evidence(v)
    receipt=close(v)
    assert receipt.outcome==('rejected' if case=='task_rejected' else 'needs_review')
    assert v.store.get_attempt(v.root)['state']=='completed'
    assert session(v)==before and child_evidence(v)==children and state(v)['delivery_count']==0
    assert close(v,verifier=lambda _:pytest.fail('Nonpass replay invoked cleanup'))==receipt
    assert len(v.runtime_calls)==len(v.provider_calls)==1
