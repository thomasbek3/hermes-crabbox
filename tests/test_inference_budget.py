from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from cloudworkbench.inference_budget import (
    Admission, AttemptScope, BudgetError, BudgetPolicy, InferenceBudget, RootScope, ensure_schema,
)

ROOT = RootScope('owner', 'project', 'session', 'turn', 'root', 1)
ROOT_ATTEMPT = AttemptScope(ROOT, 'root', 1)


@contextmanager
def transaction(path, *, immediate=True):
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'state.db'
    clock = [1000.0]
    budget = InferenceBudget(clock=lambda: clock[0])
    with transaction(path) as db:
        ensure_schema(db)
        budget.register_root(db, ROOT, admitted_at=1000)
        budget.register_attempt(db, ROOT_ATTEMPT)
    return path, clock, budget


def nonce(n):
    return hashlib.sha256(str(n).encode()).hexdigest()


def request(budget, db, scope=ROOT_ATTEMPT, n=0, **kw):
    return budget.admit(db, scope, nonce=nonce(n), payload_digest=kw.get('payload', 'a'*64), profile_digest=kw.get('profile', 'b'*64))


def count(path):
    with sqlite3.connect(path) as db:
        return (db.execute('SELECT used FROM inference_budget_roots ORDER BY root_id').fetchall(),
                db.execute('SELECT used FROM inference_budget_attempts ORDER BY attempt_id').fetchall(),
                db.execute('SELECT count(*) FROM inference_budget_requests').fetchone()[0])


def policy_setup(tmp_path, policy):
    path = tmp_path / 'custom.db'
    clock = [1000.0]
    budget = InferenceBudget(clock=lambda: clock[0])
    with transaction(path) as db:
        ensure_schema(db)
        budget.register_root(db, ROOT, policy, admitted_at=1000)
        budget.register_attempt(db, ROOT_ATTEMPT)
    return path, clock, budget


def test_defaults_are_exact_d9_policy():
    assert BudgetPolicy() == BudgetPolicy('d9-v1', 128, 32, 60, 4, 14400, 160)


def test_registration_and_refusal_require_explicit_transaction(setup):
    path, _, budget = setup
    with sqlite3.connect(path) as db:
        for call in (lambda: ensure_schema(db), lambda: budget.register_root(db, ROOT, admitted_at=1000),
                     lambda: budget.register_attempt(db, ROOT_ATTEMPT), lambda: request(budget, db)):
            with pytest.raises(BudgetError, match='budget_transaction_required'):
                call()


def test_default_burst_retry_and_rate_refill(setup):
    path, clock, budget = setup
    with transaction(path) as db:
        results = [request(budget, db, n=i) for i in range(5)]
        assert [r.decision for r in results] == ['charged']*4 + ['refused']
        assert results[-1].reason == 'attempt_rate_limit'
        assert results[-1].retry_after_seconds == 1
        assert request(budget, db, n=0).decision == 'replay'
    clock[0] += .5
    with transaction(path) as db:
        assert request(budget, db, n=5).retry_after_seconds == .5
    clock[0] += .5
    with transaction(path) as db:
        assert request(budget, db, n=5).decision == 'charged'
        assert request(budget, db, n=6).reason == 'attempt_rate_limit'
    assert count(path) == ([(5,)], [(5,)], 5)


def test_new_nonce_identical_payload_charges_each_call(setup):
    path, _, budget = setup
    with transaction(path) as db:
        first = request(budget, db, n=0)
        second = request(budget, db, n=1)
        assert second.attempt_requests_used == first.attempt_requests_used+1
        assert second.policy_digest == first.policy_digest


@pytest.mark.parametrize('field', ['payload', 'profile'])
def test_nonce_conflict_is_not_a_retry(setup, field):
    path, _, budget = setup
    with transaction(path) as db:
        request(budget, db)
    with transaction(path) as db:
        with pytest.raises(BudgetError, match='budget_nonce_conflict'):
            request(budget, db, **{field:'c'*64})
    assert count(path) == ([(1,)], [(1,)], 1)


