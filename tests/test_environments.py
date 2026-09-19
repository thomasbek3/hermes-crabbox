import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess

import pytest
from pydantic import ValidationError

from cloudworkbench.environments import EnvironmentError, EnvironmentRegistry, Manifest, docker_qualifier, main

IMAGE='sha256:'+'1'*64

@pytest.fixture
def manifest():
    return {'project_id':'demo','version':'v1','architecture':'amd64','base_image_digest':IMAGE,
            'image_digest':IMAGE,'cli_versions':{'claude':'2.1.274'},
            'readiness_probes':[{'id':'python','argv':['python3','--version']}],
            'checks':[{'id':'booking','description':'Calendar dates are validated.',
                       'argv':['python3','/run/task/check.py'],'script_id':'booking','script_sha256':'a'*64}]}

@pytest.fixture
def registry(tmp_path):
    return EnvironmentRegistry(tmp_path/'registry.db')


def passed(record):
    m=record['manifest']
    return {'manifest_sha256':record['manifest_sha256'],'image_digest':m['image_digest'],
            'os':m['os'],'architecture':m['architecture'],'source':'trusted_builder','cli_versions':m['cli_versions'],
            'probes':[{'id':p['id'],'exit_code':0} for p in m['readiness_probes']]}


def ready(registry,manifest):
    registry.register(manifest)
    return registry.qualify(manifest['project_id'],manifest['version'],passed)


def test_immutable_version_idempotent_and_detached(registry,manifest):
    first=registry.register(manifest)
    assert registry.register(manifest)==first
    first['manifest']['checks'][0]['argv'][0]='evil'
    assert registry.get('demo','v1')['manifest']['checks'][0]['argv'][0]=='python3'
    manifest['checks'][0]['description']='Different acceptance'
    with pytest.raises(EnvironmentError,match='immutable'):
        registry.register(manifest)


def test_failed_candidate_preserves_active_and_prior_resolution(registry,manifest):
    original=ready(registry,manifest)
    registry.activate('demo','v1',expected_active=None)
    v2={**manifest,'version':'v2','image_digest':'sha256:'+'2'*64}
    registry.register(v2)
    def failed_build(record):
        raise RuntimeError('private build diagnostic TOKEN')
    with pytest.raises(EnvironmentError,match='active environment unchanged') as error:
        registry.qualify('demo','v2',failed_build)
    assert 'TOKEN' not in str(error.value)
    assert registry.active('demo')==original
    with pytest.raises(EnvironmentError,match='not qualified'):
        registry.activate('demo','v2',expected_active='v1')
    with registry.db() as db:
        row=db.execute('SELECT status,error_code FROM qualification_attempts WHERE version=?',('v2',)).fetchone()
    assert dict(row)=={'status':'failed','error_code':'qualification_failed'}


def test_concurrent_activation_compare_and_swap(registry,manifest):
    ready(registry,manifest);registry.activate('demo','v1',expected_active=None)
    for version in ('v2','v3'):
        ready(registry,{**manifest,'version':version})
    def promote(version):
        try:
            registry.activate('demo',version,expected_active='v1')
            return version
        except EnvironmentError:
            return None
    with ThreadPoolExecutor(2) as pool:
        winners=[value for value in pool.map(promote,['v2','v3']) if value]
    assert len(winners)==1
    assert registry.active('demo')['manifest']['version']==winners[0]
    # Previously queued attempts keep this exact immutable version/snapshot.
    assert registry.resolve('demo','v1')['manifest']['version']=='v1'


@pytest.mark.parametrize('change',[
    {'manifest_sha256':'a'*64}, {'image_digest':'sha256:'+'2'*64}, {'architecture':'arm64'},
    {'probes':[{'id':'wrong','exit_code':0}]}, {'probes':[{'id':'python','exit_code':1}]},
    {'probes':[{'id':'python','exit_code':0}]*2}, {'extra':'ignored?'}, {'cli_versions':{'claude':'wrong'}}])
def test_false_or_mismatched_qualification_rejected(registry,manifest,change):
    registry.register(manifest)
    with pytest.raises(EnvironmentError):
        registry.qualify('demo','v1',lambda record:{**passed(record),**change})
    assert not registry.get('demo','v1')['qualified']
    assert registry.active('demo') is None


def test_qualification_callback_cannot_mutate_snapshot(registry,manifest):
    expected=registry.register(manifest)
    def builder(record):
        receipt=passed(record)
        record['manifest']['checks'].clear()
        return receipt
    registry.qualify('demo','v1',builder)
    assert registry.resolve('demo','v1')['manifest']==expected['manifest']


