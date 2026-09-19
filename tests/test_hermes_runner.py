"""Runner admission guards for the normal Hermes lane; no Docker/provider calls."""
import pytest
from tests.test_runner import setup


def configure(value):
    store,owner,runtime,runner=value
    project=runner.config['projects']['project']
    project['allowed_agents'].append('hermes')
    project['models']['hermes']=['grok-4.6']
    runner.config['hermes_enabled']=False
    return store,owner,runtime,runner


def submit(store,owner,key='hermes',**changes):
    return store.create_session(owner,{'agent':'hermes','project_id':'project',
        'goal':'Create the status summary','model':'grok-4.6',
        'environment_version':'fixture-v1',**changes},key)


def test_disabled_hermes_stays_queued_without_runtime_or_native_state(setup):
    store,owner,runtime,runner=configure(setup)
    created=submit(store,owner)
    runner.tick()
    value=store.get_attempt(created['attempt_id'])
    assert value['state']=='queued' and value['reason']
    assert runtime.launches==[]
    assert not (runtime.root/'native'/created['session_id']).exists()
    before=len(store.events(owner,created['session_id']))
    runner.tick()
    assert len(store.events(owner,created['session_id']))==before
    runner.close()


def test_disabled_hermes_does_not_block_existing_legacy_adapter_job(setup):
    store,owner,runtime,runner=configure(setup)
    hermes=submit(store,owner)
    legacy=store.create_session(owner,{'agent':'fixture','project_id':'project',
        'goal':'Existing adapter','environment_version':'fixture-v1'},'legacy')
    runner.tick()
    assert store.get_attempt(hermes['attempt_id'])['state']=='queued'
    started=store.get_attempt(legacy['attempt_id'])
    assert started['state']=='running'
    assert len(runtime.launches)==1 and runtime.launches[0]['attempt_id']==legacy['attempt_id']
    runner.close()


def test_hermes_admission_respects_registered_provider_account_fence(setup):
    from cloudworkbench.provider_leases import ProviderLeases
    store,owner,runtime,runner=configure(setup)
    leases=ProviderLeases(store,cleanup_verifier=lambda _:None,inspector_id='synthetic')
    leases.register_account('grok-test',legacy_agent='hermes',persistent_owner_id='test-owner')
    # Use the controller lease API; no provider runtime or credential is created.
    with leases.account_lock('grok-test'):
        with store._tx() as db:
            leases.reserve_in_transaction(db,'grok-test',persistent_owner_id='test-owner')
    created=submit(store,owner)
    assert store.claim_next(blocked_agents=[]) is None
    value=store.get_attempt(created['attempt_id'])
    assert value['state']=='queued' and value['reason']=='provider_account_reserved'
    assert runtime.launches==[]
    runner.close()


NATIVE='20260918_143228_4e6beb'


def complete_native(store,created,provider=None,*,state='completed'):
    value=store.claim_next();assert value['id']==created['attempt_id']
    if state=='failed':
        store.transition(value['id'],'failed',expected_generation=value['generation'],reason='synthetic_failure')
        return
    store.transition(value['id'],'running',expected_generation=value['generation'])
    store.transition(value['id'],'verifying',expected_generation=value['generation'])
    store.transition(value['id'],'completed',expected_generation=value['generation'],outcome='unverified',
        result={'provider_result':provider if provider is not None else {'is_error':False,'native_session_id':NATIVE}})


@pytest.mark.parametrize('schema',[1,3])
def test_native_resume_uses_exact_same_session_previous_success(setup,schema):
    store,owner,_,runner=configure(setup)
    if schema==3:store.migrate_scheduler();store.migrate_delivery()
    first=submit(store,owner)
    assert store.get_native_resume_session(first['attempt_id']) is None
    complete_native(store,first)
    next_attempt=store.add_message(owner,first['session_id'],'Add strict','followup')
    assert store.get_native_resume_session(next_attempt['attempt_id'])==NATIVE
    other=submit(store,owner,key='other')
    assert store.get_native_resume_session(other['attempt_id']) is None
    runner.close()


