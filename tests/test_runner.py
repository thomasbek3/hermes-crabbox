"""Controller/runtime integration with real durable state and a bounded fake runtime."""
import hashlib
import json
import os
from pathlib import Path
import pytest
from cloudworkbench.runner import Runner
from cloudworkbench.adapters import parse_events
from cloudworkbench.store import Store, StoreError


class FakeRuntime:
    image = 'sha256:' + 'a' * 64

    def __init__(self, root):
        self.root = root
        self.runtimes = {}
        self.launches = []
        self.stops = []
        self.cleaned = []
        self.stop_effective = True
        self.status_error = None
        self.uncertain_launch = False

    def make_workspace(self, session_id):
        path = self.root / 'workspaces' / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def native_state(self, session_id):
        path = self.root / 'native' / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def launch(self, attempt_id, session_id, argv, env, mounts=None, generation=1):
        rid = 'runtime-' + attempt_id
        self.runtimes[rid] = {'state':'running','exit_code':None,'oom':False,'generation':generation,'attempt_id':attempt_id,'session_id':session_id,'logs':b''}
        if not attempt_id.endswith('-verify'):
            spool = self.native_state(session_id) / 'events' / f'{attempt_id}.{generation}.jsonl'
            spool.parent.mkdir(exist_ok=True)
            spool.touch(exist_ok=False)
        self.launches.append({'id':rid,'attempt_id':attempt_id,'argv':argv,'env':env,'mounts':mounts,'generation':generation})
        if self.uncertain_launch:
            raise TimeoutError('creation receipt lost')
        return rid

    def status(self, runtime_id, expected_generation=None):
        if self.status_error:
            raise self.status_error
        value = self.runtimes.get(runtime_id)
        if value is None:
            return {'state':'missing','exit_code':None,'oom':False}
        if expected_generation is not None and value['generation'] != expected_generation:
            raise ValueError('stale runtime generation')
        return {key:value[key] for key in ('state','exit_code','oom')}

    def stop(self, runtime_id, *, expected_generation):
        self.status(runtime_id, expected_generation)
        self.stops.append(runtime_id)
        if self.stop_effective and self.runtimes[runtime_id]['state'] == 'running':
            self.runtimes[runtime_id].update(state='exited', exit_code=143)

    def cleanup(self, runtime_id, *, expected_generation):
        assert self.status(runtime_id, expected_generation)['state'] in ('created','exited')
        self.cleaned.append(runtime_id)
        del self.runtimes[runtime_id]

    def cleanup_infrastructure(self, attempt_id, *, expected_generation):
        pass

    def logs(self, runtime_id, max_bytes):
        return self.runtimes[runtime_id]['logs'][:max_bytes]

    def list_owned(self):
        return [{'id':rid, 'labels':{'io.cloudworkbench.attempt':value['attempt_id'],'io.cloudworkbench.generation':str(value['generation'])}} for rid,value in self.runtimes.items()]

    def emit(self,rid,records):
        runtime=self.runtimes[rid]
        if runtime['attempt_id'].endswith('-verify'):
            return
        spool=self.native_state(runtime['session_id'])/'events'/f"{runtime['attempt_id']}.{runtime['generation']}.jsonl"
        with spool.open('ab') as output:
            for record in records:
                for event in parse_events(json.dumps(record).encode()):
                    output.write((json.dumps(event)+'\n').encode())

    def complete(self, rid, records, exit_code=0, oom=False):
        self.emit(rid,records)
        self.runtimes[rid].update(state='exited',exit_code=exit_code,oom=oom,logs=b'\n'.join(json.dumps(record).encode() for record in records))


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / 'state'; state.mkdir()
    store = Store(state / 'state.db')
    owner = store.add_client('owner','x'*40,['submit','observe','retrieve','cancel'],['project'])
    runtime = FakeRuntime(tmp_path)
    config = {'state_root':str(state),'input_root':str(tmp_path/'inputs'),'runtime':{'gid':os.getgid()},'test_mode':True,'capacity':2,'projects':{'project':{'allowed_agents':['fixture'],'environment_versions':['fixture-v1'],'models':{},'checks':[{'id':'protected','argv':['python3','check.py'],'mandatory':True}]}}}
    runner = Runner(store,config,runtime=runtime)
    runner.capacity = lambda: 0
    return store,owner,runtime,runner


