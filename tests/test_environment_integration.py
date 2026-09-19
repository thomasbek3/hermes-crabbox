import copy
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from cloudworkbench.api import create_app
from cloudworkbench.environments import EnvironmentRegistry, legacy_manifest
from cloudworkbench.runner import Runner
from test_runner import setup, start, finish_agent, finish_verifier, submit
from test_environments import passed


def enable_registry(setup,tmp_path):
    store,owner,runtime,runner=setup
    script=tmp_path/'check.py';script.write_text('assert True\n')
    project=runner.config['projects']['project']
    project['checks']=[{'id':'protected','description':'Protected behavior.',
                        'argv':['python3','/run/task/check.py'],'script_name':'check.py',
                        'script_source':str(script)}]
    runner.config['runtime'].update(image=runtime.image,cpus=1,memory_mib=1024,pids=128,workspace_mib=256)
    registry=EnvironmentRegistry(tmp_path/'environments.db')
    manifest=legacy_manifest('project','fixture-v1',project,runner.config['runtime'],
                             architecture='amd64',cli_versions={'python3':'3.13'},
                             readiness_probes=[{'id':'python','argv':['python3','--version']}])
    registry.register(manifest);registry.qualify('project','fixture-v1',passed)
    registry.activate('project','fixture-v1',expected_active=None)
    runner.config['environment_registry']=str(registry.path)
    return registry,manifest,script


def test_attempt_snapshot_survives_activation_restart_and_followup(setup,tmp_path):
    store,owner,runtime,runner=setup
    registry,manifest,_=enable_registry(setup,tmp_path)
    attempt=start(setup,acceptance=[{'id':'protected','description':'Protected behavior.'}])
    snapshot=attempt['result']['environment']
    assert snapshot['manifest']['version']=='fixture-v1'
    # Promote a new check definition while first attempt still runs.
    next_manifest=copy.deepcopy(manifest);next_manifest['version']='fixture-v2'
    next_manifest['checks'][0]['description']='Different v2 behavior.'
    registry.register(next_manifest);registry.qualify('project','fixture-v2',passed)
    registry.activate('project','fixture-v2',expected_active='fixture-v1')
    runner.config['projects']['project']['checks'][0]['description']='Changed mutable config'
    # Recovery can use the persisted snapshot even with registry temporarily absent.
    registry.path.rename(tmp_path/'registry-offline.db')
    replacement=Runner(store,runner.config,runtime=runtime);replacement.capacity=lambda:0
    resumed=(store,owner,runtime,replacement)
    verifying=finish_agent(resumed,attempt)
    check=json.loads((replacement.root/'tasks'/attempt['id']/'verify.json').read_text())['checks'][0]
    assert check['description']=='Protected behavior.'
    completed=finish_verifier(resumed,verifying)
    assert completed['outcome']=='verified'
    assert completed['result']['environment']==snapshot
    created=store.add_message(owner,attempt['session_id'],'Follow up without changing behavior','next')
    replacement.tick()
    follow=store.get_attempt(created['attempt_id'])
    assert follow['state']=='running'
    assert follow['result']['environment']==snapshot
    assert runtime.image==snapshot['manifest']['image_digest']


@pytest.mark.parametrize('change',[{'image_digest':'sha256:'+'b'*64},
                                   {'resources':{'cpus':2,'memory_mib':1024,'pids':128,'workspace_mib':256}},
                                   {'network_profile':'undeclared'}, {'secret_refs':['other-secret']},
                                   {'startup_commands':[{'id':'serve','argv':['python3','server.py']}]}])
def test_worker_never_mutates_shared_runtime_to_match_candidate(setup,tmp_path,change):
    store,owner,runtime,runner=setup
    registry,manifest,_=enable_registry(setup,tmp_path)
    candidate={**manifest,**change,'version':'fixture-v2'}
    registry.register(candidate);registry.qualify('project','fixture-v2',passed)
    runner.config['projects']['project']['environment_versions'].append('fixture-v2')
    created=submit(setup,environment_version='fixture-v2')
    runner.tick()
    result=store.get_attempt(created['attempt_id'])
    assert result['state']=='failed' and result['reason']=='policy_rejected'
    assert runtime.launches==[]
    assert runtime.image==manifest['image_digest']


