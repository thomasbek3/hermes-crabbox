from dataclasses import replace
import json

import pytest
from cloudworkbench.store import Store, StoreError
from cloudworkbench.scheduler import RoleScheduler
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.pstack_routing import BackendProfile, RoleRouter, requested_policy
from cloudworkbench.workflow_routing import EXPECTED_IDENTITIES, select_workflow
from cloudworkbench.workflow_submission import enqueue_workflow
from cloudworkbench.workflow_revisions import RevisionBinding, capture_revision
from cloudworkbench.role_broker import RoleBroker, ParentScope
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.routed_stage import prepare_child_stage



from tests.test_routed_verification import composed, prepare, execute, acceptance, cancel
from tests.test_routed_results import result_phase
from tests.test_routed_driver import driven as original_driven
from tests.test_routed_runtime import setup
import hashlib
import tempfile
from pathlib import Path
from tests.test_provider_dispatch import FakeRuntime
from cloudworkbench.inference_budget import RootScope, AttemptScope
from cloudworkbench.inference_relay import AttemptBinding, WorkerDispatcher, WorkerSocketServer
from cloudworkbench.provider_dispatch import ProviderDispatch
from cloudworkbench.provider_executor import ProviderExecutor
from cloudworkbench.supervised_executor import SupervisedProviderExecutor
from cloudworkbench.worker_service import WorkerService
from cloudworkbench import routed_decision as decision
from cloudworkbench.routed_verification import VerificationPhaseError

@pytest.fixture
def stage(tmp_path, monkeypatch):
    import tests.test_routed_driver as driver_test
    import tests.test_routed_cleanup as cleanup_test
    from tests.test_routed_stage import prepare as original_prepare
    from cloudworkbench.routed_driver import drive_prepared_child
    profile = NativeProfile('openai-codex', 'gpt-5.6-sol', 'max')
    monkeypatch.setattr(driver_test, 'prepare', lambda value: original_prepare(value, execution_profile=profile))
    def drive(value, **kwargs):
        scheduler, _, _, prepared, runtime, spec, _, _ = value
        return drive_prepared_child(scheduler, runtime, prepared, spec, execution_profile=profile, **kwargs)
    monkeypatch.setattr(cleanup_test, 'run', drive)
    store=Store(tmp_path/'state.db');owner=store.add_client('stage','x'*40,['submit','observe','cancel'],['demo'])
    store.migrate_scheduler()
    leases=ProviderLeases(store,cleanup_verifier=lambda _:None,inspector_id='fixture',clock=lambda:1000)
    leases.register_account('account',legacy_agent='hermes',persistent_owner_id='owner')
    scheduler=RoleScheduler(store,leases,clock=lambda:1000)
    providers={'anthropic':'claude-code','openai':'openai-codex','xai':'xai-oauth'}
    profiles={name:BackendProfile(name,providers[family],model,'chat_completions' if family=='anthropic' else 'codex_responses',
        effort,family,'a'*64,(effort,)) for name,(family,model,effort) in EXPECTED_IDENTITIES.items()}
    router=RoleRouter(requested_policy(),profiles,ready=lambda _:True)
    pstack=tmp_path/'pstack';ref=pstack/'skills/principle-prove-it-works/SKILL.md';ref.parent.mkdir(parents=True);ref.write_text('Review the assigned code.')
    request={'project_id':'demo','agent':'hermes','goal':'Review existing code'}
    root=enqueue_workflow(scheduler,owner,request,'root',selection=select_workflow(request['goal'],allowed_workflows=['verification'],explicit_workflow='verification'),
        allowed_workflows=['verification'],router=router,parent=profiles['fable-max'],accounts={'account':'owner'},trusted_pstack_root=pstack)
    scheduler.admit_root(root['attempt_id'],accounts={'account':'owner'})
    scheduler.transition(root['attempt_id'],'running',expected_generation=1)
    workspace=tmp_path/'work';workspace.mkdir();(workspace/'code.py').write_text('answer = 43\n')
    revisions=tmp_path/'revisions';revisions.mkdir()
    binding=RevisionBinding(owner['id'],'demo',root['session_id'],root['turn_id'],root['attempt_id'],1,root['attempt_id'],1)
    revision=capture_revision(store,binding,workspace,revisions,selected_paths=('code.py',),controller_attests_quiesced=True)
    broker=RoleBroker(store.path,authorize_parent=scheduler.authorize_parent,clock=lambda:1000)
    scope=ParentScope(owner['id'],'demo',root['attempt_id'],root['attempt_id'],1,'native')
    with store._connect() as db:provenance=json.loads(db.execute('SELECT frozen FROM workflow_roots').fetchone()[0])['provenance']
    if 'stage_progression_version' in provenance:
        from cloudworkbench.routed_progression import register_root_input
        register_root_input(scheduler,scope,revision)
    _,token=broker.issue(scope,roles=('acceptance_verification',),expires_at=2000)
    pending=broker.prepare(token,native_session_id='native',native_call_id='call',role='acceptance_verification',task='Review code.py for correctness')
    with store._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?',(root['attempt_id'],)).fetchone()[0])
    scheduler.admit_request(broker,scope,pending['id'],plan=frozen['role_plans']['acceptance_verification'],step_id='verify',input_revision_sha256=revision.sha256)
    child=scheduler.claim_child(root['attempt_id'])
    return scheduler,owner,child,revision,pstack,tmp_path/'stage'

