"""Real scheduler admission bindings; no model/runtime execution."""
import json
from types import SimpleNamespace
import pytest

from cloudworkbench.routed_progression import VERSION, register_root_input, stage_inputs
from cloudworkbench.workflow_submission import enqueue_workflow
from cloudworkbench.workflow_routing import EXPECTED_IDENTITIES,select_workflow
from cloudworkbench.workflow_instructions import _ROLE_REFS,_CODING_REFS
from cloudworkbench.pstack_routing import BackendProfile,RoleRouter,requested_policy
from cloudworkbench.store import Store,StoreError
from cloudworkbench.scheduler import RoleScheduler,ChildCleanupReceipt
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.role_broker import RoleBroker,ParentScope
from cloudworkbench.workflow_revisions import RevisionBinding,capture_revision
from cloudworkbench.stage_contracts import VERSION as CONTRACT_VERSION


@pytest.fixture
def flow(tmp_path):
    store=Store(tmp_path/'state.db');owner=store.add_client('progression','x'*40,['submit','observe'],['demo'])
    store.migrate_scheduler()
    leases=ProviderLeases(store,cleanup_verifier=lambda _:None,inspector_id='fixture',clock=lambda:1000)
    leases.register_account('account',legacy_agent='hermes',persistent_owner_id='owner')
    scheduler=RoleScheduler(store,leases,clock=lambda:1000)
    profiles={name:BackendProfile(name,family,model,'synthetic',effort,family,'a'*64,(effort,))
        for name,(family,model,effort) in EXPECTED_IDENTITIES.items()}
    router=RoleRouter(requested_policy(),profiles,ready=lambda _:True)
    pstack=tmp_path/'pstack'
    for ref in (_ROLE_REFS|_CODING_REFS).values():
        path=pstack/ref;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('Synthetic instructions')
    request={'project_id':'demo','agent':'hermes','goal':'Build sample'}
    root=enqueue_workflow(scheduler,owner,request,'root',
        selection=select_workflow(request['goal'],allowed_workflows=['feature'],explicit_workflow='feature'),
        allowed_workflows=['feature'],router=router,parent=profiles['fable-max'],accounts={'account':'owner'},
        trusted_pstack_root=pstack,_stage_contract_version=CONTRACT_VERSION,_stage_progression_version=VERSION)
    scheduler.admit_root(root['attempt_id'],accounts={'account':'owner'})
    scheduler.transition(root['attempt_id'],'running',expected_generation=1)
    workspace=tmp_path/'workspace';workspace.mkdir();(workspace/'code.py').write_text('answer=42\n')
    revisions=tmp_path/'revisions';revisions.mkdir()
    binding=RevisionBinding(owner['id'],'demo',root['session_id'],root['turn_id'],root['attempt_id'],1,root['attempt_id'],1)
    revision=capture_revision(store,binding,workspace,revisions,selected_paths=('code.py',),controller_attests_quiesced=True)
    scope=ParentScope(owner['id'],'demo',root['attempt_id'],root['attempt_id'],1,'native')
    broker=RoleBroker(store.path,authorize_parent=scheduler.authorize_parent,clock=lambda:1000)
    _,token=broker.issue(scope,roles=('planning','plan_review'),expires_at=2000)
    with store._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots').fetchone()[0])
    return SimpleNamespace(s=scheduler,store=store,owner=owner,root=root,revision=revision,
        workspace=workspace,revisions=revisions,scope=scope,broker=broker,token=token,frozen=frozen)


def request(value,*,role='planning',step='plan',revision=None,refs=(),call='first'):
    pending=value.broker.prepare(value.token,native_session_id='native',native_call_id=call,
        role=role,task='Perform assigned work',context_refs=tuple(refs))
    return value.s.admit_request(value.broker,value.scope,pending['id'],plan=value.frozen['role_plans'][role],
        step_id=step,input_revision_sha256=revision or value.revision.sha256)


def test_first_stage_requires_registered_input(flow):
    with pytest.raises(StoreError,match='Registered workflow input required'):request(flow)
    with flow.store._connect() as db:assert not db.execute('SELECT 1 FROM workflow_seats').fetchone()


def test_registered_input_is_exact_idempotent_and_private_path_stays_private(flow):
    first=register_root_input(flow.s,flow.scope,flow.revision)
    assert register_root_input(flow.s,flow.scope,flow.revision)==first
    result=request(flow)
    assert request(flow)==result
    with flow.store._connect() as db:
        events=db.execute("SELECT payload FROM events WHERE type='workflow.root_input'").fetchall()
        assert len(events)==1 and 'storage_path' not in json.loads(events[0]['payload'])
        assert db.execute('SELECT COUNT(*) FROM workflow_seats').fetchone()[0]==1


def test_initial_revision_substitution_fails(flow):
    register_root_input(flow.s,flow.scope,flow.revision)
    with pytest.raises(StoreError,match='differs from finalized output'):request(flow,revision='a'*64)


def test_first_stage_cannot_inject_context(flow):
    register_root_input(flow.s,flow.scope,flow.revision)
    with pytest.raises(StoreError,match='context differs'):
        request(flow,refs=[{'artifact_id':'other','sha256':'b'*64}])


