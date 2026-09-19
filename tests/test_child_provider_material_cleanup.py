from dataclasses import replace
import pytest
from test_child_launch import child_launch,setup
from test_provider_dispatch import FakeRuntime
from cloudworkbench.provider_dispatch import ProviderDispatch,DispatchError
from cloudworkbench.provider_recovery import recover_request
from cloudworkbench.inference_relay import AttemptBinding


def test_child_material_recovery_preserves_shared_root_reservation(child_launch):
    st,o,l,s,root,child,args=child_launch
    runtime=FakeRuntime();l.verifier=lambda target:replace(runtime.cleanup(target),inspector_id=l.inspector_id)
    reservation=l.current('synthetic-account')['reservation']
    grant=l.issue_grant(reservation,attempt_id=child['id'],generation=1)
    dispatch=ProviderDispatch(l,runtime)
    profile=args['execution_profile'].digest
    spec=dispatch.admit(reservation,grant,request_nonce='a'*64,payload=b'fixture',profile_digest=profile,remaining_seconds=160)
    dispatch.launch(spec.lease.request_id,b'fixture')
    def fail(_):raise RuntimeError('private material remains')
    runtime.after_request_cleanup=fail
    with pytest.raises(DispatchError,match='post_cleanup_material_pending'):dispatch.cleanup(spec.lease.request_id)
    binding=AttemptBinding(child['id'],1,profile)
    with pytest.raises(DispatchError):recover_request(dispatch,spec.lease.request_id,reservation=reservation,grant_id=grant,binding=binding)
    runtime.after_request_cleanup=lambda _:None
    assert recover_request(dispatch,spec.lease.request_id,reservation=reservation,grant_id=grant,binding=binding).cleanup_confirmed
    assert sum(e[0]=='cleanup' for e in runtime.events)==1
    assert l.current('synthetic-account')=={'reservation':reservation,'state':'held','reason':None,'requires_reconciliation':False}
    with st._connect() as db:
        row=db.execute('SELECT state,child_attempt_id FROM workflow_roots').fetchone()
        assert tuple(row)==('held',child['id'])