def submit(setup, key='create', **kwargs):
    store,owner,_,_=setup
    return store.create_session(owner,{'project_id':'project','goal':'test fixture','agent':'fixture','environment_version':'fixture-v1',**kwargs},key)


def start(setup, **kwargs):
    created=submit(setup,**kwargs)
    setup[3].tick()
    return setup[0].get_attempt(created['attempt_id'])


def finish_agent(setup, attempt, **result_fields):
    store,_,runtime,runner=setup
    runtime.complete(attempt['runtime_id'],[{'type':'result','is_error':False,'result':'Created requested files',**result_fields}])
    runner.tick()
    return store.get_attempt(attempt['id'])


def finish_verifier(setup, attempt, checks=None):
    store,_,runtime,runner=setup
    checks=[{'id':'protected','result':'passed'}] if checks is None else checks
    runtime.complete(attempt['runtime_id'],[{'type':'verification','checks':checks}])
    runner.tick()
    return store.get_attempt(attempt['id'])


def test_synthetic_success_sequential_verifier_and_artifacts(setup):
    store,owner,runtime,runner=setup
    attempt=start(setup)
    assert attempt['state']=='running'
    workspace=runtime.make_workspace(attempt['session_id'])
    (workspace/'answer.txt').write_text('answer')
    verifying=finish_agent(setup,attempt)
    assert verifying['state']=='verifying'
    assert len(runtime.launches)==2
    assert runtime.launches[1]['attempt_id']==attempt['id']+'-verify'
    assert runtime.launches[1]['env']=={}
    assert all(m['target']!='/run/secrets/claude-token' for m in runtime.launches[1]['mounts'])
    completed=finish_verifier(setup,verifying)
    assert completed['state']=='completed' and completed['outcome']=='verified'
    artifacts=store.list_artifacts(owner,attempt['session_id'])
    assert artifacts[0]['sha256']==hashlib.sha256(b'answer').hexdigest()
    assert any(e['type']=='verification.result' for e in store.events(owner,attempt['session_id']))
    assert store.active_attempts()==[]


def test_cancellation_waits_for_confirmed_stop(setup):
    store,owner,runtime,runner=setup
    attempt=start(setup)
    store.cancel(owner,attempt['id'],'cancel')
    runtime.stop_effective=False
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='running'
    assert len(store.active_attempts())==1
    runtime.stop_effective=True
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='cancelled'


def test_missing_runtime_interrupts_preserving_workspace(setup):
    store,owner,runtime,runner=setup
    attempt=start(setup)
    output=runtime.make_workspace(attempt['session_id'])/'keep.txt';output.write_text('preserve')
    del runtime.runtimes[attempt['runtime_id']]
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='interrupted'
    assert output.read_text()=='preserve'


def test_status_failure_keeps_uncertain_runtime_reserved(setup):
    store,_,runtime,runner=setup
    attempt=start(setup)
    runtime.status_error=TimeoutError('docker unavailable')
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='running'
    assert store.claim_next(capacity=1) is None
    heartbeat=json.loads((runner.root/'heartbeat.json').read_text())
    assert heartbeat['errors'][0]['error']=='TimeoutError'


def test_lost_launch_receipt_reconciles_single_runtime(setup):
    store,_,runtime,runner=setup
    runtime.uncertain_launch=True
    attempt=start(setup)
    assert attempt['state']=='running' and attempt['runtime_id']
    runner.tick()
    assert len(runtime.launches)==1


def test_permission_denial_stops_before_verification(setup):
    store,_,runtime,_=setup
    attempt=start(setup)
    result=finish_agent(setup,attempt,permission_denials=[{'tool_name':'Bash'}])
    assert result['state']=='failed' and result['reason']=='permission_unsupported'
    assert len(runtime.launches)==1


def test_failed_check_is_rejected_not_infrastructure_failure(setup):
    attempt=start(setup)
    verifying=finish_agent(setup,attempt)
    completed=finish_verifier(setup,verifying,[{'id':'protected','result':'failed'}])
    assert completed['state']=='completed' and completed['outcome']=='rejected'


