import copy
import pytest
from cloudworkbench.workflow_routing import WORKFLOWS, WorkflowError, payload, parse_assessment, select_workflow, policy_digest


def assessment(choice='feature', confidence=.95):
    return {'model':'jev-test','answers':{'workflow':{'type':'choice','choice':choice,'confidence':confidence,
        'probabilities':{k:float(k==choice) for k in list(WORKFLOWS)+['no_match']}}},'usage':{'input_tokens':100,'output_tokens':10}}

class Client:
    def __init__(self,response=None): self.calls=[]; self.response=response or assessment()
    def evaluate(self,request): self.calls.append(request); return self.response

def test_feature_resolves_exact_approved_sequence():
    c=Client(); r=select_workflow('Build an export feature',allowed_workflows=list(WORKFLOWS),client=c,external_allowed=True).receipt()
    assert [s['profile'] for s in r['steps']]==['fable-max','astra-high','fable-max','grok-xhigh','astra-high','sol-max']
    assert r['dispatch_performed'] is False and r['policy_sha256']==policy_digest()
    assert len(c.calls)==1 and c.calls[0]['state']=={'task_summary':'Build an export feature'}
    assert 'export feature' not in str(r)

def test_explicit_selection_bypasses_jev():
    c=Client(); r=select_workflow('Review code',allowed_workflows=['code_review'],explicit_workflow='code_review',client=c,external_allowed=True)
    assert r.source=='explicit' and r.workflow=='code_review' and not c.calls
    assert r.receipt()['steps'][0]['profile']=='astra-high'

def test_external_optin_required():
    c=Client(); r=select_workflow('Build feature',allowed_workflows=['feature'],client=c)
    assert r.status=='blocked' and not c.calls

@pytest.mark.parametrize('choice,confidence,reason',[('no_match',1,'no_match'),('feature',.1,'low_confidence')])
def test_uncertainty_never_launches_fallback(choice,confidence,reason):
    r=parse_assessment(assessment(choice,confidence),payload('ambiguous',list(WORKFLOWS)),confidence_threshold=.8)
    assert r.workflow is None and r.reason==reason and 'steps' not in r.receipt()

@pytest.mark.parametrize('mutate',[
    lambda r:r['answers']['workflow'].update(choice='invented'),
    lambda r:r['answers']['workflow'].update(type='noul'),
    lambda r:r['answers']['workflow'].update(confidence=True),
    lambda r:r['answers']['workflow'].update(confidence=float('nan')),
    lambda r:r['answers']['workflow']['probabilities'].update(feature=.5),
    lambda r:r['answers']['workflow']['probabilities'].update(invented=0),
    lambda r:r['answers']['workflow']['probabilities'].update(feature=True),
    lambda r:r['answers']['workflow'].update(choice='bug_fix'),
    lambda r:r.update(model='not-jev'),
    lambda r:r['usage'].update(input_tokens=-1),
    lambda r:r['usage'].update(output_tokens=True),
    lambda r:r['answers'].update(other={}),
])
def test_bad_reply_rejected(mutate):
    r=copy.deepcopy(assessment()); mutate(r)
    with pytest.raises(WorkflowError,match='invalid_jev_assessment'):
        parse_assessment(r,payload('work',list(WORKFLOWS)),confidence_threshold=.8)

def test_reply_cannot_expand_allowed_subset():
    with pytest.raises(WorkflowError,match='invalid_jev_assessment'):
        parse_assessment(assessment(),payload('work',['verification']),confidence_threshold=.8)

@pytest.mark.parametrize('allowed',[[],['unknown'],['feature','feature'],'feature',[None]])
def test_invalid_candidates(allowed):
    with pytest.raises(WorkflowError): payload('work',allowed)

def test_api_failure_does_not_retry():
    from cloudworkbench.jev_client import JevError
    class Failed:
        calls=0
        def evaluate(self,request):
            self.calls+=1
            raise JevError('transport_error')
    c=Failed(); r=select_workflow('work',allowed_workflows=['feature'],client=c,external_allowed=True)
    assert r.status=='needs_clarification' and r.workflow is None and c.calls==1

def test_override_cannot_expand_permissions():
    with pytest.raises(WorkflowError,match='explicit_workflow_not_allowed'):
        select_workflow('work',allowed_workflows=['verification'],explicit_workflow='feature')


