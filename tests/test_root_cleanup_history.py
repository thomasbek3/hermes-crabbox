"""Inert generic cleanup reader with actual leases and explicit offline corruption."""
import json
import sqlite3
import pytest

from cloudworkbench.provider_dispatch import ProviderDispatch
from cloudworkbench.root_cleanup_history import load_root_cleanup, RootCleanupHistoryError
from cloudworkbench.store import encode
from tests.test_provider_dispatch import FakeRuntime
from tests.test_root_cleanup import setup, finish, release


@pytest.fixture
def completed(setup):
    finish(setup)
    return setup,release(setup)


def read(value,*,generation=1):
    store,_,_,_,root,_=value
    with store._tx() as db:return load_root_cleanup(db,root['attempt_id'],generation)


def dump(value):
    with value[0]._connect() as db:return tuple(db.iterdump())


def offline_journal(value,column,change):
    """Bypass only the exact immutable guard in this disposable fixture DB."""
    with value[0]._tx() as db:
        ddl=db.execute("SELECT sql FROM sqlite_master WHERE name='workflow_cleanup_immutable'").fetchone()[0]
        raw=db.execute(f'SELECT {column} FROM workflow_root_cleanup').fetchone()[0]
        db.execute('DROP TRIGGER workflow_cleanup_immutable')
        db.execute(f'UPDATE workflow_root_cleanup SET {column}=?',(change(raw),))
        db.execute(ddl)


def changed(raw,key,value):
    body=json.loads(raw);body[key]=value;return encode(body)


def test_success_is_exact_inert_and_requires_transaction(completed,monkeypatch):
    value,expected=completed;before=dump(value)
    def forbidden(*_a,**_k):pytest.fail('Historical read consulted live ownership/cleanup')
    monkeypatch.setattr(value[3],'_owner_current',forbidden)
    monkeypatch.setattr(value[2],'cleanup_owner',forbidden)
    monkeypatch.setattr(value[2],'verifier',forbidden)
    assert read(value)==expected and read(value)==expected and dump(value)==before
    with value[0]._connect() as db:
        with pytest.raises(RootCleanupHistoryError):load_root_cleanup(db,value[4]['attempt_id'],1)


def test_new_owner_and_account_metadata_do_not_change_original_proof(completed):
    value,expected=completed;leases=value[2]
    newer=leases.reserve('account-a',persistent_owner_id='owner-a')
    before=dump(value)
    assert read(value)==expected and dump(value)==before
    assert leases.current('account-a')['reservation']==newer


def test_reader_never_reads_current_account_table(completed):
    value,expected=completed
    with value[0]._tx() as db:
        def authorizer(action,table,*_args):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table=='provider_accounts' else sqlite3.SQLITE_OK
        db.set_authorizer(authorizer)
        try:assert load_root_cleanup(db,value[4]['attempt_id'],1)==expected
        finally:db.set_authorizer(None)


@pytest.mark.parametrize('field,bad_value',[('execution_kind','unsupported'),('generation',True)])
def test_malformed_nested_target_has_fixed_error(completed,field,bad_value):
    value,_=completed
    def corrupt(raw):
        target=json.loads(raw);target['attempts'][0][field]=bad_value
        return encode(target)
    offline_journal(value,'target',corrupt)
    before=dump(value)
    with pytest.raises(RootCleanupHistoryError,match='^root_cleanup_history_invalid$') as error:
        read(value)
    assert error.value.code=='root_cleanup_history_invalid'
    assert error.value.__suppress_context__ and dump(value)==before


@pytest.mark.parametrize('terminal',['completed','cancelled','interrupted'])
def test_other_terminal_states_preserve_exact_historical_membership(setup,terminal):
    store,_,_,scheduler,root,_=setup
    if terminal=='completed':scheduler.transition(root['attempt_id'],'verifying',expected_generation=1)
    scheduler.transition(root['attempt_id'],terminal,expected_generation=1,
        **({'outcome':'needs_review'} if terminal=='completed' else {}))
    expected=release(setup)
    assert read(setup)==expected


def test_existing_provider_dispatch_owner_cleanup_is_authenticated(setup):
    store,_,leases,scheduler,root,_=setup
    runtime=FakeRuntime();leases.verifier=runtime.cleanup;leases.inspector_id='synthetic-inspector'
    owner=leases.current('account-a')['reservation']
    grant=leases.issue_grant(owner,attempt_id=root['attempt_id'],generation=1)
    dispatch=ProviderDispatch(leases,runtime)
    spec=dispatch.admit(owner,grant,request_nonce='a'*64,payload=b'fixture',profile_digest='b'*64)
    dispatch.launch(spec.lease.request_id,b'fixture')
    finish(setup);expected=release(setup)
    assert not runtime.objects and read(setup)==expected
    before=len(runtime.events)
    with store._tx() as db:db.execute("UPDATE provider_dispatch SET uncertain=1 WHERE request_id=?",(spec.lease.request_id,))
    with pytest.raises(RootCleanupHistoryError):read(setup)
    assert len(runtime.events)==before


