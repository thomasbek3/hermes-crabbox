from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
import os
import sqlite3
import time
import zipfile

import pytest

from cloudworkbench import resource_monitor as rm
from cloudworkbench.result_bundle import authorized_snapshot, build_bundle
from cloudworkbench.store import StoreError
from test_resource_monitor import ManualExecutor, sample, payload
from test_runner import setup, start, finish_agent, finish_verifier


def install_monitor(store,runtime,runner,monkeypatch):
    runtime.owner='resource-integration';runtime.uid=os.getuid();runtime.gid=os.getgid();runtime.docker='/usr/bin/unused-docker'
    clock=[0.0];executor=ManualExecutor()
    monkeypatch.setattr(rm,'_ADMISSION',rm._Admission(clock=lambda:clock[0]))
    runner.resources.close()
    runner.resources=rm.ResourceMonitor(store.resource_attempt,store.append_resource_sample,executor=executor,clock=lambda:clock[0])
    return clock,executor


def bind(store,runtime,attempt,character='b'):
    rid=character*64
    runtime.runtimes[rid]=runtime.runtimes.pop(attempt['runtime_id'])
    attempt=store.transition(attempt['id'],attempt['state'],runtime_id=rid,expected_generation=attempt['generation'])
    return attempt,rm.Binding(attempt['session_id'],attempt['id'],attempt['generation'],rid,'verifier' if attempt['state']=='verifying' else 'execution')


def publish(store,binding,runtime,**changes):
    body=payload(binding,sample(binding,runtime,**changes))
    return store.append_resource_sample(binding,rm.EVENT_TYPE,body,expected_generation=binding.generation,dedupe_key='resource:'+body['sample_id'])


def test_atomic_publication_dedupe_and_reserved_namespace(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);clock,executor=install_monitor(store,runtime,runner,monkeypatch)
    attempt,binding=bind(store,runtime,attempt)
    body=payload(binding,sample(binding,runtime))
    args=(binding,rm.EVENT_TYPE,body);kw={'expected_generation':binding.generation,'dedupe_key':'resource:'+body['sample_id']}
    first=store.append_resource_sample(*args,**kw)
    assert store.append_resource_sample(*args,**kw)==first
    body['sample']['cpu_percent']=42
    with pytest.raises(StoreError,match='conflict'):store.append_resource_sample(*args,**kw)
    with pytest.raises(StoreError,match='guarded'):store.append_event(binding.attempt_id,rm.EVENT_TYPE,body,expected_generation=binding.generation)
    store.cancel(owner,attempt['id'],'cancel')
    assert publish(store,binding,runtime) is None


@pytest.mark.parametrize('change',['terminal','generation','runtime','role','session'])
def test_guarded_store_refuses_every_stale_binding(setup,monkeypatch,change):
    store,owner,runtime,runner=setup
    attempt=start(setup);install_monitor(store,runtime,runner,monkeypatch);attempt,binding=bind(store,runtime,attempt)
    if change=='terminal':store.transition(attempt['id'],'failed',expected_generation=attempt['generation'])
    elif change=='generation':store.fence_attempt(attempt['id'],attempt['generation'])
    elif change=='runtime':store.transition(attempt['id'],'running',runtime_id='c'*64,expected_generation=attempt['generation'])
    elif change=='role':store.transition(attempt['id'],'verifying',expected_generation=attempt['generation'])
    else:binding=replace(binding,session_id='foreign')
    assert publish(store,binding,runtime) is None


def test_transaction_race_completion_wins_before_publication(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);install_monitor(store,runtime,runner,monkeypatch);attempt,binding=bind(store,runtime,attempt)
    with store._tx() as db:
        db.execute("UPDATE attempts SET state='failed',result=? WHERE id=?",(json.dumps({'immutable':True}),attempt['id']))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(publish,store,binding,runtime)
            # Busy publication fails quickly; it cannot bypass the writer lock.
            with pytest.raises(sqlite3.OperationalError):future.result(timeout=.5)
    assert publish(store,binding,runtime) is None
    assert store.get_attempt(attempt['id'])['result']=={'immutable':True}