def test_resolve_workflow_binds_exact_profiles_and_preserves_blocked_step():
    from cloudworkbench.pstack_routing import BackendProfile, RoleRouter, requested_policy
    from cloudworkbench.workflow_routing import resolve_workflow
    from cloudworkbench.workflow_routing import EXPECTED_IDENTITIES
    profiles={p:BackendProfile(p,EXPECTED_IDENTITIES[p][0],EXPECTED_IDENTITIES[p][1],'synthetic',EXPECTED_IDENTITIES[p][2],EXPECTED_IDENTITIES[p][0],'a'*64,(EXPECTED_IDENTITIES[p][2],))
              for p in ['fable-max','grok-xhigh','astra-high','sol-max']}
    router=RoleRouter(requested_policy(),profiles,ready=lambda p:p.profile_id!='astra-high')
    selection=select_workflow('Build export',allowed_workflows=['feature'],explicit_workflow='feature')
    plan=resolve_workflow(selection,'Build export',allowed_workflows=['feature'],router=router,parent=profiles['fable-max'])
    assert not plan['ready'] and len(plan['steps'])==6
    assert [s['id'] for s in plan['steps'] if not s['ready']]==['challenge_plan','review_code']
    assert plan['steps'][-1]['profile']['profile_id']=='sol-max'
    with pytest.raises(WorkflowError,match='stale_workflow_selection'):
        resolve_workflow(selection,'Different work',allowed_workflows=['feature'],router=router,parent=profiles['fable-max'])
    changed=requested_policy(); changed['plan_review']=('grok-xhigh',)
    router=RoleRouter(changed,profiles,ready=lambda p:True)
    with pytest.raises(WorkflowError,match='approved_role_policy_mismatch'):
        resolve_workflow(selection,'Build export',allowed_workflows=['feature'],router=router,parent=profiles['fable-max'])


def test_split_mass_cannot_hide_behind_high_reported_confidence():
    r=assessment(); probs=r['answers']['workflow']['probabilities']
    probs.update(feature=.34,bug_fix=.33,refactoring=.33)
    result=parse_assessment(r,payload('work',list(WORKFLOWS)),confidence_threshold=.8)
    assert result.status=='needs_clarification' and result.confidence_threshold==.8


@pytest.mark.parametrize('workflow',list(WORKFLOWS))
def test_every_workflow_expands_to_approved_roles(workflow):
    result=select_workflow('synthetic task',allowed_workflows=[workflow],explicit_workflow=workflow)
    assert all(s['profile'] in {'fable-max','grok-xhigh','astra-high','sol-max'} for s in result.receipt()['steps'])


@pytest.mark.parametrize('threshold',[0,-.1,1.1,True,float('nan')])
def test_invalid_threshold(threshold):
    with pytest.raises(WorkflowError,match='invalid_confidence_threshold'):
        select_workflow('work',allowed_workflows=['feature'],confidence_threshold=threshold)


@pytest.mark.parametrize('summary',['','   ','x'*12001])
def test_invalid_summary(summary):
    with pytest.raises(WorkflowError,match='invalid_task_summary'):payload(summary,['feature'])


def test_threshold_boundary_is_inclusive():
    r=assessment(confidence=.8)
    r['answers']['workflow']['probabilities'].update(feature=.8,no_match=.2)
    assert parse_assessment(r,payload('work',list(WORKFLOWS)),confidence_threshold=.8).status=='selected'


def test_non_dict_reply_and_missing_client_fail_closed():
    with pytest.raises(WorkflowError,match='invalid_jev_assessment'):
        parse_assessment([],payload('work',list(WORKFLOWS)),confidence_threshold=.8)
    with pytest.raises(WorkflowError,match='jev_client_required'):
        select_workflow('work',allowed_workflows=['feature'],external_allowed=True)


def test_mislabelled_model_identity_is_rejected():
    from cloudworkbench.pstack_routing import BackendProfile,RoleRouter
    from cloudworkbench.workflow_routing import resolve_workflow
    fake=BackendProfile('astra-high','xai','grok-4.6','synthetic','high','xai','a'*64,('high',))
    router=RoleRouter({'code_review':['astra-high']},{'astra-high':fake},ready=lambda _:True)
    selected=select_workflow('Review code',allowed_workflows=['code_review'],explicit_workflow='code_review')
    with pytest.raises(WorkflowError,match='approved_role_policy_mismatch'):
        resolve_workflow(selected,'Review code',allowed_workflows=['code_review'],router=router,parent=fake)
    blocked=select_workflow('Review code',allowed_workflows=['code_review'])
    with pytest.raises(WorkflowError,match='workflow_not_selected'):
        resolve_workflow(blocked,'Review code',allowed_workflows=['code_review'],router=router,parent=fake)