def test_attempt_limit_spans_restart_and_generation_and_replay_at_cap(tmp_path):
    path, clock, budget = policy_setup(tmp_path, BudgetPolicy(attempt_requests=2))
    with transaction(path) as db:
        request(budget, db, n=0)
    newer = replace(ROOT_ATTEMPT, generation=2)
    budget = InferenceBudget(clock=lambda: clock[0])
    with transaction(path) as db:
        budget.register_attempt(db, newer, previous_generation=1)
        assert request(budget, db, newer, n=0).decision == 'charged'
        assert request(budget, db, newer, n=1).reason == 'attempt_request_limit'
        assert request(budget, db, newer, n=0).decision == 'replay'
        with pytest.raises(BudgetError, match='budget_generation_conflict'):
            request(budget, db, ROOT_ATTEMPT, n=0)
    assert count(path) == ([(2,)], [(2,)], 2)


def test_generation_change_does_not_reset_rate_bucket(setup):
    path, _, budget = setup
    with transaction(path) as db:
        for i in range(4):request(budget, db, n=i)
        newer = replace(ROOT_ATTEMPT, generation=2)
        budget.register_attempt(db, newer, previous_generation=1)
        assert request(budget, db, newer, n=5).reason == 'attempt_rate_limit'


def test_root_limit_includes_children_and_grandchildren(tmp_path):
    path, _, budget = policy_setup(tmp_path, BudgetPolicy(root_requests=3))
    child, grandchild = AttemptScope(ROOT, 'child', 1), AttemptScope(ROOT, 'grandchild', 1)
    with transaction(path) as db:
        budget.register_attempt(db, child, parent=ROOT_ATTEMPT)
        budget.register_attempt(db, grandchild, parent=child)
        for scope in (ROOT_ATTEMPT, child, grandchild):
            assert request(budget, db, scope).decision == 'charged'
        assert request(budget, db, child, n=1).reason == 'root_request_limit'
        assert request(budget, db, grandchild).decision == 'replay'
    assert count(path) == ([(3,)], [(1,), (1,), (1,)], 3)


@pytest.mark.parametrize('field,value', [('owner_id','other'),('project_id','other'),('session_id','other'),('turn_id','other'),('root_generation',2)])
def test_frozen_root_scope_cannot_be_rebound(setup, field, value):
    path, _, budget = setup
    changed = replace(ROOT, **{field:value})
    with transaction(path) as db:
        with pytest.raises(BudgetError, match='root_budget_conflict'):
            budget.register_root(db, changed, admitted_at=1000)
        with pytest.raises(BudgetError, match='root_budget_scope_mismatch'):
            request(budget, db, replace(ROOT_ATTEMPT, root=changed))


def test_policy_admission_and_deadline_frozen(setup):
    path, clock, budget = setup
    clock[0] = 1001
    with transaction(path) as db:
        original = budget.register_root(db, ROOT, admitted_at=1000)
        assert original == budget.register_root(db, ROOT, admitted_at=1000)
        for policy, admitted in ((BudgetPolicy(root_requests=129),1000),(BudgetPolicy(version='v2'),1000),(BudgetPolicy(),1001)):
            with pytest.raises(BudgetError, match='root_budget_conflict'):
                budget.register_root(db, ROOT, policy, admitted_at=admitted)
        with pytest.raises(BudgetError, match='attempt_budget_conflict'):
            budget.register_attempt(db, ROOT_ATTEMPT, deadline_at=2000)


def test_parent_is_explicit_and_cross_root_or_cycles_refused(setup):
    path, _, budget = setup
    child = AttemptScope(ROOT,'child',1)
    otherroot = replace(ROOT, root_attempt_id='other')
    other = AttemptScope(otherroot,'other',1)
    with transaction(path) as db:
        with pytest.raises(BudgetError, match='budget_parent_required'):
            budget.register_attempt(db, child)
        with pytest.raises(BudgetError, match='budget_parent_mismatch'):
            budget.register_attempt(db, child, parent=child)
        with pytest.raises(BudgetError, match='budget_parent_mismatch'):
            budget.register_attempt(db, child, parent=replace(ROOT_ATTEMPT,generation=2))
        budget.register_root(db, otherroot, admitted_at=1000)
        budget.register_attempt(db, other)
        with pytest.raises(BudgetError, match='budget_parent_mismatch'):
            budget.register_attempt(db, child, parent=other)
        budget.register_attempt(db, child, parent=ROOT_ATTEMPT)
        with pytest.raises(BudgetError, match='budget_parent_mismatch'):
            budget.register_attempt(db, ROOT_ATTEMPT, parent=child)
        with pytest.raises(BudgetError, match='attempt_budget_conflict'):
            budget.register_attempt(db, replace(child,root=otherroot), parent=other)


