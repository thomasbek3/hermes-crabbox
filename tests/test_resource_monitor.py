from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

import pytest

from cloudworkbench import resource_monitor as rm
from cloudworkbench.adapters import EventSpoolError, public_spool_event
from cloudworkbench.runtime import Runtime
from cloudworkbench.store import Store

RID = 'b'*64


class ManualExecutor:
    def __init__(self):
        self.jobs=[]

    def submit(self, function, *args, **kwargs):
        future=Future();self.jobs.append((future,function,args,kwargs));return future

    def finish(self, value):
        self.jobs[-1][0].set_result(value)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock=[0.0]
    monkeypatch.setattr(rm,'_ADMISSION',rm._Admission(clock=lambda:clock[0]))
    store=Store(tmp_path/'db.sqlite')
    owner=store.add_client('owner','a'*40,['submit','observe','retrieve','cancel'],['project'])
    task=store.create_session(owner,{'project_id':'project','goal':'fixture','agent':'fixture'},'create')
    store.claim_next()
    store.transition(task['attempt_id'],'running',runtime_id=RID,expected_generation=task['generation'])
    binding=rm.Binding(task['session_id'],task['attempt_id'],task['generation'],RID,'execution')
    runtime=Runtime({'root':tmp_path,'image':'sha256:'+'a'*64,'owner':'resource-tests'})
    executor=ManualExecutor()
    threads=[];before_publish=[None]
    def publish(binding,kind,payload,*,expected_generation,dedupe_key):
        threads.append(threading.get_ident())
        if before_publish[0]:before_publish[0]()
        # Adapter contract: parent adds this guard transaction to Store at integration.
        with store._tx() as db:
            current=store._attempt(db,binding.attempt_id)
            if not binding.matches(current):return None
            assert kind==rm.EVENT_TYPE and expected_generation==binding.generation
            assert dedupe_key.startswith('resource:')
            return store._event(db,binding.session_id,binding.attempt_id,kind,payload)
    monitor=rm.ResourceMonitor(store.get_attempt,publish,executor=executor,clock=lambda:clock[0])
    yield store,owner,binding,runtime,executor,monitor,clock,threads,before_publish,publish
    monitor.close()
    for future,_,_,_ in executor.jobs:
        if not future.done():future.set_result(None)


def sample(binding,runtime,**changes):
    return {'sampled_at':datetime.now(timezone.utc).isoformat(),'status':'observed','reason':None,
            'provenance':'docker_cli_observed','runtime_id':binding.runtime_id,'generation':binding.generation,
            'runtime_owner':runtime.owner,'source_policy_sha256':rm._policy(runtime),'elapsed_ms':12,
            'cpu_percent':0.0,'memory_bytes_approx':4096,'memory_limit_bytes_approx':134217728,'pids':1,**changes}


def payload(binding,value):
    return {'schema_version':1,'source':rm.SOURCE,'binding':asdict(binding),'sample_id':uuid.uuid4().hex,'sample':value}


def resource_events(store,binding):
    with store._connect() as db:
        return db.execute('SELECT payload FROM events WHERE attempt_id=? AND type=?',(binding.attempt_id,rm.EVENT_TYPE)).fetchall()


def test_startup_global_cadence_and_single_outstanding_across_monitors(setup):
    store,owner,binding,runtime,executor,monitor,clock,threads,hook,publish=setup
    other=rm.ResourceMonitor(store.get_attempt,publish,executor=executor,clock=lambda:clock[0])
    try:
        assert monitor.offer(binding,runtime)=='cadence'
        clock[0]=10
        assert monitor.offer(binding,runtime)=='submitted'
        assert other.offer(binding,runtime)=='busy'
        assert monitor.poll()=={'status':'pending'}
        executor.finish(sample(binding,runtime))
        assert other.offer(binding,runtime)=='busy'  # unpolled result still occupies slot
        assert monitor.poll()['status']=='recorded'
        assert other.offer(binding,runtime)=='cadence'
        clock[0]=20
        assert other.offer(binding,runtime)=='submitted'
    finally:
        other.close()


def test_only_safe_fixed_fields_persisted_on_controller_thread(setup):
    store,owner,binding,runtime,executor,monitor,clock,threads,hook,publish=setup
    clock[0]=10
    assert monitor.offer(binding,runtime)=='submitted'
    executor.finish(sample(binding,runtime,private='PRIVATE_SENTINEL'))
    assert monitor.poll()['status']=='recorded'
    rows=resource_events(store,binding)
    assert len(rows)==1 and 'PRIVATE_SENTINEL' not in rows[0]['payload']
    assert threads==[threading.get_ident()]
    event=json.loads(rows[0]['payload'])
    assert event['binding']==asdict(binding) and event['source']==rm.SOURCE
    with pytest.raises(EventSpoolError):
        public_spool_event({'type':rm.EVENT_TYPE,'payload':event})
    assert monitor.poll()['status']=='idle'