def test_verifier_infrastructure_failure_distinct(setup):
    attempt=start(setup)
    verifying=finish_agent(setup,attempt)
    completed=finish_verifier(setup,verifying,[{'id':'protected','result':'infrastructure_error'}])
    assert completed['state']=='failed' and completed['reason']=='verifier_infrastructure'


def test_missing_configured_check_cannot_be_verified(setup):
    attempt=start(setup)
    verifying=finish_agent(setup,attempt)
    completed=finish_verifier(setup,verifying,[])
    assert completed['state']=='failed' and completed['reason']=='verifier_infrastructure'


def test_user_criterion_remains_reviewable_without_receipt(setup):
    attempt=start(setup,acceptance=['Confirm the page looks right'])
    verifying=finish_agent(setup,attempt)
    completed=finish_verifier(setup,verifying)
    assert completed['state']=='completed'
    assert completed['outcome']=='needs_review'
    assert completed['result']['remaining_criteria']


def test_followup_reuses_uploaded_input_and_previous_context(setup,tmp_path):
    store,owner,runtime,runner=setup
    root=Path(runner.config['input_root']);root.mkdir()
    content=root/'source';content.write_bytes(b'input')
    item=store.reserve_input(owner,{'name':'source.txt','mime':'text/plain'},'input')
    store.finalize_input(owner,item['id'],{'storage_path':str(content),'bytes':5,'sha256':hashlib.sha256(b'input').hexdigest()})
    first=start(setup,input_ids=[item['id']])
    first=finish_verifier(setup,finish_agent(setup,first))
    assert first['state']=='completed'
    follow=store.add_message(owner,first['session_id'],'Continue the same work','follow')
    runner.tick()
    attempt=store.get_attempt(follow['attempt_id'])
    assert attempt['state']=='running'
    task=json.loads((runner.root/'tasks'/attempt['id']/'task.json').read_text())
    assert 'Created requested files' in task['continuation_summary']
    assert (runner.root/'tasks'/attempt['id']/'inputs'/item['id']).read_bytes()==b'input'
    assert any(m['target']=='/inputs' and m['readonly'] for m in runtime.launches[-1]['mounts'])


def test_stale_controller_cannot_change_or_emit(setup):
    store,_,_,runner=setup
    attempt=start(setup)
    current=store.fence_attempt(attempt['id'],attempt['generation'])
    for operation in [lambda:runner.change(attempt,'failed',reason='old'),lambda:runner.event(attempt,'error',{'old':True})]:
        with pytest.raises(StoreError) as err:
            operation()
        assert err.value.status_code==409
    assert store.get_attempt(attempt['id'])['generation']==current['generation']
    assert store.get_attempt(attempt['id'])['state']=='running'


def test_timeout_does_not_release_runtime_until_stopped(setup):
    store,_,runtime,runner=setup
    attempt=start(setup)
    runner.config['execution_seconds']=-1
    runtime.stop_effective=False
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='running'
    assert runtime.status(attempt['runtime_id'])['state']=='running'
    assert len(store.active_attempts())==1

def test_queue_wait_does_not_consume_execution_budget(setup):
    import sqlite3
    store,_,_,runner=setup
    created=submit(setup)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE attempts SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?",(created['attempt_id'],))
    runner.tick()
    runner.tick()
    assert store.get_attempt(created['attempt_id'])['state']=='running'


def test_input_directory_symlink_cannot_write_outside_workspace(setup,tmp_path):
    store,owner,runtime,runner=setup
    root=Path(runner.config['input_root']);root.mkdir()
    content=root/'source';content.write_bytes(b'input')
    item=store.reserve_input(owner,{'name':'source.txt','mime':'text/plain'},'input')
    store.finalize_input(owner,item['id'],{'storage_path':str(content),'bytes':5,'sha256':hashlib.sha256(b'input').hexdigest()})
    created=submit(setup,input_ids=[item['id']])
    outside=tmp_path/'outside';outside.mkdir()
    (runtime.make_workspace(created['session_id'])/'inputs').symlink_to(outside,target_is_directory=True)
    runner.tick()
    assert list(outside.iterdir())==[]
    assert store.get_attempt(created['attempt_id'])['state']=='running'
    assert (runner.root/'tasks'/created['attempt_id']/'inputs'/item['id']).read_bytes()==b'input'

