"""Explicit migration and immutable storage boundaries; no delivery authorization."""
import hashlib
import json
import sqlite3

import pytest
from cloudworkbench import delivery_schema as delivery
from cloudworkbench.store import Store, StoreError, encode
from tests.test_scheduler import setup, request


@pytest.fixture
def empty(tmp_path):
    store=Store(tmp_path/'state.db')
    owner=store.add_client('TEST delivery','x'*40,['submit','observe'],['demo'])
    store.migrate_scheduler()
    return store,owner


@pytest.fixture
def routed(setup):
    store,owner,leases,scheduler,plan,frozen=setup
    store.migrate_delivery()
    created=scheduler.enqueue_root(owner,request(),'root',frozen=frozen)
    with store._tx() as db:
        base=delivery.freeze_base(db,created['attempt_id'],1)
    return store,owner,scheduler,created,base


def intent(db,root,**changes):
    values=dict(root_id=root,expected_generation=1,expected_delivery_version=0,base_revision=None,
        base_artifact=None,selected_revision_sha256='a'*64,kind='workspace',proof={'fixture':'bounded'},created_at='2026-09-18T00:00:00Z')
    values.update(changes)
    return delivery.record_intent(db,**values)


def snapshot(store):
    with store._connect() as db:
        return list(db.iterdump())


def test_v1_open_never_migrates_and_explicit_delivery_requires_v2(tmp_path):
    store=Store(tmp_path/'state.db');before=snapshot(store)
    with pytest.raises(StoreError,match='scheduler migration'):store.migrate_delivery()
    assert snapshot(store)==before
    Store(store.path)
    with store._connect() as db:assert db.execute('SELECT version FROM schema_version').fetchone()[0]==1


def test_v2_history_preserved_null_defaults_and_reopen(empty):
    store,owner=empty;created=store.create_session(owner,request('claude'),'legacy')
    before=store.get_attempt(created['attempt_id'])
    with store._connect() as db:
        history=[tuple(row) for row in db.execute('SELECT * FROM events')]
        oldsession=dict(db.execute('SELECT * FROM sessions').fetchone())
    store.migrate_delivery();Store(store.path);store.migrate_delivery()
    assert store.get_attempt(created['attempt_id'])==before
    with store._connect() as db:
        assert delivery.enabled(db) and store._routed_schema(db)
        session=dict(db.execute('SELECT * FROM sessions').fetchone())
        assert all(session[k]==v for k,v in oldsession.items())
        assert {k:session[k] for k in ('delivery_version','delivered_revision_sha256','delivered_artifact_id','last_delivery_artifact_id')}==dict(delivery_version=0,delivered_revision_sha256=None,delivered_artifact_id=None,last_delivery_artifact_id=None)
        assert history==[tuple(row) for row in db.execute('SELECT * FROM events')]
        assert not db.execute('SELECT 1 FROM workflow_delivery_bases').fetchone()
    assert store.claim_next()['id']==created['attempt_id']


def test_migration_rollback_after_all_ddl(empty,monkeypatch):
    store,_=empty;before=snapshot(store)
    monkeypatch.setattr(delivery,'verify_schema',lambda *_:(_ for _ in ()).throw(RuntimeError('injected verify failure')))
    with pytest.raises(RuntimeError,match='injected'):store.migrate_delivery()
    assert snapshot(store)==before


@pytest.mark.parametrize('mutation',[
    'DROP TRIGGER workflow_delivery_bases_no_delete',
    'DROP TRIGGER session_delivery_update',
    "ALTER TABLE workflow_delivery_intents ADD COLUMN surprise TEXT",
    "CREATE TRIGGER workflow_delivery_extra BEFORE INSERT ON workflow_delivery_bases BEGIN SELECT 1; END",
])
def test_corrupt_v3_ddl_refuses_open_and_repeated_migration(empty,mutation):
    store,_=empty;store.migrate_delivery()
    with store._tx() as db:db.execute(mutation)
    with pytest.raises(StoreError,match='schema differs'):Store(store.path)
    with pytest.raises(StoreError,match='schema differs'):store.migrate_delivery()


def test_corrupt_v2_scheduler_refused_without_delivery_mutation(empty):
    store,_=empty
    with store._tx() as db:db.execute('DROP INDEX one_live_child')
    before=snapshot(store)
    with pytest.raises(StoreError,match='schema differs'):store.migrate_delivery()
    assert snapshot(store)==before


def test_live_legacy_refuses_and_does_not_add_columns(empty):
    store,owner=empty;store.create_session(owner,request('claude'),'legacy');store.claim_next()
    before=snapshot(store)
    with pytest.raises(StoreError,match='quiescent'):store.migrate_delivery()
    assert snapshot(store)==before


def test_pending_routed_root_refuses_without_false_backfill(setup):
    store,owner,_,scheduler,_,frozen=setup
    scheduler.enqueue_root(owner,request(),'pending',frozen=frozen)
    before=snapshot(store)
    with pytest.raises(StoreError,match='quiescent'):store.migrate_delivery()
    assert snapshot(store)==before


