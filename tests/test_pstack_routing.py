from dataclasses import replace
import pytest
from cloudworkbench.pstack_routing import BackendProfile,RoleRouter,RoutingError

def profile(name='worker',family='synthetic-a'):
    return BackendProfile(name,'synthetic-provider','synthetic-model','synthetic-transport','high',family,'a'*64,('high',))

def test_panel_preserves_each_entry_and_real_profile_identity():
    a,b=profile(),profile('critic','synthetic-b')
    router=RoleRouter({'arena runners':['worker','critic','auto','inherit-parent']},{'worker':a,'critic':b},ready=lambda p:True)
    seats=router.seats('arena runners',parent=a)
    assert [s.profile for s in seats]==[a,b,a,a]
    assert [s.ordinal for s in seats]==[1,2,3,4]
    assert [s.inherited for s in seats]==[False,False,True,True]
    assert all(s.provenance()['runtime']=='hermes' for s in seats)

def test_cross_judge_prefers_different_family_and_does_not_silently_fallback():
    a,b=profile(),profile('critic','synthetic-b')
    calls=[]
    router=RoleRouter({'arena cross-judge pool':['worker','critic']},{'worker':a,'critic':b},ready=lambda p:calls.append(p) or True)
    assert router.seats('arena cross-judge pool',parent=a)[0].profile==b
    assert calls==[b]
    router._ready=lambda p:False
    with pytest.raises(RoutingError,match='backend_not_ready'):router.seats('arena cross-judge pool',parent=a)

def test_revocation_and_readiness_exception_fail_closed():
    a=profile();state={'ready':True}
    router=RoleRouter({'feature':['worker']},{'worker':a},ready=lambda p:state['ready'])
    assert router.seats('feature',parent=a)
    state['ready']=False
    with pytest.raises(RoutingError):router.seats('feature',parent=a)
    router._ready=lambda p:1/0
    with pytest.raises(RoutingError,match='backend_not_ready'):router.seats('feature',parent=a)

@pytest.mark.parametrize('routes',[{'feature':['unknown']},{'feature':['worker','worker']},{'invented':['worker']},{'arena runners':[]},{'arena runners':['worker']*9},{'feature':'worker'}])
def test_invalid_routes_rejected(routes):
    with pytest.raises(RoutingError):RoleRouter(routes,{'worker':profile()},ready=lambda p:True)

def test_missing_role_is_not_a_silent_default():
    router=RoleRouter({'feature':['worker']},{'worker':profile()},ready=lambda p:True)
    with pytest.raises(RoutingError,match='role_not_configured'):router.seats('bug-fix',parent=profile())

def test_configuration_hash_binds_backend_and_effort_without_credentials():
    a=profile();b=replace(a,model='different-synthetic-model')
    x=RoleRouter({'feature':['worker']},{'worker':a},ready=lambda p:True)
    y=RoleRouter({'feature':['worker']},{'worker':b},ready=lambda p:True)
    assert x.sha256!=y.sha256
    assert 'qualification_sha256' in x.seats('feature',parent=a)[0].provenance()

@pytest.mark.parametrize('updates',[{'effort':'max'},{'qualification_sha256':''},{'model':'bad model'},{'profile_id':'auto'}])
def test_unqualified_or_unsupported_profiles_rejected(updates):
    with pytest.raises(RoutingError):replace(profile(),**updates)

@pytest.mark.parametrize('updates', [
    {'qualification_sha256': None}, {'qualification_sha256': []},
    {'supported_efforts': ([],)}, {'supported_efforts': ('invented',)},
    {'effort': []},
])
def test_invalid_profile_values_have_stable_routing_errors(updates):
    with pytest.raises(RoutingError):
        replace(profile(), **updates)


def test_same_family_judge_fallback_is_visible_and_deterministic():
    a, b = profile(), profile('second')
    router = RoleRouter({'arena_cross_judge': ['worker', 'second']},
                        {'worker': a, 'second': b}, ready=lambda p: True)
    seat, = router.seats('arena_cross_judge', parent=a)
    assert seat.profile == a and seat.ordinal == 1
    assert seat.provenance()['diversity_fallback'] is True
    assert seat.provenance()['qualification_reference_verified'] is False


def test_unready_different_family_does_not_choose_another_ready_seat():
    a, b, c = profile(), profile('critic', 'synthetic-b'), profile('other', 'synthetic-c')
    called = []
    router = RoleRouter({'arena_cross_judge': ['worker', 'critic', 'other']},
                        {p.profile_id: p for p in (a, b, c)},
                        ready=lambda p: called.append(p.profile_id) or p != b)
    with pytest.raises(RoutingError, match='backend_not_ready'):
        router.seats('arena_cross_judge', parent=a)
    assert called == ['critic']


