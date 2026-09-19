import threading
import pytest
from test_provider_dispatch import setup
from test_supervised_executor import build
from test_hermes_inference_protocol import REQUEST


@pytest.mark.parametrize('missing', ['registration', 'schema'])
def test_missing_budget_admission_refuses_before_runtime_creation(setup, missing):
    wrapped, context = build(setup)
    with setup[0]._tx() as db:
        if missing == 'registration':
            db.execute('DELETE FROM inference_budget_generations')
            db.execute('DELETE FROM inference_budget_attempts')
        else:
            db.execute('DROP TABLE inference_budget_version')
    result = wrapped(context, REQUEST, threading.Event())
    assert result.response.status != 200
    assert result.outer_cleanup_confirmed
    assert not setup[6].events and not setup[6].objects
    assert wrapped.last_receipt.stopped and wrapped.last_receipt.cancelled
    assert wrapped.last_receipt.durable_revocation_confirmed
