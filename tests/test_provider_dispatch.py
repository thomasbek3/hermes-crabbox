from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import pytest

from cloudworkbench.provider_leases import ProviderLeases, LeaseError, CleanupReceipt
from cloudworkbench.provider_dispatch import ProviderDispatch, DispatchError, Resources, ObservedResource, OperationResolution
from cloudworkbench.store import Store


class FakeRuntime:
    def __init__(self):
        self.objects={};self.events=[];self.hooks={};self.resolution=None
    def hook(self,name,*args):
        if name in self.hooks:return self.hooks[name](*args)
    def create(self,spec,payload,*,deadline):
        self.events.append(('create',spec.lease.request_id))
        resources=Resources(*(hashlib.sha256((spec.launch_nonce+role).encode()).hexdigest() for role in ('p','g','i','e')))
        for component,identity,name in [('provider',resources.provider_id,spec.provider_name),('gateway',resources.gateway_id,spec.gateway_name)]:
            self.objects[identity]=ObservedResource(identity,name,spec.labels(component),'created')
        for role,identity,name in [('internal',resources.internal_network_id,spec.internal_network_name),('external',resources.external_network_id,spec.external_network_name)]:
            self.objects[identity]=ObservedResource(identity,name,spec.network_labels(role),'created')
        self.hook('create',spec,resources)
        return resources
    def inspect(self,spec,resources,*,deadline):
        self.events.append(('inspect',spec.lease.request_id))
        result=tuple(self.objects[identity] for identity in (resources.provider_id,resources.gateway_id,resources.internal_network_id,resources.external_network_id))
        return self.hook('inspect',spec,result) or result
    def start(self,spec,resources,*,deadline):
        self.events.append(('start',spec.lease.request_id))
        self.hook('start',spec,resources)
        for identity in (resources.provider_id,resources.gateway_id):self.objects[identity]=replace(self.objects[identity],state='running')
    def collect(self,spec,resources,*,limit,deadline):
        self.events.append(('collect',spec.lease.request_id))
        return self.hook('collect',spec,resources) or b'{"answer":"synthetic"}'
    def resolve(self,spec,row,*,deadline):
        self.events.append(('resolve',spec.lease.request_id))
        return self.resolution(spec,row) if self.resolution else None
    def cleanup(self,target):
        self.events.append(('cleanup',target.scope))
        for items in target.dispatches:
            row=dict(items)
            for key in ('provider_id','gateway_id','internal_network_id','external_network_id'):
                identity=row[key]
                if identity is not None:
                    obj=self.objects[identity]
                    assert obj.labels['io.cloudworkbench.provider-launch']==row['launch_nonce']
                    del self.objects[identity]
        return CleanupReceipt(target,'synthetic-inspector','terminated',1000.0,'d'*64)


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'state.db')
    principal=store.add_client('test','t'*40,['submit','observe','retrieve','cancel'],['project'])
    attempt=store.create_session(principal,{'project_id':'project','agent':'hermes','goal':'synthetic'},'task')
    store.claim_next()
    runtime=FakeRuntime();clock=[1000.0]
    leases=ProviderLeases(store,cleanup_verifier=runtime.cleanup,inspector_id='synthetic-inspector',clock=lambda:clock[0])
    leases.register_account('account',legacy_agent='claude',persistent_owner_id='logical-owner')
    owner=leases.reserve('account',persistent_owner_id='logical-owner')
    grant=leases.issue_grant(owner,attempt_id=attempt['attempt_id'],generation=attempt['generation'])
    dispatcher=ProviderDispatch(leases,runtime)
    return store,principal,attempt,leases,owner,grant,runtime,dispatcher,clock


def admit(setup,nonce=None,payload=b'payload',profile='b'*64):
    _,_,_,_,owner,grant,_,dispatcher,_=setup
    return dispatcher.admit(owner,grant,request_nonce=nonce or secrets.token_hex(32),payload=payload,profile_digest=profile)