def test_root_input_cannot_be_replaced(flow):
    register_root_input(flow.s,flow.scope,flow.revision)
    (flow.workspace/'code.py').write_text('answer=0\n')
    other=capture_revision(flow.store,flow.revision.binding,flow.workspace,flow.revisions,
        selected_paths=('code.py',),controller_attests_quiesced=True)
    with pytest.raises(StoreError,match='input is immutable'):register_root_input(flow.s,flow.scope,other)


def test_revoked_client_cannot_register_input(flow):
    with flow.store._tx() as db:db.execute("UPDATE clients SET revoked_at='revoked' WHERE id=?",(flow.owner['id'],))
    with pytest.raises(StoreError,match='authority unavailable'):register_root_input(flow.s,flow.scope,flow.revision)


def test_corrupted_input_event_cannot_admit(flow):
    register_root_input(flow.s,flow.scope,flow.revision)
    with flow.store._tx() as db:db.execute("UPDATE events SET payload='{}' WHERE type='workflow.root_input'")
    with pytest.raises(StoreError,match='input event changed'):request(flow)


@pytest.fixture
def predecessor(flow,monkeypatch):
    """Stub only finalizer readers to isolate admission checks, not finalizer proof."""
    from cloudworkbench import routed_stage_decision
    register_root_input(flow.s,flow.scope,flow.revision);request(flow)
    child=flow.s.claim_child(flow.root['attempt_id'])
    flow.s.transition(child['id'],'running',expected_generation=1)
    flow.s.transition(child['id'],'verifying',expected_generation=1)
    flow.s.transition(child['id'],'completed',expected_generation=1,outcome='verified')
    metadata={'id':'fixture-stage','attempt_id':child['id'],'session_id':child['session_id'],
        'generation':1,'sha256':'b'*64,'reviewed_revision_sha256':flow.revision.sha256}
    with flow.store._tx() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?)',
            (metadata['id'],child['session_id'],child['id'],json.dumps(metadata)))
    flow.s.record_step_gate(child['id'],expected_generation=1,decision='pass',
        revision_sha256=flow.revision.sha256,reviewed_artifact_id=metadata['id'],
        reviewed_artifact_sha256=metadata['sha256'],evidence_sha256='b'*64)
    flow.s.release_child(child['id'],expected_generation=1,
        verifier=lambda target:ChildCleanupReceipt(target,'confirmed_stopped','c'*64))
    body={'root_id':flow.root['attempt_id'],'root_generation':1,'session_id':child['session_id'],
        'step_id':'plan','role':'planning','gate':'pass','input_revision_sha256':flow.revision.sha256,
        'carried_revision_sha256':flow.revision.sha256}
    proof=SimpleNamespace(flow=flow,body=body,metadata=metadata,released=True)
    monkeypatch.setattr(routed_stage_decision,'load_finalized_stage',lambda *a,**kw:(body,metadata))
    monkeypatch.setattr(routed_stage_decision,'load_stage_release',lambda *a,**kw:{'fixture':True} if proof.released else None)
    return proof


def next_request(p,**kwargs):
    args={'role':'plan_review','step':'challenge_plan','call':'second',
        'refs':({'artifact_id':p.metadata['id'],'sha256':p.metadata['sha256']},)}
    args.update(kwargs)
    return request(p.flow,**args)


def test_next_admission_uses_predecessor_carried_output(predecessor):
    p=predecessor;p.body['carried_revision_sha256']='d'*64
    selected=stage_inputs(p.flow.s,p.flow.scope,'challenge_plan')
    assert selected=={'step_id':'challenge_plan','role':'plan_review','input_revision_sha256':'d'*64,
        'context_refs':[{'artifact_id':p.metadata['id'],'sha256':p.metadata['sha256']}]}
    result=next_request(p,revision='d'*64)
    assert next_request(p,revision='d'*64)==result
    with p.flow.store._connect() as db:
        assert db.execute("SELECT input_revision_sha256 FROM workflow_steps WHERE step_id='challenge_plan'").fetchone()[0]=='d'*64


@pytest.mark.parametrize('mutation',[
    {'root_id':'other'}, {'root_generation':2}, {'session_id':'other'},
    {'step_id':'other'}, {'role':'code_review'}, {'gate':'reject'},
    {'input_revision_sha256':'a'*64},
])
def test_wrong_predecessor_scope_cannot_admit(predecessor,mutation):
    predecessor.body.update(mutation)
    with pytest.raises(StoreError,match='output chain changed'):next_request(predecessor)


def test_prior_output_substitution_fails(predecessor):
    predecessor.body['carried_revision_sha256']='d'*64
    with pytest.raises(StoreError,match='differs from finalized output'):next_request(predecessor)


def test_previous_stage_must_have_durable_release(predecessor):
    predecessor.released=False
    with pytest.raises(StoreError,match='cleanup release required'):next_request(predecessor)


@pytest.mark.parametrize('refs',[(),({'artifact_id':'unrelated','sha256':'b'*64},),
    ({'artifact_id':'fixture-stage','sha256':'c'*64},)])
def test_next_context_cannot_omit_or_substitute_finalized_outputs(predecessor,refs):
    with pytest.raises(StoreError,match='context differs'):next_request(predecessor,refs=refs)
