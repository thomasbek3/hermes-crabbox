import sqlite3
import pytest
from test_provider_dispatch import setup
from test_provider_recovery import interrupted
from cloudworkbench.provider_dispatch import Resources,OperationResolution


def test_failed_audit_insert_cannot_commit_resolution_state(setup):
    spec=interrupted(setup)
    def resolve(spec,row):
        resources=Resources(*(row[k] for k in ('provider_id','gateway_id','internal_network_id','external_network_id')))
        return OperationResolution(spec.lease.request_id,spec.launch_nonce,row['version'],row['operation'],'completed',resources,'a'*64)
    setup[6].resolution=resolve
    with setup[0]._tx() as db:
        db.execute("CREATE TRIGGER reject_resolution_audit BEFORE INSERT ON events WHEN NEW.type='provider_operation_resolved' BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='synthetic audit failure'):
        setup[7].reconcile(spec.lease.request_id)
    row=setup[7].read(spec.lease.request_id)
    assert row['uncertain']==1 and row['operation']=='start'
    assert row['operation_receipt'] is None
    assert setup[6].objects
    with setup[0]._connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE type='provider_operation_resolved'").fetchone()[0]==0
    with setup[0]._tx() as db:
        db.execute('DROP TRIGGER reject_resolution_audit')
    setup[7].reconcile(spec.lease.request_id)
    with setup[0]._connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE type='provider_operation_resolved'").fetchone()[0]==1
    assert setup[7].read(spec.lease.request_id)['uncertain']==0
