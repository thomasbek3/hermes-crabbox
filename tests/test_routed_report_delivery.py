"""Report delivery with real controllers and synthetic inference/physical cleanup.

Prior session pointers, when requested, are explicit synthetic setup performed
inside root enqueue before freeze_base/admission. This is not follow-up import.
"""
import hashlib
import json
from pathlib import Path

import pytest

from cloudworkbench import delivery_schema
from cloudworkbench.provider_leases import CleanupReceipt
from cloudworkbench.routed_delivery import prepare_root_delivery, finalize_root_delivery, load_root_delivery
from cloudworkbench.scheduler import RootCleanupReceipt
from cloudworkbench.store import Store, encode
from tests import test_six_stage_composition as six


def session(flow):
    with flow.store._connect() as db:
        return dict(db.execute('SELECT * FROM sessions WHERE id=?',(flow.root['session_id'],)).fetchone())


def durable_state(flow):
    with flow.store._connect() as db:
        return {table:[dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            for table in ('sessions','attempts','artifacts','events','workflow_roots',
                'workflow_root_cleanup','workflow_delivery_bases','workflow_delivery_intents',
                'provider_accounts','provider_reservations')}


@pytest.mark.parametrize('workflow,expected',[
    ('planning',six.EXPECTED[:3]),('code_review',[six.EXPECTED[4]])])
@pytest.mark.parametrize('prior_workspace',[False,True],ids=['new-session-null-pair','synthetic-pre-admission-prior-pair'])
def test_report_prepare_finalize_load_preserves_workspace_exactly_once(tmp_path,monkeypatch,
        record_property,workflow,expected,prior_workspace):
    migrate=Store.migrate_scheduler
    def migrate_v3(store):
        migrate(store);store.migrate_delivery()
    monkeypatch.setattr(Store,'migrate_scheduler',migrate_v3)
    select=six.select_workflow;enqueue=six.enqueue_qualified_workflow
    monkeypatch.setattr(six,'EXPECTED',expected)
    monkeypatch.setattr(six,'select_workflow',lambda goal,**kw:
        select(goal,allowed_workflows=[workflow],explicit_workflow=workflow))
    def report_enqueue(*args,**kwargs):
        kwargs['allowed_workflows']=[workflow]
        return enqueue(*args,**kwargs)
    monkeypatch.setattr(six,'enqueue_qualified_workflow',report_enqueue)
    prior_digest=hashlib.sha256(b'explicit synthetic prior workspace pointer').hexdigest()
    if prior_workspace:
        freeze=delivery_schema.freeze_base
        def seed_before_base(db,root_id,generation):
            row=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
            assert row['state']=='queued' and row['child_attempt_id'] is None
            assert not db.execute('SELECT 1 FROM workflow_accounts WHERE root_id=?',(root_id,)).fetchone()
            assert not db.execute('SELECT 1 FROM workflow_delivery_bases WHERE root_id=?',(root_id,)).fetchone()
            for identity,kind in (('synthetic-prior-workspace','workspace'),('synthetic-prior-report','report')):
                metadata={'id':identity,'session_id':row['session_id'],'attempt_id':root_id,
                    'provenance':'synthetic_test_session_pointer','kind':kind,
                    'fixture_only':True,'not_followup_import':True}
                db.execute('INSERT INTO artifacts(id,session_id,attempt_id,metadata) VALUES(?,?,?,?)',
                    (identity,row['session_id'],root_id,encode(metadata)))
            db.execute('UPDATE sessions SET delivery_version=7,delivered_revision_sha256=?,delivered_artifact_id=?,last_delivery_artifact_id=? WHERE id=?',
                (prior_digest,'synthetic-prior-workspace','synthetic-prior-report',row['session_id']))
            return freeze(db,root_id,generation)
        monkeypatch.setattr(delivery_schema,'freeze_base',seed_before_base)
    flow=six.flow.__wrapped__(tmp_path)
    with flow.store._connect() as db:
        assert db.execute('SELECT version FROM schema_version').fetchone()[0]==3
        base=dict(db.execute('SELECT * FROM workflow_delivery_bases WHERE root_id=?',(flow.root['attempt_id'],)).fetchone())
    assert base['delivery_version']==(7 if prior_workspace else 0)
    assert base['revision_sha256']==(prior_digest if prior_workspace else None)
    for index in range(len(expected)):
        path=tmp_path/f'stage-{index}';path.mkdir(mode=0o700)
        with monkeypatch.context() as boundary:six.execute_stage(flow,index,path,boundary)
    assert flow.revision==flow.initial
    provider_calls=[];root_calls=[]
    def provider_cleanup(target):
        provider_calls.append(target)
        return CleanupReceipt(target,'synthetic-inspector','terminated',1000,'a'*64)
    def root_cleanup(target):
        root_calls.append(target)
        return RootCleanupReceipt(target,'terminated',1000,'c'*64)
    flow.s.leases.verifier=provider_cleanup
    flow.s.leases.inspector_id='synthetic-inspector'
    storage=tmp_path/'root-delivery';storage.mkdir(mode=0o700)
    before=session(flow)
    prepared=prepare_root_delivery(flow.s,flow.root['attempt_id'],expected_generation=1,
        expected_delivery_version=before['delivery_version'],
        expected_base_revision_sha256=before['delivered_revision_sha256'],
        selected_revision=flow.revision,storage_root=storage,forbidden_values=())
    assert session(flow)==before
    assert flow.store.get_attempt(flow.root['attempt_id'])['state']=='verifying'
    assert not root_calls and not provider_calls
    assert prepare_root_delivery(flow.s,flow.root['attempt_id'],expected_generation=1,
        expected_delivery_version=before['delivery_version'],
        expected_base_revision_sha256=before['delivered_revision_sha256'],
        selected_revision=flow.revision,storage_root=storage,forbidden_values=())==prepared
    result=finalize_root_delivery(flow.s,prepared,cleanup_verifier=root_cleanup)
    after=session(flow)
    assert result.kind=='report' and result.outcome=='unverified'
    assert result.selected_revision_sha256 is None
    assert after['delivery_version']==result.delivery_version==before['delivery_version']+1
    assert (after['delivered_revision_sha256'],after['delivered_artifact_id'])==(
        before['delivered_revision_sha256'],before['delivered_artifact_id'])
    assert after['last_delivery_artifact_id']==result.artifact_id!=before['last_delivery_artifact_id']
    attempt=flow.store.get_attempt(flow.root['attempt_id'])
    assert attempt['state']=='completed' and attempt['outcome']=='unverified'
    assert flow.s.leases.current('account') is None
    assert len(provider_calls)==len(root_calls)==1
    saved=durable_state(flow)
    def forbidden_callback(*args,**kwargs):pytest.fail('Historical report delivery must not call cleanup')
    monkeypatch.setattr(flow.s.leases,'verifier',forbidden_callback)
    assert load_root_delivery(flow.s,flow.root['attempt_id'],expected_generation=1)==result
    assert finalize_root_delivery(flow.s,prepared,cleanup_verifier=forbidden_callback)==result
    assert durable_state(flow)==saved
    with flow.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE attempt_id=? AND type='workflow.delivery'",(flow.root['attempt_id'],)).fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM artifacts WHERE json_extract(metadata,'$.provenance')='controller_root_delivery'").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM artifacts WHERE json_extract(metadata,'$.provenance')='controller_protected_checks'").fetchone()[0]==0
        metadata=json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?',(result.artifact_id,)).fetchone()[0])
    raw=Path(metadata['storage_path']).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==metadata['sha256']==result.sha256
    assert b'storage_path' not in raw and str(storage).encode() not in raw
    record_property('report_delivery_receipt',json.dumps({'workflow':workflow,'kind':result.kind,
        'outcome':result.outcome,'generation':1,'schema_version':3,
        'completed_catalog_steps':len(expected),'delivery_version_before':before['delivery_version'],
        'delivery_version_after':after['delivery_version'],'workspace_pair_preserved':True,
        'null_prior_pair':not prior_workspace,'last_artifact_changed':True,
        'historical_replay_no_callbacks_or_writes':True,'provider_cleanup_callbacks':len(provider_calls),
        'root_cleanup_callbacks':len(root_calls),'protected_verification_steps':0,
        'boundary':'Synthetic Docker/inference and typed physical-cleanup callbacks; actual controller composition',
        'prior_pair_boundary':'Synthetic session pointer setup before root base freeze/admission; not follow-up import' if prior_workspace else 'Real new root/session with null prior workspace pair'},sort_keys=True))
