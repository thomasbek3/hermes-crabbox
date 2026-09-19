#!/usr/bin/env python3
"""Explicit operator activation of the normal Hermes API lane. Default: validate only.

Snapshot manifest.json pins files (exact MODULES names), expected_live_modules,
expected_configs {api,worker}, proof {file,sha256}, registry {file,sha256},
environment_version, grok_auth, schema_version=1. All source files live beneath
snapshot/src/cloudworkbench. Registry is an already qualified copy of the live
registry: old rows and active selections must remain byte-for-byte identical.
No provider call, image build, scheduler migration, or automatic rollback here.
"""
from __future__ import annotations

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request

MODULES = tuple(sorted(('runner.py','api.py','server.py','store.py','adapters.py',
    'hermes_coordinator_runtime.py','hermes_job_entrypoint.py','provider_leases.py',
    'resource_monitor.py','resource_samples.py','result_bundle.py')))
IMAGE='sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
NETWORK='hermes-coordinator-bridge-tools-none'
SECRETS=['grok-dedicated-oauth']
TARGET=Path('/opt/cloud-workbench/src/cloudworkbench')
CONFIGS={k:Path('/etc/cloud-workbench')/(k+'.json') for k in ('api','worker')}
PYTHON='/opt/cloud-workbench/.venv/bin/python'
SERVICES=('cloud-workbench-api','cloud-workbench-worker','cloudd')
PROVIDER_TABLES={'provider_schema_version','provider_accounts','provider_reservations',
    'provider_execution_grants','provider_request_leases','provider_dispatch'}

class ActivationError(Exception): pass

def require(value, code):
    if not value: raise ActivationError(code)

def digest(raw): return hashlib.sha256(raw).hexdigest()
def encoded(value): return (json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
def run(argv, timeout=30):
    p=subprocess.run(argv,capture_output=True,timeout=timeout)
    require(p.returncode==0,'operator_command_failed')
    return p.stdout.decode()

def protected(path, *, limit=4*1024**2, owner=0):
    path=Path(path)
    require(path.is_absolute() and path.resolve()==path,'noncanonical_protected_path')
    for parent in path.parents:
        info=parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022,
                'untrusted_protected_parent')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==owner
            and not info.st_mode&0o022 and info.st_size<=limit,'untrusted_protected_file')
        with os.fdopen(fd,'rb',closefd=False) as stream: raw=stream.read(limit+1)
        require(len(raw)<=limit,'protected_file_oversize')
        return raw
    finally:os.close(fd)

def validate_source_subset(package):
    require(package.is_dir() and not package.is_symlink(),'invalid_source_package')
    entries=list(package.iterdir())
    require({p.name for p in entries}==set(MODULES)
        and all(stat.S_ISREG(p.lstat().st_mode) and p.lstat().st_nlink==1 for p in entries),
        'source_subset_must_be_exact')

def staged(snapshot, name, expected, limit=4*1024**2):
    require(isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_.-]+',name)
        and name not in ('.','..'),'invalid_stage_name')
    raw=protected(snapshot/name,limit=limit)
    require(digest(raw)==expected,'staged_hash_mismatch')
    return raw

def validate_proof(proof, files):
    require(proof.get('schema_version')==1 and proof.get('host')=='archived-worker.invalid'
        and proof.get('uid')==959 and proof.get('image')==IMAGE and proof.get('result')=='passed',
        'passing_api_proof_required')
    for key in ('cleanup_confirmed','api_thread_stopped','services_unchanged','source_unchanged','observer_stopped'):
        require(proof.get(key) is True,'api_proof_incomplete')
    require(proof.get('production_services_modified') is False and proof.get('remaining_active_attempts')==[]
        and proof.get('remaining_container_ids')==[] and proof.get('runtime_observation_errors')==[], 'api_proof_cleanup_missing')
    require(all(proof.get('source_sha256',{}).get('cloudworkbench/'+name)==sha for name,sha in files.items()),
        'api_proof_source_mismatch')
    turns=proof.get('turns')
    require(isinstance(turns,list) and len(turns)==2,'two_turn_proof_required')
    for n,t in enumerate(turns,1):
        require(t.get('number')==n and t.get('state')=='completed' and t.get('outcome')=='verified'
            and re.fullmatch(r'[0-9]{8}_[0-9]{6}_[0-9a-f]{6}',str(t.get('native_session_id',''))),
            'native_turn_proof_invalid')
        require(t.get('independent')=={'accepted':True,'cases':14*n,
            'functions':['add'] if n==1 else ['add','subtract']},'independent_acceptance_missing')
    require(turns[0]['session_id']==turns[1]['session_id']
        and turns[0]['native_session_id']==turns[1]['native_session_id']
        and turns[0]['attempt_id']!=turns[1]['attempt_id'],'native_resume_not_proven')

