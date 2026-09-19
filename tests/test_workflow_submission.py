import json
import pytest
from cloudworkbench.pstack_routing import BackendProfile, RoleRouter, requested_policy
from cloudworkbench.workflow_routing import WorkflowError, select_workflow
from cloudworkbench.workflow_submission import enqueue_workflow
from cloudworkbench.store import Store, StoreError
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.scheduler import RoleScheduler


def test_real_scheduler_freezes_selection_and_exact_profiles(tmp_path):
    store=Store(tmp_path/'state.db')
    owner=store.add_client('TEST workflow','x'*40,['submit','observe'],['demo'])
    store.migrate_scheduler()
    leases=ProviderLeases(store,cleanup_verifier=lambda _:None,inspector_id='fixture')
    leases.register_account('synthetic',legacy_agent='hermes',persistent_owner_id='test-owner')
    scheduler=RoleScheduler(store,leases)
    from cloudworkbench.workflow_routing import EXPECTED_IDENTITIES
    profiles={p:BackendProfile(p,EXPECTED_IDENTITIES[p][0],EXPECTED_IDENTITIES[p][1],'synthetic',EXPECTED_IDENTITIES[p][2],EXPECTED_IDENTITIES[p][0],'a'*64,(EXPECTED_IDENTITIES[p][2],))
              for p in ('fable-max','grok-xhigh','astra-high','sol-max')}
    router=RoleRouter(requested_policy(),profiles,ready=lambda _:True)
    selection=select_workflow('Build sample export',allowed_workflows=['feature'],explicit_workflow='feature')
    from cloudworkbench.workflow_instructions import _ROLE_REFS, _CODING_REFS
    pstack=tmp_path/'pstack'
    for ref in (_ROLE_REFS | _CODING_REFS).values():
        path=pstack/ref;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('Synthetic instructions')
    args=dict(selection=selection,allowed_workflows=['feature'],router=router,parent=profiles['fable-max'],accounts={'synthetic':'test-owner'},trusted_pstack_root=pstack)
    request={'project_id':'demo','agent':'hermes','goal':'Build sample export'}
    result=enqueue_workflow(scheduler,owner,request,'workflow-1',**args)
    assert enqueue_workflow(scheduler,owner,request,'workflow-1',**args)==result
    with store._connect() as db:
        frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?',(result['attempt_id'],)).fetchone()[0])
        assert db.execute('SELECT COUNT(*) FROM workflow_roots').fetchone()[0]==1
    plan=frozen['provenance']['workflow']
    assert [s['profile']['profile_id'] for s in plan['steps']]==['fable-max','astra-high','fable-max','grok-xhigh','astra-high','sol-max']
    assert plan['selection']['source']=='explicit'
    assert len(frozen['provenance']['pstack_references'])==6
    with pytest.raises(WorkflowError,match='stale_workflow_selection'):
        enqueue_workflow(scheduler,owner,dict(request,goal='Changed task'),'workflow-2',**args)
    router._ready=lambda p:p.profile_id!='astra-high'
    with pytest.raises(WorkflowError,match='workflow_backend_not_ready'):
        enqueue_workflow(scheduler,owner,request,'workflow-3',**args)
    assert store.claim_next() is None