@pytest.fixture
def driven(stage,setup,monkeypatch,request):
    value=original_driven.__wrapped__(stage,setup,monkeypatch)
    if getattr(request,'param',None)=='failed':
        reader=value[-1]
        result=json.loads(reader.files['result'])
        result.update(status='failed',failure='hermes_execution_failed',exit_code=1)
        reader.files['result']=json.dumps(result).encode()
    return value


@pytest.fixture
def cleanup(driven, tmp_path):
    scheduler, owner, child, prepared, runtime, spec, _, _ = driven
    reservation = scheduler.leases.current('account')['reservation']
    grant = scheduler.leases.issue_grant(reservation, attempt_id=child['id'], generation=1)
    provider = FakeRuntime()
    provider.after_request_cleanup = lambda spec: None  # Fixture creates no private material.
    scheduler.leases.verifier = provider.cleanup
    scheduler.leases.inspector_id = 'synthetic-inspector'
    root = scheduler.store.get_attempt(child['workflow_root_id'])
    scope = AttemptScope(RootScope(owner['id'], 'demo', root['session_id'], root['turn_id'], root['id'], 1), child['id'], 1)
    dispatch = ProviderDispatch(scheduler.leases, provider, budget=scheduler.budget, budget_scope=scope)
    binding = AttemptBinding(child['id'], 1, prepared.profile_digest)
    executor = ProviderExecutor(dispatch, reservation=reservation, grant_id=grant, binding=binding,
        profile=NativeProfile('openai-codex','gpt-5.6-sol','max'), remaining_seconds=lambda:90)
    wrapped = SupervisedProviderExecutor(executor)
    worker = WorkerDispatcher(journal_path=tmp_path/'worker.db', binding=binding,
        capability_sha256=hashlib.sha256(b'a'*64).hexdigest(), authorize=wrapped.authorize,
        execute_request=wrapped)
    socket_home=tempfile.TemporaryDirectory(prefix='cc-',dir='/tmp')
    socket_dir=Path(socket_home.name).resolve()
    service=WorkerService(WorkerSocketServer(socket_dir/'w',worker))
    service.start()
    values={'driven':driven,'scheduler':scheduler,'runtime':runtime,'spec':spec,
            'dispatch':dispatch,'provider':provider,'grant':grant,'reservation':reservation,
            'service':service,'wrapped':wrapped,'binding':binding}
    yield values
    service.close(timeout_seconds=2)
    socket_home.cleanup()


def complete(value, prepared):
    d = value.data
    return decision.complete_task_verification(d['scheduler'], d['runtime'], d['spec'], value.results,
        prepared, value.verifier, input_revision=stage_revision(value),
        services=(d['service'],), forbidden_values=())

def stage_revision(value):
    from cloudworkbench.workflow_revisions import WorkspaceRevision, RevisionBinding
    d=value.data; root=d['scheduler'].store.get_attempt(d['spec'].attempt_id)['workflow_root_id']
    a=d['scheduler'].store.get_attempt(root)
    binding=RevisionBinding(d['driven'][1]['id'],'demo',a['session_id'],a['turn_id'],root,1,root,1)
    digest=d['driven'][3].materialization.revision_sha256
    return WorkspaceRevision(value.storage.parent/'revisions'/digest,digest,binding)

def release(value, prepared, receipt):
    d=value.data
    return decision.release_decided_child(d['scheduler'],d['runtime'],d['spec'],value.results,
        prepared,value.verifier,input_revision=stage_revision(value),decision_sha256=receipt.sha256,
        services=(d['service'],),forbidden_values=())