@pytest.mark.parametrize('column,change',[
    ('runtime_receipt',lambda _:None),
    ('runtime_receipt',lambda r:changed(r,'evidence_sha256','f'*64)),
    ('runtime_receipt',lambda r:changed(r,'observed_at',True)),
    ('runtime_receipt',lambda r:changed(r,'extra',0)),
    ('runtime_receipt',lambda r:changed(r,'observed_at',float('nan'))),
    ('runtime_receipt',lambda r:r+' '),
    ('runtime_receipt',lambda r:'{"outcome":"terminated",'+r[1:]),
    ('runtime_receipt',lambda r:' '*262145),
    ('target',lambda r:changed(r,'generation',True)),
    ('target',lambda r:changed(r,'controller_instance_id','other')),
    ('target',lambda r:changed(r,'inspector_id','other')),
    ('target',lambda r:changed(r,'attempts',[])),
    ('target',lambda r:changed(r,'extra',1)),
    ('target',lambda r:changed(r,'frozen_at',1001)),
    ('release_receipt',lambda r:changed(r,'generation',True)),
    ('release_receipt',lambda r:changed(r,'extra',1)),
    ('release_receipt',lambda r:changed(r,'provider_receipt_sha256',{})),
    ('release_receipt',lambda r:changed(r,'runtime_receipt_sha256','e'*64)),
    ('release_receipt',lambda r:changed(r,'released_at',None)),
])
def test_offline_journal_corruption_refused_without_changes(completed,column,change):
    value,_=completed;offline_journal(value,column,change);before=dump(value)
    with pytest.raises(RootCleanupHistoryError):read(value)
    assert dump(value)==before


@pytest.mark.parametrize('change',[
    lambda r:changed(r,'evidence_sha256','c'*64),
    lambda r:changed(r,'observed_at',True),
    lambda r:changed(r,'inspector_id','other'),
    lambda r:changed(r,'extra',1),
    lambda r:changed(r,'target',{**json.loads(r)['target'],'scope':'request'}),
    lambda r:changed(r,'target',{**json.loads(r)['target'],'generation':1}),
    lambda r:r+' ',
])
def test_original_provider_receipt_corruption_refused(completed,change):
    value,_=completed
    with value[0]._tx() as db:
        row=db.execute("SELECT id,cleanup_receipt FROM provider_reservations WHERE account_id='account-a'").fetchone()
        db.execute('UPDATE provider_reservations SET cleanup_receipt=? WHERE id=?',(change(row['cleanup_receipt']),row['id']))
    before=dump(value)
    with pytest.raises(RootCleanupHistoryError):read(value)
    assert dump(value)==before


@pytest.mark.parametrize('kind',['root_version','root_state','member_state','event_missing','event_duplicate','event_body','reservation_state','account_membership'])
def test_membership_or_release_event_drift_refused(completed,kind):
    value,_=completed;root=value[4]['attempt_id']
    with value[0]._tx() as db:
        if kind=='root_version':db.execute('UPDATE workflow_roots SET version=version+1')
        elif kind=='root_state':db.execute("UPDATE workflow_roots SET state='quarantined'")
        elif kind=='member_state':db.execute("UPDATE attempts SET state='interrupted' WHERE id=?",(root,))
        elif kind=='event_missing':db.execute("DELETE FROM events WHERE type='workflow.root_released'")
        elif kind=='event_duplicate':
            row=dict(db.execute("SELECT * FROM events WHERE type='workflow.root_released'").fetchone())
            row['sequence']=db.execute('SELECT max(sequence)+1 FROM events WHERE session_id=?',(row['session_id'],)).fetchone()[0]
            db.execute(f'INSERT INTO events({",".join(row)}) VALUES({",".join("?" for _ in row)})',tuple(row.values()))
        elif kind=='event_body':db.execute("UPDATE events SET payload='{}' WHERE type='workflow.root_released'")
        elif kind=='reservation_state':db.execute("UPDATE provider_reservations SET state='cleaning'")
        else:db.execute("DELETE FROM workflow_accounts WHERE account_id='account-b'")
    before=dump(value)
    with pytest.raises(RootCleanupHistoryError):read(value)
    assert dump(value)==before


def test_runtime_identity_corruption_requires_offline_bypass_and_is_detected(completed):
    value,_=completed
    with value[0]._tx() as db:
        with pytest.raises(sqlite3.IntegrityError,match='immutable_workflow_runtime'):
            db.execute("UPDATE attempts SET runtime_id='foreign' WHERE id=?",(value[4]['attempt_id'],))
        ddl=db.execute("SELECT sql FROM sqlite_master WHERE name='workflow_runtime_immutable'").fetchone()[0]
        db.execute('DROP TRIGGER workflow_runtime_immutable')
        db.execute("UPDATE attempts SET runtime_id='foreign' WHERE id=?",(value[4]['attempt_id'],))
        db.execute(ddl)
    with pytest.raises(RootCleanupHistoryError):read(value)


def test_stale_generation_and_unreleased_root_refused(setup):
    with pytest.raises(RootCleanupHistoryError):read(setup)
    finish(setup);release(setup)
    for generation in (True,0,2):
        with pytest.raises(RootCleanupHistoryError):read(setup,generation=generation)
