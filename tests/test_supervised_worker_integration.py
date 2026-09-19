import hashlib
import threading
from test_provider_dispatch import setup
from test_supervised_executor import build
from test_hermes_inference_protocol import REQUEST
from cloudworkbench.inference_relay import WorkerDispatcher, _json


def test_authenticated_worker_uses_supervised_budget_and_cached_nonce(setup, tmp_path):
    wrapped, context = build(setup)
    capability = 'c'*64
    worker = WorkerDispatcher(journal_path=tmp_path/'worker.db',binding=context.binding,
        capability_sha256=hashlib.sha256(capability.encode()).hexdigest(),
        authorize=wrapped.authorize,execute_request=wrapped)
    request = {'binding':context.binding.wire(),'capability':capability,
        'nonce':context.request_nonce,'payload_digest':hashlib.sha256(_json(REQUEST)).hexdigest(),
        'payload':REQUEST}
    try:
        first = worker.dispatch(request,threading.Event())
        assert first['response']['status']==200
        assert wrapped.last_receipt.stopped and not wrapped.last_receipt.cancelled
        assert not setup[6].objects
        assert worker.dispatch(request,threading.Event())==first
        with setup[0]._connect() as db:
            assert db.execute('SELECT used FROM inference_budget_roots').fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM provider_dispatch').fetchone()[0]==1
        assert len([e for e in setup[6].events if e[0]=='create'])==1
    finally:
        worker.close()