def next_configs(configs, version, registry, auth):
    require(isinstance(version,str) and re.fullmatch(r'hermes-[a-z0-9][a-z0-9.-]{0,90}',version),
        'explicit_hermes_version_required')
    result=copy.deepcopy(configs)
    for cfg in result.values():
        require(cfg.get('hermes_enabled') is not True and 'hermes' not in cfg['agents'],'already_enabled')
        require(cfg['environment_registry']==configs['worker']['environment_registry'],'registry_config_mismatch')
        project=cfg['projects']['sample-web']
        require('claude' in project['allowed_agents'] and version not in project['environment_versions'],
            'existing_project_policy_mismatch')
        cfg['hermes_enabled']=True;cfg['agents'].append('hermes')
        cfg['environment_registry']=str(registry)
        project['allowed_agents'].append('hermes');project['models']['hermes']=['grok-4.6']
        project['environment_versions'].append(version)
    capabilities=result['api'].setdefault('capabilities',{})
    require(isinstance(capabilities,dict),'invalid_capabilities_map')
    capabilities['hermes']={'enabled':True,'authentication':'dedicated_grok_oauth',
        'native_resume':'supported','continuation':'native_session','live_delivery':'unsupported',
        'structured_events':'supported','readiness_reason':None}
    worker=result['worker'];runtime=worker['runtime']
    require({k:runtime.get(k) for k in ('cpus','memory_mib','pids','workspace_mib')}==
        {'cpus':1,'memory_mib':1024,'pids':128,'workspace_mib':256},'existing_resources_changed')
    require(runtime.get('uid')==958 and runtime.get('gid')==959,'existing_workspace_identity_changed')
    require('hermes_runtime' not in worker,'existing_hermes_runtime_refused')
    worker['qualified_images']=list(dict.fromkeys(worker.get('qualified_images',[runtime['image']])+[IMAGE]))
    worker['hermes_runtime']={'hermes_source_root':str(TARGET),'hermes_grok_auth':str(auth),
        'docker_socket':'/run/docker.sock','coordinator_uid':959,'coordinator_gid':960,
        'docker_gid':966,'tool_gid':1000,'tool_shared_gid':959,
        'environment_network_profile':NETWORK,'environment_secret_refs':SECRETS}
    return result

def database_snapshot(db):
    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    require(all(re.fullmatch(r'[a-z_]+',v) for v in tables),'unexpected_database_table')
    result={}
    for name in tables:
        h=hashlib.sha256()
        for row in db.execute('SELECT * FROM "'+name+'" ORDER BY rowid'):
            h.update(encoded(list(row)))
        result[name]=h.hexdigest()
    return result