def test_display_labels_are_explicit_aliases_not_fuzzy_matching():
    a = profile()
    canonical = RoleRouter({'bug_fix': ['worker']}, {'worker': a}, ready=lambda p: True)
    display = RoleRouter({'bug-fix': ['worker']}, {'worker': a}, ready=lambda p: True)
    assert canonical.sha256 == display.sha256
    assert display.seats('bug-fix', parent=a)[0].role == 'bug_fix'
    with pytest.raises(RoutingError, match='duplicate_role_route'):
        RoleRouter({'bug_fix': ['worker'], 'bug-fix': ['worker']},
                   {'worker': a}, ready=lambda p: True)
    with pytest.raises(RoutingError):
        canonical.seats([], parent=a)
    with pytest.raises(RoutingError):
        canonical.seats('fix a bug', parent=a)


def test_requested_policy_preserves_models_and_has_no_qualified_defaults():
    from cloudworkbench.pstack_routing import requested_policy, ROLES, PANELS
    policy = requested_policy()
    assert set(policy) == ROLES - PANELS - {'arena_cross_judge'}
    assert policy['coordinator'] == policy['synthesis'] == ('fable-max',)
    assert policy['feature'] == ('grok-xhigh',)
    assert policy['plan_review'] == policy['code_review'] == ('astra-high',)
    assert policy['acceptance_verification'] == policy['reflect_tooling'] == ('sol-max',)
    with pytest.raises(RoutingError, match='unresolved_backend_route'):
        RoleRouter(policy, {}, ready=lambda p: True)


def test_ambiguous_combined_display_label_is_not_silently_partial():
    with pytest.raises(RoutingError, match='invalid_role_route'):
        RoleRouter({'judgment and prose': ['worker']}, {'worker': profile()}, ready=lambda p: True)


def test_blocked_judge_preserves_selected_fallback_receipt():
    a = profile()
    router = RoleRouter({'arena_cross_judge': ['auto', 'auto']}, {'worker': a}, ready=lambda p: False)
    with pytest.raises(RoutingError, match='backend_not_ready') as raised:
        router.seats('arena_cross_judge', parent=a)
    assert raised.value.receipt['seat'] == 1
    assert raised.value.receipt['diversity_fallback'] is True
    assert raised.value.receipt['inherited'] is True
    assert raised.value.receipt['routing_sha256'] == router.sha256


def test_late_panel_failure_returns_no_partial_seats_and_records_blocker():
    a, b = profile(), profile('critic', 'synthetic-b')
    calls = []
    router = RoleRouter({'arena_runner': ['worker', 'auto', 'worker', 'critic']},
                        {'worker': a, 'critic': b}, ready=lambda p: calls.append(p) or p != b)
    with pytest.raises(RoutingError, match='backend_not_ready') as raised:
        router.seats('arena_runner', parent=a)
    assert calls == [a, a, a, b]
    assert raised.value.receipt['seat'] == 4
    assert raised.value.receipt['profile_id'] == 'critic'


def test_inherited_readiness_requires_boolean_true():
    a = profile()
    router = RoleRouter({'feature': ['auto']}, {'worker': a}, ready=lambda p: 1)
    with pytest.raises(RoutingError, match='backend_not_ready') as raised:
        router.seats('feature', parent=a)
    assert raised.value.receipt['inherited'] is True


@pytest.mark.parametrize('changes', [
    {'family': 'Anthropic'}, {'provider': 'anthropic', 'family': 'xai'},
    {'provider': 'xai-oauth', 'family': 'anthropic'},
])
def test_provider_lineage_mismatch_is_refused(changes):
    with pytest.raises(RoutingError, match='invalid_provider_lineage'):
        replace(profile(), **changes)


def test_inherited_parent_cannot_drift_outside_frozen_policy():
    a = profile()
    router = RoleRouter({'feature': ['auto']}, {'worker': a}, ready=lambda p: True)
    with pytest.raises(RoutingError, match='invalid_parent_profile'):
        router.seats('feature', parent=replace(a, model='drifted'))


def test_all_requested_roles_match_user_policy():
    from cloudworkbench.pstack_routing import requested_policy
    assert requested_policy() == {
        'coordinator': ('fable-max',), 'synthesis': ('fable-max',),
        'feature': ('grok-xhigh',), 'refactoring': ('grok-xhigh',),
        'bug_fix': ('grok-xhigh',), 'perf_issue': ('grok-xhigh',),
        'hillclimb': ('grok-xhigh',), 'how_explorer': ('grok-xhigh',),
        'how_explainer': ('fable-max',), 'why_investigator': ('grok-xhigh',),
        'why_synthesizer': ('fable-max',), 'reflect_tooling': ('sol-max',),
        'reflect_judgment': ('fable-max',), 'reflect_divergent': ('fable-max',),
        'reflect_synthesizer': ('fable-max',), 'swarm_worker': ('grok-xhigh',),
        'judgment': ('fable-max',), 'prose': ('fable-max',), 'hardest': ('fable-max',),
        'planning': ('fable-max',), 'plan_revision': ('fable-max',),
        'plan_review': ('astra-high',), 'code_review': ('astra-high',),
        'acceptance_verification': ('sol-max',),
    }
