"""Real local scheduler/observations/export with synthetic runtime adapters."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloudworkbench import routed_stage_decision as decision
from cloudworkbench.store import Store, encode
from cloudworkbench.scheduler import RoleScheduler, _child_control_tx
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.pstack_routing import BackendProfile, RoleRouter, requested_policy
from cloudworkbench.workflow_routing import EXPECTED_IDENTITIES, select_workflow
from cloudworkbench.workflow_submission import enqueue_workflow
from cloudworkbench.workflow_revisions import RevisionBinding, capture_revision
from cloudworkbench.role_broker import RoleBroker, ParentScope
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.inference_transport import PinnedCLI
from cloudworkbench.stage_contracts import VERSION, stage_contract
from cloudworkbench.routed_stage import prepare_child_stage
from cloudworkbench.routed_driver import drive_prepared_child
from cloudworkbench.routed_cleanup import RoutedChildCleanup
from cloudworkbench.routed_results import publish_child_results
from tests.test_routed_runtime import setup
from tests.test_routed_collection import event
from tests.test_routed_results import result_phase
from tests import test_routed_driver as driver_test
from tests import test_routed_cleanup as cleanup_test


def make_stage(tmp_path, *, role='code_review', version=VERSION):
    """Planning fixture retains next frozen steps; coding fixture isolates implement."""
    store=Store(tmp_path/'state.db'); owner=store.add_client('stage','x'*40,['submit','observe','cancel'],['demo'])
    store.migrate_scheduler()
    leases=ProviderLeases(store,cleanup_verifier=lambda _:None,inspector_id='fixture',clock=lambda:1000)
    leases.register_account('account',legacy_agent='hermes',persistent_owner_id='owner')
    scheduler=RoleScheduler(store,leases,clock=lambda:1000)
    providers={'anthropic':'claude-code','openai':'openai-codex','xai':'xai-oauth'}
    profiles={name:BackendProfile(name,providers[family],model,'chat_completions' if family=='anthropic' else 'codex_responses',
        effort,family,'a'*64,(effort,)) for name,(family,model,effort) in EXPECTED_IDENTITIES.items()}
    router=RoleRouter(requested_policy(),profiles,ready=lambda _:True)
    workflow = {'planning':'planning','plan_review':'plan_review','code_review':'code_review',
                'how_explorer':'investigation','why_investigator':'why','reflect_tooling':'tooling_reflection'}.get(role,role)
    pstack=tmp_path/'pstack'
    from cloudworkbench.workflow_instructions import _reference
    from cloudworkbench.workflow_routing import WORKFLOWS
    for step in WORKFLOWS[workflow].steps:
        ref=pstack/_reference(step.role);ref.parent.mkdir(parents=True,exist_ok=True);ref.write_text('Inspect the assigned work.')
    request={'project_id':'demo','agent':'hermes','goal':'Work on code.py'}
    # A coding unit needs no fake previous passing gates. Its controller-owned
    # fixture freezes only implement; full production progression is separate.
    if role in decision.CODING_ROLES:
        enqueue_root=scheduler.enqueue_root
        def only_implement(principal,request,key,*,frozen):
            frozen['provenance']['workflow']['steps']=[v for v in frozen['provenance']['workflow']['steps'] if v['role']==role]
            return enqueue_root(principal,request,key,frozen=frozen)
        scheduler.enqueue_root=only_implement
    root=enqueue_workflow(scheduler,owner,request,'root',selection=select_workflow(request['goal'],allowed_workflows=[workflow],explicit_workflow=workflow),
        allowed_workflows=[workflow],router=router,parent=profiles['fable-max'],accounts={'account':'owner'},trusted_pstack_root=pstack,
        _stage_contract_version=version)
    scheduler.admit_root(root['attempt_id'],accounts={'account':'owner'})
    scheduler.transition(root['attempt_id'],'running',expected_generation=1)
    workspace=tmp_path/'work';workspace.mkdir();(workspace/'code.py').write_text('answer = 43\n' if role not in decision.CODING_ROLES else 'answer = 42\n')
    revisions=tmp_path/'revisions';revisions.mkdir()
    binding=RevisionBinding(owner['id'],'demo',root['session_id'],root['turn_id'],root['attempt_id'],1,root['attempt_id'],1)
    revision=capture_revision(store,binding,workspace,revisions,selected_paths=('code.py',),controller_attests_quiesced=True)
    broker=RoleBroker(store.path,authorize_parent=scheduler.authorize_parent,clock=lambda:1000)
    scope=ParentScope(owner['id'],'demo',root['attempt_id'],root['attempt_id'],1,'native')
    _,token=broker.issue(scope,roles=(role,),expires_at=2000)
    pending=broker.prepare(token,native_session_id='native',native_call_id='call',role=role,task='Inspect code.py')
    with store._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?',(root['attempt_id'],)).fetchone()[0])
    step=next(s for s in frozen['provenance']['workflow']['steps'] if s['role']==role)
    scheduler.admit_request(broker,scope,pending['id'],plan=frozen['role_plans'][role],step_id=step['id'],input_revision_sha256=revision.sha256)
    child=scheduler.claim_child(root['attempt_id'])
    profile=profiles[requested_policy()[role][0]]
    execution=(PinnedCLI(Path('/opt/claude'), 'a'*64,'2.1.274',profile.model,profile.effort)
               if profile.provider=='claude-code' else NativeProfile(profile.provider,profile.model,profile.effort))
    return (scheduler,owner,child,revision,pstack,tmp_path/'stage'),execution


@pytest.fixture
def stage(tmp_path,request):
    return make_stage(tmp_path,role=getattr(request,'param','code_review'))


@pytest.fixture
def driven(stage,setup,monkeypatch,request):
    value,profile=stage
    def prepare(value):
        s,_,c,r,p,d=value
        return prepare_child_stage(s,c['id'],expected_generation=1,revision=r,destination=d,
            execution_profile=profile,trusted_pstack_root=p,qualify=lambda *_:True)
    monkeypatch.setattr(driver_test,'prepare',prepare)
    value=driver_test.driven.__wrapped__(value,setup,monkeypatch)
    def run(value,**kwargs):
        s,_,_,prepared,runtime,spec,_,_=value
        result = drive_prepared_child(s,runtime,prepared,spec,execution_profile=profile,**kwargs)
        assert result.caller_cleanup is not None and result.cleanup_error is None and result.grant_fence_confirmed, (result.execution_status, result.cleanup_error, result.grant_fence_confirmed, result.caller_cleanup)
        return result
    monkeypatch.setattr(cleanup_test,'run',run)
    c=stage_contract(stage[0][0].child_assignment(stage[0][2]['id'],expected_generation=1)['role'],stage[0][3].sha256,[])
    answer={'schema_version':1,'contract_sha256':c.digest,'role':c.role,'input_revision_sha256':c.input_revision_sha256,
        'context_refs':[],'summary':'The assigned work is complete.','evidence':[]}
    if c.role in ('code_review','plan_review'):answer.update(recommendation='approve',findings=[])
    else:
        answer['status']='complete'
        if c.role in ('planning','plan_revision'):answer['plan']=[{'step':1,'action':'Inspect code.py.','validation':'Run the protected criterion.'}]
    mode=getattr(request,'param','complete')
    if mode in ('revise','needs_review'):
        answer['recommendation' if c.role in ('code_review','plan_review') else 'status']=mode
    summary='not JSON' if mode=='malformed' else json.dumps(answer)
    value[-1].files['events']=json.dumps(event(summary=summary)).encode()+b'\n'
    if mode=='failed':
        result=json.loads(value[-1].files['result']);result.update(status='failed',failure='hermes_execution_failed',exit_code=1)
        value[-1].files['result']=json.dumps(result).encode()
    return value,profile


@pytest.fixture
def cleanup(driven,tmp_path):
    # The existing real service/dispatch fixture accepts an injected executor
    # profile; no provider request calls are needed for this finalization unit.
    from tests.test_routed_decision import cleanup as old_cleanup
    value,profile=driven
    # Its executor is Sol-specific. Reuse its implementation with profile factory
    # injected only in the fixture module, preserving all authority checks.
    from tests import test_routed_decision as legacy
    previous=legacy.NativeProfile
    legacy.NativeProfile=lambda *args:profile
    generator=old_cleanup.__wrapped__(value,tmp_path)
    try:
        result=next(generator)
    finally:
        legacy.NativeProfile=previous
    yield result
    with pytest.raises(StopIteration):next(generator)


@pytest.fixture
def completed_input(result_phase,stage,tmp_path):
    data,q,args,_=result_phase
    result=publish_child_results(data['scheduler'],data['runtime'],data['driven'][3],data['spec'],q,**args)
    storage=tmp_path/'stage-decisions';storage.mkdir(mode=0o700)
    return SimpleNamespace(data=data,results=result,input=stage[0][3],storage=storage)


def complete(value):
    d=value.data
    return decision.complete_stage(d['scheduler'],d['runtime'],d['spec'],value.results,
        input_revision=value.input,storage_root=value.storage,services=(d['service'],),forbidden_values=())


def release(value,result):
    d=value.data
    return decision.release_stage_child(d['scheduler'],d['runtime'],d['spec'],value.results,
        input_revision=value.input,decision_sha256=result.sha256,services=(d['service'],),forbidden_values=())


def load(value):
    s=value.data['scheduler']
    with _child_control_tx(s.store,write=False) as db:
        return decision.load_finalized_stage(db,value.data['spec'].attempt_id)


def assert_held(value):
    s=value.data['scheduler']
    with s.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0]==value.data['spec'].attempt_id
        assert not db.execute("SELECT 1 FROM events WHERE type='workflow.stage_decision'").fetchone()
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()


@pytest.mark.parametrize('stage',['code_review','planning','plan_review','feature','how_explorer','why_investigator','reflect_tooling'],indirect=True)
def test_complete_authenticated_stage_and_exact_release(completed_input):
    v=completed_input; r=complete(v)
    body,metadata=load(v)
    assert r.outcome=='unverified' and r.gate=='pass'
    assert body['verification_scope']=='stage_contract' and metadata['verification_pass'] is False
    assert body['stage_output']['role']==body['role']
    assert body['carried_revision_sha256']==(v.results.candidate.revision.sha256 if body['role'] in decision.CODING_ROLES else v.input.sha256)
    assert complete(v)==r
    s=v.data['scheduler']
    with _child_control_tx(s.store,write=False) as db:
        assert decision.load_stage_release(db,r.attempt_id) is None
    receipt=release(v,r)
    assert receipt['outcome']=='confirmed_stopped'
    assert release(v,r)==receipt
    assert s.leases.current('account')['reservation']==v.data['reservation']
    with s.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] is None


@pytest.mark.parametrize('driven',['revise','needs_review'],indirect=True)
def test_reviews_block_continuation_without_task_acceptance(completed_input,driven):
    v=completed_input;r=complete(v);body,_=load(v)
    assert r.outcome=='needs_review'
    assert r.gate==('reject' if body['stage_output']['recommendation']=='revise' else None)
    release(v,r)


@pytest.mark.parametrize('driven',['malformed','failed'],indirect=True)
def test_invalid_saved_answer_does_not_finalize(completed_input):
    with pytest.raises(decision.StageDecisionError):complete(completed_input)
    assert_held(completed_input)


def test_readonly_changed_candidate_refused(completed_input,monkeypatch):
    original=decision.verify_revision
    def changed(store,revision):
        result=original(store,revision)
        if revision==completed_input.results.candidate.revision:result={**result,'files':[]}
        return result
    monkeypatch.setattr(decision,'verify_revision',changed)
    with pytest.raises(decision.StageDecisionError,match='readonly_changed'):complete(completed_input)
    assert_held(completed_input)


def test_event_failure_rolls_back_artifact_terminal_and_gate(completed_input,monkeypatch):
    v=completed_input;s=v.data['scheduler'];original=s.store._event
    def fail(db,session,attempt,kind,payload):
        if kind=='workflow.stage_decision':raise RuntimeError('injected')
        return original(db,session,attempt,kind,payload)
    monkeypatch.setattr(s.store,'_event',fail)
    with pytest.raises(RuntimeError,match='injected'):complete(v)
    assert_held(v)
    monkeypatch.setattr(s.store,'_event',original)
    assert complete(v).outcome=='unverified'


def test_cancel_after_staging_refuses_commit(completed_input,monkeypatch):
    v=completed_input;original=decision._immutable
    def cancelled(*a,**k):
        original(*a,**k)
        v.data['scheduler'].store.cancel(v.data['driven'][1],v.data['spec'].attempt_id,'cancel')
    monkeypatch.setattr(decision,'_immutable',cancelled)
    with pytest.raises(ValueError):complete(v)
    assert_held(v)


def test_historical_replay_does_not_touch_new_owner_or_runtime(completed_input,monkeypatch):
    v=completed_input;r=complete(v);released=release(v,r)
    monkeypatch.setattr(v.data['scheduler'],'_owner_current',lambda *_:pytest.fail('historical read'))
    monkeypatch.setattr(decision.RoutedChildCleanup,'collect',lambda *_a,**_k:pytest.fail('historical cleanup'))
    assert complete(v)==r and release(v,r)==released
    assert load(v)[0]['outcome']=='unverified'


@pytest.mark.parametrize('kind',['workflow.stage_decision','artifact.created','attempt.state','workflow.step_gate','workflow.candidate_published'])
def test_loader_rejects_missing_exact_events(completed_input,kind):
    v=completed_input;r=complete(v)
    with v.data['scheduler'].store._tx() as db:
        db.execute('DELETE FROM events WHERE attempt_id=? AND type=?',(r.attempt_id,kind))
    with pytest.raises(decision.StageDecisionError):load(v)


def test_release_failure_retains_occupancy(completed_input,monkeypatch):
    v=completed_input;r=complete(v)
    monkeypatch.setattr(decision.RoutedChildCleanup,'collect',lambda *_a,**_k: (_ for _ in ()).throw(ValueError('uncertain cleanup')))
    with pytest.raises(ValueError,match='uncertain'):release(v,r)
    with v.data['scheduler'].store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0]==r.attempt_id
        assert not db.execute("SELECT 1 FROM events WHERE type='workflow.stage_child_released'").fetchone()


def test_stricter_loader_secret_policy_refuses(completed_input):
    v=completed_input;r=complete(v)
    with _child_control_tx(v.data['scheduler'].store,write=False) as db:
        with pytest.raises(decision.StageDecisionError,match='secret_refused'):
            decision.load_finalized_stage(db,r.attempt_id,forbidden_values=(b'The assigned work',))


@pytest.mark.parametrize('driven',['malformed'],indirect=True)
def test_in_memory_answer_does_not_override_saved_final(completed_input):
    v=completed_input;observation=v.results.quiesced.observation
    assignment=v.data['scheduler'].child_assignment(v.data['spec'].attempt_id,expected_generation=1)
    contract=stage_contract(assignment['role'],v.input.sha256,[])
    answer={'schema_version':1,'contract_sha256':contract.digest,'role':contract.role,
        'input_revision_sha256':v.input.sha256,'context_refs':[],'summary':'Approved.',
        'evidence':[],'recommendation':'approve','findings':[]}
    terminal=replace(observation.events[-1],payload_json=json.dumps(event(summary=json.dumps(answer))['payload']))
    v.results=replace(v.results,quiesced=replace(v.results.quiesced,
        observation=replace(observation,events=(terminal,))))
    with pytest.raises(decision.StageDecisionError,match='output_invalid'):complete(v)
    assert_held(v)


def test_candidate_from_other_attempt_refused(completed_input):
    v=completed_input
    candidate=v.results.candidate
    v.results=replace(v.results,candidate=replace(candidate,artifact_id='not-published-by-child'))
    with pytest.raises(decision.StageDecisionError,match='artifact_missing'):complete(v)
    assert_held(v)


def test_reference_metadata_drift_after_staging_refuses_commit(completed_input,monkeypatch):
    v=completed_input;original=decision._immutable
    def altered(*a,**kw):
        original(*a,**kw)
        with v.data['scheduler'].store._tx() as db:
            row=db.execute('SELECT metadata FROM artifacts WHERE id=?',(v.results.candidate.artifact_id,)).fetchone()
            metadata=json.loads(row[0]);metadata['bytes']+=1
            db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(encode(metadata),v.results.candidate.artifact_id))
    monkeypatch.setattr(decision,'_immutable',altered)
    with pytest.raises(decision.StageDecisionError,match='evidence_changed'):complete(v)
    assert_held(v)


def test_release_event_failure_rolls_back_occupancy_and_retry_is_exact(completed_input,monkeypatch):
    v=completed_input;r=complete(v);s=v.data['scheduler'];original=s.store._event
    def fail(db,session,attempt,kind,payload):
        if kind=='workflow.stage_child_released':raise RuntimeError('release failure')
        return original(db,session,attempt,kind,payload)
    monkeypatch.setattr(s.store,'_event',fail)
    with pytest.raises(RuntimeError,match='release failure'):release(v,r)
    with s.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0]==r.attempt_id
    monkeypatch.setattr(s.store,'_event',original)
    assert release(v,r)['outcome']=='confirmed_stopped'


@pytest.mark.parametrize('field',['controller_instance_id','target','accounts_retained'])
def test_release_loader_rejects_rehashed_changed_identity(completed_input,field):
    v=completed_input;r=complete(v);payload=release(v,r)
    if field=='controller_instance_id':payload[field]='another-instance'
    elif field=='target':payload[field]['reservation_version']+=1
    else:payload[field]=[]
    payload['receipt_sha256']=decision._sha(decision._raw({k:v for k,v in payload.items() if k!='receipt_sha256'}))
    with v.data['scheduler'].store._tx() as db:
        db.execute("UPDATE events SET payload=? WHERE attempt_id=? AND type='workflow.stage_child_released'",(encode(payload),r.attempt_id))
    with _child_control_tx(v.data['scheduler'].store,write=False) as db:
        with pytest.raises(decision.StageDecisionError,match='release_changed'):
            decision.load_stage_release(db,r.attempt_id)


def test_context_loader_requires_transaction(completed_input):
    v=completed_input;r=complete(v)
    with v.data['scheduler'].store._connect() as db:
        with pytest.raises(decision.StageDecisionError,match='read_context_invalid'):
            decision.load_finalized_stage(db,r.attempt_id)


@pytest.mark.parametrize('role',['acceptance_verification','judgment','coordinator','future'])
def test_only_current_nonacceptance_roles_supported(role):
    frozen=encode({'provenance':{'stage_contract_version':VERSION}})
    assignment={'workflow_digest':hashlib.sha256(frozen.encode()).hexdigest(),'role':role}
    with pytest.raises(decision.StageDecisionError,match='role_unsupported'):
        decision._contract({'frozen':frozen},assignment)
