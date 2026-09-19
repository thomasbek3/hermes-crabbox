import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('auth_deploy',Path(__file__).parents[1]/'scripts/deploy-auth-checkpoint.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)


def test_upgrade_preserves_old_versions_clients_and_independent_gateway():
    project={name:{'environment_versions':[versions[0]],'allowed_agents':['claude']} for name,versions in deploy.VERSIONS.items()}
    config={'worker':{'projects':project,'runtime':{'image':deploy.OLD_IMAGE,'uid':958,'approved_mount_roots':['/approved']},'claude_token':'/private/path','environment_secret_refs':['claude-subscription']},
            'api':{'projects':copy.deepcopy(project),'agents':['claude'],'claude_enabled':True}}
    before=copy.deepcopy(config);new='sha256:'+'b'*64
    result=deploy.next_configs(config,new,Path('/registry'))
    assert config==before
    assert result['worker']['qualified_images']==[deploy.OLD_IMAGE,new]
    assert result['worker']['runtime']=={**before['worker']['runtime'],'image':new,'egress_image':deploy.OLD_IMAGE}
    assert result['worker']['claude_token']=='/private/path'
    assert result['worker']['environment_secret_refs']==['claude-subscription']
    for service in result.values():
        for project,versions in deploy.VERSIONS.items():assert service['projects'][project]['environment_versions']==list(versions)


def test_upgrade_preserves_explicit_gateway():
    projects={name:{'environment_versions':[versions[0]]} for name,versions in deploy.VERSIONS.items()}
    result=deploy.next_configs({'worker':{'projects':projects,'runtime':{'image':deploy.OLD_IMAGE,'egress_image':'gateway'}},'api':{'projects':projects}},'new',Path('/registry'))
    assert result['worker']['runtime']['egress_image']=='gateway'


def test_missing_previous_version_blocks_plan():
    projects={name:{'environment_versions':[]} for name in deploy.VERSIONS}
    with pytest.raises(ValueError,match='Prior environment'):
        deploy.next_configs({'worker':{'projects':projects,'runtime':{'image':deploy.OLD_IMAGE}},'api':{'projects':projects}},'new',Path('/registry'))


def test_snapshot_rejects_modified_sources_and_path_escape(tmp_path):
    required=('runner','api','store','server','adapters','entrypoint','credential_state','environments','retention','repositories','artifacts','runtime','egress')
    names=['src/cloudworkbench/'+name+'.py' for name in required]+['deploy/Dockerfile.runtime']
    hashes={}
    for name in names:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('test')
        hashes[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'files':hashes}))
    assert deploy.validate_snapshot(tmp_path)['files']==hashes
    (tmp_path/names[0]).write_text('changed')
    with pytest.raises(ValueError,match='Snapshot identity'):deploy.validate_snapshot(tmp_path)
    hashes={'../escape':'a'*64};manifest.write_text(json.dumps({'files':hashes}))
    with pytest.raises(ValueError,match='Invalid snapshot path'):deploy.validate_snapshot(tmp_path)


@pytest.mark.parametrize('role',['job','egress'])
def test_live_inventory_includes_jobs_and_proxies(monkeypatch,role):
    identity='a'*64
    commands=[]
    def run(command,**kwargs):
        commands.append(command)
        if 'ls' in command:return identity
        return json.dumps([{'Id':identity,'Config':{'Labels':{'io.cloudworkbench.managed':'true','io.cloudworkbench.owner':'primary','io.cloudworkbench.role':role}},'State':{'Status':'running'}}])
    monkeypatch.setattr(deploy,'run',run)
    assert deploy.owned_live_containers('primary')==[{'id':identity,'role':role,'state':'running'}]
    assert 'label=io.cloudworkbench.owner=primary' in commands[0]
    assert not any('stop' in command or 'rm' in command for command in commands)


def test_inventory_failure_propagates_without_implying_quiescence(monkeypatch):
    def failed(command,**kwargs):raise OSError('Docker unavailable')
    monkeypatch.setattr(deploy,'run',failed)
    with pytest.raises(OSError):deploy.owned_live_containers('primary')


def test_created_orphan_is_not_considered_quiescent(monkeypatch):
    identity='b'*64
    def run(command,**kwargs):
        if 'ls' in command:return identity
        return json.dumps([{'Id':identity,'Config':{'Labels':{'io.cloudworkbench.managed':'true','io.cloudworkbench.owner':'primary','io.cloudworkbench.role':'job'}},'State':{'Status':'created'}}])
    monkeypatch.setattr(deploy,'run',run)
    assert deploy.owned_live_containers('primary')[0]['state']=='created'


def test_gateway_uses_actual_pinned_image_and_cleans_probe(monkeypatch):
    commands=[];digest='d'*64;image='sha256:'+'e'*64
    def run(command,**kwargs):
        commands.append(command)
        return digest if 'run' in command else ''
    monkeypatch.setattr(deploy,'run',run)
    proof=deploy.gateway_provenance(image,digest)
    assert proof['image_digest']==image and proof['egress_sha256']==digest
    assert image in commands[0] and '--network=none' in commands[0] and '--read-only' in commands[0]
    assert commands[-1][:4]==['/usr/bin/docker','rm','--force',commands[0][commands[0].index('--name')+1]]
    with pytest.raises(ValueError,match='Pinned gateway source'):
        deploy.gateway_provenance(image,'f'*64)