def test_late_cancel_sample_dropped_without_lifecycle_wait(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);clock,executor=install_monitor(store,runtime,runner,monkeypatch);attempt,binding=bind(store,runtime,attempt)
    clock[0]=10;runner.offer_resource_sample();assert len(executor.jobs)==1
    executor.jobs[0][0].set_running_or_notify_cancel()
    store.cancel(owner,attempt['id'],'cancel')
    before=time.monotonic();runner.tick();assert time.monotonic()-before<.5
    assert store.get_attempt(attempt['id'])['state']=='cancelled'
    executor.finish(sample(binding,runtime));runner.tick()
    assert not [e for e in store.events(owner,attempt['session_id']) if e['type']==rm.EVENT_TYPE]


def test_execution_and_verifier_metrics_from_durable_controller_events(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);clock,executor=install_monitor(store,runtime,runner,monkeypatch);attempt,execution=bind(store,runtime,attempt)
    clock[0]=10;runner.offer_resource_sample();executor.finish(sample(execution,runtime,cpu_percent=12));runner.resources.poll()
    verifying=finish_agent(setup,attempt)
    verifying,verification=bind(store,runtime,verifying,'c')
    clock[0]=20;runner.offer_resource_sample();executor.finish(sample(verification,runtime,cpu_percent=5));runner.resources.poll()
    final=finish_verifier(setup,verifying)
    assert final['state']=='completed' and final['outcome']=='verified'
    snapshot,artifacts=authorized_snapshot(store,owner,attempt['session_id'])
    blob=build_bundle(snapshot,artifacts,runner.root/'artifacts')
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        result=json.loads(archive.read('result.json'));measured=result['resources']['measured_usage']
    assert measured['true_peak'] is False and measured['sidecars_included'] is False
    assert measured['roles']['execution']['observed_max']['cpu_percent']==12
    assert measured['roles']['verifier']['observed_max']['cpu_percent']==5
    assert result['cost']['value'] is None
    assert publish(store,verification,runtime) is None
    # No late events change an already completed bundle, even after reconstruction.
    again,files=authorized_snapshot(store,owner,attempt['session_id'])
    assert build_bundle(again,files,runner.root/'artifacts')==blob
    assert json.loads((runner.root/'results'/(attempt['id']+'.json')).read_text())==final['result']


def test_legacy_provider_metrics_not_promoted(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);install_monitor(store,runtime,runner,monkeypatch);attempt,binding=bind(store,runtime,attempt)
    store.transition(attempt['id'],'failed',expected_generation=attempt['generation'],result={'resources':{'true_peak':True,'cpu_percent':9000}})
    snapshot,artifacts=authorized_snapshot(store,owner,attempt['session_id'])
    assert snapshot['bundle_resources']=={'measured_usage':None,'reason':'no_controller_resource_samples'}


def test_completion_intent_freezes_resource_publication(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);install_monitor(store,runtime,runner,monkeypatch);attempt,binding=bind(store,runtime,attempt)
    store.transition(attempt['id'],'verifying',expected_generation=attempt['generation'],result={'completion_intent':{'marker':True}})
    assert publish(store,replace(binding,role='verifier'),runtime) is None


def test_fair_round_robin_offers_do_not_starve_second_attempt(setup,monkeypatch):
    store,owner,runtime,runner=setup
    clock,executor=install_monitor(store,runtime,runner,monkeypatch)
    bindings=[]
    for index in range(2):
        task=store.create_session(owner,{'project_id':'project','goal':'resource fixture','agent':'fixture'+str(index)},'fair'+str(index))
        claim=store.claim_next(capacity=2)
        rid=('b' if index==0 else 'c')*64
        store.transition(claim['id'],'running',runtime_id=rid,expected_generation=claim['generation'])
        bindings.append(rm.Binding(task['session_id'],task['attempt_id'],claim['generation'],rid,'execution'))
    bindings.sort(key=lambda b:b.attempt_id)
    clock[0]=10;runner.offer_resource_sample();executor.finish(sample(bindings[0],runtime));runner.resources.poll()
    clock[0]=20;runner.offer_resource_sample()
    assert executor.jobs[-1][2][1]==bindings[1].runtime_id
    executor.finish(sample(bindings[1],runtime));runner.resources.poll()


