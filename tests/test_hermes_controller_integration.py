"""Same-Store cancellation across role capabilities and provider grants.

No real runtime cleanup or child scheduling is simulated as completed.
"""
import pytest
from cloudworkbench.store import Store
from cloudworkbench.models import LIVE
from cloudworkbench.role_broker import ParentScope, RoleBroker, BrokerError
from cloudworkbench.provider_leases import ProviderLeases, LeaseError


def test_store_cancel_fences_broker_and_provider_without_releasing_account(tmp_path):
    store=Store(tmp_path/'controller.db')
    principal=store.add_client('synthetic','x'*40,['submit','observe','retrieve','cancel'],['project'])
    root=store.create_session(principal,{'project_id':'project','goal':'synthetic root','agent':'hermes'},'root')
    assert store.claim_next()['id']==root['attempt_id']
    scope=ParentScope(principal['id'],'project',root['attempt_id'],root['attempt_id'],root['generation'],'native-session')
    def authorize(db, candidate):
        if candidate != scope:return False
        row=db.execute('SELECT a.*,s.owner_id,s.project_id FROM attempts a JOIN sessions s ON s.id=a.session_id WHERE a.id=?',(candidate.parent_attempt_id,)).fetchone()
        return bool(row and row['owner_id']==candidate.owner_id and row['project_id']==candidate.project_id
                    and row['generation']==candidate.generation and not row['cancel_requested'] and row['state'] in LIVE)
    broker=RoleBroker(store.path,authorize_parent=authorize,clock=lambda:1000)
    _,token=broker.issue(scope,roles=('feature',),expires_at=2000)
    pending=broker.prepare(token,native_session_id=scope.native_session_id,native_call_id='call1',role='feature',task='synthetic task')
    def no_cleanup(target):raise AssertionError('this test never authorizes physical cleanup')
    leases=ProviderLeases(store,cleanup_verifier=no_cleanup,inspector_id='inspector',clock=lambda:1000)
    leases.register_account('account',legacy_agent='claude',persistent_owner_id='provider-owner')
    reservation=leases.reserve('account',persistent_owner_id='provider-owner')
    grant=leases.issue_grant(reservation,attempt_id=root['attempt_id'],generation=root['generation'])
    request=leases.acquire_request(reservation,grant)
    assert leases.authorize_request(request)
    assert broker.read(token,pending['id'])['id']==pending['id']
    legacy=store.create_session(principal,{'project_id':'project','goal':'historical account job','agent':'claude'},'legacy')
    store.cancel(principal,root['attempt_id'],'cancel')
    with pytest.raises(LeaseError,match='grant_revoked'):leases.authorize_request(request)
    with pytest.raises(BrokerError) as denied:
        broker.prepare(token,native_session_id=scope.native_session_id,native_call_id='call2',role='feature',task='must not enqueue')
    assert denied.value.code=='parent_scope_denied'
    with pytest.raises(BrokerError):broker.read(token,pending['id'])
    assert store.claim_next() is None
    assert store.get_attempt(legacy['attempt_id'])['reason']=='provider_account_reserved'
    with store._tx() as db:
        assert db.execute('SELECT COUNT(*) FROM role_broker_pending').fetchone()[0]==1
        assert db.execute('SELECT state FROM provider_request_leases').fetchone()[0]=='quarantined'
        assert db.execute('SELECT active_id FROM provider_accounts').fetchone()[0]==reservation.reservation_id
        assert db.execute('SELECT cleanup_receipt FROM provider_request_leases').fetchone()[0] is None