@pytest.mark.parametrize('provider',[
    {},{'is_error':True,'native_session_id':NATIVE},
    {'is_error':0,'native_session_id':NATIVE},
    {'is_error':False,'native_session_id':'../../foreign'},
    {'is_error':False,'native_session_id':NATIVE+'\n'},
    {'is_error':False,'native_session_id':'20260918_143228_4E6BEB'},
    {'is_error':False,'native_session_id':123},
    {'is_error':False,'native_session_id':'20260918_143228_4e6beb','padding':'x'*1048576},
])
def test_native_resume_refuses_missing_invalid_or_oversized_prior_record(setup,provider):
    from cloudworkbench.store import StoreError
    store,owner,_,runner=configure(setup)
    first=submit(store,owner);complete_native(store,first,provider)
    following=store.add_message(owner,first['session_id'],'Followup','follow')
    with pytest.raises(StoreError,match='explicit recovery'):
        store.get_native_resume_session(following['attempt_id'])
    runner.close()


def test_native_resume_never_skips_failed_latest_attempt_for_older_success(setup):
    from cloudworkbench.store import StoreError
    store,owner,_,runner=configure(setup)
    first=submit(store,owner);complete_native(store,first)
    second=store.add_message(owner,first['session_id'],'Second','second')
    complete_native(store,second,state='failed')
    third=store.add_message(owner,first['session_id'],'Third','third')
    with pytest.raises(StoreError,match='explicit recovery'):
        store.get_native_resume_session(third['attempt_id'])
    runner.close()


def test_native_resume_refuses_live_prior_and_nonhermes_current(setup):
    from cloudworkbench.store import StoreError
    store,owner,_,runner=configure(setup)
    first=submit(store,owner);store.claim_next()
    following=store.add_message(owner,first['session_id'],'Next','next')
    with pytest.raises(StoreError,match='explicit recovery'):
        store.get_native_resume_session(following['attempt_id'])
    other=store.create_session(owner,{'project_id':'project','agent':'fixture','goal':'Existing adapter'},'fixture')
    with pytest.raises(StoreError,match='scope'):
        store.get_native_resume_session(other['attempt_id'])
    runner.close()


@pytest.mark.parametrize('enabled',[False,1,'true'])
def test_runner_does_not_treat_truthy_configuration_as_hermes_enabled(setup,enabled):
    _,_,_,runner=configure(setup)
    runner.config['hermes_enabled']=enabled
    assert 'hermes' in runner.blocked()
    runner.close()


def test_enabled_hermes_runs_exact_entrypoint_and_native_followup_through_collection(setup):
    import json
    from cloudworkbench.adapters import public_spool_event
    from tests.test_runner import finish_verifier
    store,owner,runtime,runner=configure(setup)
    runner.config['hermes_enabled']=True
    first=submit(store,owner);runner.tick()
    attempt=store.get_attempt(first['attempt_id'])
    assert attempt['state']=='running'
    assert runtime.launches[0]['argv']==['/opt/hermes/venv/bin/python','-m',
        'cloudworkbench.hermes_job_entrypoint','/run/task/task.json']
    task=json.loads((runner.task_dir(attempt)/'task.json').read_text())
    assert task['resume_session_id'] is None and task['tool_image']==runtime.image
    assert task['attempt_id']==attempt['id'] and task['session_id']==attempt['session_id']
    assert not any(m['target'].startswith('/run/secrets') for m in runtime.launches[0]['mounts'])
    workspace=runtime.make_workspace(attempt['session_id'])
    (workspace/'status_summary.py').write_text('answer = 43\n')
    spool=runtime.native_state(attempt['session_id'])/'events'/f"{attempt['id']}.{attempt['generation']}.jsonl"
    record=public_spool_event({'type':'adapter.result','payload':{
        'is_error':False,'summary':'Created status summary','native_session_id':NATIVE}})
    with spool.open('a') as out:out.write(json.dumps(record)+'\n')
    runtime.runtimes[attempt['runtime_id']].update(state='exited',exit_code=0)
    runner.tick();verifying=store.get_attempt(attempt['id'])
    assert verifying['state']=='verifying'
    assert verifying['result']['provider_result']['native_session_id']==NATIVE
    assert verifying['result']['continuation']=='native_session'
    assert runtime.launches[-1]['argv'][0]=='python3'
    completed=finish_verifier(setup,verifying)
    assert completed['state']=='completed' and completed['outcome']=='verified'
    artifacts=store.list_artifacts(owner,attempt['session_id'])
    assert any(item['path']=='status_summary.py' for item in artifacts)
    follow=store.add_message(owner,attempt['session_id'],'Add strict mode','next')
    runner.tick();following=store.get_attempt(follow['attempt_id'])
    assert following['state']=='running'
    next_task=json.loads((runner.task_dir(following)/'task.json').read_text())
    assert next_task['resume_session_id']==NATIVE
    assert next_task['state_host']==task['state_host'] and next_task['workspace_host']==task['workspace_host']
    assert next_task['prompt']=='Add strict mode'
    assert (workspace/'status_summary.py').read_text()=='answer = 43\n'
    runner.close()


