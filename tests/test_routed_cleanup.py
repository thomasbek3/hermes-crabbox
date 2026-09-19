from dataclasses import replace
import hashlib
import json
import secrets
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.test_routed_stage import stage
from tests.test_routed_runtime import setup
from tests.test_routed_driver import driven, run
from tests.test_provider_dispatch import FakeRuntime
from cloudworkbench.inference_budget import RootScope, AttemptScope
from cloudworkbench.inference_relay import AttemptBinding, WorkerDispatcher, WorkerSocketServer
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.provider_dispatch import ProviderDispatch, DispatchError
from cloudworkbench.provider_dispatch import OperationResolution, Resources
from cloudworkbench.provider_executor import ProviderExecutor
from cloudworkbench.routed_cleanup import RoutedChildCleanup, ChildCleanupError
from cloudworkbench.supervised_executor import SupervisedProviderExecutor
from cloudworkbench.worker_service import WorkerService
from cloudworkbench.store import StoreError


@pytest.fixture
def cleanup(driven, tmp_path):
    scheduler, owner, child, prepared, runtime, spec, _, _ = driven
    reservation = scheduler.leases.current('account')['reservation']
    grant = scheduler.leases.issue_grant(reservation, attempt_id=child['id'], generation=1)
    provider = FakeRuntime()
    provider.after_request_cleanup = lambda spec: None  # Fixture creates no private material.
    scheduler.leases.verifier = provider.cleanup
    scheduler.leases.inspector_id = 'synthetic-inspector'
    root = scheduler.store.get_attempt(child['workflow_root_id'])
    scope = AttemptScope(RootScope(owner['id'], 'demo', root['session_id'], root['turn_id'], root['id'], 1), child['id'], 1)
    dispatch = ProviderDispatch(scheduler.leases, provider, budget=scheduler.budget, budget_scope=scope)
    binding = AttemptBinding(child['id'], 1, prepared.profile_digest)
    executor = ProviderExecutor(dispatch, reservation=reservation, grant_id=grant, binding=binding,
        profile=NativeProfile('openai-codex','gpt-6-astra','high'), remaining_seconds=lambda:90)
    wrapped = SupervisedProviderExecutor(executor)
    worker = WorkerDispatcher(journal_path=tmp_path/'worker.db', binding=binding,
        capability_sha256=hashlib.sha256(b'a'*64).hexdigest(), authorize=wrapped.authorize,
        execute_request=wrapped)
    socket_home=tempfile.TemporaryDirectory(prefix='cc-',dir='/tmp')
    socket_dir=Path(socket_home.name).resolve()
    service=WorkerService(WorkerSocketServer(socket_dir/'w',worker))
    service.start()
    values={'driven':driven,'scheduler':scheduler,'runtime':runtime,'spec':spec,
            'dispatch':dispatch,'provider':provider,'grant':grant,'reservation':reservation,
            'service':service,'wrapped':wrapped,'binding':binding}
    yield values
    service.close(timeout_seconds=2)
    socket_home.cleanup()


def request(value, *, launch=True, clean=False):
    dispatch=value['dispatch']
    spec=dispatch.admit(value['reservation'],value['grant'],request_nonce=secrets.token_hex(32),
        payload=b'fixture',profile_digest=value['binding'].profile_digest)
    if launch:dispatch.launch(spec.lease.request_id,b'fixture')
    if clean:dispatch.cleanup(spec.lease.request_id)
    return spec


def ready(value, **changes):
    result=run(value['driven'])
    return RoutedChildCleanup(value['scheduler'],value['runtime'],value['spec'],result,
        services=changes.get('services',(value['service'],)))


def test_exact_caller_worker_and_provider_proof_keeps_root_accounts_and_seat(cleanup):
    spec=request(cleanup)
    collector=ready(cleanup)
    evidence=collector.collect()
    body=json.loads(evidence.evidence_path.read_bytes())
    assert hashlib.sha256(evidence.evidence_path.read_bytes()).hexdigest()==evidence.evidence_sha256
    assert not cleanup['provider'].objects
    assert body['providers'][0]['request_id']==spec.lease.request_id
    assert body['providers'][0]['material_cleanup_confirmed'] is True
    assert body['workers'][0]['executor']['supervisors_stopped'] is True
    assert cleanup['service'].receipt.resources_closed
    assert cleanup['scheduler'].leases.current('account')['reservation']==cleanup['reservation']
    with cleanup['scheduler'].store._connect() as db:
        root=db.execute('SELECT state,child_attempt_id FROM workflow_roots').fetchone()
        assert root['state']=='held' and root['child_attempt_id']==cleanup['spec'].attempt_id
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()
    assert collector.collect()==evidence


