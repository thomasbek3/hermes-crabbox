"""Offline host bootstrap policy: no Docker/service/auth calls."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('host_installer',ROOT/'scripts/install-host.py')
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)

def args(**changes):
    return SimpleNamespace(origin='https://worker.example.ts.net',grok_auth=None,capacity=2,memory_mib=4096,workspace_mib=20480,**changes)

def test_reject_non_tailnet_origin():
    for value in ('https://example.com','http://worker.example.ts.net','https://worker.example.ts.net/path','https://user@worker.example.ts.net','https://worker.example.ts.net\nSECRET=x'):
        with pytest.raises(host.InstallError):host.origin(value)
    assert host.origin('https://worker.example.ts.net')

def test_rendered_environment_is_resolvable_without_qualification_claim(tmp_path):
    from cloudworkbench.environments import EnvironmentRegistry,legacy_manifest
    ids={'job':4001,'job_group':4002,'worker':4003,'service_group':4004,'docker_group':4005}
    image='sha256:'+'a'*64
    api,worker=host.configurations(image,ids,args())
    manifest=legacy_manifest(host.PROJECT,host.VERSION,api['projects'][host.PROJECT],worker['runtime'],architecture='amd64',cli_versions={'hermes':'0.21.3'},readiness_probes=[{'id':'hermes','argv':['python','--version'],'timeout_seconds':30}])
    manifest.update(network_profile='crabbox-private-tools-bridge',secret_refs=['grok-dedicated-oauth'])
    registry=EnvironmentRegistry(tmp_path/'environments.db');record=registry.register(manifest)
    with pytest.raises(ValueError):registry.resolve(host.PROJECT,host.VERSION)
    result=registry.resolve(host.PROJECT,host.VERSION,operator_approved_sha256=record['manifest_sha256'])
    assert result['operator_approved'] is True
    api,worker=host.configurations(image,ids,args(),record['manifest_sha256'])
    assert worker['runtime']['uid']==4001
    assert worker['hermes_runtime']['coordinator_uid']==4003
    assert worker['hermes_runtime']['crabbox_pstack_images']==[]
    assert 'qualified_images' not in worker
    assert 'runtime' not in api and 'hermes_runtime' not in api

def test_write_is_idempotent_and_refuses_changed_file(tmp_path):
    file=tmp_path/'file';host.write_new(file,b'first',0o600);host.write_new(file,b'first',0o600)
    with pytest.raises(host.InstallError):host.write_new(file,b'second')
    assert file.read_bytes()==b'first'
    link=tmp_path/'link';link.symlink_to(file)
    with pytest.raises(host.InstallError):host.write_new(link,b'first')

def test_plan_is_read_only_and_reports_platform(monkeypatch):
    monkeypatch.setattr(host.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(host.shutil,'which',lambda _:None)
    monkeypatch.setattr(host,'source_hash',lambda:'a'*64)
    result=host.preflight(args())
    assert result['ready'] is False
    assert any('native_linux' in issue for issue in result['issues'])
    assert 'dedicated_valid_private_grok_oidc_auth_file_required' in result['issues']
    assert result['mode']=='plan'

def test_auth_failures_do_not_echo_secret(tmp_path):
    file=tmp_path/'auth.json';file.write_text('{"secret":"DO-NOT-PRINT"}');file.chmod(0o600)
    assert host.auth_valid(file) is False

def test_public_build_pins_and_no_host_sources():
    data=(ROOT/'deploy/Dockerfile.portable').read_text()
    assert 'FROM python:3.11.13-slim-bookworm' in data
    assert '3b0e392e5a6922034feccac5771041ac78467757' in data
    assert '5100149a1903211c889de4e545bf36d90803740cea4f99aa22651649f9205ea1' in data
    assert 'COPY /' not in data and 'BASE_IMAGE' not in data
    assert 'JOB_UID' in data and 'JOB_GID' in data


def test_generated_runtime_initializes_with_dynamic_identities(tmp_path):
    from cloudworkbench.crabbox_runtime import CrabboxRuntime
    ids={'job':4001,'job_group':4002,'worker':4003,'service_group':4004,'docker_group':4005}
    api,worker=host.configurations('sha256:'+'a'*64,ids,args())
    config={**worker['runtime'],**worker['hermes_runtime'],'hermes_journal_root':str(tmp_path/'journal')}
    for key in ('root','hermes_grok_auth'):
        config[key]=str(Path(config[key]).resolve())
    for key in ('approved_mount_roots','approved_writable_mount_roots'):
        config[key]=[str(Path(value).resolve()) for value in config[key]]
    instance=CrabboxRuntime(config)
    assert instance.uid==4001 and instance.gid==4002
    assert instance.tool_network=='bridge'
    from cloudworkbench.runner import Runner
    stub=SimpleNamespace(config=worker,runtime=instance)
    assert Runner.require_qualified_image(stub,instance.image)==instance.image


def test_import_validates_refresh_contract_without_mutating_login(tmp_path):
    from datetime import datetime,timezone,timedelta
    file=tmp_path/'auth.json'
    client='b1a00492-073a-47ea-816f-4c329264a828'
    row={'auth_mode':'oidc','oidc_issuer':'https://auth.x.ai','oidc_client_id':client,
         'key':'synthetic-access-key-123456789','refresh_token':'synthetic-refresh-key-123456789',
         'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()}
    raw=json.dumps({'https://auth.x.ai::'+client:row}).encode()
    file.write_bytes(raw);file.chmod(0o600)
    assert host.auth_valid(file)
    assert host.auth_bytes(file)==raw and file.read_bytes()==raw
    del row['refresh_token'];file.write_text(json.dumps({'https://auth.x.ai::'+client:row}))
    assert not host.auth_valid(file)


def test_source_context_excludes_untracked_credentials(tmp_path):
    import subprocess
    (tmp_path/'src').mkdir()
    (tmp_path/'src/safe.py').write_text('pass\n')
    (tmp_path/'src/auth.json').write_text('never-copy-me')
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(tmp_path),'add','src/safe.py'],check=True,capture_output=True)
    assert host.source_files(tmp_path)==[tmp_path/'src/safe.py']


def test_generated_fresh_api_accepts_first_task(tmp_path):
    from cloudworkbench.environments import EnvironmentRegistry
    from cloudworkbench.store import Store
    from cloudworkbench.api import create_app
    from fastapi.testclient import TestClient
    ids={'job':4001,'job_group':4002,'worker':4003,'service_group':4004,'docker_group':4005}
    image='sha256:'+'b'*64
    api,worker=host.configurations(image,ids,args())
    registry=EnvironmentRegistry(tmp_path/'registry.db')
    record=registry.register(host.environment_manifest(api,image))
    api,worker=host.configurations(image,ids,args(),record['manifest_sha256'])
    api['environment_registry']=str(tmp_path/'registry.db')
    store=Store(tmp_path/'state.db')
    store.add_client('synthetic-caller','a'*40,['submit','observe'],[host.PROJECT])
    client=TestClient(create_app(store,api),base_url=api['dashboard_origin'])
    response=client.post('/v1/sessions',json={'project_id':host.PROJECT,'environment_version':host.VERSION,
        'agent':'hermes','model':'grok-4.6','goal':'Synthetic installer policy check'},
        headers={'Authorization':'Bearer '+'a'*40,'Idempotency-Key':'synthetic-first-task'})
    assert response.status_code==201,response.text
    response=client.post('/auth/session',headers={'Origin':api['dashboard_origin'],'Authorization':'Bearer '+'a'*40})
    assert response.status_code==200,response.text
    choices=client.get('/auth/config').json()['projects'][0]['environments_by_agent']['hermes']
    assert choices=={'versions':[host.VERSION],'default':host.VERSION}
    # A stale approval cannot advertise an unqualified image to the caller.
    api['projects'][host.PROJECT]['operator_approved_environments'][host.VERSION]='0'*64
    choices=client.get('/auth/config').json()['projects'][0]['environments_by_agent']['hermes']
    assert choices=={'versions':[],'default':None}


def test_resume_refuses_unrelated_tailscale_routes_or_funnel():
    value={'TCP':{'443':{'HTTPS':True}},'Web':{'worker.example.ts.net:443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:7780'}}}}}
    assert host.owned_serve_config(value,'https://worker.example.ts.net')
    value['Web']['worker.example.ts.net:443']['Handlers']['/other']={'Proxy':'http://127.0.0.1:9999'}
    assert not host.owned_serve_config(value,'https://worker.example.ts.net')
    del value['Web']['worker.example.ts.net:443']['Handlers']['/other']
    value['AllowFunnel']={'worker.example.ts.net:443':True}
    assert not host.owned_serve_config(value,'https://worker.example.ts.net')