def test_missing_prior_native_identity_never_silently_launches_fresh_followup(setup):
    store,owner,runtime,runner=configure(setup)
    first=submit(store,owner);complete_native(store,first,{'is_error':False})
    following=store.add_message(owner,first['session_id'],'Continue','next')
    runner.config['hermes_enabled']=True
    runner.tick()
    assert runtime.launches==[]
    assert store.get_attempt(following['attempt_id'])['state']=='failed'
    runner.close()


def test_hermes_enabled_keeps_legacy_claude_entrypoint_and_dedicated_mount(setup,tmp_path):
    from cloudworkbench.runner import Runner
    store,owner,runtime,old=configure(setup)
    token=tmp_path/'synthetic-token';token.write_bytes(b'sk-ant-oat-synthetic-only-not-a-provider-token');token.chmod(0o640)
    config=old.config
    config.update(hermes_enabled=True,claude_enabled=True,claude_token=str(token))
    config['projects']['project']['allowed_agents'].append('claude')
    config['projects']['project']['models']['claude']=['legacy-approved']
    runner=Runner(store,config,runtime=runtime);runner.capacity=lambda:0
    runner.credential_state.initialize()
    created=store.create_session(owner,{'agent':'claude','project_id':'project','goal':'Existing job',
        'model':'legacy-approved','environment_version':'fixture-v1'},'claude')
    runner.tick()
    a=store.get_attempt(created['attempt_id']);assert a['state']=='running'
    assert runtime.launches[-1]['argv']==['python3','/opt/cloudworkbench/entrypoint.py','/run/task/task.json']
    assert any(m['target']=='/run/secrets/claude-token' and m['readonly'] for m in runtime.launches[-1]['mounts'])
    runner.close();old.close()


def test_real_runner_constructor_uses_hermes_facade_but_plain_isolated_verifier(tmp_path,monkeypatch):
    from cloudworkbench.runner import Runner
    from cloudworkbench.runtime import Runtime
    from cloudworkbench.hermes_coordinator_runtime import HermesCoordinatorRuntime
    from cloudworkbench.store import Store
    root=tmp_path.resolve();state=root/'state';state.mkdir()
    config={'state_root':str(state),'hermes_enabled':True,'resource_sampling_enabled':False,
        'runtime':{'root':str(root/'runtime'),'image':'sha256:'+'a'*64,'network_enabled':True,
                   'allowed_domains':['example.com']},
        'hermes_runtime':{'hermes_source_root':str(root/'source'),
            'hermes_grok_auth':str(root/'synthetic-auth.json'),'docker_socket':str(root/'docker.sock')},
        'projects':{}}
    monkeypatch.setattr(Runtime,'_run',lambda *a,**k:pytest.fail('Constructor must not call Docker'))
    runner=Runner(Store(state/'state.db'),config)
    assert type(runner.runtime) is HermesCoordinatorRuntime
    assert type(runner.verifier) is Runtime
    assert runner.runtime.network_enabled is True
    assert runner.verifier.network_enabled is False
    assert runner.verifier.config['workspace_readonly'] is True
    assert runner.runtime.config['hermes_journal_root']==str(state/'hermes-runtime')
    runner.close()


def test_native_resume_refuses_routed_root_even_if_hermes_agent(setup):
    from cloudworkbench.provider_leases import ProviderLeases
    from cloudworkbench.scheduler import RoleScheduler
    from cloudworkbench.store import StoreError
    store,owner,_,runner=configure(setup)
    store.migrate_scheduler()
    leases=ProviderLeases(store,cleanup_verifier=lambda _:None,inspector_id='synthetic')
    scheduler=RoleScheduler(store,leases)
    root=scheduler.enqueue_root(owner,{'agent':'hermes','project_id':'project','goal':'Routed'},'root',
        frozen={'accounts':{'test':'owner'},'role_plans':{},'provenance':{'synthetic':True}})
    with pytest.raises(StoreError,match='scope'):
        store.get_native_resume_session(root['attempt_id'])
    runner.close()
