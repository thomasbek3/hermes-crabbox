import pytest
from test_provider_dispatch import setup, admit
from test_provider_recovery import interrupted
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.provider_dispatch import ProviderDispatch, DispatchError, Resources, OperationResolution
from cloudworkbench.provider_recovery import recover_previous_owner


def restarted(setup):
    leases=ProviderLeases(setup[0],cleanup_verifier=setup[6].cleanup,
        inspector_id='synthetic-inspector',clock=lambda:setup[8][0])
    return ProviderDispatch(leases,setup[6])


def test_previous_controller_unknown_effects_remain_quarantined(setup):
    spec=interrupted(setup);dispatch=restarted(setup)
    with pytest.raises(DispatchError,match='operation_still_unknown'):
        recover_previous_owner(dispatch,reservation=setup[4],takeover_authorized=True)
    assert setup[6].objects
    assert dispatch.leases.current('account')['reservation']==setup[4]
    with setup[0]._connect() as db:
        assert db.execute('SELECT revoked_at FROM provider_execution_grants').fetchone()[0] is not None


def test_previous_controller_settled_effects_clean_before_new_epoch(setup):
    spec=interrupted(setup);dispatch=restarted(setup)
    def resolve(spec,row):
        resources=Resources(*(row[k] for k in ('provider_id','gateway_id','internal_network_id','external_network_id')))
        return OperationResolution(spec.lease.request_id,spec.launch_nonce,row['version'],row['operation'],'completed',resources,'a'*64)
    setup[6].resolution=resolve
    assert recover_previous_owner(dispatch,reservation=setup[4],takeover_authorized=True)==(spec.lease.request_id,)
    assert not setup[6].objects and dispatch.leases.current('account') is None
    new=dispatch.leases.reserve('account',persistent_owner_id='logical-owner')
    assert new.epoch==setup[4].epoch+1
    before=list(setup[6].events)
    assert recover_previous_owner(dispatch,reservation=setup[4],takeover_authorized=True)==()
    assert dispatch.leases.current('account')['reservation']==new
    assert setup[6].events==before
    assert len([e for e in setup[6].events if e[0]=='start'])==1


def test_current_controller_owner_is_not_restart_recovered(setup):
    with pytest.raises(DispatchError,match='recovery_requires_previous_controller'):
        recover_previous_owner(setup[7],reservation=setup[4])
    assert not setup[6].events


def test_actual_controller_exit_mid_start_keeps_owner_without_resolution(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    from cloudworkbench.store import Store
    from test_provider_dispatch import FakeRuntime
    store=Store(tmp_path/'state.db')
    principal=store.add_client('crash-test','t'*40,['submit'],['project'])
    attempt=store.create_session(principal,{'project_id':'project','agent':'hermes','goal':'crash-test'},'task')
    store.claim_next()
    script='''import os,sys
from cloudworkbench.store import Store
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.provider_dispatch import ProviderDispatch
from test_provider_dispatch import FakeRuntime
store=Store(sys.argv[1]);runtime=FakeRuntime()
leases=ProviderLeases(store,cleanup_verifier=runtime.cleanup,inspector_id='synthetic-inspector')
leases.register_account('crash-account',legacy_agent='claude',persistent_owner_id='crash-owner')
owner=leases.reserve('crash-account',persistent_owner_id='crash-owner')
grant=leases.issue_grant(owner,attempt_id=sys.argv[2],generation=1)
dispatch=ProviderDispatch(leases,runtime)
spec=dispatch.admit(owner,grant,request_nonce='a'*64,payload=b'x',profile_digest='b'*64)
runtime.hooks['start']=lambda *unused: os._exit(77)
dispatch.launch(spec.lease.request_id,b'x')
'''
    env=dict(os.environ)
    root=Path(__file__).resolve().parents[1]
    env['PYTHONPATH']=str(root/'src')+os.pathsep+str(root/'tests')
    result=subprocess.run([sys.executable,'-c',script,str(store.path),attempt['attempt_id']],
                          env=env,capture_output=True,text=True,timeout=5)
    assert result.returncode==77,result.stderr
    runtime=FakeRuntime()
    leases=ProviderLeases(store,cleanup_verifier=runtime.cleanup,inspector_id='synthetic-inspector')
    dispatch=ProviderDispatch(leases,runtime)
    owner=leases.current('crash-account')['reservation']
    with store._connect() as db:
        assert db.execute('SELECT state FROM provider_dispatch').fetchone()[0]=='start_intent'
    with pytest.raises(DispatchError,match='operation_still_unknown'):
        recover_previous_owner(dispatch,reservation=owner,takeover_authorized=True)
    assert leases.current('crash-account')['reservation']==owner
    assert not any(event[0] in ('create','start','cleanup') for event in runtime.events)


def test_other_controller_cannot_fence_healthy_owner_without_authorization(setup):
    spec=admit(setup);setup[7].launch(spec.lease.request_id,b'payload')
    dispatch=restarted(setup);before=list(setup[6].events)
    with pytest.raises(DispatchError,match='recovery_requires_takeover_authorization'):
        recover_previous_owner(dispatch,reservation=setup[4])
    assert setup[6].events==before and setup[6].objects
    with setup[0]._connect() as db:
        assert db.execute('SELECT revoked_at FROM provider_execution_grants').fetchone()[0] is None
    setup[3].authorize_request(spec.lease)


@pytest.mark.parametrize('phase',['running','collected'])
def test_authorized_takeover_cleans_active_scope_and_revokes_delivery(setup,phase):
    spec=admit(setup);setup[7].launch(spec.lease.request_id,b'payload')
    if phase=='collected':
        setup[7].collect(spec.lease.request_id)
    dispatch=restarted(setup)
    assert recover_previous_owner(dispatch,reservation=setup[4],takeover_authorized=True)==(spec.lease.request_id,)
    assert not setup[6].objects and dispatch.leases.current('account') is None
    with pytest.raises(DispatchError,match='response_revoked'):
        setup[7].deliver(spec.lease.request_id)
    assert len([e for e in setup[6].events if e[0]=='start'])==1


def test_owner_cleanup_failure_retains_account_until_settled_retry(setup):
    from cloudworkbench.provider_leases import LeaseError
    spec=admit(setup);setup[7].launch(spec.lease.request_id,b'payload')
    dispatch=restarted(setup)
    dispatch.leases.verifier=lambda target:False
    with pytest.raises(LeaseError,match='cleanup_unconfirmed'):
        recover_previous_owner(dispatch,reservation=setup[4],takeover_authorized=True)
    assert setup[6].objects and dispatch.leases.current('account') is not None
    assert dispatch.read(spec.lease.request_id)['operation']=='cleanup'
    def resolve(spec,row):
        resources=Resources(*(row[k] for k in ('provider_id','gateway_id','internal_network_id','external_network_id')))
        return OperationResolution(spec.lease.request_id,spec.launch_nonce,row['version'],row['operation'],
                                   'definitive_no_effect',resources,'a'*64)
    setup[6].resolution=resolve
    dispatch.leases.verifier=setup[6].cleanup
    recover_previous_owner(dispatch,reservation=setup[4],takeover_authorized=True)
    assert not setup[6].objects and dispatch.leases.current('account') is None
    assert len([e for e in setup[6].events if e[0]=='start'])==1