def test_changed_protected_script_is_not_executed(setup,tmp_path):
    store,owner,runtime,runner=setup
    _,_,script=enable_registry(setup,tmp_path)
    attempt=start(setup)
    script.write_text('raise SystemExit(0) # replacement\n')
    result=finish_agent(setup,attempt)
    assert result['state']=='failed' and result['reason']=='verifier_infrastructure'
    assert len(runtime.launches)==1
    assert result['result']['environment']['qualified']


def test_api_rejects_unqualified_version_before_session_creation(setup,tmp_path):
    store,owner,runtime,runner=setup
    registry,manifest,_=enable_registry(setup,tmp_path)
    settings={**runner.config,'artifact_root':tmp_path/'exports'}
    settings['projects']['project']['environment_versions'].append('fixture-v2')
    registry.register({**manifest,'version':'fixture-v2'})
    client=TestClient(create_app(store,settings))
    payload={'project_id':'project','agent':'fixture','goal':'one','environment_version':'fixture-v2'}
    headers={'Authorization':'Bearer '+'x'*40,'Idempotency-Key':'one'}
    assert client.post('/v1/sessions',json=payload,headers=headers).status_code==422
    assert store.list_sessions(owner)==[]
    payload['environment_version']='fixture-v1'
    assert client.post('/v1/sessions',json=payload,headers=headers).status_code==201
    assert len(store.list_sessions(owner))==1


def test_missing_environment_registry_fails_with_policy_reason(setup, tmp_path):
    registry,_,_=enable_registry(setup,tmp_path)
    registry.path.rename(tmp_path/'offline.db')
    attempt=start(setup)
    assert attempt['state']=='failed' and attempt['reason']=='policy_rejected'
    assert not setup[2].launches and not setup[0].active_attempts()


