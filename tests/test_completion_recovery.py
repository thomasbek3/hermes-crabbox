import pytest
from cloudworkbench.runner import Runner
from test_runner import setup,start,finish_agent


@pytest.mark.parametrize('after_commit',[False,True])
def test_completion_commit_crash_recovers_without_replaying_tools(setup,monkeypatch,after_commit):
    store,owner,runtime,runner=setup
    attempt=start(setup);verifying=finish_agent(setup,attempt)
    runtime.complete(verifying['runtime_id'],[{'type':'verification','checks':[{'id':'protected','result':'passed'}]}])
    transition=store.transition
    def crash(identity,state,**fields):
        if state=='completed':
            if after_commit:transition(identity,state,**fields)
            raise SystemExit('simulated process death at final DB commit')
        return transition(identity,state,**fields)
    monkeypatch.setattr(store,'transition',crash)
    with pytest.raises(SystemExit):runner.tick()
    monkeypatch.setattr(store,'transition',transition)
    launched=len(runtime.launches)
    cleanup_calls=[]
    monkeypatch.setattr(runtime,'cleanup_infrastructure',lambda identity,expected_generation:cleanup_calls.append((identity,expected_generation)))
    restarted=Runner(store,runner.config,runtime=runtime);restarted.capacity=lambda:0
    restarted.tick()
    final=store.get_attempt(attempt['id'])
    assert final['state']=='completed' and final['outcome']=='verified'
    assert len(runtime.launches)==launched
    assert not store.active_attempts()
    assert not runtime.runtimes
    assert cleanup_calls == ([] if after_commit else [(attempt['id']+'-verify',1)])
    events=store.events(owner,attempt['session_id'])
    assert len([e for e in events if e['type']=='attempt.state' and e['payload'].get('state')=='completed'])==1


def test_completion_intent_recovers_before_result_sidecar(setup,monkeypatch):
    import cloudworkbench.runner as module
    store,owner,runtime,runner=setup
    attempt=start(setup);verifying=finish_agent(setup,attempt)
    runtime.complete(verifying['runtime_id'],[{'type':'verification','checks':[{'id':'protected','result':'passed'}]}])
    write=module.write_json
    def crash(path,value,*args,**kwargs):
        if path.parent.name=='results':raise SystemExit('death before sidecar')
        return write(path,value,*args,**kwargs)
    monkeypatch.setattr(module,'write_json',crash)
    with pytest.raises(SystemExit):runner.tick()
    assert store.get_attempt(attempt['id'])['result']['completion_intent']['generation']==1
    monkeypatch.setattr(module,'write_json',write)
    launches=len(runtime.launches)
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='completed'
    assert len(runtime.launches)==launches
    assert (runner.root/'results'/(attempt['id']+'.json')).is_file()


def test_transient_commit_error_retains_intent_for_retry(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);verifying=finish_agent(setup,attempt)
    runtime.complete(verifying['runtime_id'],[{'type':'verification','checks':[{'id':'protected','result':'passed'}]}])
    transition=store.transition
    def fail(identity,state,**fields):
        if state=='completed':raise OSError('simulated transient database error')
        return transition(identity,state,**fields)
    monkeypatch.setattr(store,'transition',fail);runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='verifying'
    monkeypatch.setattr(store,'transition',transition);runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='completed'
    assert len(runtime.launches)==2


def test_wrong_generation_completion_intent_never_adopted(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);verifying=finish_agent(setup,attempt)
    runtime.complete(verifying['runtime_id'],[{'type':'verification','checks':[{'id':'protected','result':'passed'}]}])
    transition=store.transition
    def crash(identity,state,**fields):
        if state=='completed':raise SystemExit
        return transition(identity,state,**fields)
    monkeypatch.setattr(store,'transition',crash)
    with pytest.raises(SystemExit):runner.tick()
    monkeypatch.setattr(store,'transition',transition)
    current=store.get_attempt(attempt['id']);result=current['result']
    result['completion_intent']['generation']+=1
    transition(attempt['id'],'verifying',expected_generation=1,result=result)
    runner.tick()
    final=store.get_attempt(attempt['id'])
    assert final['state']=='failed' and final['outcome']!='verified'
    assert len(runtime.launches)==2
    assert not (runner.root/'results'/(attempt['id']+'.json')).exists()


@pytest.mark.parametrize('boundary',['intent','event','logs'])
def test_transient_proof_collection_retries_without_destroying_evidence(setup,monkeypatch,boundary):
    store,owner,runtime,runner=setup
    attempt=start(setup);verifying=finish_agent(setup,attempt)
    runtime.complete(verifying['runtime_id'],[{'type':'verification','checks':[{'id':'protected','result':'passed'}]}])
    transition=store.transition;event=store.append_event;logs=runtime.logs
    raised=False
    def maybe_fail():
        nonlocal raised
        if not raised:
            raised=True
            raise OSError('one transient proof collection error')
    def change(identity,state,**fields):
        if boundary=='intent' and state=='verifying' and (fields.get('result') or {}).get('completion_intent'):maybe_fail()
        return transition(identity,state,**fields)
    def append(*args,**kwargs):
        if boundary=='event' and kwargs.get('dedupe_key')=='verification-result':maybe_fail()
        return event(*args,**kwargs)
    def read(*args,**kwargs):
        if boundary=='logs':maybe_fail()
        return logs(*args,**kwargs)
    monkeypatch.setattr(store,'transition',change);monkeypatch.setattr(store,'append_event',append);monkeypatch.setattr(runtime,'logs',read)
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='verifying'
    assert verifying['runtime_id'] not in runtime.cleaned
    runner.tick()
    final=store.get_attempt(attempt['id'])
    assert final['state']=='completed' and final['outcome']=='verified'
    assert len(runtime.launches)==2


def test_persistent_proof_collection_failure_is_bounded(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup);verifying=finish_agent(setup,attempt)
    runtime.complete(verifying['runtime_id'],[{'type':'verification','checks':[{'id':'protected','result':'passed'}]}])
    def unavailable(*args,**kwargs):raise OSError('persistent proof read error')
    monkeypatch.setattr(runtime,'logs',unavailable)
    for count in range(1,4):
        runner.tick()
        current=store.get_attempt(attempt['id'])
        assert current['state']=='verifying'
        assert current['result']['verification_collection_retries']==count
    runner.tick()
    final=store.get_attempt(attempt['id'])
    assert final['state']=='failed' and final['reason']=='verifier_infrastructure'
    assert len(runtime.launches)==2
