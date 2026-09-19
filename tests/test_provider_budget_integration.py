import uuid
import pytest
from test_provider_dispatch import setup
from cloudworkbench.inference_budget import InferenceBudget,RootScope,AttemptScope,BudgetPolicy,ensure_schema
from cloudworkbench.provider_dispatch import ProviderDispatch,BudgetAdmissionRefused


def configure(setup,policy=BudgetPolicy()):
    store,principal,attempt,leases,owner,grant,runtime,dispatch,clock=setup
    with store._connect() as db:
        row=db.execute('SELECT a.session_id,a.turn_id,s.owner_id,s.project_id FROM attempts a JOIN sessions s ON s.id=a.session_id WHERE a.id=?',(attempt['attempt_id'],)).fetchone()
    root=RootScope(row['owner_id'],row['project_id'],row['session_id'],row['turn_id'],attempt['attempt_id'],attempt['generation'])
    scope=AttemptScope(root,attempt['attempt_id'],attempt['generation'])
    budget=InferenceBudget(clock=lambda:clock[0])
    with store._tx() as db:
        ensure_schema(db);budget.register_root(db,root,policy,admitted_at=clock[0]);budget.register_attempt(db,scope)
    dispatch=ProviderDispatch(leases,runtime,budget=budget,budget_scope=scope)
    return dispatch,budget,scope


def admit(setup,d,nonce=None):
    return d.admit(setup[4],setup[5],request_nonce=nonce or uuid.uuid4().hex*2,payload=b'payload',profile_digest='b'*64)


def test_atomic_charge_and_dispatch_replay(setup):
    d,budget,scope=configure(setup);nonce='a'*64
    first=admit(setup,d,nonce);again=admit(setup,d,nonce)
    assert first==again
    with setup[0]._connect() as db:
        assert db.execute('SELECT used FROM inference_budget_roots').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM provider_dispatch').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM inference_budget_requests').fetchone()[0]==1


def test_dispatch_insert_failure_rolls_back_budget_charge(setup):
    d,budget,scope=configure(setup)
    with setup[0]._tx() as db:
        db.execute("CREATE TRIGGER reject_dispatch BEFORE INSERT ON provider_dispatch BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(Exception):admit(setup,d)
    with setup[0]._connect() as db:
        assert db.execute('SELECT used FROM inference_budget_roots').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM inference_budget_requests').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM provider_request_leases').fetchone()[0]==0


def test_root_limit_refuses_without_creating_dispatch(setup):
    d,budget,scope=configure(setup,BudgetPolicy(root_requests=1))
    spec=admit(setup,d);d.launch(spec.lease.request_id,b'payload');d.collect(spec.lease.request_id);d.cleanup(spec.lease.request_id)
    with pytest.raises(BudgetAdmissionRefused,match='root_request_limit'):admit(setup,d)
    with setup[0]._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM provider_dispatch').fetchone()[0]==1
        assert db.execute('SELECT used FROM inference_budget_roots').fetchone()[0]==1


def test_expiry_observation_commits_and_clock_rollback_cannot_revive(setup):
    d,budget,scope=configure(setup,BudgetPolicy(root_wall_seconds=200))
    setup[8][0]=1201
    with pytest.raises(BudgetAdmissionRefused,match='root_budget_exceeded'):admit(setup,d)
    setup[8][0]=1001
    with pytest.raises(BudgetAdmissionRefused,match='budget_clock_rollback'):admit(setup,d)
    with setup[0]._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM provider_dispatch').fetchone()[0]==0
        assert db.execute('SELECT high_water_us FROM inference_budget_roots').fetchone()[0]==1201000000


def test_wrong_frozen_owner_refused_before_charge(setup):
    from dataclasses import replace
    from cloudworkbench.provider_dispatch import DispatchError
    d,budget,scope=configure(setup)
    d.budget_scope=replace(scope,root=replace(scope.root,owner_id='other-owner'))
    with pytest.raises(DispatchError,match='budget_root_authorization_failed'):admit(setup,d)
    with setup[0]._connect() as db:
        assert db.execute('SELECT used FROM inference_budget_roots').fetchone()[0]==0


def test_budget_pair_required(setup):
    from cloudworkbench.provider_dispatch import DispatchError
    with pytest.raises(DispatchError,match='invalid_budget_binding'):
        ProviderDispatch(setup[3],setup[6],budget=InferenceBudget())


def test_executor_budget_refusal_preserves_policy_status(setup):
    from dataclasses import replace
    import threading
    from test_provider_executor import executor
    from test_hermes_inference_protocol import REQUEST
    adapter,context,envelope=executor(setup)
    d,budget,scope=configure(setup,BudgetPolicy(root_requests=1))
    adapter.dispatch=d
    first=adapter(context,REQUEST,threading.Event())
    assert first.response.status==200
    refused=adapter(replace(context,request_nonce='f'*64),REQUEST,threading.Event())
    assert refused.response.status==429 and refused.outer_cleanup_confirmed
    assert b'root_request_limit' in refused.response.body