def test_atomic_pass_replay_and_exact_release(composed):
    p=prepare(composed); execute(composed,p)
    r=complete(composed,p)
    assert r.outcome=='verified' and r.gate=='pass'
    assert r.input_revision_sha256!=r.tested_revision_sha256
    assert r.carried_revision_sha256==r.input_revision_sha256
    before=list(composed.calls)
    assert complete(composed,p)==r and composed.calls==before
    s=composed.data['scheduler']
    with s.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0]==r.attempt_id
        assert db.execute('SELECT count(*) FROM workflow_step_gates').fetchone()[0]==1
    released=release(composed,p,r)
    before=list(composed.calls)
    assert release(composed,p,r)==released and composed.calls==before
    assert s.leases.current('account')['reservation']==composed.data['reservation']
    with s.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] is None

def test_missing_publication_never_runs_or_commits(composed):
    p=prepare(composed)
    with pytest.raises(VerificationPhaseError,match='publication_missing'): complete(composed,p)
    assert not composed.launched

def test_event_failure_rolls_back_every_decision_write(composed,monkeypatch):
    p=prepare(composed); execute(composed,p)
    s=composed.data['scheduler']; original=s.store._event
    def fail(db,session,attempt,kind,payload):
        if kind=='workflow.task_decision':raise RuntimeError('injected')
        return original(db,session,attempt,kind,payload)
    monkeypatch.setattr(s.store,'_event',fail)
    with pytest.raises(RuntimeError,match='injected'):complete(composed,p)
    with s.store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()
        assert not db.execute("SELECT 1 FROM artifacts WHERE id LIKE 'decision-%'").fetchone()
    assert s.store.get_attempt(composed.data['spec'].attempt_id)['state']=='running'
    monkeypatch.setattr(s.store,'_event',original)
    assert complete(composed,p).outcome=='verified'

def test_cancel_during_loader_refuses_decision(composed):
    p=prepare(composed); execute(composed,p)
    composed.before_load=lambda _:cancel(composed)
    with pytest.raises(Exception,match='authority'):complete(composed,p)
    with composed.data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()

def test_rejected_checks_commit_reject_not_verified(composed):
    composed.exit_code=1
    p=prepare(composed); execute(composed,p)
    r=complete(composed,p)
    assert (r.outcome,r.gate)==('rejected','reject')

def test_release_event_failure_keeps_occupancy_then_replays(composed,monkeypatch):
    p=prepare(composed);execute(composed,p);r=complete(composed,p)
    s=composed.data['scheduler'];original=s.store._event
    def fail(db,session,attempt,kind,payload):
        if kind=='workflow.decided_child_released':raise RuntimeError('release-crash')
        return original(db,session,attempt,kind,payload)
    monkeypatch.setattr(s.store,'_event',fail)
    with pytest.raises(RuntimeError,match='release-crash'):release(composed,p,r)
    with s.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0]==r.attempt_id
    monkeypatch.setattr(s.store,'_event',original)
    assert release(composed,p,r)['outcome']=='confirmed_stopped'

@pytest.mark.parametrize('kind', ['artifact.created','attempt.state','workflow.step_gate','workflow.task_decision'])
def test_terminal_replay_refuses_missing_commit_event(composed,kind):
    p=prepare(composed);execute(composed,p);r=complete(composed,p)
    s=composed.data['scheduler']
    with s.store._tx() as db:
        if kind=='artifact.created':
            db.execute("DELETE FROM events WHERE type=? AND json_extract(payload,'$.id')=?",(kind,r.artifact_id))
        elif kind=='attempt.state':
            db.execute("DELETE FROM events WHERE attempt_id=? AND type=? AND json_extract(payload,'$.state')='completed'",(r.attempt_id,kind))
        else:db.execute('DELETE FROM events WHERE attempt_id=? AND type=?',(r.attempt_id,kind))
    before=list(composed.calls)
    with pytest.raises(decision.DecisionError,match='event_conflict'):complete(composed,p)
    assert composed.calls==before

@pytest.mark.parametrize('field,value',[('outcome','unknown'),('target',{}),('accounts_retained',[]),('verifier_cleanup',[])])
def test_release_replay_rejects_tampered_full_receipt(composed,field,value):
    p=prepare(composed);execute(composed,p);r=complete(composed,p);release(composed,p,r)
    s=composed.data['scheduler']
    with s.store._tx() as db:
        row=db.execute("SELECT sequence,payload FROM events WHERE type='workflow.decided_child_released'").fetchone()
        body=json.loads(row['payload']);body[field]=value
        db.execute('UPDATE events SET payload=? WHERE sequence=?',(json.dumps(body),row['sequence']))
    before=list(composed.calls)
    with pytest.raises(decision.DecisionError,match='release_conflict'):release(composed,p,r)
    assert composed.calls==before