def test_matching_protected_criterion_can_be_verified(setup):
    setup[3].config['projects']['project']['checks'][0]['description']='Protected smoke passes'
    attempt=start(setup,acceptance=[{'id':'protected','description':'Protected smoke passes','mandatory':True}])
    completed=finish_verifier(setup,finish_agent(setup,attempt))
    assert completed['state']=='completed' and completed['outcome']=='verified'


def test_matching_id_with_different_meaning_needs_review(setup):
    setup[3].config['projects']['project']['checks'][0]['description']='Protected smoke passes'
    attempt=start(setup,acceptance=[{'id':'protected','description':'Entirely different untested behavior','mandatory':True}])
    completed=finish_verifier(setup,finish_agent(setup,attempt))
    assert completed['state']=='completed' and completed['outcome']=='needs_review'
    assert completed['result']['remaining_criteria']==['protected']

def test_verifier_launch_failure_releases_only_after_confirmed_absence(setup,monkeypatch):
    store,_,runtime,runner=setup
    attempt=start(setup)
    original=runtime.launch
    def launch(attempt_id,*args,**kwargs):
        if attempt_id.endswith('-verify'):
            raise RuntimeError('create rejected before any runtime')
        return original(attempt_id,*args,**kwargs)
    monkeypatch.setattr(runtime,'launch',launch)
    failed=finish_agent(setup,attempt)
    runner.tick()
    assert failed['state']=='failed' and failed['reason']=='verifier_infrastructure'
    assert store.active_attempts()==[]
    follow=submit(setup,key='next')
    runner.tick()
    assert store.get_attempt(follow['attempt_id'])['state']=='running'


def test_lost_verifier_launch_receipt_adopts_without_second_launch(setup,monkeypatch):
    store,_,runtime,runner=setup
    attempt=start(setup)
    original=runtime.launch
    def launch(attempt_id,*args,**kwargs):
        rid=original(attempt_id,*args,**kwargs)
        if attempt_id.endswith('-verify'):
            raise TimeoutError('start accepted but response lost')
        return rid
    monkeypatch.setattr(runtime,'launch',launch)
    verifying=finish_agent(setup,attempt)
    assert verifying['state']=='verifying' and verifying['runtime_id']
    runner.tick()
    assert len(runtime.launches)==2
    assert finish_verifier(setup,verifying)['outcome']=='verified'


def test_unknown_verifier_creation_holds_capacity_until_inventory_recovers(setup,monkeypatch):
    store,_,runtime,runner=setup
    attempt=start(setup)
    original_launch,original_list=runtime.launch,runtime.list_owned
    def launch(attempt_id,*args,**kwargs):
        original_launch(attempt_id,*args,**kwargs)
        raise TimeoutError('unknown launch outcome')
    def unavailable():
        raise TimeoutError('inventory unavailable')
    monkeypatch.setattr(runtime,'launch',launch)
    monkeypatch.setattr(runtime,'list_owned',unavailable)
    verifying=finish_agent(setup,attempt)
    assert verifying['state']=='verifying' and verifying['runtime_id'] is None
    queued=submit(setup,key='next')
    for _ in range(3):
        runner.tick()
    assert len(store.active_attempts())==1
    assert store.get_attempt(queued['attempt_id'])['state']=='queued'
    assert len(runtime.launches)==2
    monkeypatch.setattr(runtime,'list_owned',original_list)
    runner.tick()
    recovered=store.get_attempt(attempt['id'])
    assert recovered['state']=='verifying' and recovered['runtime_id']
    assert len(runtime.launches)==2


def test_preparation_event_failure_does_not_hide_terminal_policy_rejection(setup,monkeypatch):
    store,_,_,runner=setup
    created=submit(setup)
    runner.config['projects']['project']['environment_versions']=[]
    def event_failure(*args,**kwargs):
        raise OSError('event writer unavailable')
    monkeypatch.setattr(store,'append_event',event_failure)
    runner.tick()
    attempt=store.get_attempt(created['attempt_id'])
    assert attempt['state']=='failed' and attempt['reason']=='policy_rejected'
    assert store.active_attempts()==[]