def test_bundle_restart_history_is_bounded_and_counts_older_bindings(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);install_monitor(store,runtime,runner,monkeypatch);attempt,old=bind(store,runtime,attempt)
    publish(store,old,runtime,cpu_percent=999)
    attempt,current=bind(store,runtime,attempt,'c')
    for index in range(125):
        publish(store,current,runtime,cpu_percent=index)
    store.transition(attempt['id'],'failed',expected_generation=attempt['generation'])
    # Read a fresh database connection after monitor closure: no in-memory window.
    runner.close()
    snapshot,_=authorized_snapshot(store,owner,attempt['session_id'])
    roles=snapshot['bundle_resources']['measured_usage']['roles']
    history=roles['execution']
    assert history['binding']['runtime_id']==current.runtime_id
    assert history['role_runtime_bindings']==2 and history['omitted_older_runtime_bindings']==1
    assert history['history_total_events']==125 and history['history_retained_events']==120
    assert history['history_omitted_events']==5 and len(history['samples'])==120
    assert history['observed_max']['cpu_percent']==124
    assert roles['verifier']=={'status':'unknown','reason':'no_controller_resource_samples'}


def test_unknown_controller_observations_remain_unknown_in_bundle(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);install_monitor(store,runtime,runner,monkeypatch);attempt,binding=bind(store,runtime,attempt)
    publish(store,binding,runtime,status='unknown',reason='stats_unavailable',cpu_percent=None,
            memory_bytes_approx=None,memory_limit_bytes_approx=None,pids=None)
    store.transition(attempt['id'],'failed',expected_generation=attempt['generation'])
    snapshot,_=authorized_snapshot(store,owner,attempt['session_id'])
    resources=snapshot['bundle_resources']
    assert resources['reason']=='no_valid_controller_resource_samples'
    history=resources['measured_usage']['roles']['execution']
    assert history['status']=='unknown' and history['unknown_samples']==1
    assert all(value is None for value in history['observed_max'].values())


def test_optional_poll_and_close_failures_do_not_block_cancellation(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup)
    store.cancel(owner,attempt['id'],'cancel')
    # Resource monitor construction and lifecycle may be on different threads.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(runner.tick).result(timeout=1)
        pool.submit(runner.close).result(timeout=1)
    assert store.get_attempt(attempt['id'])['state']=='cancelled'
    heartbeat=json.loads((runner.root/'heartbeat.json').read_text())['resource_observer']
    assert heartbeat['drops']['poll_unavailable']==1
    assert runner._resource_health['drops']['close_unavailable']==1


def test_failing_candidate_does_not_starve_healthy_candidate(setup,monkeypatch):
    store,owner,runtime,runner=setup
    clock,executor=install_monitor(store,runtime,runner,monkeypatch)
    candidates=[]
    for index in range(2):
        task=store.create_session(owner,{'project_id':'project','goal':'resource fixture','agent':'fixture'+str(index)},'badfair'+str(index))
        claim=store.claim_next(capacity=2)
        candidates.append(store.transition(claim['id'],'running',runtime_id=('b' if index==0 else 'c')*64,expected_generation=claim['generation']))
    candidates.sort(key=lambda value:value['id'])
    original=runner.sampling_runtime
    def choose(attempt,**kwargs):
        if attempt['id']==candidates[0]['id']:raise KeyError('PRIVATE_SENTINEL')
        return original(attempt,**kwargs)
    monkeypatch.setattr(runner,'sampling_runtime',choose)
    clock[0]=10;runner.offer_resource_sample()
    assert executor.jobs[-1][2][1]==candidates[1]['runtime_id']
    assert runner._resource_health['drops']=={'candidate_unavailable':1}
    assert 'PRIVATE_SENTINEL' not in json.dumps(runner._resource_health)
    runner.close()