def test_qualified_images_keep_recovery_and_followups_pinned(setup,tmp_path,monkeypatch):
    import os
    from cloudworkbench.runtime import Runtime
    store,owner,backend,runner=setup
    registry,manifest,_=enable_registry(setup,tmp_path)
    old_image=manifest['image_digest'];new_image='sha256:'+'b'*64
    gateway='sha256:'+'c'*64
    config=runner.config
    config['qualified_images']=[old_image,new_image]
    config['runtime'].update(root=str(tmp_path/'runtime'),uid=max(1,os.getuid()),
        gid=max(1,os.getgid()),approved_mount_roots=[str(runner.root/'tasks')],
        approved_writable_mount_roots=[str(tmp_path)],network_enabled=True,
        allowed_domains=['api.anthropic.com'],egress_image=gateway)
    # The manifest policy remains the already-qualified named profile. Docker is
    # replaced only at launch; cloned Runtime constructors and policy are real.
    config['environment_network_profile']='none'
    backend.config=copy.deepcopy(config['runtime']);backend.egress_image=gateway
    runner.verifier=Runtime({**config['runtime'],'network_enabled':False,'workspace_readonly':True})
    captured=[]
    base_launch=backend.launch
    def capture(runtime,*args,**kwargs):
        captured.append({'image':runtime.image,'config':copy.deepcopy(runtime.config)})
        return base_launch(*args,**kwargs)
    backend.launch=lambda *args,**kwargs:capture(backend,*args,**kwargs)
    monkeypatch.setattr(Runtime,'launch',capture)
    registry.register({**manifest,'version':'fixture-v2','image_digest':new_image})
    registry.qualify('project','fixture-v2',passed)
    config['projects']['project']['environment_versions'].append('fixture-v2')
    first=start(setup,key='old-image')
    queued=submit(setup,key='new-image',environment_version='fixture-v2')
    second=store.get_attempt(queued['attempt_id'])
    second,_=runner.environment_project(second,pin=True)
    new_runtime=runner.runtime_for(second)
    assert first['state']=='running' and second['state']=='queued'
    assert [row['image'] for row in captured]==[old_image]
    assert new_runtime.image==new_image
    assert backend.image==old_image and backend.config['image']==old_image
    assert new_runtime.config['approved_mount_roots']==[str(runner.root/'tasks')]
    assert new_runtime.config['egress_image']==gateway
    assert new_runtime.config['allowed_domains']==['api.anthropic.com']
    assert new_runtime.config['uid']==config['runtime']['uid']
    assert new_runtime.config['network_enabled'] is True
    # Upgrade worker default while old is running and new queued, then recover with registry
    # unavailable. Old sessions must still use their immutable persisted image.
    config['runtime']['image']=new_image
    backend.image=new_image;backend.config=copy.deepcopy(config['runtime'])
    replacement=Runner(store,config,runtime=backend);replacement.capacity=lambda:0
    replacement.verifier=Runtime({**config['runtime'],'network_enabled':False,'workspace_readonly':True})
    resumed=(store,owner,backend,replacement)
    registry.path.rename(tmp_path/'registry-offline.db')
    old_verifying=finish_agent(resumed,first)
    assert old_verifying['state']=='verifying'
    assert old_verifying['result']['image_digest']==old_image
    assert captured[-1]['image']==old_image
    assert captured[-1]['config']['network_enabled'] is False
    assert captured[-1]['config']['workspace_readonly'] is True
    assert old_verifying['result']['environment']==first['result']['environment']
    finish_verifier(resumed,old_verifying)
    second=store.get_attempt(second['id'])
    assert second['state']=='running' and captured[-1]['image']==new_image
    new_verifying=finish_agent(resumed,second)
    assert new_verifying['result']['image_digest']==new_image
    assert captured[-1]['image']==new_image
    finish_verifier(resumed,new_verifying)
    follow=store.add_message(owner,first['session_id'],'Keep the old environment','follow-image')
    replacement.tick()
    follow=store.get_attempt(follow['attempt_id'])
    assert follow['state']=='running'
    assert captured[-1]['image']==old_image
    assert follow['result']['environment']==first['result']['environment']
    assert backend.image==new_image and backend.config['image']==new_image


@pytest.mark.parametrize('allowed',[[],['latest'],['sha256:'+'A'*64],['sha256:'+'a'*64]*33,'sha256:'+'a'*64])
def test_invalid_qualified_image_policy_fails_before_launch(setup,tmp_path,allowed):
    enable_registry(setup,tmp_path)
    setup[3].config['qualified_images']=allowed
    attempt=start(setup)
    assert attempt['state']=='failed' and attempt['reason']=='policy_rejected'
    assert setup[2].launches==[]


def test_revoked_pinned_image_is_not_silently_replaced(setup,tmp_path):
    store,owner,backend,runner=setup
    enable_registry(setup,tmp_path)
    attempt=start(setup)
    runner.config['qualified_images']=['sha256:'+'b'*64]
    result=finish_agent(setup,attempt)
    assert result['state']=='failed'
    assert result['result']['environment']==attempt['result']['environment']
    assert len(backend.launches)==1


def test_production_constructor_builds_hardened_verifier_without_injection(setup,tmp_path):
    import os
    store,_,backend,runner=setup
    config=copy.deepcopy(runner.config)
    config['runtime'].update(image=backend.image,root=str(tmp_path/'actual-runtime'),
        uid=max(1,os.getuid()),gid=max(1,os.getgid()),network_enabled=True,
        allowed_domains=['api.anthropic.com'],approved_mount_roots=[str(runner.root/'tasks')])
    actual=Runner(store,config)
    assert actual.runtime is not actual.verifier
    assert actual.runtime.network_enabled is True
    assert actual.verifier.network_enabled is False
    assert actual.verifier.config['workspace_readonly'] is True
    assert actual.runtime.allowed_roots==actual.verifier.allowed_roots
    assert actual.runtime.uid==actual.verifier.uid
    assert actual.runtime.image==actual.verifier.image==backend.image