def test_created_runtime_cleaned_after_lost_start_receipt(setup,monkeypatch):
    store,_,runtime,runner=setup
    original=runtime.launch
    def launch(*args,**kwargs):
        rid=original(*args,**kwargs)
        runtime.runtimes[rid]['state']='created'
        raise RuntimeError('Docker start rejected')
    monkeypatch.setattr(runtime,'launch',launch)
    attempt=start(setup)
    assert attempt['state']=='failed'
    assert runtime.cleaned==[runtime.launches[0]['id']]
    assert runtime.runtimes=={} and store.active_attempts()==[]


def test_known_created_runtime_cleanup_failure_retains_reservation(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup)
    runtime.runtimes[attempt['runtime_id']]['state']='created'
    original=runtime.cleanup
    def cleanup(*args,**kwargs):
        raise OSError('rm unavailable')
    monkeypatch.setattr(runtime,'cleanup',cleanup)
    for _ in range(5):
        runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='running'
    assert len([e for e in store.events(owner,attempt['session_id']) if e['type']=='error'])==1
    monkeypatch.setattr(runtime,'cleanup',original)
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='interrupted'
    assert runtime.runtimes=={}


def test_persistent_cleanup_failure_emits_bounded_error_events(setup,monkeypatch):
    store,owner,runtime,runner=setup
    attempt=start(setup)
    runtime.complete(attempt['runtime_id'],[],exit_code=1)
    original=runtime.cleanup
    def cleanup(*args,**kwargs):
        raise OSError('persistent rm failure')
    monkeypatch.setattr(runtime,'cleanup',cleanup)
    for _ in range(20):
        runner.tick()
    errors=[e for e in store.events(owner,attempt['session_id']) if e['type']=='error']
    assert len(errors)==1 and errors[0]['payload']['stage']=='result_collection'
    assert len(store.active_attempts())==1
    monkeypatch.setattr(runtime,'cleanup',original)
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='failed'
    assert store.active_attempts()==[]


def test_verifier_nonzero_exit_is_infrastructure_failure(setup):
    store,_,runtime,runner=setup
    verifying=finish_agent(setup,start(setup))
    runtime.complete(verifying['runtime_id'],[],exit_code=2)
    runner.tick()
    failed=store.get_attempt(verifying['id'])
    assert failed['state']=='failed' and failed['reason']=='verifier_infrastructure'
    assert failed['result']['verification_error']=='nonzero_exit'
    assert runtime.runtimes=={}


def test_verification_has_own_budget_and_timeout_reason(setup):
    import sqlite3
    store,_,runtime,runner=setup
    verifying=finish_agent(setup,start(setup))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE attempts SET started_at='2000-01-01T00:00:00+00:00' WHERE id=?",(verifying['id'],))
    runner.tick()
    assert store.get_attempt(verifying['id'])['state']=='verifying'
    assert runtime.stops==[]
    runner.config['verification_seconds']=-1
    runner.tick()
    failed=store.get_attempt(verifying['id'])
    assert failed['state']=='failed' and failed['reason']=='verifier_infrastructure'
    assert failed['result']['verification_error']=='timeout'
    assert runtime.runtimes=={}


def test_missing_job_retries_partial_infrastructure_cleanup(setup,monkeypatch):
    store,_,runtime,runner=setup
    attempt=start(setup)
    del runtime.runtimes[attempt['runtime_id']]
    seen=[]
    def unavailable(identity,*,expected_generation):
        seen.append((identity,expected_generation))
        raise OSError('proxy removal failed')
    monkeypatch.setattr(runtime,'cleanup_infrastructure',unavailable)
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='running'
    assert seen and seen[0]==(attempt['id'],attempt['generation'])
    monkeypatch.setattr(runtime,'cleanup_infrastructure',lambda *args,**kwargs:None)
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='interrupted'

def test_live_events_and_restart_resume_beyond_log_tail(setup):
    store,owner,runtime,runner=setup
    attempt=start(setup)
    records=[{'type':'system','subtype':'init','model':'actual-model','claude_code_version':'test'}]+[{'type':'assistant','message':{'content':[{'type':'text','text':f'message-{index}'}]}} for index in range(500)]
    runtime.emit(attempt['runtime_id'],records)
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='running'
    events=store.events(owner,attempt['session_id'])
    assert len([e for e in events if e['type']=='assistant.message'])==500
    assert next(e for e in events if e['type']=='adapter.provenance')['payload']['model']=='actual-model'
    restarted=Runner(store,runner.config,runtime=runtime);restarted.capacity=lambda:0
    restarted.tick()
    assert store.events(owner,attempt['session_id'])==events
    runtime.complete(attempt['runtime_id'],[{'type':'result','is_error':False,'result':'complete'}])
    restarted.tick()
    assert store.get_attempt(attempt['id'])['state']=='verifying'