def test_concurrent_root_cap_and_deferred_transactions(tmp_path):
    path, _, budget = policy_setup(tmp_path, BudgetPolicy(root_requests=7,burst=100))
    children = [AttemptScope(ROOT,f'child-{i}',1) for i in range(20)]
    with transaction(path) as db:
        for child in children:budget.register_attempt(db, child, parent=ROOT_ATTEMPT)
    def call(child):
        with transaction(path, immediate=False) as db:return request(budget,db,child).decision
    with ThreadPoolExecutor(max_workers=10) as pool:
        results=list(pool.map(call,children))
    assert results.count('charged') == 7
    assert results.count('refused') == 13
    assert count(path)[0] == [(7,)] and count(path)[2] == 7


def test_concurrent_same_nonce_charges_once(setup):
    path, _, budget = setup
    def call(_):
        with transaction(path) as db:return request(budget,db).decision
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(call,range(16)))
    assert results.count('charged') == 1 and results.count('replay') == 15
    assert count(path) == ([(1,)], [(1,)], 1)


def test_failure_after_charge_rolls_back_dispatch_and_budget(setup):
    path, _, budget = setup
    with transaction(path) as db:db.execute('CREATE TABLE dispatch(nonce TEXT PRIMARY KEY)')
    with pytest.raises(RuntimeError):
        with transaction(path) as db:
            request(budget,db)
            db.execute('INSERT INTO dispatch VALUES(?)',(nonce(0),))
            raise RuntimeError('synthetic later failure')
    assert count(path) == ([(0,)], [(0,)], 0)
    with transaction(path) as db:
        assert db.execute('SELECT count(*) FROM dispatch').fetchone()[0] == 0
        assert request(budget,db).decision == 'charged'
        db.execute('INSERT INTO dispatch VALUES(?)',(nonce(0),))
    assert count(path) == ([(1,)], [(1,)], 1)