def test_terminal_child_uses_combined_verifier_but_root_account_stays_held(cleanup):
    request(cleanup)
    collector=ready(cleanup)
    scheduler=cleanup['scheduler']
    scheduler.transition(cleanup['spec'].attempt_id,'failed',expected_generation=1)
    scheduler.release_child(cleanup['spec'].attempt_id,expected_generation=1,verifier=collector.verifier)
    with scheduler.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] is None
    assert scheduler.leases.current('account')['reservation']==cleanup['reservation']


def test_pending_material_is_retried_after_physical_cleanup(cleanup):
    spec=request(cleanup,clean=True)
    physical=sum(e[0]=='cleanup' for e in cleanup['provider'].events)
    calls=[]
    def pending(spec):
        calls.append(spec.lease.request_id)
        if len(calls)==1:raise OSError('fixture private detail')
    cleanup['provider'].after_request_cleanup=pending
    collector=ready(cleanup)
    with pytest.raises(DispatchError,match='post_cleanup_material_pending'):collector.collect()
    assert not list(cleanup['runtime'].root.glob('child-cleanup-*.json'))
    result=collector.collect()
    assert calls==[spec.lease.request_id]*2 and result.evidence_path.is_file()
    assert sum(e[0]=='cleanup' for e in cleanup['provider'].events)==physical


def test_unknown_provider_effects_never_become_absence_success(cleanup):
    spec=request(cleanup,launch=False)
    def unknown(*args):raise TimeoutError
    cleanup['provider'].hooks['create']=unknown
    with pytest.raises(DispatchError):cleanup['dispatch'].launch(spec.lease.request_id,b'fixture')
    collector=ready(cleanup)
    cleanup['provider'].objects.clear()
    with pytest.raises(DispatchError,match='operation_still_unknown'):collector.collect()
    assert not list(cleanup['runtime'].root.glob('child-cleanup-*.json'))


def test_extra_grant_without_drained_service_refuses_before_cleanup(cleanup):
    cleanup['scheduler'].leases.issue_grant(cleanup['reservation'],attempt_id=cleanup['spec'].attempt_id,generation=1)
    request(cleanup)
    with pytest.raises(ChildCleanupError,match='uncovered_child_grant'):ready(cleanup).collect()
    assert cleanup['provider'].objects


def test_missing_dispatch_does_not_hide_request_lease(cleanup):
    cleanup['scheduler'].leases.acquire_request(cleanup['reservation'],cleanup['grant'])
    with pytest.raises(ChildCleanupError,match='membership_unproven'):ready(cleanup).collect()


def test_foreign_profile_is_not_cleaned(cleanup):
    dispatch=cleanup['dispatch']
    spec=dispatch.admit(cleanup['reservation'],cleanup['grant'],request_nonce=secrets.token_hex(32),
        payload=b'fixture',profile_digest='e'*64)
    dispatch.launch(spec.lease.request_id,b'fixture')
    with pytest.raises(ChildCleanupError,match='dispatch_binding_changed'):ready(cleanup).collect()
    assert cleanup['provider'].objects


def test_supervisor_shutdown_failure_retains_provider_resources(cleanup):
    request(cleanup)
    cleanup['wrapped']._fenced=True
    with pytest.raises(ChildCleanupError,match='observer_unconfirmed'):ready(cleanup).collect()
    assert cleanup['provider'].objects


def test_stale_cleanup_target_cannot_release_or_clean(cleanup):
    request(cleanup)
    collector=ready(cleanup)
    target=collector._snapshot()[0]
    with pytest.raises(ChildCleanupError,match='target_changed'):
        collector.collect(target=replace(target,child_generation=2))
    assert cleanup['provider'].objects
    assert not cleanup['service'].receipt.resources_closed


def test_tampered_physical_receipt_not_accepted(cleanup):
    spec=request(cleanup,clean=True)
    with cleanup['scheduler'].store._tx() as db:
        row=db.execute('SELECT cleanup_receipt FROM provider_request_leases WHERE id=?',(spec.lease.request_id,)).fetchone()
        proof=json.loads(row[0]);proof['target']['generation']=99
        db.execute('UPDATE provider_request_leases SET cleanup_receipt=? WHERE id=?',(json.dumps(proof),spec.lease.request_id))
    with pytest.raises(ChildCleanupError,match='proof_mismatch'):ready(cleanup).collect()


def test_grant_with_no_request_can_drain_without_inventing_provider_proof(cleanup):
    result=ready(cleanup).collect()
    assert json.loads(result.evidence_path.read_text())['providers']==[]
    assert not cleanup['provider'].events


def test_root_owner_change_refuses_all_cleanup(cleanup):
    request(cleanup)
    collector=ready(cleanup)
    with cleanup['scheduler'].store._tx() as db:
        db.execute("UPDATE provider_reservations SET controller_instance_id='replacement'")
    with pytest.raises(ChildCleanupError,match='authority_changed'):collector.collect()
    assert cleanup['provider'].objects