def additive_schema(database, module_path):
    spec=importlib.util.spec_from_file_location('cloudworkbench._activation_provider_schema',module_path)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
        require(db.execute('SELECT version FROM schema_version').fetchall()==[(1,)],'production_schema_not_one')
        before=database_snapshot(db)
        ddl={r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        module.ensure_schema(db)
        after=database_snapshot(db)
        require(all(after.get(k)==v for k,v in before.items()),'additive_schema_changed_rows')
        require(set(after)-set(before)<=PROVIDER_TABLES,'unexpected_schema_extension')
        current={r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        require(all(current.get(k)==v for k,v in ddl.items()),'additive_schema_changed_ddl')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(),'database_foreign_key_error')
    return {'schema_version':1,'existing_rows_unchanged':True,'existing_ddl_unchanged':True,
        'added_tables':sorted(set(after)-set(before))}

def registry_check(old_path,new_path,version,project):
    from cloudworkbench.environments import EnvironmentRegistry,QualificationReceipt
    with sqlite3.connect(old_path.as_uri()+'?mode=ro',uri=True) as old, sqlite3.connect(new_path.as_uri()+'?mode=ro',uri=True) as new:
        for table in ('environments','qualification_attempts','active_environments'):
            oldrows=old.execute('SELECT * FROM '+table).fetchall();newrows=new.execute('SELECT * FROM '+table).fetchall()
            require(all(row in newrows for row in oldrows),'prior_registry_record_changed')
            if table=='active_environments':require(sorted(oldrows)==sorted(newrows),'active_environment_changed')
            if table=='qualification_attempts':
                require(all(row[1:3]==('sample-web',version) for row in newrows if row not in oldrows),
                    'qualification_scope_changed')
        extra=new.execute('SELECT project,version FROM environments').fetchall()
        prior=old.execute('SELECT project,version FROM environments').fetchall()
        require(set(extra)-set(prior)=={('sample-web',version)},'registry_scope_changed')
    record=EnvironmentRegistry(new_path,read_only=True).resolve('sample-web',version);m=record['manifest']
    require(m['image_digest']==IMAGE and m['network_profile']==NETWORK and m['secret_refs']==SECRETS
        and m['resources']=={'cpus':1.0,'memory_mib':1024,'pids':128,'workspace_mib':256},'hermes_manifest_policy_mismatch')
    require(not m['repositories'] and not m['startup_commands'] and not m['install_commands']
        and m['legacy_template_id']=='sample-web','hermes_template_policy_mismatch')
    q=record['qualification'];QualificationReceipt.model_validate({k:v for k,v in q.items() if k not in ('qualification_id','qualified_at')})
    require(all(q[k]==v for k,v in (('manifest_sha256',record['manifest_sha256']),
        ('image_digest',m['image_digest']),('os',m['os']),('architecture',m['architecture']),('cli_versions',m['cli_versions'])))
        and sorted(p['id'] for p in q['probes'])==sorted(p['id'] for p in m['readiness_probes'])
        and all(p['exit_code']==0 for p in q['probes']),'qualification_identity_mismatch')
    configured={c['id']:c for c in project.get('checks',[])}
    require(set(configured)=={c['id'] for c in m['checks']},'protected_checks_changed')
    for check in m['checks']:
        cfg=configured[check['id']]
        require(check['script_id']==cfg['id']
            and all(check[k]==cfg.get(k) for k in ('description','argv','script_name'))
            and check['timeout_seconds']==cfg.get('timeout',30)
            and check['script_sha256']==digest(protected(Path(cfg['script_source']),limit=1024**2)),
            'protected_check_binding_changed')
    return record

def atomic(path,raw,mode=0o600,gid=0):
    fd=os.open(str(path)+'.new',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as out:
        os.fchown(out.fileno(),0,gid);os.fchmod(out.fileno(),mode);out.write(raw);out.flush();os.fsync(out.fileno())
    os.replace(str(path)+'.new',path)
    fd=os.open(path.parent,os.O_DIRECTORY|os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def sqlite_backup(source,destination):
    require(not destination.exists(),'backup_already_exists')
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as original,sqlite3.connect(destination) as saved:original.backup(saved)
    destination.chmod(0o600)

def services():
    return {name:dict(line.split('=',1) for line in run(['systemctl','show',name,'--property=ActiveState,MainPID']).splitlines()) for name in SERVICES}

def idle(database,owner):
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        require(not db.execute("SELECT 1 FROM attempts WHERE state NOT IN ('completed','failed','cancelled','interrupted','paused') LIMIT 1").fetchone(),'production_attempts_not_idle')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_accounts'").fetchone():
            require(not db.execute('SELECT 1 FROM provider_accounts WHERE active_id IS NOT NULL LIMIT 1').fetchone(),'provider_reservation_active')
    ids=run(['/usr/bin/docker','ps','-aq','--no-trunc','--filter','label=io.cloudworkbench.owner='+owner]).splitlines()
    require(not ids,'owned_containers_present')

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--snapshot',type=Path,required=True);ap.add_argument('--manifest-sha256',required=True)
    ap.add_argument('--checkpoint',required=True);ap.add_argument('--execute',action='store_true')
    args=ap.parse_args(argv)
    require(os.geteuid()==0 and socket.gethostname()=='archived-worker.invalid','root_omarchy_required')
    require(re.fullmatch(r'hermes-[a-z0-9-]{1,64}',args.checkpoint),'invalid_checkpoint')
    snapshot=args.snapshot;raw=protected(snapshot/'manifest.json');require(digest(raw)==args.manifest_sha256,'manifest_hash_mismatch')
    m=json.loads(raw)
    require(set(m)=={'schema_version','files','expected_live_modules','expected_configs','proof','registry','environment_version','grok_auth'}
        and m['schema_version']==1 and set(m['files'])==set(MODULES),'invalid_activation_manifest')
    validate_source_subset(snapshot/'src/cloudworkbench')
    source={name:protected(snapshot/'src/cloudworkbench'/name) for name in MODULES}
    require(all(digest(raw)==m['files'][name] for name,raw in source.items()),'source_hash_mismatch')
    require(set(m['expected_configs'])==set(CONFIGS),'invalid_config_binding')
    config_raw={k:protected(p) for k,p in CONFIGS.items()}
    require(all(digest(v)==m['expected_configs'][k] for k,v in config_raw.items()),'live_config_drift')
    cfg={k:json.loads(v) for k,v in config_raw.items()};worker=cfg['worker']
    require(cfg['api']['database']==worker['database'],'database_config_mismatch')
    database=Path(worker['database']);owner=worker['runtime']['owner'];old_registry=Path(worker['environment_registry'])
    require(database==Path('/var/lib/cloud-workbench/control/state.db') and owner=='primary','production_scope_changed')
    require(set(MODULES)<=set(m['expected_live_modules']),'live_baseline_incomplete')
    def source_baseline():
        for name,expected in m['expected_live_modules'].items():
            require(re.fullmatch(r'[a-z_]+\.py',name),'invalid_live_module_name')
            p=TARGET/name
            require((not p.exists() and not p.is_symlink()) if expected is None else digest(protected(p))==expected,
                'live_source_drift')
    source_baseline()
    proof=json.loads(staged(snapshot,m['proof']['file'],m['proof']['sha256'],16*1024**2));validate_proof(proof,m['files'])
    registry_raw=staged(snapshot,m['registry']['file'],m['registry']['sha256'],64*1024**2)
    sys.dont_write_bytecode=True;sys.path.insert(0,str(TARGET.parent))
    import cloudworkbench
    cloudworkbench.__path__.insert(0,str(snapshot/'src/cloudworkbench'))
    record=registry_check(old_registry,snapshot/m['registry']['file'],m['environment_version'],worker['projects']['sample-web'])
    auth=Path(m['grok_auth']);info=auth.lstat()
    require(auth.is_absolute() and auth.resolve()==auth and stat.S_ISREG(info.st_mode) and info.st_nlink==1
        and info.st_uid==959 and stat.S_IMODE(info.st_mode)==0o600 and 0<info.st_size<=65536,'dedicated_auth_metadata_invalid')
    # Probe permission only; credential bytes never enter this process or a receipt.
    run(['/usr/bin/sudo','-n','-u','cloud-worker','/usr/bin/test','-r',str(auth)])
    require(json.loads(run(['/usr/bin/docker','image','inspect',IMAGE]))[0]['Id']==IMAGE,'image_identity_changed')
    registry_path=Path('/var/lib/cloud-workbench/environments')/(args.checkpoint+'.db')
    backup=Path('/var/lib/cloud-workbench/operator-backups')/args.checkpoint
    require(not backup.exists() and not registry_path.exists(),'checkpoint_already_exists')
    updated=next_configs(cfg,m['environment_version'],registry_path,auth)
    # Import only against the exact overlay, without constructing Store or Runner.
    probe="import sys; import cloudworkbench; cloudworkbench.__path__.insert(0,sys.argv[1]); from cloudworkbench import runner,server,api,store,hermes_job_entrypoint; print('ok')"
    require(run([PYTHON,'-B','-c',probe,str(snapshot/'src/cloudworkbench')]).strip()=='ok','overlay_import_failed')
    idle(database,owner);before=services();require(all(v['ActiveState']=='active' for v in before.values()),'services_not_active')
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        clients_before=digest(encoded(db.execute('SELECT * FROM clients ORDER BY id').fetchall()))
    receipt={'schema_version':1,'checkpoint':args.checkpoint,'host':socket.gethostname(),'execute':args.execute,
        'manifest_sha256':args.manifest_sha256,'script_sha256':digest(Path(__file__).read_bytes()),
        'source_sha256':m['files'],'proof_sha256':m['proof']['sha256'],'environment_sha256':record['manifest_sha256'],
        'started_at':time.time(),'services_before':before,'passed':False,'stage':'validated','backup_path':str(backup)}
    if not args.execute:
        print(json.dumps(receipt,indent=2));return 0
    backup.mkdir(mode=0o700)
    lock=None;stopped=[]
    def checkpoint():atomic(backup/'activation-receipt.json',encoded({**receipt,'stopped_services':list(stopped)}))
    try:
        checkpoint()
        for k,v in config_raw.items():atomic(backup/(k+'.json'),v)
        (backup/'modules').mkdir(mode=0o700)
        for name in MODULES:
            if (TARGET/name).exists():atomic(backup/'modules'/name,protected(TARGET/name))
        receipt['stage']='quiesce';checkpoint()
        run(['systemctl','stop',SERVICES[0]]);stopped.append(SERVICES[0]);idle(database,owner)
        run(['systemctl','stop',SERVICES[1]]);stopped.append(SERVICES[1]);idle(database,owner)
        fd=os.open(Path(worker['state_root'])/'runner.lock',os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
        lock=fd;fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        require(stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_nlink==1,'unsafe_runner_lock')
        source_baseline();require(all(digest(protected(p))==m['expected_configs'][k] for k,p in CONFIGS.items()),'config_changed_during_quiesce')
        sqlite_backup(database,backup/'state.db');sqlite_backup(old_registry,backup/'environments.db')
        # Revalidate the staged registry against the now-quiescent current registry.
        registry_check(old_registry,snapshot/m['registry']['file'],m['environment_version'],worker['projects']['sample-web'])
        require(digest(protected(snapshot/m['registry']['file'],limit=64*1024**2))==m['registry']['sha256'],'staged_registry_drift')
        receipt['stage']='additive_schema';checkpoint()
        receipt['schema']=additive_schema(database,snapshot/'src/cloudworkbench/provider_leases.py')
        receipt['stage']='publication';checkpoint()
        atomic(registry_path,registry_raw,0o640,960)
        for name,raw in source.items():atomic(TARGET/name,raw,0o644)
        for k,path in CONFIGS.items():atomic(path,encoded(updated[k]),0o640,960)
        require(all(digest(protected(TARGET/name))==sha for name,sha in m['files'].items()),'published_source_mismatch')
        os.close(lock);lock=None
        receipt['stage']='restart';checkpoint()
        for service in (SERVICES[1],SERVICES[0]):
            run(['systemctl','start',service]);stopped.remove(service)
        token=Path('/var/lib/cloud-workbench/control/client.token').read_text().strip()
        deadline=time.monotonic()+45;ready=None
        while time.monotonic()<deadline:
            try:
                req=urllib.request.Request('http://127.0.0.1:7780/v1/ready',headers={'Authorization':'Bearer '+token})
                with urllib.request.urlopen(req,timeout=2) as response:ready=json.load(response)
                if ready.get('ready') is True and {'claude','hermes'}<=set(ready.get('worker',{}).get('available_agents',[])):break
            except (OSError,ValueError):pass
            time.sleep(.5)
        require(ready and ready.get('ready') is True and {'claude','hermes'}<=set(ready.get('worker',{}).get('available_agents',[])),'post_start_readiness_failed')
        after=services();require(after['cloudd']==before['cloudd'],'legacy_service_changed')
        require(all(v['ActiveState']=='active' for v in after.values()),'service_not_active')
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
            require(digest(encoded(db.execute('SELECT * FROM clients ORDER BY id').fetchall()))==clients_before,'client_grants_changed')
        receipt.update(passed=True,stage='complete',services_after=after,readiness=ready,
            config_sha256_after={k:digest(protected(p)) for k,p in CONFIGS.items()},
            production_provider_calls=0,client_grants_changed=False)
    except Exception as exc:
        receipt['error']=str(exc) if type(exc) is ActivationError else type(exc).__name__
        receipt['recovery']='Inspect exact backup and live state; no automatic rollback or restart after uncertain publication.'
    finally:
        if lock is not None:os.close(lock)
        receipt['finished_at']=time.time();receipt['services_after']=services();checkpoint()
    print(json.dumps(receipt,indent=2));return 0 if receipt['passed'] else 1

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print(json.dumps({'preflight_failure':str(exc) if type(exc) is ActivationError else type(exc).__name__}));sys.exit(1)
