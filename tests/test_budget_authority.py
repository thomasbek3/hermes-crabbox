from dataclasses import replace
import json
import sqlite3
import threading
import time

import pytest

from cloudworkbench.budget_authority import BudgetAuthority
from cloudworkbench.cancellation_supervisor import CancellationSupervisor
from cloudworkbench.inference_budget import InferenceBudget, BudgetPolicy, AttemptScope, RootScope, ensure_schema
from cloudworkbench.inference_relay import AttemptBinding
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.store import Store


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'authority.db')
    principal=store.add_client('TEST authority','synthetic-authority-token-000000000000000',['submit','observe'],['project'])
    result=store.create_session(principal,{'project_id':'project','agent':'hermes','goal':'synthetic'},'TEST-root')
    attempt=store.claim_next()
    clock=[1000.0]
    budget=InferenceBudget(clock=lambda:clock[0])
    root=RootScope(principal['id'],'project',result['session_id'],attempt['turn_id'],attempt['id'],attempt['generation'])
    scope=AttemptScope(root,attempt['id'],attempt['generation'])
    with store._tx() as db:
        ensure_schema(db)
        budget.register_root(db,root,admitted_at=1000)
        budget.register_attempt(db,scope)
    return store,principal,scope,budget,clock


def observe(setup,scope=None,now=None):
    store,_,original,_,clock=setup
    with store._connect() as db:
        return BudgetAuthority(scope or original).check(db,now=clock[0] if now is None else now)


def descendants(setup):
    """Disposable future-seat projection, NOT production Store child admission.

    Legacy unique session/turn/agent live-slot indexes currently prevent multiple
    live seats. Remove them only in this synthetic DB; preserve actual Store table
    columns, foreign keys, owner/turn/client records and all new budget schemas.
    """
    store,_,root,budget,_=setup
    child=AttemptScope(root.root,'TEST-child',1)
    grandchild=AttemptScope(root.root,'TEST-grandchild',1)
    with store._tx() as db:
        for name in ('one_live_session','one_active_turn','one_live_credential'):
            db.execute(f'DROP INDEX {name}')
        for scope,parent in ((child,root),(grandchild,child)):
            db.execute('''INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at)
                SELECT ?,session_id,turn_id,agent,1,'running',created_at,updated_at FROM attempts WHERE id=?''',(scope.attempt_id,root.attempt_id))
            budget.register_attempt(db,scope,parent=parent)
    return child,grandchild


def test_actual_store_root_is_authorized_without_schema_or_counter_writes(setup):
    store,*_=setup
    with store._connect() as db:
        before=list(db.iterdump());changes=db.total_changes
        db.execute('PRAGMA query_only=ON')
        result=BudgetAuthority(setup[2]).check(db,now=1000)
        assert result.allowed and result.deadline_at==15400 and len(result.policy_digest)==64
        assert db.total_changes==changes and list(db.iterdump())==before
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('one_live_session','one_active_turn','one_live_credential')").fetchone()[0]==3


@pytest.mark.parametrize('target',['root','child','grandchild'])
@pytest.mark.parametrize('change',['cancel','terminal','generation'])
def test_every_live_store_ancestor_cancel_terminal_or_generation_refuses_predicate(setup,target,change):
    child,leaf=descendants(setup)
    scope={'root':setup[2],'child':child,'grandchild':leaf}[target]
    assert observe(setup,leaf).allowed
    sql={'cancel':'cancel_requested=1','terminal':"state='failed'",'generation':'generation=generation+1'}[change]
    with setup[0]._tx() as db:db.execute(f'UPDATE attempts SET {sql} WHERE id=?',(scope.attempt_id,))
    result=observe(setup,leaf)
    assert not result.allowed
    assert result.reason==('budget_authority_generation_changed' if change=='generation' else 'budget_authority_cancelled')


@pytest.mark.parametrize('field',['owner_id','project_id','session_id','turn_id','root_generation'])
def test_forged_frozen_scope_cannot_authorize(setup,field):
    scope=setup[2]
    changed=replace(scope.root,**{field:2 if field=='root_generation' else 'foreign'})
    assert not observe(setup,replace(scope,root=changed)).allowed


@pytest.mark.parametrize('target',['root','child','grandchild'])
@pytest.mark.parametrize('field',['session_id','turn_id'])
def test_actual_store_scope_mismatch_in_any_ancestor_is_refused(setup,target,field):
    child,leaf=descendants(setup)
    scope={'root':setup[2],'child':child,'grandchild':leaf}[target]
    with setup[0]._tx() as db:
        db.execute("INSERT INTO sessions(id,owner_id,project_id,goal,created_at) VALUES('other-session',?,'project','TEST','now')",(scope.root.owner_id,))
        db.execute("INSERT INTO turns(id,session_id,ordinal,request,created_at) VALUES('other-turn','other-session',1,'{}','now')")
        db.execute(f'UPDATE attempts SET {field}=? WHERE id=?',('other-session' if field=='session_id' else 'other-turn',scope.attempt_id))
    assert observe(setup,leaf).reason=='budget_authority_scope_mismatch'