@pytest.mark.parametrize('change',['terminal','generation','runtime','cancel','role'])
def test_completion_is_dropped_when_controller_binding_changed(setup,change):
    store,owner,binding,runtime,executor,monitor,clock,*_=setup
    clock[0]=10;assert monitor.offer(binding,runtime)=='submitted'
    if change=='terminal':store.transition(binding.attempt_id,'failed',expected_generation=binding.generation,result={'immutable':True})
    elif change=='generation':store.fence_attempt(binding.attempt_id,binding.generation)
    elif change=='runtime':store.transition(binding.attempt_id,'running',runtime_id='c'*64,expected_generation=binding.generation)
    elif change=='cancel':store.cancel(owner,binding.attempt_id,'cancel')
    else:store.transition(binding.attempt_id,'verifying',expected_generation=binding.generation)
    before=store.get_attempt(binding.attempt_id)
    executor.finish(sample(binding,runtime))
    assert monitor.poll()=={'status':'dropped','reason':'stale_binding'}
    assert not resource_events(store,binding)
    assert store.get_attempt(binding.attempt_id)==before


def test_terminal_race_inside_atomic_publisher_does_not_append(setup):
    store,owner,binding,runtime,executor,monitor,clock,threads,hook,publish=setup
    clock[0]=10;monitor.offer(binding,runtime)
    hook[0]=lambda:store.transition(binding.attempt_id,'failed',expected_generation=binding.generation,result={'immutable':True})
    executor.finish(sample(binding,runtime))
    assert monitor.poll()=={'status':'dropped','reason':'stale_at_publication'}
    assert not resource_events(store,binding)
    assert store.get_attempt(binding.attempt_id)['result']=={'immutable':True}


@pytest.mark.parametrize('change',[{'runtime_id':'c'*64},{'generation':99},{'runtime_owner':'different'},
 {'source_policy_sha256':'d'*64},{'provenance':'worker_reported'},{'cpu_percent':float('nan')},
 {'reason':'PRIVATE_SENTINEL'},{'elapsed_ms':9000}])
def test_bad_or_provider_observations_cannot_be_promoted(setup,change):
    store,owner,binding,runtime,executor,monitor,clock,*_=setup
    clock[0]=10;monitor.offer(binding,runtime);executor.finish(sample(binding,runtime,**change))
    assert monitor.poll()=={'status':'dropped','reason':'invalid_observation'}
    assert not resource_events(store,binding)


def test_worker_exception_not_serialized(setup):
    store,owner,binding,runtime,executor,monitor,clock,*_=setup
    clock[0]=10;monitor.offer(binding,runtime)
    executor.jobs[-1][0].set_exception(RuntimeError('PRIVATE_SENTINEL'))
    assert monitor.poll()=={'status':'dropped','reason':'invalid_observation'}
    assert not resource_events(store,binding)


def test_unknown_sample_remains_unknown_not_zero(setup):
    store,owner,binding,runtime,executor,monitor,clock,*_=setup
    clock[0]=10;monitor.offer(binding,runtime)
    executor.finish(sample(binding,runtime,status='unknown',reason='deadline_exceeded'))
    assert monitor.poll()['status']=='recorded'
    value=rm.history_summary(store.path,binding)
    assert value['status']=='unknown' and value['observed_max']['cpu_percent'] is None
    assert value['samples'][0]['reason']=='deadline_exceeded'


def test_pending_close_never_waits_or_releases_global_slot_early(setup):
    store,owner,binding,runtime,executor,monitor,clock,threads,hook,publish=setup
    clock[0]=10;monitor.offer(binding,runtime)
    assert executor.jobs[-1][0].set_running_or_notify_cancel()
    started=time.monotonic();monitor.close();assert time.monotonic()-started<.1
    clock[0]=100
    other=rm.ResourceMonitor(store.get_attempt,publish,executor=executor,clock=lambda:clock[0])
    try:
        assert other.offer(binding,runtime)=='busy'
        executor.finish(sample(binding,runtime))
        assert monitor.poll()['status']=='closed' and not resource_events(store,binding)
        assert other.offer(binding,runtime)=='submitted'
    finally:other.close()


def test_real_worker_wait_does_not_block_offer_poll_or_close(setup):
    store,owner,binding,runtime,manual,unused,clock,threads,hook,publish=setup
    entered=threading.Event();release=threading.Event()
    def blocking(*args,**kwargs):
        entered.set();assert release.wait(2);return sample(binding,runtime)
    pool=ThreadPoolExecutor(max_workers=1)
    monitor=rm.ResourceMonitor(store.get_attempt,publish,sampler=blocking,executor=pool,clock=lambda:clock[0])
    try:
        clock[0]=10;assert monitor.offer(binding,runtime)=='submitted';assert entered.wait(1)
        started=time.monotonic();assert monitor.poll()['status']=='pending';monitor.close()
        assert time.monotonic()-started<.1 and not resource_events(store,binding)
    finally:release.set();pool.shutdown(wait=True)