def test_cursor_write_crash_replays_without_duplicate_events(setup,monkeypatch):
    import cloudworkbench.runner as module
    store,owner,runtime,runner=setup
    attempt=start(setup)
    runtime.emit(attempt['runtime_id'],[{'type':'assistant','message':{'content':[{'type':'text','text':'durable'}]}}])
    original=module.write_json
    def fail_cursor(path,value,*args,**kwargs):
        if str(path).endswith('.events.cursor.json'):
            raise OSError('simulated crash before cursor commit')
        return original(path,value,*args,**kwargs)
    monkeypatch.setattr(module,'write_json',fail_cursor)
    runner.tick()
    monkeypatch.setattr(module,'write_json',original)
    restarted=Runner(store,runner.config,runtime=runtime);restarted.capacity=lambda:0
    restarted.tick()
    events=store.events(owner,attempt['session_id'])
    assert len([e for e in events if e['type']=='assistant.message'])==1
    assert store.get_attempt(attempt['id'])['state']=='running'


def test_live_partial_secret_then_replacement_stops_job(setup):
    store,owner,runtime,runner=setup
    attempt=start(setup)
    secret=b'synthetic-secret-123';runner.forbidden=(secret,)
    path=runtime.native_state(attempt['session_id'])/'events'/f"{attempt['id']}.{attempt['generation']}.jsonl"
    line=(json.dumps({'type':'assistant.message','payload':{'text':secret.decode()}})+'\n').encode()
    path.write_bytes(line[:40]);runner.tick()
    assert not [e for e in store.events(owner,attempt['session_id']) if e['type']=='assistant.message']
    with path.open('ab') as output:
        output.write(line[40:])
    runner.tick()
    events=store.events(owner,attempt['session_id'])
    assert secret.decode() not in json.dumps(events)
    assert next(e for e in events if e['type']=='assistant.message')['payload']['text']=='[REDACTED]'
    path.rename(path.with_suffix('.old'));path.write_bytes(line)
    runner.tick()
    assert store.get_attempt(attempt['id'])['reason']=='event_capture_failed'
    assert runtime.runtimes=={}

def test_error_diagnostics_distinguish_known_faults_without_stderr_leak(setup):
    from cloudworkbench.runtime import RuntimeError as DockerRuntimeError
    store,owner,_,runner=setup
    attempt=start(setup)
    for code in ('docker_image_missing','docker_mount_invalid'):
        error=DockerRuntimeError('synthetic-secret-in-stderr',code=code,operation='create')
        for _ in range(5):
            runner.record_error(attempt,'verifier_launch',error)
    unexpected=ValueError('another-secret')
    unexpected.code='another-secret';unexpected.operation='another-secret'
    runner.record_error(attempt,'verifier_launch',unexpected)
    errors=[e['payload'] for e in store.events(owner,attempt['session_id']) if e['type']=='error']
    assert len(errors)==3
    assert {e['code'] for e in errors}=={'docker_image_missing','docker_mount_invalid','value_error'}
    assert 'secret' not in json.dumps(errors)


def test_verifier_launch_and_proof_failure_have_durable_detail(setup,monkeypatch):
    store,_,runtime,_=setup
    attempt=start(setup)
    def rejected(*args,**kwargs):
        raise RuntimeError('create failed')
    monkeypatch.setattr(runtime,'launch',rejected)
    result=finish_agent(setup,attempt)
    assert result['result']['verification_error']=='launch_failed'