@pytest.mark.parametrize('change',[
    {'image_digest':'latest'}, {'os':'darwin'}, {'architecture':'unknown'},
    {'skills':['unreviewed']}, {'mcp':['personal']}, {'secret_refs':['/home/token']},
    {'package_lockfiles':{'../lock':'a'*64}}, {'expected_deliverables':['/etc/passwd']},
    {'repositories':[{'repository_id':'repo','commit':'main','destination':'repo'}]},
    {'repositories':[{'repository_id':'repo','commit':'a'*40,'destination':'../repo'}]},
    {'readiness_probes':[{'id':'test','argv':['python3','a\x00b']}]},
    {'checks':[{'id':'check','description':'real','argv':['test'],'script_id':'check'}]},
])
def test_manifest_rejects_unpinned_or_unbounded_data(manifest,change):
    with pytest.raises((ValidationError,ValueError)):
        Manifest.model_validate({**manifest,**change})


def test_manifest_rejects_overlapping_repos(manifest):
    manifest['repositories']=[{'repository_id':'first','commit':'a'*40,'destination':'repo'},
                              {'repository_id':'second','commit':'b'*40,'destination':'repo/nested'}]
    with pytest.raises(ValidationError,match='overlap'):
        Manifest.model_validate(manifest)


def test_database_manifest_tampering_fails_closed(registry,manifest):
    ready(registry,manifest)
    with registry.db() as db:
        value=registry.get('demo','v1')['manifest'];value['checks'].clear()
        db.execute('UPDATE environments SET manifest=?',(json.dumps(value),))
    with pytest.raises(EnvironmentError,match='integrity'):
        registry.resolve('demo','v1')


def test_docker_qualifier_enforces_bounds_no_mounts_no_inherited_entrypoint(registry,manifest,monkeypatch):
    record=registry.register(manifest);calls=[]
    def run(argv,**kwargs):
        calls.append((argv,kwargs))
        return subprocess.CompletedProcess(argv,0,IMAGE+' linux amd64\n')
    monkeypatch.setattr(subprocess,'run',run)
    receipt=docker_qualifier(record)
    assert receipt['source']=='docker_image_readiness'
    command,options=calls[3]
    for flag in ('--network','--read-only','--user','--cap-drop','--security-opt','--pids-limit','--memory','--memory-swap','--cpus','--log-driver','--entrypoint'):
        assert flag in command
    assert command[-3:]==['python3',IMAGE,'--version']
    assert '--mount' not in command and '-v' not in command
    assert options['stdout']==subprocess.DEVNULL and options['timeout']==30
    assert calls[-1][0][1:3]==['rm','--force']


def test_docker_timeout_cleans_probe_without_leaking_output(registry,manifest,monkeypatch):
    record=registry.register(manifest);calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='run':
            raise subprocess.TimeoutExpired(argv,1,output='SECRET')
        return subprocess.CompletedProcess(argv,0,IMAGE+' linux amd64\n')
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(EnvironmentError) as error:
        registry.qualify('demo','v1',docker_qualifier)
    assert calls[-1][1:3]==['rm','--force']
    assert 'SECRET' not in str(error.value)
    assert not registry.get('demo','v1')['qualified']


def test_docker_cleanup_uncertainty_never_qualifies(registry,manifest,monkeypatch):
    registry.register(manifest)
    def run(argv,**kwargs):
        return subprocess.CompletedProcess(argv,1 if argv[1]=='rm' else 0,IMAGE+' linux amd64\n')
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(EnvironmentError):
        registry.qualify('demo','v1',docker_qualifier)
    assert not registry.get('demo','v1')['qualified']


def test_cli_registration_does_not_activate(tmp_path,manifest,capsys):
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest));db=tmp_path/'registry.db'
    assert main(['--registry',str(db),'register',str(path)])==0
    assert EnvironmentRegistry(db).active('demo') is None
    assert main(['--registry',str(db),'activate','demo','v1','--expected-active','NONE'])==1
    assert 'failed' in capsys.readouterr().err


def test_legacy_import_pins_protected_checks_without_host_or_secret_paths(tmp_path):
    from cloudworkbench.environments import legacy_manifest
    script=tmp_path/'check.py';script.write_text('assert True\n')
    project={'environment_versions':['demo-v1'],'template':'/operator/demo',
             'checks':[{'id':'booking','description':'Valid calendar dates',
                        'argv':['python3','/run/task/check.py'],'script_source':str(script),'timeout':20}],
             'token':'private-value'}
    value=legacy_manifest('sample-web','demo-v1',project,{'image':IMAGE,'network_enabled':False},
                          architecture='amd64',cli_versions={'claude':'2.1.274'},
                          readiness_probes=[{'id':'python','argv':['python3','--version']}])
    assert value['legacy_template_id']=='sample-web'
    assert value['checks'][0]['script_sha256']
    assert value['checks'][0]['timeout_seconds']==20
    encoded=json.dumps(value)
    assert str(tmp_path) not in encoded and '/operator/demo' not in encoded and 'private-value' not in encoded