def test_wrong_thread_cannot_publish(setup):
    monitor=setup[5]
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError,match='controller thread'):pool.submit(monitor.poll).result()


def test_history_exact_binding_bounded_rows_and_restart_counts(setup):
    store,owner,binding,runtime,*_=setup
    with store._tx() as db:
        for index in range(125):
            value=sample(binding,runtime,cpu_percent=index)
            store._event(db,binding.session_id,binding.attempt_id,rm.EVENT_TYPE,payload(binding,value))
        for other in (replace(binding,role='verifier'),replace(binding,generation=2),replace(binding,runtime_id='c'*64)):
            store._event(db,binding.session_id,binding.attempt_id,rm.EVENT_TYPE,payload(other,sample(other,runtime)))
        store._event(db,binding.session_id,binding.attempt_id,'adapter.result',payload(binding,sample(binding,runtime,cpu_percent=900)))
    value=rm.history_summary(store.path,binding)
    assert value['history_status']=='available' and value['history_total_events']==125
    assert value['retained_samples']==120 and value['dropped_samples']==5 and value['total_samples']==125
    assert value['samples'][0]['cpu_percent']==5 and value['observed_max']['cpu_percent']==124
    # Reopening has no volatile window dependency.
    assert rm.history_summary(Store(store.path).path,binding)['samples']==value['samples']


def test_no_samples_and_unavailable_history_not_faked(setup,tmp_path):
    store,owner,binding,*_=setup
    value=rm.history_summary(store.path,binding)
    assert value['status']=='unknown' and value['history_total_events']==0 and value['history_status']=='available'
    missing=tmp_path/'missing.sqlite'
    value=rm.history_summary(missing,binding)
    assert value['history_total_events'] is None and value['history_status']=='unknown'
    assert not missing.exists()


def test_malformed_or_provider_history_cannot_reconstruct_observation(setup):
    store,owner,binding,runtime,*_=setup
    event={'type':rm.EVENT_TYPE,'session_id':binding.session_id,'attempt_id':binding.attempt_id,'sequence':1,
           'payload':payload(binding,sample(binding,runtime))}
    event['type']='adapter.result'
    assert rm.summarize_history(binding,[event],1)['history_reason']=='invalid_controller_history'
    event['type']=rm.EVENT_TYPE;event['payload']['sample']['runtime_owner']='PRIVATE_SENTINEL'*100
    value=rm.summarize_history(binding,[event],1)
    assert value['status']=='unknown' and 'PRIVATE_SENTINEL' not in json.dumps(value)
    assert rm.summarize_history(binding,[],1)['history_reason']=='invalid_history_count'


def test_history_query_budget_is_unknown(setup,monkeypatch):
    store,owner,binding,*_=setup
    clock=iter([0,1,1,1,1,1,1,1,1,1])
    monkeypatch.setattr(rm.time,'monotonic',lambda:next(clock,1))
    assert rm.history_summary(store.path,binding)['history_status']=='unknown'


@pytest.mark.parametrize('cadence',[0,9.99,True,float('inf'),float('nan'),3601])
def test_cadence_policy(cadence):
    with pytest.raises(ValueError):rm.ResourceMonitor(lambda _:None,lambda *_:None,cadence_seconds=cadence)


def test_history_locked_database_is_bounded_unknown(setup):
    store,owner,binding,*_=setup
    lock=sqlite3.connect(store.path)
    try:
        lock.execute('PRAGMA journal_mode=DELETE')
        lock.execute('BEGIN EXCLUSIVE')
        started=time.monotonic();value=rm.history_summary(store.path,binding)
        assert value['history_status']=='unknown' and time.monotonic()-started<.3
    finally:lock.rollback();lock.close()


def test_history_rejects_boolean_schema_and_oversized_record(setup):
    store,owner,binding,runtime,*_=setup
    event={'type':rm.EVENT_TYPE,'session_id':binding.session_id,'attempt_id':binding.attempt_id,'sequence':1,
           'payload':payload(binding,sample(binding,runtime))}
    event['payload']['schema_version']=True
    assert rm.summarize_history(binding,[event],1)['history_status']=='unknown'
    event['payload']['schema_version']=1;event['payload']['binding']['generation']=True
    assert rm.summarize_history(binding,[event],1)['history_status']=='unknown'
    event['payload']['binding']['generation']=binding.generation
    event['payload']['unexpected']='PRIVATE_SENTINEL'*1000
    value=rm.summarize_history(binding,[event],1)
    assert value['history_status']=='unknown' and 'PRIVATE_SENTINEL' not in json.dumps(value)