def test_owner_is_logical_and_two_requests_have_distinct_runtime_ids(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    assert not hasattr(owner,'runtime_id')
    ids=[]
    for _ in range(2):
        spec=admit(setup);ids.append(d.launch(spec.lease.request_id,b'payload'))
        assert d.collect(spec.lease.request_id)
        with pytest.raises(DispatchError,match='response_not_cleaned'):d.deliver(spec.lease.request_id)
        d.cleanup(spec.lease.request_id)
        assert d.deliver(spec.lease.request_id)==b'{"answer":"synthetic"}'
        assert d.deliver(spec.lease.request_id)==b'{"answer":"synthetic"}'
        assert not runtime.objects
    assert ids[0]!=ids[1] and leases.current('account')['reservation']==owner


def test_immutable_nonce_payload_profile_and_cached_request(setup):
    *_,d,_=setup
    nonce=secrets.token_hex(32);first=admit(setup,nonce=nonce)
    assert admit(setup,nonce=nonce)==first
    for payload,profile in [(b'other','b'*64),(b'payload','c'*64)]:
        with pytest.raises(DispatchError,match='dispatch_idempotency_conflict'):admit(setup,nonce=nonce,payload=payload,profile=profile)
    d.launch(first.lease.request_id,b'payload')
    with pytest.raises(DispatchError,match='dispatch_already_launched'):d.launch(first.lease.request_id,b'payload')
    assert d.read(first.lease.request_id)['state']=='running'


def test_names_labels_and_inspection_before_bind_start(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    spec=admit(setup)
    assert spec.provider_name.startswith('cwb2-e1-'+spec.launch_nonce)
    assert spec.gateway_name.endswith('-gateway')
    assert spec.labels('provider')=={
        'io.cloudworkbench.provider-reservation':owner.reservation_id,
        'io.cloudworkbench.provider-epoch':'1','io.cloudworkbench.provider-request':spec.lease.request_id,
        'io.cloudworkbench.provider-launch':spec.launch_nonce,'io.cloudworkbench.provider-component':'provider'}
    d.launch(spec.lease.request_id,b'payload')
    assert [event[0] for event in runtime.events]==['create','inspect','start','inspect']


@pytest.mark.parametrize('fault',['labels','name','id','state'])
def test_create_identity_mismatch_quarantines_without_start(setup,fault):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    def bad(spec,observations):
        fields={'labels':{},'name':'other','runtime_id':'f'*64,'state':'running'}
        key='runtime_id' if fault=='id' else fault
        return (replace(observations[0],**{key:fields[key]}),*observations[1:])
    runtime.hooks['inspect']=bad;spec=admit(setup)
    with pytest.raises(DispatchError,match='create_outcome_unknown'):d.launch(spec.lease.request_id,b'payload')
    assert not any(kind=='start' for kind,_ in runtime.events)
    assert d.read(spec.lease.request_id)['uncertain']==1
    with pytest.raises(LeaseError,match='dispatch_outcome_unknown'):leases.cleanup_owner(owner)


@pytest.mark.parametrize('operation',['create','start'])
def test_unknown_operation_never_becomes_absence_cleanup(setup,operation):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    def timeout(*args):raise TimeoutError('untrusted detail')
    runtime.hooks[operation]=timeout;spec=admit(setup)
    with pytest.raises(DispatchError,match=operation+'_outcome_unknown'):d.launch(spec.lease.request_id,b'payload')
    runtime.objects.clear()  # An empty subsequent observation is insufficient.
    with pytest.raises(DispatchError,match='operation_still_unknown'):d.reconcile(spec.lease.request_id)
    with pytest.raises(DispatchError,match='operation_still_unknown'):d.cleanup(spec.lease.request_id)
    with pytest.raises(LeaseError,match='dispatch_outcome_unknown'):leases.cleanup_owner(owner)
    assert leases.current('account')['reservation']==owner


def test_definitive_resolution_binds_exact_ids_and_never_retries_start(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    def timeout(*args):raise TimeoutError
    runtime.hooks['create']=timeout;spec=admit(setup)
    with pytest.raises(DispatchError):d.launch(spec.lease.request_id,b'payload')
    resources=Resources(*tuple(runtime.objects))
    runtime.resolution=lambda s,row:OperationResolution(s.lease.request_id,s.launch_nonce,row['version'],'create','completed',resources,'a'*64)
    d.reconcile(spec.lease.request_id)
    row=d.read(spec.lease.request_id)
    assert row['provider_id']==resources.provider_id and row['operation_receipt']
    assert row['state']=='quarantined' and row['uncertain']==0
    assert not any(kind=='start' for kind,_ in runtime.events)
    d.cleanup(spec.lease.request_id)
    assert not runtime.objects
    with pytest.raises(DispatchError,match='response_unavailable'):d.deliver(spec.lease.request_id)


def test_cancel_between_create_and_start_never_starts(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    runtime.hooks['create']=lambda *args:store.cancel(principal,attempt['attempt_id'],'cancel')
    spec=admit(setup)
    with pytest.raises(DispatchError,match='dispatch_cancelled'):d.launch(spec.lease.request_id,b'payload')
    assert not any(kind=='start' for kind,_ in runtime.events)
    d.cleanup(spec.lease.request_id)
    assert not runtime.objects and leases.current('account') is not None


def test_cancel_during_start_waits_same_lock_then_cleans(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    spec=admit(setup);entered=threading.Event();resume=threading.Event()
    def blocked_start(*args):entered.set();assert resume.wait(3)
    runtime.hooks['start']=blocked_start
    def launch():
        try:d.launch(spec.lease.request_id,b'payload')
        except DispatchError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        running=pool.submit(launch);assert entered.wait(3)
        store.cancel(principal,attempt['attempt_id'],'cancel')
        cleanup=pool.submit(d.cleanup,spec.lease.request_id)
        time.sleep(.05)
        assert not cleanup.done() and not any(kind=='cleanup' for kind,_ in runtime.events)
        resume.set()
        assert running.result()=='dispatch_cancelled';cleanup.result()
    assert not runtime.objects
    with pytest.raises(DispatchError,match='response_revoked'):d.deliver(spec.lease.request_id)


def test_cleanup_receipt_binds_ids_version_and_rejects_stale_cancel(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    spec=admit(setup);resources=d.launch(spec.lease.request_id,b'payload');d.collect(spec.lease.request_id)
    original=leases.verifier;targets=[]
    def inspect_then_cancel(target):
        targets.append(target);receipt=original(target)
        store.cancel(principal,attempt['attempt_id'],'cancel')
        return receipt
    leases.verifier=inspect_then_cancel
    with pytest.raises(DispatchError,match='cleanup_outcome_unknown'):d.cleanup(spec.lease.request_id)
    bound=dict(targets[0].dispatches[0])
    assert bound['provider_id']==resources.provider_id and bound['gateway_id']==resources.gateway_id
    assert bound['launch_nonce']==spec.launch_nonce and type(bound['version']) is int
    assert d.read(spec.lease.request_id)['uncertain']==1


def test_expired_and_revoked_grants_cannot_deliver_cached_response(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    spec=admit(setup);d.launch(spec.lease.request_id,b'payload');d.collect(spec.lease.request_id);d.cleanup(spec.lease.request_id)
    leases.revoke_grant(grant)
    with pytest.raises(DispatchError,match='grant_revoked'):d.deliver(spec.lease.request_id)
    with pytest.raises(DispatchError,match='grant_revoked'):admit(setup,nonce=spec.request_nonce)


def test_cleanup_reserve_and_bounded_response(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    with pytest.raises(DispatchError,match='cleanup_reserve_unavailable'):
        d.admit(owner,grant,request_nonce=secrets.token_hex(32),payload=b'payload',profile_digest='a'*64,remaining_seconds=159)
    spec=admit(setup);d.launch(spec.lease.request_id,b'payload')
    runtime.hooks['collect']=lambda *args:b'x'*(d.response_bytes+1)
    with pytest.raises(DispatchError,match='collection_failed'):d.collect(spec.lease.request_id)
    assert d.read(spec.lease.request_id)['response_digest'] is None
    d.cleanup(spec.lease.request_id)


def test_refresh_free_legacy_lease_has_no_dispatch_authority(setup):
    *_,d,_=setup
    with pytest.raises(DispatchError,match='dispatch_not_found'):d.launch(str(uuid.uuid4()),b'payload')


def test_pre_d9_schema_is_explicitly_refused_without_rewrite(tmp_path):
    path=tmp_path/'old.db';store=Store(path)
    with store._connect() as db:
        db.execute('DROP TABLE provider_dispatch')
        db.execute('DROP TABLE provider_request_leases')
        db.execute('DROP TABLE provider_execution_grants')
        db.execute('DROP TABLE provider_reservations')
        db.execute('DROP TABLE provider_schema_version')
        db.execute('CREATE TABLE provider_reservations(id TEXT PRIMARY KEY,runtime_id TEXT,state TEXT)')
        db.execute("INSERT INTO provider_reservations VALUES('old','actual-old-id','held')")
    with pytest.raises(LeaseError,match='provider_schema_incompatible'):Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM provider_reservations').fetchall()==[('old','actual-old-id','held')]
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_schema_version'").fetchone()


@pytest.mark.parametrize('phase',['create','cleanup'])
def test_actual_process_death_releases_flock_not_durable_owner(tmp_path,phase):
    store=Store(tmp_path/'state.db');principal=store.add_client('test','t'*40,['submit'],['project'])
    attempt=store.create_session(principal,{'project_id':'project','agent':'hermes','goal':'synthetic'},'task');store.claim_next()
    marker=tmp_path/'inside-create'
    script='''import sys,time,uuid,secrets
from pathlib import Path
from cloudworkbench.store import Store
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.provider_dispatch import ProviderDispatch
store=Store(Path(sys.argv[1]))
def verifier(target):
    Path(sys.argv[3]).write_text('durable cleanup intent; lock held')
    time.sleep(60)
leases=ProviderLeases(store,cleanup_verifier=verifier,inspector_id='test')
leases.register_account('account',legacy_agent='claude',persistent_owner_id='owner')
owner=leases.reserve('account',persistent_owner_id='owner')
grant=leases.issue_grant(owner,attempt_id=sys.argv[2],generation=1)
class Runtime:
    def create(self,*args,**kwargs):
        Path(sys.argv[3]).write_text('durable create intent; lock held')
        time.sleep(60)
    def inspect(self,*args,**kwargs):return None
    def start(self,*args,**kwargs):raise AssertionError
    def collect(self,*args,**kwargs):raise AssertionError
    def resolve(self,*args,**kwargs):return None
d=ProviderDispatch(leases,Runtime())
spec=d.admit(owner,grant,request_nonce=secrets.token_hex(32),payload=b'x',profile_digest='a'*64)
if sys.argv[4]=='create':d.launch(spec.lease.request_id,b'x')
else:d.cleanup(spec.lease.request_id)
'''
    proc=subprocess.Popen([sys.executable,'-c',script,str(store.path),attempt['attempt_id'],str(marker),phase],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+5
        while not marker.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.01)
        assert marker.exists()
        leases=ProviderLeases(store,cleanup_verifier=lambda target:False,inspector_id='test')
        with pytest.raises(LeaseError,match='account_lock_busy'):
            with leases.account_lock('account',timeout=.1):pass
        os.kill(proc.pid,signal.SIGKILL);proc.wait(timeout=2)
        with leases.account_lock('account',timeout=.5):pass
        owner=leases.current('account')['reservation']
        assert leases.current('account')['requires_reconciliation']
        with pytest.raises(LeaseError,match='account_reserved'):leases.reserve('account',persistent_owner_id='owner')
        with store._connect() as db:
            row=dict(db.execute('SELECT * FROM provider_dispatch').fetchone())
        assert row['state']==('create_intent' if phase=='create' else 'cleaning') and row['provider_id'] is None
        assert row['operation']==phase
        if phase=='cleanup':assert row['uncertain']==1
        with pytest.raises(LeaseError,match='dispatch_outcome_unknown'):leases.cleanup_owner(owner)
        d=ProviderDispatch(leases,FakeRuntime())
        with pytest.raises(DispatchError,match='operation_still_unknown'):d.reconcile(row['request_id'])
        assert leases.current('account') is not None
        d.runtime.resolution=lambda spec,frozen:OperationResolution(spec.lease.request_id,spec.launch_nonce,frozen['version'],phase,'definitive_no_effect',None,'a'*64)
        d.reconcile(row['request_id'])
        leases.verifier=lambda target:CleanupReceipt(target,'test','terminated',time.time(),'c'*64)
        leases.cleanup_owner(owner)
        assert leases.current('account') is None
        assert leases.reserve('account',persistent_owner_id='owner').epoch==owner.epoch+1
    finally:
        if proc.poll() is None:proc.kill();proc.wait(timeout=2)


def test_private_lock_directory_and_symlink_file_refused(setup,tmp_path):
    store,principal,attempt,leases,owner,*_=setup
    unsafe=tmp_path/'unsafe';unsafe.mkdir(mode=0o750)
    with pytest.raises(LeaseError,match='unsafe_account_lock_directory'):
        ProviderLeases(store,cleanup_verifier=lambda _:False,inspector_id='test',lock_dir=unsafe)
    path=leases.lock_dir/(hashlib.sha256('other'.encode()).hexdigest()+'.lock')
    path.symlink_to(tmp_path/'target')
    with pytest.raises(OSError):
        with leases.account_lock('other'):pass


def test_expiry_observation_survives_clock_rollback(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    spec=admit(setup)
    clock[0]=1121
    with pytest.raises(DispatchError,match='request_expired'):d.launch(spec.lease.request_id,b'payload')
    clock[0]=1000
    with pytest.raises(DispatchError,match='request_requires_reconciliation'):d.launch(spec.lease.request_id,b'payload')
    d.cleanup(spec.lease.request_id)
    clock[0]=5000
    with pytest.raises(DispatchError,match='grant_expired'):admit(setup)
    clock[0]=1000
    with pytest.raises(DispatchError,match='grant_revoked'):admit(setup)
    assert not runtime.events or all(event[0]=='cleanup' for event in runtime.events)


def test_profile_is_frozen_across_request_nonces(setup):
    *_,d,_=setup
    spec=admit(setup);d.cleanup(spec.lease.request_id)
    with pytest.raises(DispatchError,match='attempt_profile_conflict'):admit(setup,profile='e'*64)


@pytest.mark.parametrize('index',[2,3])
def test_network_identity_is_independently_bound_before_start(setup,index):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    def wrong(spec,observed):
        rows=list(observed);rows[index]=replace(rows[index],labels=spec.network_labels('external' if index==2 else 'internal'))
        return tuple(rows)
    runtime.hooks['inspect']=wrong;spec=admit(setup)
    with pytest.raises(DispatchError,match='create_outcome_unknown'):d.launch(spec.lease.request_id,b'payload')
    assert not any(event[0]=='start' for event in runtime.events)


def test_owner_quarantine_cleanup_refusal_does_not_poison_dispatch(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    spec=admit(setup);before=d.read(spec.lease.request_id)
    leases.quarantine(owner)
    with pytest.raises(LeaseError,match='owner_requires_reconciliation'):d.cleanup(spec.lease.request_id)
    assert d.read(spec.lease.request_id)==before
    assert not runtime.events
    leases.cleanup_owner(owner)
    assert leases.current('account') is None


def test_no_resource_cleanup_failure_definitively_reconciles(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    spec=admit(setup);original=leases.verifier;leases.verifier=lambda target:False
    with pytest.raises(DispatchError,match='cleanup_outcome_unknown'):d.cleanup(spec.lease.request_id)
    runtime.resolution=lambda s,row:OperationResolution(s.lease.request_id,s.launch_nonce,row['version'],'cleanup','definitive_no_effect',None,'a'*64)
    d.reconcile(spec.lease.request_id);leases.verifier=original
    d.cleanup(spec.lease.request_id)
    assert leases.active_request(owner) is None


@pytest.mark.parametrize('field,value',[
    ('request_id','other'),('launch_nonce','0'*32),('dispatch_version',-1),
    ('operation','other'),('outcome','absent'),('evidence_sha256','not-a-hash')])
def test_resolution_wrong_binding_never_releases(setup,field,value):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    runtime.hooks['create']=lambda *args:(_ for _ in ()).throw(TimeoutError())
    spec=admit(setup)
    with pytest.raises(DispatchError):d.launch(spec.lease.request_id,b'payload')
    before=d.read(spec.lease.request_id)['failure_code']
    def resolve(s,row):
        valid=OperationResolution(s.lease.request_id,s.launch_nonce,row['version'],'create','completed',Resources(*runtime.objects),'a'*64)
        return replace(valid,**{field:value})
    runtime.resolution=resolve
    with pytest.raises(DispatchError,match='operation_still_unknown'):d.reconcile(spec.lease.request_id)
    assert d.read(spec.lease.request_id)['failure_code']==before
    assert leases.active_request(owner) is not None


@pytest.mark.parametrize('outcome',['completed','definitive_no_effect','settled_cleaned'])
def test_start_resolution_retains_all_bound_resource_ids(setup,outcome):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    runtime.hooks['start']=lambda *args:(_ for _ in ()).throw(TimeoutError())
    spec=admit(setup)
    with pytest.raises(DispatchError):d.launch(spec.lease.request_id,b'payload')
    resources=Resources(*runtime.objects)
    runtime.resolution=lambda s,row:OperationResolution(s.lease.request_id,s.launch_nonce,row['version'],'start',outcome,resources,'a'*64)
    d.reconcile(spec.lease.request_id);d.cleanup(spec.lease.request_id)
    assert not runtime.objects and leases.active_request(owner) is None


def test_launch_deadline_clamps_elapsed_root_budget(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    spec=admit(setup);assert d.read(spec.lease.request_id)['deadline_at']==1160
    clock[0]=1110;deadlines=[];original=runtime.create
    def create(*args,deadline):
        deadlines.append(deadline-time.monotonic())
        return original(*args,deadline=deadline)
    runtime.create=create;d.launch(spec.lease.request_id,b'payload')
    assert 0<deadlines[0]<=10


def test_cleanup_completed_but_receipt_failed_can_reconcile_then_deliver(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    spec=admit(setup);resources=d.launch(spec.lease.request_id,b'payload');d.collect(spec.lease.request_id)
    def failed_receipt(target):runtime.cleanup(target);return False
    leases.verifier=failed_receipt
    with pytest.raises(DispatchError,match='cleanup_outcome_unknown'):d.cleanup(spec.lease.request_id)
    assert not runtime.objects
    runtime.resolution=lambda s,row:OperationResolution(s.lease.request_id,s.launch_nonce,row['version'],'cleanup','settled_cleaned',resources,'a'*64)
    d.reconcile(spec.lease.request_id)
    with pytest.raises(DispatchError,match='response_not_cleaned'):d.deliver(spec.lease.request_id)
    def confirm(target):
        assert not runtime.objects and dict(target.dispatches[0])['operation_receipt']
        return CleanupReceipt(target,'synthetic-inspector','terminated',1000.0,'b'*64)
    leases.verifier=confirm;d.cleanup(spec.lease.request_id)
    assert d.deliver(spec.lease.request_id)==b'{"answer":"synthetic"}'


def test_start_return_without_starting_is_unknown_not_running(setup):
    store,principal,attempt,leases,owner,grant,runtime,d,clock=setup
    runtime.start=lambda *args,**kwargs:None
    spec=admit(setup)
    with pytest.raises(DispatchError,match='start_outcome_unknown'):d.launch(spec.lease.request_id,b'payload')
    assert d.read(spec.lease.request_id)['state']=='quarantined'


@pytest.mark.parametrize('nonce',['a'*32,'a'*63,'a'*65,'G'*64])
def test_request_nonce_matches_canonical_relay_64hex(setup,nonce):
    with pytest.raises(DispatchError,match='invalid_request_nonce'):admit(setup,nonce=nonce)
