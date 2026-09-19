from dataclasses import replace
import pytest
from test_provider_dispatch import setup, admit
from cloudworkbench.inference_relay import AttemptBinding
from cloudworkbench.provider_dispatch import DispatchError, OperationResolution
from cloudworkbench.provider_recovery import recover_request


def args(setup, spec):
    return dict(reservation=setup[4], grant_id=setup[5],
                binding=AttemptBinding(spec.attempt_id,spec.generation,spec.profile_digest))


def interrupted(setup):
    spec=admit(setup)
    def fail(*unused):
        raise RuntimeError('synthetic interrupted start')
    setup[6].hooks['start']=fail
    with pytest.raises(DispatchError,match='start_outcome_unknown'):
        setup[7].launch(spec.lease.request_id,b'payload')
    return spec


def test_unknown_effects_are_retained_without_cleanup(setup):
    spec=interrupted(setup)
    with pytest.raises(DispatchError,match='operation_still_unknown'):
        recover_request(setup[7],spec.lease.request_id,**args(setup,spec))
    assert setup[6].objects
    assert not any(event[0]=='cleanup' for event in setup[6].events)


def test_matching_settled_journal_allows_exact_cleanup_no_restart(setup):
    from cloudworkbench.provider_dispatch import Resources
    spec=interrupted(setup)
    def resolve(spec,row):
        resources=Resources(*(row[key] for key in ('provider_id','gateway_id','internal_network_id','external_network_id')))
        return OperationResolution(spec.lease.request_id,spec.launch_nonce,row['version'],row['operation'],'completed',resources,'a'*64)
    setup[6].resolution=resolve
    receipt=recover_request(setup[7],spec.lease.request_id,**args(setup,spec))
    assert receipt.reconciled and receipt.cleanup_confirmed and not setup[6].objects
    again=recover_request(setup[7],spec.lease.request_id,**args(setup,spec))
    assert again.cleanup_confirmed and not again.reconciled
    assert len([e for e in setup[6].events if e[0]=='start'])==1


def test_running_request_is_not_recovered(setup):
    spec=admit(setup);setup[7].launch(spec.lease.request_id,b'payload')
    with pytest.raises(DispatchError,match='recovery_requires_quarantine'):
        recover_request(setup[7],spec.lease.request_id,**args(setup,spec))
    assert setup[6].objects


def test_wrong_generation_cannot_recover(setup):
    spec=interrupted(setup);values=args(setup,spec)
    values['binding']=replace(values['binding'],generation=spec.generation+1)
    with pytest.raises(DispatchError,match='recovery_binding_invalid'):
        recover_request(setup[7],spec.lease.request_id,**values)
    assert setup[6].objects


@pytest.mark.parametrize('field', ['grant_id','reservation','profile'])
def test_foreign_binding_does_not_touch_runtime(setup, field):
    spec=interrupted(setup);values=args(setup,spec)
    if field=='grant_id':
        values['grant_id']='foreign-grant'
    elif field=='reservation':
        values['reservation']=replace(values['reservation'],reservation_id='foreign-reservation')
    else:
        values['binding']=replace(values['binding'],profile_digest='f'*64)
    before=list(setup[6].events)
    with pytest.raises(DispatchError,match='recovery_binding_invalid'):
        recover_request(setup[7],spec.lease.request_id,**values)
    assert setup[6].events==before and setup[6].objects


def test_cleanup_failure_cannot_produce_recovery_receipt(setup):
    from cloudworkbench.provider_dispatch import Resources
    from cloudworkbench.provider_leases import LeaseError
    spec=interrupted(setup)
    def resolve(spec,row):
        resources=Resources(*(row[key] for key in ('provider_id','gateway_id','internal_network_id','external_network_id')))
        return OperationResolution(spec.lease.request_id,spec.launch_nonce,row['version'],row['operation'],'completed',resources,'a'*64)
    setup[6].resolution=resolve
    def unavailable(target):
        raise RuntimeError('synthetic cleanup failure')
    setup[3].verifier=unavailable
    with pytest.raises(LeaseError):
        recover_request(setup[7],spec.lease.request_id,**args(setup,spec))
    assert setup[6].objects
    assert setup[7].read(spec.lease.request_id)['state']!='cleaned'