@pytest.mark.parametrize('change',['owner','project','turn_session','client_revoked'])
def test_actual_owner_project_turn_relationship_and_revocation(setup,change):
    store,principal,scope,*_=setup
    with store._tx() as db:
        if change=='owner':
            db.execute("INSERT INTO clients(id,name,token_hash,scopes,projects,created_at) VALUES('other','TEST','synthetic','[]','[]','now')")
            db.execute("UPDATE sessions SET owner_id='other' WHERE id=?",(scope.root.session_id,))
        elif change=='project':db.execute("UPDATE sessions SET project_id='foreign' WHERE id=?",(scope.root.session_id,))
        elif change=='turn_session':
            db.execute("INSERT INTO sessions(id,owner_id,project_id,goal,created_at) VALUES('other',?,'project','TEST','now')",(principal['id'],))
            db.execute("UPDATE turns SET session_id='other' WHERE id=?",(scope.root.turn_id,))
        else:db.execute("UPDATE clients SET revoked_at='revoked' WHERE id=?",(principal['id'],))
    assert not observe(setup).allowed


def test_current_project_grant_list_is_not_reinterpreted_as_execution_grant(setup):
    # Matches current ProviderLeases semantics: an existing grant remains valid
    # until revoked; this additional predicate cannot create/replace that grant.
    with setup[0]._tx() as db:db.execute("UPDATE clients SET projects='[]' WHERE id=?",(setup[1]['id'],))
    assert observe(setup).allowed


def test_root_recovery_invalidates_frozen_ancestor_edge_without_new_charge(setup):
    _,leaf=descendants(setup)
    store,_,scope,budget,_=setup
    with store._tx() as db:
        budget.register_attempt(db,replace(scope,generation=2),previous_generation=1)
        db.execute('UPDATE attempts SET generation=2 WHERE id=?',(scope.attempt_id,))
    assert observe(setup,replace(scope,generation=2)).allowed
    assert observe(setup,leaf).reason=='budget_authority_generation_changed'
    with store._connect() as db:
        assert db.execute('SELECT used FROM inference_budget_roots').fetchone()[0]==0


@pytest.mark.parametrize('table',['inference_budget_attempts','attempts'])
def test_missing_registered_or_actual_ancestor_is_refused(setup,table):
    child,leaf=descendants(setup)
    # Disable foreign-key checking only for explicit broken-record injection.
    with sqlite3.connect(setup[0].path) as db:db.execute(f'DELETE FROM {table} WHERE '+('attempt_id' if table.startswith('inference') else 'id')+'=?',(child.attempt_id,))
    assert not observe(setup,leaf).allowed


def test_cycle_and_unterminated_chain_fail_closed(setup):
    child,leaf=descendants(setup)
    with setup[0]._tx() as db:
        db.execute('UPDATE inference_budget_attempts SET parent_id=?,parent_generation=1 WHERE attempt_id=?',(leaf.attempt_id,child.attempt_id))
    assert observe(setup,leaf).reason=='budget_authority_ancestry_invalid'


@pytest.mark.parametrize('field,value',[('scope_json','{}'),('policy_json','{}'),('policy_digest','a'*64),('high_water_us',-1),('deadline_us',15401_000000),('admitted_us',-1)])
def test_corrupt_budget_root_metadata_refused(setup,field,value):
    with setup[0]._tx() as db:db.execute(f'UPDATE inference_budget_roots SET {field}=?',(value,))
    assert not observe(setup).allowed


def test_missing_original_generation_record_refused(setup):
    with setup[0]._tx() as db:db.execute('DELETE FROM inference_budget_generations')
    assert not observe(setup).allowed


def test_root_and_attempt_deadline_boundaries_without_cleanup_reserve_recheck(setup):
    assert observe(setup,now=15399.999999).allowed
    assert observe(setup,now=15400).reason=='root_budget_exceeded'
    with setup[0]._tx() as db:db.execute('UPDATE inference_budget_attempts SET deadline_us=1100_000000')
    # Already-active request remains allowed with less than160s; caller enforces
    # its existing execution/cleanup subdeadlines. This does not admit new work.
    assert observe(setup,now=1099.999999).allowed
    assert observe(setup,now=1100).reason=='attempt_budget_exceeded'


