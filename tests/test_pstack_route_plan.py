import pytest
from test_pstack_routing import profile
from cloudworkbench.pstack_routing import RoleRouter,RoutingError


def test_unavailable_panel_keeps_all_seats_and_duplicates_visible():
    a,b=profile(),profile('critic','synthetic-b')
    router=RoleRouter({'arena runners':['worker','critic','worker']},{'worker':a,'critic':b},ready=lambda p:p==b)
    plan=router.plan('arena runners',parent=a)
    assert [p.seat.ordinal for p in plan]==[1,2,3]
    assert [p.seat.profile for p in plan]==[a,b,a]
    assert [p.ready for p in plan]==[False,True,False]
    assert [p.blocked_reason for p in plan]==['backend_not_ready',None,'backend_not_ready']
    with pytest.raises(RoutingError,match='backend_not_ready'):
        router.seats('arena runners',parent=a)


def test_plan_does_not_substitute_available_same_family_cross_judge():
    a,b=profile(),profile('critic','synthetic-b')
    router=RoleRouter({'arena cross-judge pool':['worker','critic']},{'worker':a,'critic':b},ready=lambda p:p==a)
    plan=router.plan('arena cross-judge pool',parent=a)
    assert len(plan)==1 and plan[0].seat.profile==b and not plan[0].ready


def test_plan_observation_does_not_authorize_future_execution():
    a=profile();available=[True]
    router=RoleRouter({'feature':['inherit-parent']},{'worker':a},ready=lambda p:available[0])
    assert router.plan('feature',parent=a)[0].ready
    available[0]=False
    with pytest.raises(RoutingError,match='backend_not_ready'):
        router.seats('feature',parent=a)


def test_readiness_exception_retains_remaining_panel_seats():
    a,b=profile(),profile('critic','synthetic-b')
    observed=[]
    def ready(candidate):
        observed.append(candidate.profile_id)
        if candidate==a:raise RuntimeError('private provider detail')
        return True
    router=RoleRouter({'architect runners':['worker','critic','worker']},{'worker':a,'critic':b},ready=ready)
    plan=router.plan('architect runners',parent=a)
    assert observed==['worker','critic','worker']
    assert [p.ready for p in plan]==[False,True,False]
    assert all(p.blocked_reason in (None,'backend_not_ready') for p in plan)


def test_plan_rejects_unknown_role_and_foreign_parent_before_readiness():
    a=profile();calls=[]
    router=RoleRouter({'feature':['worker']},{'worker':a},ready=lambda p:calls.append(p) or True)
    with pytest.raises(RoutingError,match='invalid_role_route'):
        router.plan('made-up-role',parent=a)
    with pytest.raises(RoutingError,match='invalid_parent_profile'):
        router.plan('feature',parent=profile('foreign'))
    assert not calls