def test_needs_review_has_no_gate_and_replays(composed):
    composed.environment['checks']=[];composed.scripts={}
    p=prepare(composed);execute(composed,p);r=complete(composed,p)
    assert r.outcome=='needs_review' and r.gate is None
    assert complete(composed,p)==r
    with composed.data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()
    assert release(composed,p,r)['outcome']=='confirmed_stopped'


def test_old_release_api_refuses_decision_managed_child_before_callback(composed):
    p=prepare(composed);execute(composed,p);r=complete(composed,p)
    with pytest.raises(StoreError,match='Decision-managed'):
        composed.data['scheduler'].release_child(r.attempt_id,expected_generation=1,
            verifier=lambda _:pytest.fail('must not inspect via old API'))


def test_final_transaction_cancellation_after_file_staging_refuses_all_writes(composed,monkeypatch):
    p=prepare(composed);execute(composed,p)
    original=decision._immutable
    def cancelled(path,raw,**kw):
        original(path,raw,**kw)
        cancel(composed)
    monkeypatch.setattr(decision,'_immutable',cancelled)
    with pytest.raises(Exception,match='authority'):complete(composed,p)
    with composed.data['scheduler'].store._connect() as db:
        assert not db.execute("SELECT 1 FROM artifacts WHERE id LIKE 'decision-%'").fetchone()
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()


def test_final_transaction_evidence_drift_refused(composed,monkeypatch):
    p=prepare(composed);published=execute(composed,p)
    original=decision._immutable
    def changed(path,raw,**kw):
        original(path,raw,**kw)
        with composed.data['scheduler'].store._tx() as db:
            row=db.execute('SELECT metadata FROM artifacts WHERE id=?',(published.artifact_id,)).fetchone()
            body=json.loads(row[0]);body['outcome']='changed'
            db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(json.dumps(body),published.artifact_id))
    monkeypatch.setattr(decision,'_immutable',changed)
    with pytest.raises(decision.DecisionError,match='evidence_changed'):complete(composed,p)
    with composed.data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()


def test_unknown_verifier_cleanup_keeps_child_slot(composed,monkeypatch):
    p=prepare(composed);execute(composed,p);r=complete(composed,p)
    monkeypatch.setattr(composed.verifier,'reconcile',lambda *a,**k:{'cleanup_confirmed':False})
    with pytest.raises(decision.DecisionError,match='cleanup_unknown'):release(composed,p,r)
    with composed.data['scheduler'].store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0]==r.attempt_id


def test_historical_replays_do_not_check_new_owner_or_call_runtime(composed,monkeypatch):
    p=prepare(composed);execute(composed,p);r=complete(composed,p);released=release(composed,p,r)
    monkeypatch.setattr(composed.data['scheduler'],'_owner_current',lambda *a:pytest.fail('historical read must not act on newer owner'))
    monkeypatch.setattr(composed.verifier,'reconcile',lambda *a,**k:pytest.fail('no runtime replay'))
    assert complete(composed,p)==r
    assert release(composed,p,r)==released


def test_non_acceptance_authority_rejected(composed,monkeypatch):
    p=prepare(composed);execute(composed,p)
    original=decision.result_authority
    def wrong(*a,**k):
        value=original(*a,**k);value['assignment']['role']='planning';return value
    monkeypatch.setattr(decision,'result_authority',wrong)
    with pytest.raises(decision.DecisionError,match='stage_unsupported'):complete(composed,p)


@pytest.mark.parametrize('driven',['failed'],indirect=True)
def test_failed_saved_worker_cannot_become_verified_from_passing_checks(composed):
    assert composed.results.quiesced.observation.status=='failed'
    p=prepare(composed);assert execute(composed,p).outcome=='passed'
    composed.results=replace(composed.results,quiesced=replace(composed.results.quiesced,
        observation=replace(composed.results.quiesced.observation,status='completed',failure=None,exit_code=0)))
    with pytest.raises(decision.DecisionError,match='role_execution_incomplete'):complete(composed,p)
    with composed.data['scheduler'].store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()


def historical(value):
    with value.data['scheduler'].store._tx() as db:
        return decision.load_finalized_task(db,value.data['spec'].attempt_id,forbidden_values=())