def test_restarted_controller_requires_owner_reconciliation(setup):
    from cloudworkbench.provider_leases import ProviderLeases
    from cloudworkbench.provider_dispatch import ProviderDispatch
    spec=interrupted(setup)
    fresh=ProviderLeases(setup[0],cleanup_verifier=setup[6].cleanup,
                         inspector_id='synthetic-inspector',clock=lambda:setup[8][0])
    dispatch=ProviderDispatch(fresh,setup[6]);before=list(setup[6].events)
    with pytest.raises(DispatchError,match='recovery_requires_owner_reconciliation'):
        recover_request(dispatch,spec.lease.request_id,**args(setup,spec))
    assert setup[6].events==before and setup[6].objects


def test_cleanup_failure_then_cleanup_resolution_recovers_without_restart(setup):
    from cloudworkbench.provider_dispatch import Resources
    spec=interrupted(setup);operations=[]
    def resolve(spec,row):
        operations.append(row['operation'])
        resources=Resources(*(row[key] for key in ('provider_id','gateway_id','internal_network_id','external_network_id')))
        return OperationResolution(spec.lease.request_id,spec.launch_nonce,row['version'],row['operation'],
                                   'definitive_no_effect' if row['operation']=='cleanup' else 'completed',resources,'a'*64)
    setup[6].resolution=resolve
    setup[3].verifier=lambda target:False
    with pytest.raises(DispatchError,match='cleanup_outcome_unknown'):
        recover_request(setup[7],spec.lease.request_id,**args(setup,spec))
    assert setup[6].objects
    assert setup[7].read(spec.lease.request_id)['operation']=='cleanup'
    setup[3].verifier=setup[6].cleanup
    receipt=recover_request(setup[7],spec.lease.request_id,**args(setup,spec))
    assert receipt.cleanup_confirmed and not setup[6].objects
    assert operations==['start','cleanup']
    import json
    with setup[0]._connect() as db:
        receipts=[json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE type='provider_operation_resolved' ORDER BY sequence")]
    assert [row['operation'] for row in receipts]==['start','cleanup']
    assert all(row['request_id']==spec.lease.request_id for row in receipts)
    assert receipts[0]['evidence_sha256']=='a'*64
    assert len([e for e in setup[6].events if e[0]=='start'])==1


def test_recovery_waits_for_active_launch_lock_then_refuses_running(setup):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    spec=admit(setup)
    entered, release, recovering = threading.Event(), threading.Event(), threading.Event()
    def blocked_start(*unused):
        entered.set()
        assert release.wait(3)
    setup[6].hooks['start']=blocked_start
    def recover():
        recovering.set()
        return recover_request(setup[7],spec.lease.request_id,**args(setup,spec))
    with ThreadPoolExecutor(max_workers=2) as pool:
        launch=pool.submit(setup[7].launch,spec.lease.request_id,b'payload')
        assert entered.wait(2)
        before=list(setup[6].events)
        recovery=pool.submit(recover)
        try:
            assert recovering.wait(1)
            # Taking the same account lock is observable: recovery cannot run
            # any inspection/resolution/cleanup while launch owns it.
            import time
            time.sleep(.05)
            assert not recovery.done()
            assert setup[6].events==before
        finally:
            release.set()
        launch.result(timeout=2)
        with pytest.raises(DispatchError,match='recovery_requires_quarantine'):
            recovery.result(timeout=2)
    assert not any(e[0] in ('resolve','cleanup') for e in setup[6].events)
    assert setup[7].read(spec.lease.request_id)['state']=='running'
    assert setup[6].objects


@pytest.mark.parametrize('delivered',[False,True])
def test_physically_clean_recovery_retries_material_before_confirmation(setup,delivered):
    spec=admit(setup);dispatch=setup[7];runtime=setup[6]
    dispatch.launch(spec.lease.request_id,b'payload');dispatch.collect(spec.lease.request_id)
    dispatch.cleanup(spec.lease.request_id)
    if delivered:dispatch.deliver(spec.lease.request_id)
    physical=sum(e[0]=='cleanup' for e in runtime.events);calls=[]
    def material(_):calls.append('attempt');raise RuntimeError('synthetic material remains')
    runtime.after_request_cleanup=material
    with pytest.raises(DispatchError,match='post_cleanup_material_pending'):
        recover_request(dispatch,spec.lease.request_id,**args(setup,spec))
    assert not runtime.objects and len(calls)==1
    runtime.after_request_cleanup=lambda _:calls.append('complete')
    assert recover_request(dispatch,spec.lease.request_id,**args(setup,spec)).cleanup_confirmed
    assert calls==['attempt','complete']
    assert sum(e[0]=='cleanup' for e in runtime.events)==physical
    assert setup[3].current(setup[4].account_id)['reservation']==setup[4]
    assert setup[3].current(setup[4].account_id)['state']=='held'