def test_internal_write_failure_caught_by_caller_does_not_partially_charge(setup):
    path, _, budget = setup
    with transaction(path) as db:
        db.execute("CREATE TRIGGER reject_request BEFORE INSERT ON inference_budget_requests BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with pytest.raises(sqlite3.IntegrityError):request(budget,db)
    assert count(path) == ([(0,)], [(0,)], 0)


def test_clock_rollback_fails_closed_across_new_instance(setup):
    path, clock, budget = setup
    clock[0] = 1005
    with transaction(path) as db:request(budget,db)
    clock[0] = 1004
    restarted = InferenceBudget(clock=lambda:clock[0])
    with transaction(path) as db:
        assert request(restarted,db,n=1).reason == 'budget_clock_rollback'
        assert request(restarted,db,n=0).reason == 'budget_clock_rollback'
    clock[0] = 1005
    with transaction(path) as db:assert request(restarted,db,n=1).decision == 'charged'


def test_committed_expiry_observation_cannot_be_revived_by_clock_rollback(setup):
    path, clock, budget = setup
    clock[0] = 15400
    with transaction(path) as db:assert request(budget,db).reason == 'root_budget_exceeded'
    clock[0] = 15300
    with transaction(path) as db:assert request(InferenceBudget(clock=lambda:clock[0]),db).reason == 'budget_clock_rollback'
    assert count(path) == ([(0,)], [(0,)], 0)


def test_root_clock_shared_by_all_descendants(setup):
    path, clock, budget = setup
    child = AttemptScope(ROOT,'child',1)
    with transaction(path) as db:budget.register_attempt(db,child,parent=ROOT_ATTEMPT)
    clock[0] = 1100
    with transaction(path) as db:request(budget,db,child)
    clock[0] = 1099
    with transaction(path) as db:assert request(budget,db).reason == 'budget_clock_rollback'


def test_cleanup_reserve_boundary_and_replay_preserves_original_deadline(setup):
    path, clock, budget = setup
    clock[0] = 15240
    with transaction(path) as db:
        first = request(budget,db)
        assert first.decision == 'charged' and first.request_deadline_at == 15400
    clock[0] = 15240.000001
    with transaction(path) as db:
        assert request(budget,db,n=1).reason == 'insufficient_cleanup_reserve'
        retry = request(budget,db)
        assert retry.decision == 'replay' and retry.request_deadline_at == first.request_deadline_at
    clock[0] = 15400
    with transaction(path) as db:assert request(budget,db).reason == 'root_budget_exceeded'


def test_attempt_deadline_cannot_outlive_parent(setup):
    path, clock, budget = setup
    child = AttemptScope(ROOT,'child',1)
    grandchild = AttemptScope(ROOT,'grandchild',1)
    with transaction(path) as db:
        budget.register_attempt(db,child,parent=ROOT_ATTEMPT,deadline_at=2000)
        with pytest.raises(BudgetError,match='invalid_attempt_deadline'):
            budget.register_attempt(db,grandchild,parent=child)
        budget.register_attempt(db,grandchild,parent=child,deadline_at=2000)
    clock[0]=2000
    with transaction(path) as db:assert request(budget,db,child).reason=='attempt_budget_exceeded'


@pytest.mark.parametrize('commit', [False,True])
def test_actual_process_exit_before_and_after_atomic_commit(setup, commit):
    path, _, _ = setup
    script = '''
import os, sqlite3, sys
from cloudworkbench.inference_budget import *
db=sqlite3.connect(sys.argv[1]);db.execute('BEGIN IMMEDIATE')
b=InferenceBudget(clock=lambda:1000)
r=RootScope('owner','project','session','turn','root',1)
b.admit(db,AttemptScope(r,'root',1),nonce='a'*64,payload_digest='b'*64,profile_digest='c'*64)
db.execute('CREATE TABLE IF NOT EXISTS dispatch(request TEXT)')
db.execute("INSERT INTO dispatch VALUES('durable')")
if sys.argv[2]=='yes':db.commit()
os._exit(23)
'''
    env = {**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    run = subprocess.run([sys.executable,'-c',script,str(path),'yes' if commit else 'no'],env=env,capture_output=True)
    assert run.returncode==23,run.stderr
    assert count(path)==([(int(commit),)],[(int(commit),)],int(commit))
    with transaction(path) as db:
        if commit:assert db.execute('SELECT request FROM dispatch').fetchall()==[('durable',)]
        decision=InferenceBudget(clock=lambda:1000).admit(db,ROOT_ATTEMPT,nonce='a'*64,payload_digest='b'*64,profile_digest='c'*64)
        assert decision.decision == ('replay' if commit else 'charged')


@pytest.mark.parametrize('field,value', [('burst',0),('requests_per_minute',float('inf')),('root_wall_seconds',159),('cleanup_reserve_seconds',159),('attempt_requests',True),('root_requests',0)])
def test_invalid_policy_rejected_without_root_insert(tmp_path,field,value):
    path=tmp_path/'invalid.db'
    with transaction(path) as db:
        ensure_schema(db)
        with pytest.raises(BudgetError):
            InferenceBudget(clock=lambda:1000).register_root(db,ROOT,replace(BudgetPolicy(),**{field:value}),admitted_at=1000)
        assert db.execute('SELECT count(*) FROM inference_budget_roots').fetchone()[0]==0


@pytest.mark.parametrize('stamp',[float('nan'),float('inf'),-1,True])
def test_invalid_clock_never_mutates(setup,stamp):
    path,_,_=setup
    with transaction(path) as db:
        with pytest.raises(BudgetError,match='invalid_budget_time'):request(InferenceBudget(clock=lambda:stamp),db)
    assert count(path)==([(0,)],[(0,)],0)


def test_partial_schema_refused_and_existing_store_tables_untouched(tmp_path):
    path=tmp_path/'partial.db'
    with transaction(path) as db:
        db.execute('CREATE TABLE attempts(id TEXT PRIMARY KEY)')
        db.execute("INSERT INTO attempts VALUES('historical')")
        db.execute('CREATE TABLE inference_budget_version(version INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO inference_budget_version VALUES(1)')
        with pytest.raises(BudgetError,match='budget_schema_incompatible'):ensure_schema(db)
        assert db.execute('SELECT id FROM attempts').fetchall()==[('historical',)]
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='inference_budget_roots'").fetchone()


def test_default_32_per_attempt_and_128_per_root_across_restart(setup):
    path, clock, budget = setup
    scopes = [ROOT_ATTEMPT] + [AttemptScope(ROOT, f'child-{i}', 1) for i in range(4)]
    with transaction(path) as db:
        for scope in scopes[1:]:budget.register_attempt(db,scope,parent=ROOT_ATTEMPT)
    for scope in scopes[:4]:
        for index in range(32):
            clock[0] += 1
            with transaction(path) as db:
                assert request(InferenceBudget(clock=lambda:clock[0]),db,scope,n=index).decision=='charged'
        with transaction(path) as db:
            assert request(budget,db,scope,n=33).reason in ('attempt_request_limit','root_request_limit')
    with transaction(path) as db:assert request(budget,db,scopes[4]).reason=='root_request_limit'
    assert count(path)[0]==[(128,)] and count(path)[2]==128


def test_long_idle_refill_bounded_to_burst(setup):
    path, clock, budget = setup
    with transaction(path) as db:
        for i in range(4):request(budget,db,n=i)
    clock[0]+=500
    with transaction(path) as db:
        for i in range(4,8):assert request(budget,db,n=i).decision=='charged'
        assert request(budget,db,n=8).reason=='attempt_rate_limit'


def test_cross_attempt_nonce_is_not_conflated(setup):
    path, _, budget = setup
    child=AttemptScope(ROOT,'child',1)
    with transaction(path) as db:
        budget.register_attempt(db,child,parent=ROOT_ATTEMPT)
        assert request(budget,db).decision=='charged'
        assert request(budget,db,child).decision=='charged'
    assert count(path)==([(2,)],[(1,),(1,)],2)


def test_generation_advance_requires_exact_predecessor(setup):
    path, _, budget=setup
    with transaction(path) as db:
        for generation, previous in ((2,None),(3,1),(2,True)):
            with pytest.raises(BudgetError):
                budget.register_attempt(db,replace(ROOT_ATTEMPT,generation=generation),previous_generation=previous)
        assert db.execute('SELECT latest_generation FROM inference_budget_attempts').fetchone()[0]==1


def test_refusal_observation_is_explicitly_caller_transactional(setup):
    path, clock, budget=setup
    clock[0]=15400
    with pytest.raises(RuntimeError):
        with transaction(path) as db:
            assert request(budget,db).reason=='root_budget_exceeded'
            raise RuntimeError('caller aborts observation')
    with transaction(path) as db:
        assert db.execute('SELECT high_water_us FROM inference_budget_roots').fetchone()[0]==1000_000_000
    # Uncommitted wall-clock observations cannot survive a caller rollback.


def test_schema_reinitialization_is_idempotent_and_does_not_reset(setup):
    path, _, budget=setup
    with transaction(path) as db:request(budget,db)
    with transaction(path) as db:
        ensure_schema(db)
        budget.register_root(db,ROOT,admitted_at=1000)
        budget.register_attempt(db,ROOT_ATTEMPT)
        assert request(budget,db).decision=='replay'
    assert count(path)==([(1,)],[(1,)],1)


@pytest.mark.parametrize('journal', ['DELETE','WAL'])
def test_read_before_admit_requires_immediate_or_complete_transaction_retry(setup,journal):
    path,_,budget=setup
    with sqlite3.connect(path) as db:assert db.execute(f'PRAGMA journal_mode={journal}').fetchone()[0].upper()==journal
    reader=sqlite3.connect(path,timeout=.01)
    writer=sqlite3.connect(path,timeout=.01)
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT used FROM inference_budget_roots').fetchone()
        writer.execute('BEGIN IMMEDIATE')
        request(budget,writer,n=0)
        if journal=='WAL':writer.commit()
        with pytest.raises(sqlite3.OperationalError,match='locked'):
            request(budget,reader,n=1)
        reader.rollback()
        if journal=='DELETE':writer.commit()
    finally:
        reader.close();writer.close()
    with transaction(path) as db:
        # Production seam: write lock acquired before even the authorization read.
        db.execute('SELECT used FROM inference_budget_roots').fetchone()
        assert request(budget,db,n=1).decision=='charged'
    assert count(path)==([(2,)],[(2,)],2)


@pytest.mark.parametrize('journal',['DELETE','WAL'])
def test_concurrent_authorization_read_then_admit_with_immediate(setup,journal):
    path,_,budget=setup
    with sqlite3.connect(path) as db:db.execute(f'PRAGMA journal_mode={journal}')
    def admit(i):
        with transaction(path) as db:
            db.execute('SELECT scope_json FROM inference_budget_roots').fetchone()
            return request(budget,db,n=i).decision
    with ThreadPoolExecutor(max_workers=8) as pool:outcomes=list(pool.map(admit,range(16)))
    assert outcomes.count('charged')==4 and outcomes.count('refused')==12
    assert count(path)==([(4,)],[(4,)],4)


def test_parent_recovery_fences_existing_child_grandchild_and_new_descendant(setup):
    path,_,budget=setup
    child=AttemptScope(ROOT,'child',1);grandchild=AttemptScope(ROOT,'grandchild',1)
    newroot=replace(ROOT_ATTEMPT,generation=2)
    with transaction(path) as db:
        budget.register_attempt(db,child,parent=ROOT_ATTEMPT)
        budget.register_attempt(db,grandchild,parent=child)
        request(budget,db,child)
        budget.register_attempt(db,newroot,previous_generation=1)
        for scope,n in ((child,0),(child,1),(grandchild,0)):
            with pytest.raises(BudgetError,match='budget_ancestor_generation_conflict'):
                request(budget,db,scope,n=n)
        with pytest.raises(BudgetError,match='budget_parent_mismatch'):
            budget.register_attempt(db,replace(child,generation=2),parent=ROOT_ATTEMPT,previous_generation=1)
        with pytest.raises(BudgetError,match='attempt_budget_conflict'):
            budget.register_attempt(db,replace(child,generation=2),parent=newroot,previous_generation=1)
        with pytest.raises(BudgetError,match='budget_ancestor_generation_conflict'):
            budget.register_attempt(db,AttemptScope(ROOT,'further-child',1),parent=child)
        # Explicitly distinct child may be authorized by the controller; root count persists.
        reconstructed=AttemptScope(ROOT,'reconstructed',1)
        budget.register_attempt(db,reconstructed,parent=newroot)
        assert request(budget,db,reconstructed).root_requests_used==2


def test_child_only_generation_recovery_preserves_ancestor_and_counters(setup):
    path,_,budget=setup
    child=AttemptScope(ROOT,'child',1)
    with transaction(path) as db:
        budget.register_attempt(db,child,parent=ROOT_ATTEMPT)
        request(budget,db,child)
        newer=replace(child,generation=2)
        budget.register_attempt(db,newer,parent=ROOT_ATTEMPT,previous_generation=1)
        assert request(budget,db,newer).attempt_requests_used==2


def test_microseconds_are_rounded_instead_of_truncated(setup):
    path,clock,budget=setup
    clock[0]=1000.000001
    with transaction(path) as db:
        request(budget,db)
        assert db.execute('SELECT charged_us FROM inference_budget_requests').fetchone()[0]==1000000001


def test_sqlite_autorollback_preserves_original_exception(setup):
    path,_,budget=setup
    with transaction(path) as db:
        db.execute("CREATE TRIGGER abort_transaction BEFORE INSERT ON inference_budget_requests BEGIN SELECT RAISE(ROLLBACK,'original abort'); END")
    db=sqlite3.connect(path)
    try:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.IntegrityError,match='original abort'):request(budget,db)
        assert not db.in_transaction
    finally:db.close()
    assert count(path)==([(0,)],[(0,)],0)
