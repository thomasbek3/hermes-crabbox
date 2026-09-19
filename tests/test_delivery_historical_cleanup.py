"""Historical cleanup corruption must fail without reacquiring live authority.

Offline corruption is applied only to disposable fixture databases. The exact
immutable-journal trigger is restored in the same transaction before replay;
production SQL guards are never changed by the implementation under test.
"""
import json
import sqlite3

import pytest

from cloudworkbench.routed_delivery import (DeliveryError, cleanup_cancelled_root_delivery,
    finalize_root_delivery, load_root_delivery)
from cloudworkbench.store import StoreError, encode
from tests.test_delivery_cleanup import seed
from tests.test_routed_delivery import ready, prepare, session


def _database_state(value):
    with value.store._connect() as db:
        return tuple(db.iterdump())


def _corrupt(value, part):
    with value.store._tx() as db:
        row=db.execute('SELECT * FROM workflow_root_cleanup WHERE root_id=?',(value.root,)).fetchone()
        if part=='provider_receipt':
            owner=db.execute('SELECT reservation_id FROM workflow_accounts WHERE root_id=?',(value.root,)).fetchone()[0]
            raw=db.execute('SELECT cleanup_receipt FROM provider_reservations WHERE id=?',(owner,)).fetchone()[0]
            proof=json.loads(raw);proof['evidence_sha256']='d'*64
            db.execute('UPDATE provider_reservations SET cleanup_receipt=? WHERE id=?',(encode(proof),owner))
            return
        ddl=db.execute("SELECT sql FROM sqlite_master WHERE name='workflow_cleanup_immutable'").fetchone()[0]
        db.execute('DROP TRIGGER workflow_cleanup_immutable')
        if part=='runtime_receipt':
            proof=json.loads(row['runtime_receipt']);proof['evidence_sha256']='e'*64
            db.execute('UPDATE workflow_root_cleanup SET runtime_receipt=? WHERE root_id=?',(encode(proof),value.root))
        elif part=='runtime_missing':
            db.execute('UPDATE workflow_root_cleanup SET runtime_receipt=NULL WHERE root_id=?',(value.root,))
        else:
            assert part=='target'
            target=json.loads(row['target'])
            member=next(a for a in target['attempts'] if a['attempt_id']==value.root)
            member['runtime_id']='f'*64
            db.execute('UPDATE workflow_root_cleanup SET target=? WHERE root_id=?',(encode(target),value.root))
        db.execute(ddl)


@pytest.mark.parametrize('mode',['published','cancelled'])
@pytest.mark.parametrize('part',['runtime_receipt','runtime_missing','provider_receipt','target'])
def test_historical_cleanup_corruption_rejects_without_callbacks_or_mutation(ready,mode,part,monkeypatch):
    value=ready;prepared=prepare(value)
    if mode=='published':
        finalize_root_delivery(value.s,prepared,cleanup_verifier=value.verifier)
    else:
        value.store.cancel(value.owner,value.root,'cancel-before-delivery')
        cleanup_cancelled_root_delivery(value.s,prepared,cleanup_verifier=value.verifier)
    _corrupt(value,part)
    before=_database_state(value);before_session=session(value)
    calls=(tuple(value.runtime_calls),tuple(value.provider_calls))
    def forbidden(*_args,**_kwargs):pytest.fail('Historical replay invoked live cleanup')
    monkeypatch.setattr(value.s,'prepare_delivery_cleanup',forbidden)
    monkeypatch.setattr(value.s.leases,'cleanup_owner',forbidden)
    monkeypatch.setattr(value.s.leases,'verifier',forbidden)
    if mode=='published':
        with pytest.raises((DeliveryError,StoreError)):
            load_root_delivery(value.s,value.root,expected_generation=1)
        with pytest.raises((DeliveryError,StoreError)):
            finalize_root_delivery(value.s,prepared,cleanup_verifier=forbidden)
    else:
        with pytest.raises((DeliveryError,StoreError)):
            cleanup_cancelled_root_delivery(value.s,prepared,cleanup_verifier=forbidden)
    assert session(value)==before_session and _database_state(value)==before
    assert (tuple(value.runtime_calls),tuple(value.provider_calls))==calls


def test_cleanup_journal_guard_normally_rejects_receipt_corruption(ready):
    value=ready;prepared=prepare(value)
    finalize_root_delivery(value.s,prepared,cleanup_verifier=value.verifier)
    before=_database_state(value)
    with value.store._tx() as db:
        with pytest.raises(sqlite3.IntegrityError,match='immutable_root_cleanup'):
            db.execute('UPDATE workflow_root_cleanup SET runtime_receipt=NULL WHERE root_id=?',(value.root,))
    assert _database_state(value)==before
    assert load_root_delivery(value.s,value.root,expected_generation=1).outcome=='verified'


def test_terminal_cancelled_before_first_cleanup_has_inert_historical_replay(ready,monkeypatch):
    value=ready;prepared=prepare(value);before_session=session(value)
    value.store.cancel(value.owner,value.root,'cancel-and-terminalize')
    value.s.transition(value.root,'cancelled',expected_generation=1)
    receipt=cleanup_cancelled_root_delivery(value.s,prepared,cleanup_verifier=value.verifier)
    with value.store._connect() as db:
        target=json.loads(db.execute('SELECT target FROM workflow_root_cleanup WHERE root_id=?',(value.root,)).fetchone()[0])
    assert next(a['state'] for a in target['attempts'] if a['attempt_id']==value.root)=='cancelled'
    before=_database_state(value)
    def forbidden(*_args,**_kwargs):pytest.fail('Historical cancellation replay invoked cleanup')
    monkeypatch.setattr(value.s,'prepare_delivery_cleanup',forbidden)
    monkeypatch.setattr(value.s.leases,'cleanup_owner',forbidden)
    assert cleanup_cancelled_root_delivery(value.s,prepared,cleanup_verifier=forbidden)==receipt
    assert _database_state(value)==before and session(value)==before_session