@pytest.mark.parametrize('crash_point', ['after_export', 'after_verifying_transition'])
def test_restart_adopts_export_and_verification_intent(setup, monkeypatch, crash_point):
    store, owner, runtime, runner = setup
    a = start(setup)
    (runtime.make_workspace(a['session_id'])/'answer.txt').write_text('durable answer')
    runtime.complete(a['runtime_id'], [{'type':'result','is_error':False,'result':'done'}])
    if crash_point == 'after_export':
        original = runner.export
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt('simulated process loss')
        monkeypatch.setattr(runner,'export',crash)
    else:
        def crash(*args): raise KeyboardInterrupt('simulated process loss')
        monkeypatch.setattr(runner,'start_verification',crash)
    with pytest.raises(KeyboardInterrupt): runner.tick()
    replacement = Runner(store, runner.config, runtime=runtime)
    replacement.capacity = lambda: 0
    replacement.tick()
    current = store.get_attempt(a['id'])
    assert current['state'] == 'verifying'
    assert len(runtime.launches) == 2
    assert len(store.list_artifacts(owner,a['session_id'])) == 1
    runtime.complete(current['runtime_id'], [{'type':'verification','checks':[{'id':'protected','result':'passed'}]}])
    replacement.tick()
    assert store.get_attempt(a['id'])['outcome'] == 'verified'
    assert len([e for e in store.events(owner,a['session_id']) if e['type']=='artifact.created']) == 1


def test_cancel_drains_events_and_exports_partial_files(setup):
    store, owner, runtime, runner = setup
    a = start(setup)
    (runtime.make_workspace(a['session_id'])/'partial.txt').write_text('unfinished work')
    runtime.emit(a['runtime_id'], [{'type':'assistant','message':{'content':[{'type':'text','text':'last progress'}]}}])
    store.cancel(owner,a['id'],'cancel-with-progress')
    runner.tick()
    ended = store.get_attempt(a['id'])
    assert ended['state'] == 'cancelled'
    assert ended['result']['partial']
    assert ended['result']['event_capture']['complete']
    assert store.list_artifacts(owner,a['session_id'])[0]['path'] == 'partial.txt'
    assert any(e['type']=='assistant.message' for e in store.events(owner,a['session_id']))


@pytest.mark.parametrize("limited_patch", [False, True])
def test_registered_repository_delivery_patch_and_followup(setup, tmp_path, monkeypatch, limited_patch):
    import subprocess
    store,owner,runtime,runner=setup
    if limited_patch:
        import cloudworkbench.runner as runner_module
        original=runner_module.text_patch
        monkeypatch.setattr(runner_module,'text_patch',lambda *args,**kwargs:original(*args,**kwargs,max_bytes=1))
    repository=tmp_path/'registered';repository.mkdir()
    def git(*args):return subprocess.check_output(['git','-C',str(repository),*args]).decode().strip()
    git('init','-q');git('config','user.name','Qualification');git('config','user.email','q@example.invalid')
    (repository/'booking.py').write_text('def valid_date(value):\n    return True\n')
    git('add','.');git('commit','-qm','Known failing base');commit=git('rev-parse','HEAD')
    runner.config['repositories']={'sample':{'path':str(repository),'allowed_commits':[commit]}}
    runner.config['projects']['project']['repository']={'repository_id':'sample','commit':commit,'destination':'.'}
    a=start(setup)
    workspace=runtime.make_workspace(a['session_id'])
    assert (workspace/'booking.py').read_text().endswith('return True\n')
    (workspace/'booking.py').write_text('def valid_date(value):\n    return False\n')
    verifying=finish_agent(setup,a)
    completed=finish_verifier(setup,verifying)
    assert completed['result']['repository']['base_commit']==commit
    assert completed['result']['delivery']['changed_files'][0]['path']=='booking.py'
    artifacts=store.list_artifacts(owner,a['session_id'])
    patch=next(item for item in artifacts if item['path']=='@delivery/changes.patch')
    if limited_patch:
        assert Path(patch['storage_path']).read_bytes()==b''
        assert not completed['result']['delivery']['complete_text_patch']
        assert completed['state']=='completed' and completed['outcome']=='verified'
        assert any(item['path']=='booking.py' for item in artifacts)
    else:
        git('apply','--check',patch['storage_path'])
    follow=store.add_message(owner,a['session_id'],'Add a readme','follow-repo')
    runner.tick();second=store.get_attempt(follow['attempt_id'])
    assert second['state']=='running'
    assert (workspace/'booking.py').read_text().endswith('return False\n')
    assert second['result']['repository']==completed['result']['repository']