def test_multiple_requests_include_already_delivered_and_active_cleanup(cleanup):
    first=request(cleanup)
    cleanup['dispatch'].collect(first.lease.request_id)
    cleanup['dispatch'].cleanup(first.lease.request_id)
    cleanup['dispatch'].deliver(first.lease.request_id)
    second=request(cleanup)
    result=ready(cleanup).collect()
    body=json.loads(result.evidence_path.read_text())
    assert {r['request_id'] for r in body['providers']}=={first.lease.request_id,second.lease.request_id}
    assert not cleanup['provider'].objects


def test_definitive_operation_resolution_cleans_without_restart(cleanup):
    spec=request(cleanup,launch=False)
    def unknown(*args):raise TimeoutError
    cleanup['provider'].hooks['start']=unknown
    with pytest.raises(DispatchError):cleanup['dispatch'].launch(spec.lease.request_id,b'fixture')
    def resolved(spec,row):
        resources=Resources(*(row[k] for k in ('provider_id','gateway_id','internal_network_id','external_network_id')))
        return OperationResolution(spec.lease.request_id,spec.launch_nonce,row['version'],row['operation'],
                                   'completed',resources,'d'*64)
    cleanup['provider'].resolution=resolved
    ready(cleanup).collect()
    assert not cleanup['provider'].objects
    assert sum(e[0]=='start' for e in cleanup['provider'].events)==1


def test_wrong_reservation_rejected_even_with_no_provider_requests(cleanup):
    collector=ready(cleanup)
    executor=cleanup['wrapped'].executor
    executor.reservation=replace(executor.reservation,epoch=999)
    with pytest.raises(ChildCleanupError,match='worker_reservation_changed'):collector.collect()


def test_replaced_caller_cleanup_refused_before_provider_cleanup(cleanup):
    request(cleanup)
    collector=ready(cleanup)
    collector.quiesced=replace(collector.quiesced,caller_cleanup={**collector.quiesced.caller_cleanup,'caller_removed':False})
    with pytest.raises(ChildCleanupError,match='caller_cleanup_changed'):collector.collect()
    assert cleanup['provider'].objects


def test_missing_material_cleanup_contract_does_not_claim_success(cleanup):
    request(cleanup)
    del cleanup['provider'].after_request_cleanup
    with pytest.raises(ChildCleanupError,match='provider_material_cleanup_required'):ready(cleanup).collect()
    assert cleanup['provider'].objects
    assert not cleanup['service'].receipt.resources_closed


def test_two_exact_grants_and_workers_are_both_required_and_cleaned(cleanup):
    first=request(cleanup,clean=True)
    grant=cleanup['scheduler'].leases.issue_grant(cleanup['reservation'],attempt_id=cleanup['spec'].attempt_id,generation=1)
    executor=ProviderExecutor(cleanup['dispatch'],reservation=cleanup['reservation'],grant_id=grant,
        binding=cleanup['binding'],profile=cleanup['wrapped'].executor.profile,remaining_seconds=lambda:90)
    wrapped=SupervisedProviderExecutor(executor)
    with tempfile.TemporaryDirectory(prefix='cc-',dir='/tmp') as folder:
        path=Path(folder).resolve()
        worker=WorkerDispatcher(journal_path=path/'worker.db',binding=cleanup['binding'],
            capability_sha256=hashlib.sha256(b'b'*64).hexdigest(),authorize=wrapped.authorize,execute_request=wrapped)
        service=WorkerService(WorkerSocketServer(path/'socket',worker));service.start()
        try:
            second=request({**cleanup,'grant':grant})
            value=ready(cleanup,services=(cleanup['service'],service)).collect()
            proof=json.loads(value.evidence_path.read_text())
            assert {r['request_id'] for r in proof['providers']}=={first.lease.request_id,second.lease.request_id}
            assert len(proof['workers'])==2 and not cleanup['provider'].objects
        finally:service.close(2)


def test_concurrent_child_release_records_exactly_one_event(cleanup):
    request(cleanup);collector=ready(cleanup)
    scheduler=cleanup['scheduler'];child=cleanup['spec'].attempt_id
    scheduler.transition(child,'failed',expected_generation=1)
    barrier=threading.Barrier(2)
    def release():
        barrier.wait(timeout=2)
        try:scheduler.release_child(child,expected_generation=1,verifier=collector.verifier)
        except (StoreError,ChildCleanupError):return False
        return True
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:release(),range(2)))
    assert sorted(outcomes)==[False,True]
    with scheduler.store._connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE type='workflow.child_released'").fetchone()[0]==1
    assert sum(e[0]=='cleanup' for e in cleanup['provider'].events)==1
    assert scheduler.leases.current('account')['reservation']==cleanup['reservation']
