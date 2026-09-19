"""Actual scheduler admission with synthetic runtime boundaries; no provider/network calls."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from cloudworkbench import routed_root_worker as module
from cloudworkbench import routed_stage_runner
from cloudworkbench.role_broker import RoleBroker
from cloudworkbench.scheduler import RoleScheduler,RootCleanupReceipt
from cloudworkbench.store import Store,StoreError
from cloudworkbench.workflow_revisions import RevisionBinding,capture_revision
from tests import test_six_stage_composition as six


@pytest.fixture
def queued(tmp_path,monkeypatch):
    # Stop the existing fixture at enqueue, without rewriting any durable state.
    with monkeypatch.context() as fixture_only:
        migrate=Store.migrate_scheduler
        def delivery_schema(store):
            migrate(store);store.migrate_delivery()
        fixture_only.setattr(Store,'migrate_scheduler',delivery_schema)
        fixture_only.setattr(RoleScheduler,'admit_root',lambda *_a,**_k:None)
        fixture_only.setattr(RoleScheduler,'transition',lambda *_a,**_k:None)
        fixture_only.setattr(six,'register_root_input',lambda *_a,**_k:None)
        fixture_only.setattr(RoleBroker,'issue',lambda *_a,**_k:('fixture-unused','fixture-unused'))
        value=six.flow.__wrapped__(tmp_path)
    delivery=tmp_path/'delivery';delivery.mkdir(mode=0o700)
    value.delivery=delivery
    with value.store._connect() as db:
        assert db.execute('SELECT state FROM workflow_roots').fetchone()[0]=='queued'
        assert db.execute('SELECT COUNT(*) FROM provider_reservations').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]==0
    return value


def worker(value,factory):
    return module.RoutedRootWorker(value.s,stage_factory=factory,
        cleanup_verifier=lambda target:RootCleanupReceipt(target,'terminated',1000,'b'*64),
        delivery_root=value.delivery,forbidden_values=())


def run(value,instance,**kwargs):
    return instance.run_root(value.root['attempt_id'],expected_generation=kwargs.pop('expected_generation',1),
        initial_revision=kwargs.pop('initial_revision',value.initial),**kwargs)


def no_factory(*_a,**_k):pytest.fail('Factory must not be called')


def state(value):
    with value.store._connect() as db:
        return {'attempts':[dict(r) for r in db.execute('SELECT id,state,execution_kind,runtime_id FROM attempts ORDER BY id')],
            'roots':[dict(r) for r in db.execute('SELECT state,child_attempt_id FROM workflow_roots')],
            'reservations':db.execute('SELECT COUNT(*) FROM provider_reservations').fetchone()[0],
            'budgets':db.execute('SELECT COUNT(*) FROM inference_budget_roots').fetchone()[0],
            'requests':db.execute('SELECT COUNT(*) FROM workflow_requests').fetchone()[0]}


def test_busy_capacity_keeps_queued_without_factory(queued):
    before=state(queued);instance=worker(queued,no_factory)
    receipt=run(queued,instance,external_running=1)
    assert receipt.disposition=='queued' and receipt.receipt is None
    assert state(queued)==before and not instance.executions


@pytest.mark.parametrize('generation',[True,False,0,2,None,'1'])
def test_invalid_or_stale_generation_refuses_before_admission(queued,generation):
    before=state(queued)
    with pytest.raises(StoreError,match='identity'):run(queued,worker(queued,no_factory),expected_generation=generation)
    assert state(queued)==before


def test_valid_foreign_root_revision_refused(queued,tmp_path):
    v=queued
    other=v.s.enqueue_root(v.owner,{'agent':'hermes','project_id':'demo','goal':'Other task'},'other-root',frozen=v.frozen)
    binding=RevisionBinding(v.owner['id'],'demo',other['session_id'],other['turn_id'],other['attempt_id'],1,other['attempt_id'],1)
    destination=tmp_path/'other-revisions';destination.mkdir()
    revision=capture_revision(v.store,binding,v.initial.path/'files',destination,selected_paths=('code.py',),controller_attests_quiesced=True)
    before=state(v)
    with pytest.raises(StoreError,match='Exact root input'):run(v,worker(v,no_factory),initial_revision=revision)
    assert state(v)==before


def test_started_root_refused_without_second_admission(queued):
    v=queued;v.s.admit_root(v.root['attempt_id'],accounts={'account':'owner'})
    before=state(v)
    with pytest.raises(StoreError,match='explicit reconciliation'):run(v,worker(v,no_factory))
    assert state(v)==before


@pytest.mark.parametrize('change',['archive','cancel','scope'])
def test_queued_authority_refused_before_factory(queued,change):
    v=queued
    if change=='archive':v.store.archive(v.owner,v.root['session_id'],'archive')
    elif change=='cancel':v.store.cancel(v.owner,v.root['attempt_id'],'cancel')
    else:
        with v.store._tx() as db:db.execute("UPDATE clients SET scopes='[]' WHERE id=?",(v.owner['id'],))
    before=state(v)
    with pytest.raises(StoreError):run(v,worker(v,no_factory))
    assert state(v)==before


def test_stage_exception_retains_exact_handles_and_seat_no_second_launch(queued,monkeypatch):
    v=queued;calls=[];execution=module.StageExecution(object(),object(),object(),object(),(),{})
    def factory(child,revision,assignment):
        calls.append(('factory',child['id'],revision.sha256,assignment['role']))
        return execution
    def fail(*args,**kwargs):
        calls.append(('run',args[1]));raise RuntimeError('synthetic stage stopped at boundary')
    monkeypatch.setattr(routed_stage_runner,'run_prepared_stage',fail)
    instance=worker(v,factory)
    with pytest.raises(RuntimeError,match='synthetic stage'):run(v,instance)
    current=state(v);child=next(a for a in current['attempts'] if a['execution_kind']=='hermes_child')
    assert current['roots']==[{'state':'held','child_attempt_id':child['id']}]
    assert child['state']=='preparing' and child['runtime_id'] is None
    assert instance.executions=={child['id']:execution}
    assert calls[0][3]=='planning' and len(calls)==2
    with v.store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM role_broker_grants WHERE revoked=0').fetchone()[0]==0
    with pytest.raises(StoreError,match='explicit reconciliation'):run(v,instance)
    assert len(calls)==2 and state(v)==current


def test_factory_exception_preserves_claimed_child_and_requires_reconciliation(queued):
    v=queued;calls=[]
    def factory(*args):calls.append(args[0]['id']);raise RuntimeError('partial provisioning retained by factory')
    instance=worker(v,factory)
    with pytest.raises(RuntimeError,match='partial provisioning'):run(v,instance)
    current=state(v);assert len(calls)==1 and current['roots'][0]['child_attempt_id']==calls[0]
    assert current['roots'][0]['state']=='held' and not instance.executions
    with v.store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM role_broker_grants WHERE revoked=0').fetchone()[0]==0
    with pytest.raises(StoreError,match='explicit reconciliation'):run(v,instance)
    assert len(calls)==1 and state(v)==current


@pytest.mark.parametrize('reject_at',[None,'review_code'])
def test_real_fixed_six_stage_worker_delivers_or_stops_without_automatic_retry(queued,tmp_path,monkeypatch,reject_at):
    from contextlib import ExitStack
    from tests.test_routed_stage_runner import synthetic_stage_execution
    v=queued;evidence=[];chosen=[];cleanup=[]
    with ExitStack() as stack:
        def factory(child,revision,assignment):
            record={};evidence.append(record)
            with v.store._connect() as db:
                step_id=db.execute('SELECT step_id FROM workflow_steps WHERE root_id=? AND request_id=?',
                    (v.root['attempt_id'],assignment['role_request_id'])).fetchone()[0]
            chosen.append((step_id,assignment['role'],assignment['profile']['model'],assignment['profile']['effort']))
            return stack.enter_context(synthetic_stage_execution(v,child,revision,assignment,
                tmp_path/f'worker-stage-{len(chosen)}',monkeypatch,reject=step_id==reject_at,evidence=record))
        def physical(target):
            cleanup.append(target);return RootCleanupReceipt(target,'terminated',1000,'b'*64)
        instance=module.RoutedRootWorker(v.s,stage_factory=factory,cleanup_verifier=physical,
            delivery_root=v.delivery,forbidden_values=())
        with v.store._connect() as db:before=dict(db.execute('SELECT * FROM sessions').fetchone())
        receipt=run(v,instance)
        expected=six.EXPECTED if reject_at is None else six.EXPECTED[:5]
        assert chosen==expected
        assert receipt.disposition==('delivered' if reject_at is None else 'stopped')
        assert len(instance.executions)==len(expected) and len(cleanup)==1
        for record in evidence:
            assert not record['caller'].objects and not record['collector'].objects
            assert not record['provider']['provider'].objects
            assert record['provider']['service'].receipt.resources_closed
            assert sum(c[0]=='create' for c in record['caller'].calls)==1
            assert sum(c[0]=='create' for c in record['collector'].calls)==1
        with v.store._connect() as db:
            after=dict(db.execute('SELECT * FROM sessions').fetchone())
            attempt=v.store._attempt(db,v.root['attempt_id'])
            assert db.execute('SELECT state FROM workflow_roots').fetchone()[0]=='released'
            assert db.execute('SELECT COUNT(*) FROM workflow_requests').fetchone()[0]==len(expected)
            assert db.execute('SELECT COUNT(*) FROM role_broker_grants WHERE revoked=0').fetchone()[0]==0
            assert attempt['state']=='completed'
            if reject_at is None:
                assert attempt['outcome']=='verified'
                assert after['delivery_version']==before['delivery_version']+1
                assert after['delivered_revision_sha256']!=v.initial.sha256
                assert sum(k=='run' for k,_ in evidence[-1]['verifier_calls'])==1
            else:
                assert attempt['outcome']=='needs_review' and after==before
                assert all('verifier_calls' not in e for e in evidence)
                assert db.execute("SELECT request_id FROM workflow_steps WHERE step_id='verify'").fetchone()[0] is None
        assert v.s.leases.current('account') is None
        newer=v.s.leases.reserve('account',persistent_owner_id='owner')
        replay=module.RoutedRootWorker(v.s,stage_factory=no_factory,cleanup_verifier=no_factory,
            delivery_root=v.delivery,forbidden_values=())
        stable=state(v)
        assert run(v,replay,initial_revision=None)==receipt
        assert state(v)==stable and v.s.leases.current('account')['reservation']==newer
        assert not replay.executions and len(cleanup)==1


@pytest.mark.parametrize('change',['scope','archive'])
def test_authority_loss_during_revision_validation_refuses_atomic_admission(queued,monkeypatch,change):
    v=queued;validate=module.verify_revision;factory_calls=[]
    def validate_then_revoke(*args,**kwargs):
        result=validate(*args,**kwargs)
        if change=='archive':v.store.archive(v.owner,v.root['session_id'],'archive-during-validation')
        else:
            with v.store._tx() as db:db.execute("UPDATE clients SET scopes='[]' WHERE id=?",(v.owner['id'],))
        return result
    def factory(*args):
        factory_calls.append(args)
        raise RuntimeError('Factory reached after authority revocation')
    monkeypatch.setattr(module,'verify_revision',validate_then_revoke)
    instance=worker(v,factory)
    with pytest.raises(StoreError):run(v,instance)
    assert not factory_calls and not instance.executions
    with v.store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM provider_reservations').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM workflow_accounts').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM inference_budget_roots').fetchone()[0]==0
        assert db.execute('SELECT state FROM attempts WHERE id=?',(v.root['attempt_id'],)).fetchone()[0]=='queued'
        assert db.execute('SELECT state FROM workflow_roots').fetchone()[0]=='queued'


@pytest.mark.parametrize('invalid', [{'options':None},{'services':[]}],ids=['options-none','services-list'])
def test_malformed_execution_retains_exact_handles_before_validation(queued,monkeypatch,invalid):
    v=queued;execution=replace(module.StageExecution(object(),object(),object(),object(),(),{}),**invalid)
    issued=[]
    def factory(child,*_):issued.append(child['id']);return execution
    monkeypatch.setattr(routed_stage_runner,'run_prepared_stage',no_factory)
    instance=worker(v,factory)
    with pytest.raises(StoreError,match='Trusted stage execution required'):run(v,instance)
    assert len(issued)==1
    assert instance.executions=={issued[0]:execution}
    assert instance.executions[issued[0]] is execution
    current=state(v)
    assert current['roots']==[{'state':'held','child_attempt_id':issued[0]}]
    assert current['reservations']==1 and current['budgets']==1
    with v.store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM role_broker_grants WHERE revoked=0').fetchone()[0]==0
    with pytest.raises(StoreError,match='explicit reconciliation'):run(v,instance)
    assert len(issued)==1 and state(v)==current