def test_child_deadline_must_fit_ancestor_and_root(setup):
    child,leaf=descendants(setup)
    with setup[0]._tx() as db:db.execute('UPDATE inference_budget_attempts SET deadline_us=1200_000000 WHERE attempt_id=?',(child.attempt_id,))
    assert observe(setup,leaf).reason=='budget_authority_record_invalid'
    with setup[0]._tx() as db:db.execute('UPDATE inference_budget_attempts SET deadline_us=1200_000000 WHERE attempt_id=?',(leaf.attempt_id,))
    assert observe(setup,leaf).deadline_at==1200
    assert observe(setup,leaf,now=1200).reason=='attempt_budget_exceeded'


def test_durable_clock_highwater_refuses_rollback_but_observer_never_writes(setup):
    store,_,scope,budget,clock=setup
    clock[0]=1100
    with store._tx() as db:budget.admit(db,scope,nonce='a'*64,payload_digest='b'*64,profile_digest='c'*64)
    assert observe(setup,now=1099).reason=='budget_clock_rollback'
    assert observe(setup,now=1100).allowed
    assert observe(setup,now=1200).allowed
    with store._connect() as db:assert db.execute('SELECT high_water_us FROM inference_budget_roots').fetchone()[0]==1100_000000


@pytest.mark.parametrize('stamp',[True,float('nan'),float('inf'),-1])
def test_invalid_clock_is_a_fixed_refusal(setup,stamp):
    assert observe(setup,now=stamp).reason=='budget_authority_invalid_input'


def test_request_count_rate_does_not_cancel_last_admitted_call(setup):
    with setup[0]._tx() as db:
        db.execute('UPDATE inference_budget_roots SET used=128')
        db.execute('UPDATE inference_budget_attempts SET used=32,credits=0')
    assert observe(setup).allowed


def test_query_only_connection_and_caller_progress_handler(setup):
    _,leaf=descendants(setup)
    with setup[0]._connect() as db:
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda:1,1)
        with pytest.raises(sqlite3.OperationalError,match='interrupted'):
            BudgetAuthority(leaf).check(db,now=1000)


def test_missing_schema_fails_without_implicitly_creating_tables(tmp_path,setup):
    with sqlite3.connect(tmp_path/'empty.db') as db:
        with pytest.raises(sqlite3.OperationalError):BudgetAuthority(setup[2]).check(db,now=1000)
        assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]==0


def test_exactly_one_select_and_no_begin_or_commit(setup):
    with setup[0]._connect() as db:
        statements=[];db.set_trace_callback(statements.append)
        result=BudgetAuthority(setup[2]).check(db,now=1000)
        assert result.allowed and len(statements)==1 and statements[0].startswith('WITH RECURSIVE')
        assert not db.in_transaction


def test_source_backed_supervisor_bounded_operation_with_new_authority(setup):
    # Calls the actual supervisor database boundary. Its frozen _authorized
    # implementation is not monkey-patched or claimed to include this predicate.
    store,_,scope,_,_=setup
    leases=ProviderLeases(store,cleanup_verifier=lambda target:None,inspector_id='synthetic')
    leases.register_account('account',legacy_agent='claude',persistent_owner_id='logical-owner')
    owner=leases.reserve('account',persistent_owner_id='logical-owner')
    grant=leases.issue_grant(owner,attempt_id=scope.attempt_id,generation=scope.generation)
    supervisor=CancellationSupervisor(store,reservation=owner,grant_id=grant,
        binding=AttemptBinding(scope.attempt_id,scope.generation,'a'*64),
        caller_cancel=threading.Event(),controller_instance_id=leases.instance_id)
    authority=BudgetAuthority(scope)
    assert supervisor._database(lambda db:authority.check(db,now=1000)).allowed
    with store._tx() as db:db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(scope.attempt_id,))
    assert not supervisor._database(lambda db:authority.check(db,now=1000)).allowed


def test_real_store_resume_parent_is_provenance_not_budget_ancestry(setup):
    store,principal,scope,budget,_=setup
    store.transition(scope.attempt_id,'interrupted',expected_generation=scope.generation)
    resumed=store.resume(principal,scope.root.session_id,'TEST-resume')
    live=store.claim_next()
    assert live['parent_attempt_id']==scope.attempt_id and live['resume_mode']=='reconstructed'
    root=replace(scope.root,root_attempt_id=resumed['attempt_id'],root_generation=resumed['generation'])
    resumed_scope=AttemptScope(root,resumed['attempt_id'],resumed['generation'])
    with store._tx() as db:
        budget.register_root(db,root,admitted_at=1000)
        budget.register_attempt(db,resumed_scope)
    assert observe(setup,resumed_scope).allowed


