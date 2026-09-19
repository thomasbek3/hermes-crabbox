"""Account rejection must fence subsequent requests until explicit reconciliation."""
import threading
import pytest
from test_provider_dispatch import setup
from test_provider_executor import executor
from test_hermes_inference_protocol import REQUEST


@pytest.mark.parametrize('error',['auth_missing','auth_invalid','provider_auth_rejected'])
def test_auth_rejection_quarantines_logical_owner_after_physical_cleanup(setup,error):
    adapter,context,envelope=executor(setup)
    envelope.update(status='error',error=error,result=None)
    response=adapter(context,REQUEST,threading.Event())
    assert response.response.status==401 and response.outer_cleanup_confirmed
    assert not setup[6].objects
    with setup[0]._connect() as db:
        owner=db.execute('SELECT state FROM provider_reservations WHERE id=?',(setup[4].reservation_id,)).fetchone()
        grant=db.execute('SELECT revoked_at FROM provider_execution_grants WHERE id=?',(setup[5],)).fetchone()
    assert owner['state']=='quarantined'
    assert grant['revoked_at'] is not None
    assert adapter.authorize(context.binding) is False
