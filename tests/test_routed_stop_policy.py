"""Authentic finalized/released stopped prefixes; no provider inference or Docker."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from tests import test_six_stage_composition as six
from cloudworkbench import routed_stop_policy as policy
from cloudworkbench.environments import EnvironmentRegistry
from cloudworkbench.routed_stage_decision import complete_stage,release_stage_child
from cloudworkbench.routed_decision import complete_task_verification,release_decided_child
from cloudworkbench.routed_verification import run_verification
from cloudworkbench.workflow_revisions import RevisionError


class FinalizedFixtureStop(Exception):
    """Exit the existing success-only helper after real failure completion/release."""


def build_stopped(path,case):
    with pytest.MonkeyPatch.context() as fixture:
        register=EnvironmentRegistry.register
        if case.startswith('task'):
            def environment(registry,manifest):
                manifest=json.loads(json.dumps(manifest))
                if case=='task_rejected':
                    script=path/'protected-check.py'
                    script.write_bytes(b'print("synthetic protected check rejected")\nraise SystemExit(1)\n')
                    manifest['checks'][0]['script_sha256']=six.sha(script.read_bytes())
                else:manifest['checks']=[]
                return register(registry,manifest)
            fixture.setattr(EnvironmentRegistry,'register',environment)
        flow=six.flow.__wrapped__(path)
    count=6 if case.startswith('task') else 5
    for index in range(count):
        stage=path/f'stage-{index}';stage.mkdir(mode=0o700)
        with pytest.MonkeyPatch.context() as boundary:
            if index==count-1 and case=='stage_needs_review':
                answer=six.answer_for
                def needs_review(contract,**kw):
                    value=answer(contract,**kw);value['recommendation']='needs_review';return value
                boundary.setattr(six,'answer_for',needs_review)
                def finalize(*args,**kwargs):
                    decision=complete_stage(*args,**kwargs)
                    assert decision.outcome=='needs_review' and decision.gate is None
                    release=dict(kwargs);release.pop('storage_root');release['decision_sha256']=decision.sha256
                    released=release_stage_child(*args,**release)
                    assert released['decision_sha256']==decision.sha256
                    flow.blocker=decision
                    raise FinalizedFixtureStop()
                boundary.setattr(six,'complete_stage',finalize)
                with pytest.raises(FinalizedFixtureStop):six.execute_stage(flow,index,stage,boundary)
            elif index==count-1 and case.startswith('task'):
                def finalized_verification(scheduler,runtime,spec,results,prepared,verifier,**kwargs):
                    receipt=run_verification(scheduler,runtime,spec,results,prepared,verifier,**kwargs)
                    expected='rejected' if case=='task_rejected' else 'needs_review'
                    assert receipt.outcome==expected
                    decision=complete_task_verification(scheduler,runtime,spec,results,prepared,verifier,
                        input_revision=flow.revision,**kwargs)
                    assert decision.outcome==expected
                    released=release_decided_child(scheduler,runtime,spec,results,prepared,verifier,
                        input_revision=flow.revision,decision_sha256=decision.sha256,**kwargs)
                    assert released['decision_sha256']==decision.sha256
                    flow.blocker=decision
                    raise FinalizedFixtureStop()
                boundary.setattr(six,'run_verification',finalized_verification)
                with pytest.raises(FinalizedFixtureStop):six.execute_stage(flow,index,stage,boundary)
            else:
                six.execute_stage(flow,index,stage,boundary,reject=index==count-1)
    if case=='stage_rejected':flow.blocker=flow.stages[-1]['decision']
    flow.case=case;flow.completed_prefix=count
    return flow


def collect(flow,**kwargs):
    return policy.collect_stopped_workflow_proof(flow.s,flow.root['attempt_id'],expected_generation=1,**kwargs)


@pytest.fixture(scope='module',params=['stage_rejected','stage_needs_review','task_rejected','task_needs_review'])
def stopped(request,tmp_path_factory):
    flow=build_stopped(tmp_path_factory.mktemp('stop-'+request.param),request.param)
    return SimpleNamespace(flow=flow,proof=collect(flow))


def test_authentic_stopped_prefix_has_exact_blocker_and_no_success(stopped,record_property):
    flow,proof=stopped.flow,stopped.proof;public=proof.public_record()
    assert public['kind']=='stopped_workflow'
    assert public['outcome']==('rejected' if flow.case=='task_rejected' else 'needs_review')
    assert public['blocker']==public['decision_refs'][-1]
    assert public['blocker']['artifact_id']==flow.blocker.artifact_id
    assert public['blocker']['gate']==flow.blocker.gate
    assert len(public['decision_refs'])==flow.completed_prefix
    assert all(r['gate']=='pass' for r in public['decision_refs'][:-1])
    assert public['carried_revision_sha256']==flow.revision.sha256
    assert 'storage_path' not in json.dumps(public) and str(flow.initial.path) not in json.dumps(public)
    assert policy.StoppedWorkflowProof.from_json(proof.canonical_json)==proof
    assert collect(flow)==proof
    assert flow.store.get_attempt(flow.root['attempt_id'])['state']=='running'
    assert flow.s.leases.current('account')['reservation'] is not None
    record_property('stopped_prefix',json.dumps({'case':flow.case,'prefix_steps':flow.completed_prefix,
        'frozen_catalog_steps':6,'outcome':public['outcome'],'gate':flow.blocker.gate,
        'root_not_closed':True,'delivered':False,'inference_and_Docker':'synthetic',
        'protected_rejection':'actual local exit1 script' if flow.case=='task_rejected' else
            'empty protected checks retain unmet criterion' if flow.case=='task_needs_review' else 'not executed'}))


def test_historical_root_state_no_current_owner_or_files_in_validate_db(stopped,monkeypatch):
    flow,proof=stopped.flow,stopped.proof
    for name in ('_workspace','load_finalized_stage','load_finalized_task','load_stage_release','load_task_release'):
        monkeypatch.setattr(policy,name,lambda *a,**k:pytest.fail('No file/callback work in final database validation'))
    with flow.store._connect() as db:
        db.execute('BEGIN')
        try:
            db.execute("UPDATE attempts SET state='completed',outcome=?,result='{}' WHERE id=?",(proof.outcome,flow.root['attempt_id']))
            db.execute("UPDATE workflow_roots SET state='released',version=version+1 WHERE root_id=?",(flow.root['attempt_id'],))
            db.execute("UPDATE provider_accounts SET active_id=NULL")
            assert policy.validate_db(db,proof)
        finally:db.rollback()


def test_changed_released_evidence_refused(stopped):
    flow,proof=stopped.flow,stopped.proof
    with flow.store._connect() as db:
        db.execute('BEGIN')
        try:
            db.execute("DELETE FROM events WHERE type='workflow.stage_child_released'")
            with pytest.raises(policy.StoppedPolicyError,match='stopped_evidence_changed'):policy.validate_db(db,proof)
        finally:db.rollback()


def test_full_proof_private_path_obeys_known_secret_scan(stopped):
    with pytest.raises(ValueError,match='secret'):
        collect(stopped.flow,forbidden_values=(Path(stopped.flow.store.path).name.encode(),))


@pytest.mark.parametrize('mutation',[
    lambda v:v.update(schema_version=True),
    lambda v:v['public'].update(outcome='verified'),
    lambda v:v['public'].update(generation=True),
    lambda v:v['public']['blocker'].update(generation=True),
    lambda v:v['public']['decision_refs'][0].update(gate='reject'),
    lambda v:v['public']['decision_refs'][1].update(input_revision_sha256='0'*64),
    lambda v:v['public'].update(extra='unsupported'),
    lambda v:v['private'].update(database_path='relative'),
    lambda v:v['private'].update(snapshot_sha256='not-a-sha'),
])
def test_strict_proof_schema(stopped,mutation):
    value=stopped.proof.to_dict();mutation(value)
    with pytest.raises(ValueError):policy.StoppedWorkflowProof.from_json(policy._raw(value).decode())


def test_future_request_or_pending_call_refused(stopped,monkeypatch):
    original=policy._snapshot
    def changed(db,root):
        snapshot=original(db,root)
        rows=snapshot['role_broker_pending']['rows'];row=dict(rows[0])
        row.update(id='extra-pending',request_key='extra-key',native_call_id='extra-call',controller_request_id=None,terminal_reason=None)
        rows.append(row);return snapshot
    monkeypatch.setattr(policy,'_snapshot',changed)
    with pytest.raises(policy.StoppedPolicyError,match='stopped_pending_calls'):collect(stopped.flow)


def test_root_input_drift_refused(stopped):
    path=stopped.flow.initial.path/'manifest.json';raw=path.read_bytes();mode=path.stat().st_mode&0o7777
    try:
        path.chmod(0o640);path.write_bytes(raw+b' ');path.chmod(mode)
        with pytest.raises(RevisionError,match='manifest_digest_mismatch'):collect(stopped.flow)
    finally:path.chmod(0o640);path.write_bytes(raw);path.chmod(mode)


@pytest.mark.parametrize('change,code',[
    ('missing_release','stopped_child_not_released'),
    ('nonterminal_child','stopped_prefix_incomplete'),
    ('bad_context','stopped_context_changed'),
    ('extra_requested_step','stopped_prefix_incomplete'),
    ('seat_profile','stopped_profile_changed'),
])
def test_incomplete_prefix_cannot_be_closed(stopped,monkeypatch,change,code):
    original=policy._snapshot
    def changed(db,root):
        snapshot=original(db,root)
        if change=='missing_release':
            rows=snapshot['events']['rows']
            rows.remove(next(r for r in rows if r['type']=='workflow.stage_child_released'))
        if change=='nonterminal_child':
            next(r for r in snapshot['attempts']['rows'] if r['execution_kind']=='hermes_child')['state']='running'
        if change=='seat_profile':snapshot['workflow_seats']['rows'][0]['profile']='{}'
        if change=='bad_context':
            row=snapshot['workflow_requests']['rows'][1];payload=json.loads(row['payload'])
            payload['arguments']['context_refs']=[];row['payload']=policy._raw(payload).decode()
        if change=='extra_requested_step':
            rows=snapshot['workflow_requests']['rows'];row=dict(rows[0])
            row.update(id='extra-request',broker_pending_id='extra-pending',ordinal=1000);rows.append(row)
        return snapshot
    monkeypatch.setattr(policy,'_snapshot',changed)
    with pytest.raises(ValueError,match=code):collect(stopped.flow)


def test_earlier_nonpass_cannot_be_skipped(stopped,monkeypatch):
    original=policy.load_finalized_stage
    def changed(*args,**kwargs):
        body,metadata=original(*args,**kwargs)
        body['gate']='reject';body['outcome']='needs_review'
        return body,metadata
    monkeypatch.setattr(policy,'load_finalized_stage',changed)
    with pytest.raises(policy.StoppedPolicyError,match='stopped_prior_not_passed'):collect(stopped.flow)


def test_success_is_not_a_stopped_blocker(stopped,monkeypatch):
    name='load_finalized_task' if stopped.flow.case.startswith('task') else 'load_finalized_stage'
    original=getattr(policy,name)
    def changed(*args,**kwargs):
        body,metadata=original(*args,**kwargs)
        if metadata['id']==stopped.flow.blocker.artifact_id:
            body['gate']='pass';body['outcome']='verified' if stopped.flow.case.startswith('task') else 'unverified'
        return body,metadata
    monkeypatch.setattr(policy,name,changed)
    with pytest.raises(policy.StoppedPolicyError,match='stopped_blocker_required'):collect(stopped.flow)


def test_historical_collection_after_root_release_needs_no_current_owner(stopped,tmp_path):
    import sqlite3
    from cloudworkbench.store import Store
    path=tmp_path/'historical.db'
    with stopped.flow.store._connect() as source,sqlite3.connect(path) as destination:source.backup(destination)
    store=Store(path)
    with store._tx() as db:
        db.execute("UPDATE attempts SET state='completed',outcome=? WHERE id=?",(stopped.proof.outcome,stopped.flow.root['attempt_id']))
        db.execute("UPDATE workflow_roots SET state='released',version=version+1")
        db.execute('UPDATE provider_accounts SET active_id=NULL')
    proof=policy.collect_stopped_workflow_proof(SimpleNamespace(store=store),stopped.flow.root['attempt_id'],expected_generation=1)
    assert proof.public_record()==stopped.proof.public_record()
    assert proof.to_dict()['private']['database_path']==str(path)