def test_legacy_parent_pointer_is_intentionally_not_a_second_authority(setup):
    child,leaf=descendants(setup)
    with setup[0]._tx() as db:
        db.execute('UPDATE attempts SET parent_attempt_id=? WHERE id=?',(setup[2].attempt_id,leaf.attempt_id))
    assert observe(setup,leaf).allowed
    # The trusted frozen budget edge remains authoritative and changing its
    # generation does revoke authority, regardless of the legacy provenance field.
    with setup[0]._tx() as db:
        db.execute('UPDATE inference_budget_attempts SET parent_generation=2 WHERE attempt_id=?',(leaf.attempt_id,))
    assert observe(setup,leaf).reason=='budget_authority_generation_changed'


def test_real_store_archive_is_presentation_state_not_cancel(setup):
    store,principal,scope,*_=setup
    assert store.archive(principal,scope.root.session_id,'TEST-archive')['archived']
    assert observe(setup).allowed
    assert not store.get_attempt(scope.attempt_id)['cancel_requested']


def test_unregistered_live_root_never_has_implicit_authority(tmp_path):
    store=Store(tmp_path/'unregistered.db')
    principal=store.add_client('TEST unregistered','another-synthetic-token-for-testing-only',['submit'],['project'])
    store.create_session(principal,{'project_id':'project','agent':'hermes','goal':'TEST'},'create')
    attempt=store.claim_next()
    root=RootScope(principal['id'],'project',attempt['session_id'],attempt['turn_id'],attempt['id'],attempt['generation'])
    authority=BudgetAuthority(AttemptScope(root,attempt['id'],attempt['generation']))
    with store._connect() as db:
        with pytest.raises(sqlite3.OperationalError):authority.check(db,now=1000)
    with store._tx() as db:ensure_schema(db)
    with store._connect() as db:
        assert authority.check(db,now=1000).reason=='budget_authority_missing'
        assert not db.execute('SELECT cancel_requested FROM attempts').fetchone()[0]
        assert db.execute('SELECT count(*) FROM inference_budget_attempts').fetchone()[0]==0


def test_malformed_ancestor_identity_refused_even_when_joinable(setup):
    child,leaf=descendants(setup)
    with sqlite3.connect(setup[0].path) as db:
        db.execute("UPDATE attempts SET id='bad parent' WHERE id=?",(child.attempt_id,))
        db.execute("UPDATE inference_budget_attempts SET attempt_id='bad parent' WHERE attempt_id=?",(child.attempt_id,))
        db.execute("UPDATE inference_budget_generations SET attempt_id='bad parent' WHERE attempt_id=?",(child.attempt_id,))
        db.execute("UPDATE inference_budget_attempts SET parent_id='bad parent' WHERE attempt_id=?",(leaf.attempt_id,))
    assert observe(setup,leaf).reason=='budget_authority_ancestry_invalid'


def test_depth64_query_plan_and_actual_supervisor_deadline(setup,record_property):
    store,_,root,budget,_=setup
    _,parent=descendants(setup)
    with store._tx() as db:
        for depth in range(3,65):
            scope=AttemptScope(root.root,f'TEST-depth-{depth}',1)
            db.execute('''INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at)
                SELECT ?,session_id,turn_id,agent,1,'running',created_at,updated_at FROM attempts WHERE id=?''',(scope.attempt_id,root.attempt_id))
            budget.register_attempt(db,scope,parent=parent)
            parent=scope
    authority=BudgetAuthority(parent)
    leases=ProviderLeases(store,cleanup_verifier=lambda target:None,inspector_id='synthetic')
    leases.register_account('account',legacy_agent='claude',persistent_owner_id='logical-owner')
    owner=leases.reserve('account',persistent_owner_id='logical-owner')
    grant=leases.issue_grant(owner,attempt_id=parent.attempt_id,generation=parent.generation)
    supervisor=CancellationSupervisor(store,reservation=owner,grant_id=grant,
        binding=AttemptBinding(parent.attempt_id,parent.generation,'a'*64),
        caller_cancel=threading.Event(),controller_instance_id=leases.instance_id)
    started=time.monotonic()
    decision=supervisor._database(lambda db:authority.check(db,now=1000))
    elapsed=time.monotonic()-started
    assert decision.allowed
    # _database itself enforces50ms; retain measured elapsed, not a hard claim for
    # other machines, cold disks, lock contention, or uninterruptible OS IO.
    record_property('depth64_supervisor_seconds',elapsed)
    from cloudworkbench.budget_authority import _QUERY
    with store._connect() as db:
        plan=[row[3] for row in db.execute('EXPLAIN QUERY PLAN '+_QUERY,
             (parent.attempt_id,root.attempt_id,root.generation))]
        assert any('SEARCH a USING INDEX' in line for line in plan)
        assert any('SEARCH p USING INDEX' in line for line in plan)
        assert any('SEARCH inference_budget_attempts USING INDEX' in line for line in plan)
        assert not any(line.startswith(('SCAN attempts','SCAN inference_budget_attempts','SCAN a ','SCAN p ')) for line in plan)
        record_property('query_plan',json.dumps(plan))