def test_pinned_cli_failure_prevents_qualification(registry,manifest,monkeypatch):
    registry.register(manifest)
    def run(argv,**kwargs):
        return subprocess.CompletedProcess(argv,1 if argv[1]=='run' else 0,IMAGE+' linux amd64\n')
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(EnvironmentError):
        registry.qualify('demo','v1',docker_qualifier)
    assert not registry.get('demo','v1')['qualified']


def test_api_read_only_registry_resolves_without_write_privileges(registry,manifest):
    ready(registry,manifest)
    reader=EnvironmentRegistry(registry.path,read_only=True)
    assert reader.resolve('demo','v1')['qualified']
    with pytest.raises(EnvironmentError,match='read-only'):
        reader.activate('demo','v1',expected_active=None)
    with pytest.raises(EnvironmentError,match='read-only'):
        reader.register({**manifest,'version':'v2'})
    with pytest.raises(EnvironmentError,match='unavailable'):
        EnvironmentRegistry(registry.path.parent/'missing.db',read_only=True)


def test_probe_platform_mismatch_never_starts_candidate(registry,manifest,monkeypatch):
    record=registry.register(manifest);calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv,0,IMAGE+' linux arm64\n')
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(EnvironmentError,match='platform mismatch'):
        docker_qualifier(record)
    assert len(calls)==1 and calls[0][1:3]==['image','inspect']


def test_resource_defaults_and_legacy_import_match_real_runtime(tmp_path):
    from cloudworkbench.environments import Resources,legacy_manifest
    from cloudworkbench.runtime import Runtime
    config={'image':IMAGE,'root':str(tmp_path.resolve())}
    runtime=Runtime(config)
    actual={key:getattr(runtime,key) for key in ('cpus','memory_mib','pids','workspace_mib')}
    assert Resources().model_dump()==actual
    imported=legacy_manifest('demo','v1',{'environment_versions':['v1']},config,
                             architecture='amd64',cli_versions={'claude':'2.1.274'},
                             readiness_probes=[{'id':'python','argv':['python3','--version']}])
    assert imported['resources']==actual


@pytest.mark.parametrize('failure,expected',[
    ('inspect_missing','image_inspection_failed'),('inspect_timeout','probe_timeout'),
    ('inspect_unavailable','docker_unavailable'),('probe_timeout','probe_timeout'),
    ('cli_mismatch','cli_version_mismatch'),('readiness_failed','readiness_failed'),
    ('cleanup_timeout','probe_cleanup_failed'),('cleanup_failed','probe_cleanup_failed')])
def test_qualification_diagnostics_are_bounded_durable_and_visible(registry,manifest,monkeypatch,failure,expected):
    from cloudworkbench.environments import QUALIFICATION_ERRORS
    registry.register(manifest)
    def run(argv,**kwargs):
        operation=argv[1]
        if operation=='image':
            if failure=='inspect_missing':
                raise subprocess.CalledProcessError(1,argv,stderr='SECRET docker details')
            if failure=='inspect_timeout':
                raise subprocess.TimeoutExpired(argv,30,stderr='SECRET timeout details')
            if failure=='inspect_unavailable':
                raise FileNotFoundError('SECRET path')
        if operation=='run':
            if failure=='probe_timeout':
                raise subprocess.TimeoutExpired(argv,30,stderr='SECRET provider details')
            if failure=='cli_mismatch' or (failure=='readiness_failed' and argv[-1]=='--version'):
                return subprocess.CompletedProcess(argv,7)
        if operation=='rm':
            if failure=='cleanup_timeout':
                raise subprocess.TimeoutExpired(argv,20)
            if failure=='cleanup_failed':
                return subprocess.CompletedProcess(argv,1)
        return subprocess.CompletedProcess(argv,0,IMAGE+' linux amd64\n')
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(EnvironmentError) as error:
        registry.qualify('demo','v1',docker_qualifier)
    assert error.value.code==expected and expected in QUALIFICATION_ERRORS
    record=registry.get('demo','v1',include_diagnostic=True)
    assert record['latest_qualification']['error_code']==expected
    assert record['latest_qualification']['status']=='failed'
    assert not record['qualified']
    assert 'SECRET' not in json.dumps(record) and 'SECRET' not in str(error.value)
    assert 'latest_qualification' not in registry.get('demo','v1')


def test_unknown_error_category_cannot_leak_secret(registry,manifest):
    registry.register(manifest)
    def fail(record):
        raise EnvironmentError('SECRET message',code='SECRET code')
    with pytest.raises(EnvironmentError) as error:
        registry.qualify('demo','v1',fail)
    assert error.value.code=='qualification_failed'
    assert 'SECRET' not in str(error.value)
    assert registry.get('demo','v1',include_diagnostic=True)['latest_qualification']['error_code']=='qualification_failed'