def test_orphan_provider_reservation_refuses_even_without_active_pointer(empty):
    store,_=empty
    with store._tx() as db:
        db.execute("INSERT INTO provider_accounts VALUES('account','hermes','owner',0,NULL,1)")
        db.execute("INSERT INTO provider_reservations(id,account_id,epoch,controller_instance_id,state,created_at) VALUES('r','account',1,'test','quarantined',1)")
    before=snapshot(store)
    with pytest.raises(StoreError,match='quiescent'):store.migrate_delivery()
    assert snapshot(store)==before


def test_scheduler_hook_base_replay_is_exact_and_intent_immutable(routed):
    store,_,_,created,base=routed;root=created['attempt_id']
    assert base['delivery_version']==0 and base['revision_sha256'] is None
    with store._tx() as db:
        assert delivery.freeze_base(db,root,1)==base
        row=intent(db,root)
        assert intent(db,root)==row
        assert delivery.read_intent(db,root,1)==row
        assert row['proof_sha256']==hashlib.sha256(row['proof_json'].encode()).hexdigest()
        with pytest.raises(StoreError,match='replay conflict'):intent(db,root,proof={'fixture':'different'})
    for table in ('workflow_delivery_bases','workflow_delivery_intents'):
        for mutation in (f'UPDATE {table} SET generation=2',f'DELETE FROM {table}'):
            with pytest.raises(sqlite3.IntegrityError,match='immutable'):
                with store._tx() as db:db.execute(mutation)


@pytest.mark.parametrize('changes',[
    {'expected_generation':True},{'expected_delivery_version':True},{'expected_delivery_version':-1},
    {'expected_delivery_version':2**63},{'selected_revision_sha256':'z'*64},
    {'kind':'report','selected_revision_sha256':'a'*64},{'kind':'workspace','selected_revision_sha256':None},
    {'base_revision':'b'*64},{'base_artifact':'unpaired'}, {'proof':[]},{'proof':{'nan':float('nan')}},
    {'proof':{'large':'x'*(256*1024)}},{'created_at':''},
])
def test_intent_invalid_values_never_insert(routed,changes):
    store,_,_,created,_=routed
    with store._tx() as db:
        with pytest.raises(StoreError):intent(db,created['attempt_id'],**changes)
        assert delivery.read_intent(db,created['attempt_id'],1) is None


def test_report_intent_has_no_workspace_selection(routed):
    store,_,_,created,_=routed
    with store._tx() as db:
        result=intent(db,created['attempt_id'],kind='report',selected_revision_sha256=None)
        assert result['selected_revision_sha256'] is None


def test_foreign_key_and_cross_session_delivery_refused(empty):
    store,owner=empty;store.migrate_delivery()
    a=store.create_session(owner,request('claude'),'a');b=store.create_session(owner,request('fixture'),'b')
    with store._tx() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?)',('artifact',a['session_id'],a['attempt_id'],'{}'))
        for artifact in ('missing','artifact'):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute('UPDATE sessions SET delivery_version=1,last_delivery_artifact_id=? WHERE id=?',(artifact,b['session_id']))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO workflow_delivery_bases VALUES('missing',1,0,NULL,NULL,NULL)")


@pytest.mark.parametrize('version,revision,artifact,last',[
    (-1,None,None,None),(0,'a'*64,'artifact','artifact'),(1,None,None,None),
    (1,'x'*64,'artifact','artifact'),(1,'a'*64,None,'artifact'),(1,None,'artifact','artifact'),
    ('bad',None,None,'artifact'),
])
def test_session_paired_fields_enforced(empty,version,revision,artifact,last):
    store,owner=empty;store.migrate_delivery();a=store.create_session(owner,request('claude'),'a')
    with store._tx() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?)',('artifact',a['session_id'],a['attempt_id'],'{}'))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE sessions SET delivery_version=?,delivered_revision_sha256=?,delivered_artifact_id=?,last_delivery_artifact_id=? WHERE id=?',(version,revision,artifact,last,a['session_id']))


def test_workspace_then_report_retains_workspace_pointer(empty):
    store,owner=empty;store.migrate_delivery();a=store.create_session(owner,request('claude'),'a')
    with store._tx() as db:
        for identity in ('workspace','report'):db.execute('INSERT INTO artifacts VALUES(?,?,?,?)',(identity,a['session_id'],a['attempt_id'],'{}'))
        db.execute("UPDATE sessions SET delivery_version=1,delivered_revision_sha256=?,delivered_artifact_id='workspace',last_delivery_artifact_id='workspace' WHERE id=?",('a'*64,a['session_id']))
        db.execute("UPDATE sessions SET delivery_version=2,last_delivery_artifact_id='report' WHERE id=?",(a['session_id'],))
        session=db.execute('SELECT * FROM sessions WHERE id=?',(a['session_id'],)).fetchone()
        assert session['delivered_revision_sha256']=='a'*64 and session['delivered_artifact_id']=='workspace'
        assert session['last_delivery_artifact_id']=='report'


def test_caller_transaction_rolls_back_intent(routed):
    store,_,_,created,_=routed
    with pytest.raises(RuntimeError):
        with store._tx() as db:
            intent(db,created['attempt_id']);raise RuntimeError('rollback')
    with store._tx() as db:assert delivery.read_intent(db,created['attempt_id'],1) is None