@pytest.mark.parametrize('setting',[{'resource_sampling_cadence_seconds':5},{'resource_sampling_enabled':'false'}])
def test_invalid_optional_configuration_disables_only_observer(setup,setting):
    from cloudworkbench.runner import Runner
    store,owner,runtime,runner=setup
    other=Runner(store,{**runner.config,**setting},runtime=runtime)
    assert other.resources is None
    assert other._resource_health['reason']=='invalid_configuration'
    other.tick()


def test_cadence_skips_optional_database_reads(setup,monkeypatch):
    store,owner,runtime,runner=setup
    clock,executor=install_monitor(store,runtime,runner,monkeypatch)
    def forbidden():raise AssertionError('cadence should avoid query')
    monkeypatch.setattr(store,'resource_candidates',forbidden)
    runner.offer_resource_sample()
    assert executor.jobs==[]


def test_publication_failure_visible_as_fixed_heartbeat_code(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);clock,executor=install_monitor(store,runtime,runner,monkeypatch);attempt,binding=bind(store,runtime,attempt)
    clock[0]=10;runner.offer_resource_sample();executor.finish(sample(binding,runtime))
    def fail(*args,**kwargs):raise sqlite3.OperationalError('PRIVATE_SENTINEL')
    runner.resources.publish=fail
    runner.tick()
    health=json.loads((runner.root/'heartbeat.json').read_text())['resource_observer']
    assert health['drops']['publication_unavailable']==1
    assert 'PRIVATE_SENTINEL' not in json.dumps(health)


@pytest.mark.parametrize('valid_execution',[True,False])
def test_malformed_verifier_identity_does_not_erase_execution(setup,monkeypatch,valid_execution):
    store,owner,runtime,runner=setup
    attempt=start(setup);install_monitor(store,runtime,runner,monkeypatch);attempt,binding=bind(store,runtime,attempt)
    if valid_execution:publish(store,binding,runtime,cpu_percent=12)
    body=payload(replace(binding,role='verifier'),sample(binding,runtime));body['binding']['runtime_id']='malformed'
    with store._tx() as db:store._event(db,attempt['session_id'],attempt['id'],rm.EVENT_TYPE,body)
    store.transition(attempt['id'],'failed',expected_generation=attempt['generation'])
    snapshot,_=authorized_snapshot(store,owner,attempt['session_id'])
    roles=snapshot['bundle_resources']['measured_usage']['roles']
    if valid_execution:assert roles['execution']['observed_max']['cpu_percent']==12
    else:assert roles['execution']['reason']=='no_controller_resource_samples'
    assert roles['verifier']=={'status':'unknown','reason':'invalid_controller_history'}


def test_ten_thousand_observations_and_transient_bundle_refusal(setup,monkeypatch):
    from cloudworkbench import result_bundle
    store,owner,runtime,runner=setup
    attempt=start(setup);install_monitor(store,runtime,runner,monkeypatch);attempt,binding=bind(store,runtime,attempt)
    body=json.dumps(payload(binding,sample(binding,runtime)))
    with store._tx() as db:
        sequence=db.execute('SELECT MAX(sequence) FROM events WHERE session_id=?',(attempt['session_id'],)).fetchone()[0]
        db.executemany('INSERT INTO events VALUES(?,?,?,?,?,?)',[(attempt['session_id'],sequence+index+1,attempt['id'],rm.EVENT_TYPE,body,'2026-09-17T00:00:00+00:00') for index in range(10000)])
    store.transition(attempt['id'],'failed',expected_generation=attempt['generation'])
    before=time.monotonic();snapshot,_=authorized_snapshot(store,owner,attempt['session_id'])
    assert time.monotonic()-before<1
    history=snapshot['bundle_resources']['measured_usage']['roles']['execution']
    assert history['history_total_events']==10000 and history['history_retained_events']==120
    original=result_bundle.attempt_resources
    monkeypatch.setattr(result_bundle,'attempt_resources',lambda db,attempt:original(db,attempt,budget_seconds=0))
    with pytest.raises(result_bundle.BundleError) as exc:authorized_snapshot(store,owner,attempt['session_id'])
    assert exc.value.status_code==503