def test_unauthorized_readiness_records_http_status_without_secret(monkeypatch):
    from urllib.error import HTTPError
    def fail(request,**kwargs):raise HTTPError(request.full_url,401,'unauthorized',{},None)
    monkeypatch.setattr(deploy.urllib.request,'urlopen',fail)
    ready,error=deploy.wait_readiness('SYNTHETIC-PRIVATE-TOKEN',pause=0)
    assert ready is None and error=='http_401'


def test_failed_readiness_can_report_services_still_active(monkeypatch):
    class Result:
        stdout='active\n'
    monkeypatch.setattr(deploy.subprocess,'run',lambda *args,**kwargs:Result())
    assert deploy.service_states()=={name:'active' for name in deploy.SERVICES}


def test_credential_preflight_does_not_initialize_state(tmp_path,monkeypatch):
    import subprocess,sys,os
    root=tmp_path/'state';root.mkdir(mode=0o700)
    token=tmp_path/'token';token.write_text('sk-ant-oat01-SYNTHETIC-VALID-PREFIX-ONLY');token.chmod(0o600)
    config=tmp_path/'worker.json';config.write_text(json.dumps({'state_root':str(root),'claude_token':str(token)}))
    def run(command,**kwargs):
        program=command[-1].replace('/etc/cloud-workbench/worker.json',str(config))
        return subprocess.run([sys.executable,'-c',program],check=True,capture_output=True,text=True).stdout.strip()
    monkeypatch.setattr(deploy,'run',run)
    result=deploy.credential_probe(Path('/unused-source'))
    assert result['credential_readable'] and result['state_root_accessible']
    assert result['initialized'] is False and not (root/'credential-state').exists()
    result=deploy.credential_probe(Path('/unused-source'),initialize=True)
    assert result['initialized'] and (root/'credential-state'/'claude.json').is_file()
    # A subsequent preflight reads valid state rather than replacing it.
    before=(root/'credential-state'/'claude.json').read_bytes()
    assert deploy.credential_probe(Path('/unused-source'))['existing_state_valid']
    assert (root/'credential-state'/'claude.json').read_bytes()==before


def test_local_build_reference_is_checked_against_exact_image_id(monkeypatch):
    image='sha256:'+'a'*64;commands=[]
    def run(command,**kwargs):commands.append(command);return json.dumps([{'Id':image}])
    monkeypatch.setattr(deploy,'run',run)
    assert deploy.verified_base_reference(image)==deploy.BASE_IMAGE_REF
    assert commands[0]==['/usr/bin/docker','image','inspect',deploy.BASE_IMAGE_REF]
    with pytest.raises(ValueError,match='base tag identity changed'):
        deploy.verified_base_reference('sha256:'+'b'*64)


def test_qualified_resume_preserves_image_registry_and_source_boundaries():
    prior={'host':'omarchy','passed':False,'execute':True,'stage':'credential_state',
           'services_started':[],'stopped_services':[],'checkpoint':'test','base_image_id':'base',
           'old_image_id':'old','config_sha256_before':{'worker':'w','api':'a'},'legacy_pid_before':'1',
           'new_image_id':'sha256:'+'a'*64,
           'source_sha256':{'src/cloudworkbench/'+name+'.py':'original' for name in ('runner','credential_state','entrypoint')},
           'qualified_environments':{project:{'qualified':True,'manifest':{'image_digest':'sha256:'+'a'*64}} for project in deploy.VERSIONS}}
    receipt=copy.deepcopy(prior)
    receipt['source_sha256']['src/cloudworkbench/runner.py']='changed-host-only'
    receipt['source_sha256']['src/cloudworkbench/credential_state.py']='changed-host-only'
    manifest={'files':{'deploy/Dockerfile.runtime':'69fea7320d22992c8ca20ed9f5273bcc44948b02f9ec2c2dfe175123da0686a3'}}
    class Registry:
        def resolve(self,project,version):
            return prior['qualified_environments'][project] if version.endswith('v2') else {'old':project}
        def active(self,project):return prior['qualified_environments'][project]
    registry=Registry();image=prior['new_image_id']
    assert deploy.validate_qualified_resume(prior,receipt,manifest,registry,registry,image)[0]==image
    for key,value in [('stage','publish'),('services_started',['cloud-workbench-api']),('checkpoint','different')]:
        bad=copy.deepcopy(prior);bad[key]=value
        with pytest.raises(ValueError):deploy.validate_qualified_resume(bad,receipt,manifest,registry,registry,image)
    receipt['source_sha256']['src/cloudworkbench/entrypoint.py']='different-image-source'
    with pytest.raises(ValueError,match='image/source'):deploy.validate_qualified_resume(prior,receipt,manifest,registry,registry,image)
    receipt=copy.deepcopy(prior)
    class Drift(Registry):
        def active(self,project):return {'drift':True}
    with pytest.raises(ValueError,match='registry drift'):deploy.validate_qualified_resume(prior,receipt,manifest,Drift(),registry,image)
