import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest
from cloudworkbench import provider_leases

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('hermes_activation',ROOT/'scripts/activate-hermes-api.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


def configs():
    value={'agents':['claude'],'claude_enabled':True,'environment_registry':'/old.db',
      'environment_network_profile':'claude-only','environment_secret_refs':['claude-subscription'],
      'projects':{'sample-web':{'allowed_agents':['fixture','claude'],'models':{'claude':[]},
        'environment_versions':['demo-v1','demo-v2'],'template':'/demo'},'other':{'unchanged':True}},
      'runtime':{'cpus':1,'memory_mib':1024,'pids':128,'workspace_mib':256,'uid':958,'gid':959,
        'image':'sha256:'+'1'*64,'network_enabled':True},'qualified_images':['sha256:'+'1'*64]}
    return {'api':copy.deepcopy(value),'worker':copy.deepcopy(value)}


def test_config_is_additive_and_preserves_claude():
    old=configs();before=copy.deepcopy(old)
    new=a.next_configs(old,'hermes-grok-v1',Path('/new.db'),Path('/auth.json'))
    assert old==before
    assert new['worker']['runtime']==old['worker']['runtime']
    for name in new:
        assert new[name]['projects']['other']==old[name]['projects']['other']
        assert new[name]['environment_network_profile']=='claude-only'
        assert new[name]['environment_secret_refs']==['claude-subscription']
        assert new[name]['projects']['sample-web']['environment_versions']==['demo-v1','demo-v2','hermes-grok-v1']
        assert new[name]['projects']['sample-web']['models']['claude']==[]
    assert new['worker']['hermes_runtime']['environment_network_profile']==a.NETWORK
    assert new['worker']['runtime']['image']!=a.IMAGE
    assert new['api']['capabilities']['hermes']['native_resume']=='supported'


@pytest.mark.parametrize('key,value',[('memory_mib',1536),('uid',959),('gid',1000)])
def test_refuses_changed_global_policy(key,value):
    old=configs();old['worker']['runtime'][key]=value
    with pytest.raises(a.ActivationError):a.next_configs(old,'hermes-grok-v1',Path('/new.db'),Path('/auth.json'))


def proof():return json.loads((ROOT/'evidence/hermes-api-live-passed/receipt.json').read_text())

def files(p):return {name:p['source_sha256']['cloudworkbench/'+name] for name in a.MODULES}

def test_actual_two_turn_proof_accepted():
    p=proof();a.validate_proof(p,files(p))


@pytest.mark.parametrize('change',['source','resume','cleanup','verifier','count','observer','uid'])
def test_proof_gate_refuses_incomplete_or_substituted(change):
    p=proof();source=files(p)
    if change=='source':source['runner.py']='0'*64
    if change=='resume':p['turns'][1]['native_session_id']='20260918_151018_000000'
    if change=='cleanup':p['remaining_container_ids']=['a'*64]
    if change=='verifier':p['turns'][1]['outcome']='unverified'
    if change=='count':p['turns'][0]['independent']['cases']=13
    if change=='observer':p['runtime_observation_errors']=['failed']
    if change=='uid':p['uid']=0
    with pytest.raises(a.ActivationError):a.validate_proof(p,source)


def old_db(tmp_path):
    path=tmp_path/'state.db'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE schema_version(version INTEGER PRIMARY KEY);INSERT INTO schema_version VALUES(1);
        CREATE TABLE attempts(id TEXT PRIMARY KEY, agent TEXT, state TEXT);INSERT INTO attempts VALUES('old','claude','completed');
        CREATE TABLE clients(id TEXT PRIMARY KEY, scopes TEXT);INSERT INTO clients VALUES('owner','private');''')
    return path


def test_additive_schema_preserves_old_rows_and_version_and_is_idempotent(tmp_path):
    path=old_db(tmp_path)
    first=a.additive_schema(path,ROOT/'src/cloudworkbench/provider_leases.py')
    assert set(first['added_tables'])==a.PROVIDER_TABLES
    second=a.additive_schema(path,ROOT/'src/cloudworkbench/provider_leases.py')
    assert second['added_tables']==[]
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM attempts').fetchall()==[('old','claude','completed')]
        assert db.execute('SELECT * FROM schema_version').fetchall()==[(1,)]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'workflow_%'").fetchall()


def test_additive_schema_rolls_back_incompatible_partial_schema(tmp_path):
    path=old_db(tmp_path)
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE provider_accounts(unexpected TEXT)')
    with pytest.raises(Exception):a.additive_schema(path,ROOT/'src/cloudworkbench/provider_leases.py')
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='provider_schema_version'").fetchone()
        assert db.execute('SELECT * FROM attempts').fetchall()==[('old','claude','completed')]


def test_existing_newer_schema_refused_without_modification(tmp_path):
    path=old_db(tmp_path)
    with sqlite3.connect(path) as db:db.execute('UPDATE schema_version SET version=3')
    with pytest.raises(a.ActivationError,match='production_schema_not_one'):
        a.additive_schema(path,ROOT/'src/cloudworkbench/provider_leases.py')
    with sqlite3.connect(path) as db:assert db.execute('SELECT version FROM schema_version').fetchall()==[(3,)]


def test_registry_preserves_old_rows_and_active_selection(tmp_path,monkeypatch):
    from cloudworkbench.environments import EnvironmentRegistry
    old=tmp_path/'old.db';new=tmp_path/'new.db';EnvironmentRegistry(old)
    with sqlite3.connect(old) as db:
        db.execute("INSERT INTO active_environments VALUES('sample-web','demo-v2','old','stamp')")
        db.execute("INSERT INTO environments VALUES('sample-web','demo-v2','old','{}','stamp',NULL)")
    a.sqlite_backup(old,new)
    with sqlite3.connect(new) as db:db.execute("INSERT INTO environments VALUES('sample-web','hermes-grok-v1','new','{}','stamp','{}')")
    record={'manifest':{'image_digest':a.IMAGE,'network_profile':a.NETWORK,'secret_refs':a.SECRETS,
        'resources':{'cpus':1,'memory_mib':1024,'pids':128,'workspace_mib':256},'repositories':[],
        'startup_commands':[],'install_commands':[],'legacy_template_id':'sample-web','checks':[],
        'os':'linux','architecture':'amd64','cli_versions':{'hermes':'0.21.3'},'readiness_probes':[{'id':'hermes'}]},
        'manifest_sha256':'a'*64,'qualification':{'manifest_sha256':'a'*64,'image_digest':a.IMAGE,
        'os':'linux','architecture':'amd64','cli_versions':{'hermes':'0.21.3'},
        'source':'docker_image_readiness','probes':[{'id':'hermes','exit_code':0}]}}
    monkeypatch.setattr(EnvironmentRegistry,'resolve',lambda *args:record)
    assert a.registry_check(old,new,'hermes-grok-v1',{'checks':[]})==record
    with sqlite3.connect(new) as db:db.execute("UPDATE active_environments SET version='hermes-grok-v1'")
    with pytest.raises(a.ActivationError):a.registry_check(old,new,'hermes-grok-v1',{'checks':[]})


def test_existing_other_capabilities_unchanged():
    cfg=configs();cfg['api']['capabilities']={'claude':{'enabled':True,'native_resume':'unsupported'},'codex':{'enabled':False}}
    new=a.next_configs(cfg,'hermes-grok-v1',Path('/new.db'),Path('/auth.json'))
    assert new['api']['capabilities']['claude']==cfg['api']['capabilities']['claude']
    assert new['api']['capabilities']['codex']==cfg['api']['capabilities']['codex']


def test_live_check_hash_is_derived_from_protected_source(tmp_path,monkeypatch):
    # Real registry schema/receipt; live configuration intentionally has no script_sha256.
    from cloudworkbench.environments import EnvironmentRegistry
    old=tmp_path/'old.db';new=tmp_path/'new.db';EnvironmentRegistry(old);a.sqlite_backup(old,new)
    raw=b'print("protected")\n';script=tmp_path/'check.py';script.write_bytes(raw)
    monkeypatch.setattr(a,'protected',lambda path,**kwargs:Path(path).read_bytes())
    project={'checks':[{'id':'check','description':'Protected check','argv':['python3','/run/task/check.py'],
        'script_source':str(script),'script_name':'check.py','timeout':20}]}
    manifest={'project_id':'sample-web','version':'hermes-grok-v1','architecture':'amd64',
        'base_image_digest':a.IMAGE,'image_digest':a.IMAGE,'cli_versions':{'hermes':'0.21.3'},
        'readiness_probes':[{'id':'hermes','argv':['python3','--version']}],
        'network_profile':a.NETWORK,'secret_refs':a.SECRETS,
        'resources':{'cpus':1,'memory_mib':1024,'pids':128,'workspace_mib':256},'legacy_template_id':'sample-web',
        'checks':[{'id':'check','description':'Protected check','argv':['python3','/run/task/check.py'],
            'script_id':'check','script_name':'check.py','script_sha256':a.digest(raw),'timeout_seconds':20}]}
    registry=EnvironmentRegistry(new);registry.register(manifest)
    def qualify(record):
        return {'manifest_sha256':record['manifest_sha256'],'image_digest':a.IMAGE,'os':'linux','architecture':'amd64',
            'cli_versions':{'hermes':'0.21.3'},'source':'trusted_builder','probes':[{'id':'hermes','exit_code':0}]}
    registry.qualify('sample-web','hermes-grok-v1',qualify)
    assert a.registry_check(old,new,'hermes-grok-v1',project)['qualified'] is True
    script.write_bytes(b'changed')
    with pytest.raises(a.ActivationError,match='protected_check_binding_changed'):
        a.registry_check(old,new,'hermes-grok-v1',project)


@pytest.mark.parametrize('extra',['other.py','__pycache__','symlink','hardlink'])
def test_exact_publication_closure_refuses_extra_or_replaced_material(tmp_path,extra):
    package=tmp_path/'cloudworkbench';package.mkdir()
    for name in a.MODULES:(package/name).write_text('# frozen\n')
    a.validate_source_subset(package)
    if extra=='__pycache__':(package/extra).mkdir()
    elif extra=='symlink':
        (package/a.MODULES[0]).unlink();(package/a.MODULES[0]).symlink_to(package/a.MODULES[1])
    elif extra=='hardlink':
        (package/a.MODULES[0]).unlink();(package/a.MODULES[0]).hardlink_to(package/a.MODULES[1])
    else:(package/extra).write_text('# hidden unpublished dependency\n')
    with pytest.raises(a.ActivationError,match='source_subset_must_be_exact'):a.validate_source_subset(package)