def rewrite_committed(value,receipt,mutate=None,raw_transform=None):
    """Deliberately rebind controller DB hashes so inner validation, not outer hash, is tested."""
    st=value.data['scheduler'].store
    with st._tx() as db:
        metadata=json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?',(receipt.artifact_id,)).fetchone()[0])
        path=Path(metadata['storage_path']);body=json.loads(path.read_bytes())
        if mutate:mutate(body)
        raw=decision._raw(body)
        if raw_transform:raw=raw_transform(raw)
        path.chmod(0o600);path.write_bytes(raw)
        digest=hashlib.sha256(raw).hexdigest();metadata.update(sha256=digest,bytes=len(raw))
        db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(decision.encode(metadata),metadata['id']))
        result=json.loads(db.execute('SELECT result FROM attempts WHERE id=?',(receipt.attempt_id,)).fetchone()[0]);result['routed_decision']['sha256']=digest
        db.execute('UPDATE attempts SET result=? WHERE id=?',(decision.encode(result),receipt.attempt_id))
        db.execute("UPDATE events SET payload=? WHERE attempt_id=? AND type='artifact.created' AND json_extract(payload,'$.id')=?",(decision.encode({k:v for k,v in metadata.items() if k!='storage_path'}),receipt.attempt_id,metadata['id']))
        db.execute("UPDATE events SET payload=? WHERE attempt_id=? AND type='workflow.task_decision'",(decision.encode(decision._decision_event(body,metadata['id'],digest)),receipt.attempt_id))
        # Keep immutable gate guards intact; inner validation precedes gate matching.


@pytest.mark.parametrize('change',[
    lambda b:b.update(unexpected_authority=True),lambda b:b.update(schema_version=True),
    lambda b:b.update(generation=True),lambda b:b['input_binding'].update(generation=True),
    lambda b:b.update(carried_revision_sha256='e'*64),
    lambda b:b['verifier_checks'][0].update(extra='hidden'),
    lambda b:b['accounts_retained'][0].update(epoch=True),
    lambda b:b.update(stage_output_sha256='a'*64),
])
def test_historical_rejects_rehashed_inner_schema_or_types(composed,change):
    prepared=prepare(composed);execute(composed,prepared);receipt=complete(composed,prepared)
    rewrite_committed(composed,receipt,change)
    with pytest.raises(decision.DecisionError,match='body_invalid|replay_conflict'):historical(composed)
    with pytest.raises(decision.DecisionError,match='body_invalid|replay_conflict'):complete(composed,prepared)


def test_historical_rejects_rebound_duplicate_json_keys(composed):
    prepared=prepare(composed);execute(composed,prepared);receipt=complete(composed,prepared)
    rewrite_committed(composed,receipt,raw_transform=lambda raw:b'{"schema_version":1,'+raw[1:])
    with pytest.raises(decision.DecisionError,match='json_invalid'):historical(composed)


@pytest.mark.parametrize('evidence',['candidate','verification','observation'])
@pytest.mark.parametrize('change',['missing','truncate','hardlink'])
def test_historical_requires_all_original_evidence_bytes(composed,evidence,change,tmp_path):
    prepared=prepare(composed);execute(composed,prepared);receipt=complete(composed,prepared)
    body,_=historical(composed)
    st=composed.data['scheduler'].store
    with st._connect() as db:
        metadata=json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?',(body[evidence+'_artifact_id'],)).fetchone()[0])
    path=Path(metadata['storage_path'])
    if change=='missing':path.parent.chmod(0o700);path.unlink()
    elif change=='truncate':path.chmod(0o600);path.write_bytes(b'{}')
    else:(tmp_path/'extra-link').hardlink_to(path)
    with pytest.raises(decision.DecisionError):historical(composed)
    assert st.get_attempt(receipt.attempt_id)['state']=='completed'


def test_historical_original_root_input_binding_is_preserved(composed,monkeypatch):
    prepared=prepare(composed);execute(composed,prepared);receipt=complete(composed,prepared)
    monkeypatch.setattr(decision,'load_published_verification',lambda *a,**k:pytest.fail('no runtime reader'))
    monkeypatch.setattr(decision,'load_observation',lambda *a,**k:pytest.fail('no live authority'))
    monkeypatch.setattr(composed.data['scheduler'],'_owner_current',lambda *a:pytest.fail('no current owner'))
    body,_=historical(composed)
    assert body['input_binding']['attempt_id']==body['root_id']!=receipt.attempt_id
    assert complete(composed,prepared)==receipt
