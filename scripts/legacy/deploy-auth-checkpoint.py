#!/usr/bin/env python3
"""Operator-only, idle v2 deployment; default mode validates without mutations."""
from __future__ import annotations

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

import argparse
import copy
from datetime import datetime, timezone
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid

BASE_IMAGE_REF='cwb2-base-verified:b08595b4ff44'
OLD_IMAGE='sha256:408eb6e2b5c4cb58747fbb1fbac31031545135005079abf878012c33a6632331'
VERSIONS={'sample-web':('demo-v1','demo-v2'),'sample-document':('document-v1','document-v2'),'sample-repo':('repo-v1','repo-v2')}
TARGET=Path('/opt/cloud-workbench')
SERVICES=['cloud-workbench-api','cloud-workbench-worker']

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
def run(argv, *, timeout=60, **kwargs):
    return subprocess.run(argv,check=True,capture_output=True,text=True,timeout=timeout,**kwargs).stdout.strip()
def atomic(path,data,mode=0o644,gid=0):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.auth-stage')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as output:
        os.fchown(output.fileno(),0,gid);os.fchmod(output.fileno(),mode)
        output.write(data);output.flush();os.fsync(output.fileno())
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def validate_snapshot(source):
    manifest=json.loads((source/'manifest.json').read_text())
    files=manifest['files']
    for name,digest in files.items():
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or path.as_posix()!=name:
            raise ValueError('Invalid snapshot path')
        candidate=source/name
        if candidate.is_symlink() or not candidate.is_file() or sha(candidate)!=digest:
            raise ValueError('Snapshot identity mismatch')
    required={'src/cloudworkbench/'+name+'.py' for name in ('runner','api','store','server','adapters','entrypoint','credential_state','environments','retention','repositories','artifacts','runtime','egress')}
    if not required.issubset(files) or 'deploy/Dockerfile.runtime' not in files:
        raise ValueError('Incomplete source snapshot')
    return manifest

def owned_live_containers(owner):
    if not isinstance(owner,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}',owner):
        raise ValueError('Invalid runtime owner')
    ids=run(['/usr/bin/docker','container','ls','--all','--no-trunc',
        '--filter','label=io.cloudworkbench.managed=true',
        '--filter','label=io.cloudworkbench.owner='+owner,'--format','{{.ID}}']).splitlines()
    active=[]
    for identity in ids:
        if not re.fullmatch(r'[a-f0-9]{64}',identity):raise ValueError('Invalid Docker inventory')
        value=json.loads(run(['/usr/bin/docker','inspect',identity]))[0]
        labels=value['Config']['Labels']
        if value['Id']!=identity or labels.get('io.cloudworkbench.managed')!='true' or labels.get('io.cloudworkbench.owner')!=owner:
            raise ValueError('Container ownership drift')
        state=value['State']['Status']
        if state != 'exited':
            active.append({'id':identity,'role':labels.get('io.cloudworkbench.role'),'state':state})
    return active

def credential_probe(source, *, initialize=False):
    program = """import json,os
from pathlib import Path
from cloudworkbench.adapters import read_credential
from cloudworkbench.credential_state import CredentialState
c=json.loads(Path('/etc/cloud-workbench/worker.json').read_text())
root=Path(c['state_root'])
assert root.is_dir() and root.resolve()==root and os.access(root,os.W_OK|os.X_OK,effective_ids=True)
token,reason=read_credential(Path(c['claude_token']))
assert reason is None
state=CredentialState(root/'credential-state'/'claude.json',token)
assert state._startup_reason is None
if state.path.parent.exists():
    fd=state._parent();os.close(fd)
assert not state.path.is_symlink()
exists=state.path.exists()
if exists:
    state._load()
    assert state.blocked_reason(Path(c['claude_token'])) is None
elif INITIALIZE:
    state.initialize()
    assert state.blocked_reason(Path(c['claude_token'])) is None
print(json.dumps({'credential_readable':True,'state_root_accessible':True,'existing_state_valid':exists,
                  'initialized':INITIALIZE and not exists,'private_state_ready':exists or INITIALIZE}))
""".replace('INITIALIZE', repr(initialize))
    command=['/usr/bin/sudo','-n','-u','cloud-worker','/usr/bin/env',
             'PYTHONPATH='+str(source/'src'),str(TARGET/'.venv/bin/python'),'-c',program]
    return json.loads(run(command,cwd=TARGET))


def gateway_provenance(image, expected_digest):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):raise ValueError('Gateway must be an immutable local image ID')
    name='cwb-gateway-probe-'+uuid.uuid4().hex
    try:
        actual=run(['/usr/bin/docker','run','--name',name,'--pull=never','--network=none',
            '--read-only','--user','65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges',
            '--pids-limit=32','--memory=64m','--cpus=0.25','--log-driver=none','--entrypoint=python3',
            image,'-c','import hashlib; print(hashlib.sha256(open("/opt/cloudworkbench/egress.py","rb").read()).hexdigest())'],timeout=30)
    finally:
        # The unique named, secret-free probe is the only resource retired here.
        run(['/usr/bin/docker','rm','--force',name])
    if actual!=expected_digest:raise ValueError('Pinned gateway source differs from qualified deployment source')
    return {'image_digest':image,'egress_sha256':actual,'matches_frozen_source':True,'probe_network':'none','probe_had_secrets':False}


def wait_readiness(token, *, attempts=60, pause=.5):
    ready=None;error='readiness_timeout'
    for _ in range(attempts):
        try:
            request=urllib.request.Request('http://127.0.0.1:7780/v1/ready',headers={'Authorization':'Bearer '+token})
            with urllib.request.urlopen(request,timeout=2) as response:ready=json.load(response)
            if ready.get('ready') and ready.get('provider_enabled'):return ready,None
            error='worker_not_ready'
        except urllib.error.HTTPError as exc:
            error='http_'+str(exc.code)
            if exc.code in (401,403):break
        except (OSError,ValueError):error='readiness_unavailable'
        time.sleep(pause)
    return ready,error


def service_states():
    states={}
    for service in SERVICES:
        try:
            result=subprocess.run(['systemctl','is-active',service],capture_output=True,text=True,timeout=5)
            value=result.stdout.strip()
            states[service]=value if value in {'active','inactive','failed','activating','deactivating'} else 'unknown'
        except (OSError,subprocess.SubprocessError):states[service]='unknown'
    return states

def next_configs(config, new_image, registry_path):
    updated=copy.deepcopy(config)
    for value in updated.values():
        value['environment_registry']=str(registry_path)
        for project,(old,new) in VERSIONS.items():
            versions=value['projects'][project]['environment_versions']
            if old not in versions:raise ValueError('Prior environment admission missing')
            value['projects'][project]['environment_versions']=list(dict.fromkeys(versions+[new]))
    worker=updated['worker']
    worker['qualified_images']=list(dict.fromkeys(worker.get('qualified_images',[worker['runtime']['image']])+[OLD_IMAGE,new_image]))
    worker['runtime'].setdefault('egress_image',worker['runtime']['image'])
    worker['runtime']['image']=new_image
    return updated

def verified_base_reference(expected_image_id):
    inspected=json.loads(run(['/usr/bin/docker','image','inspect',BASE_IMAGE_REF]))
    if len(inspected)!=1 or inspected[0]['Id']!=expected_image_id:
        raise ValueError('Verified local base tag identity changed')
    return BASE_IMAGE_REF

def validate_qualified_resume(prior, receipt, manifest, registry, old_registry, image_id):
    if prior.get('host')!='archived-worker.invalid' or prior.get('passed') is not False or prior.get('execute') is not True or prior.get('stage')!='credential_state':
        raise ValueError('Resume requires the pre-publication credential initialization failure')
    if prior.get('services_started') or prior.get('stopped_services'):
        raise ValueError('Resume cannot cross a prior service mutation')
    for key in ('checkpoint','base_image_id','old_image_id','config_sha256_before','legacy_pid_before'):
        if prior.get(key)!=receipt[key]:raise ValueError('Resume identity drift')
    if image_id!=prior.get('new_image_id') or not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id):
        raise ValueError('Resume image drift')
    allowed={'src/cloudworkbench/runner.py','src/cloudworkbench/credential_state.py'}
    if set(prior['source_sha256'])!=set(receipt['source_sha256']):raise ValueError('Resume source inventory drift')
    for name,digest in prior['source_sha256'].items():
        if name not in allowed and receipt['source_sha256'][name]!=digest:raise ValueError('Resume image/source input drift')
    if manifest['files']['deploy/Dockerfile.runtime']!='69fea7320d22992c8ca20ed9f5273bcc44948b02f9ec2c2dfe175123da0686a3':
        raise ValueError('Resume Dockerfile drift')
    for project,(old,new) in VERSIONS.items():
        if registry.resolve(project,old)!=old_registry.resolve(project,old):raise ValueError('Resume prior manifest drift')
        expected=prior['qualified_environments'][project]
        if not expected['qualified'] or expected['manifest']['image_digest']!=image_id:
            raise ValueError('Resume candidate was not qualified')
        if registry.resolve(project,new)!=expected or registry.active(project)!=expected:
            raise ValueError('Resume candidate registry drift')
    return image_id,prior['qualified_environments']


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--checkpoint',required=True)
    parser.add_argument('--expected-worker-sha',required=True)
    parser.add_argument('--expected-api-sha',required=True)
    parser.add_argument('--base-image-id',required=True)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--resume-qualified-receipt',type=Path)
    parser.add_argument('--resume-receipt-sha')
    args=parser.parse_args(argv)
    if not re.fullmatch(r'[a-z0-9-]{1,60}',args.checkpoint):raise ValueError('Invalid checkpoint')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',args.base_image_id):raise ValueError('Base must be an immutable local image ID')
    if os.geteuid()!=0 or socket.gethostname()!='archived-worker.invalid':raise ValueError('Only root on Omarchy may run this operator command')
    source=args.snapshot.resolve();manifest=validate_snapshot(source)
    files=[name for name in manifest['files'] if name.startswith('src/cloudworkbench/') and Path(name).suffix in ('.py','.html')]
    paths={name:Path('/etc/cloud-workbench')/(name+'.json') for name in ('api','worker')}
    raw={name:path.read_bytes() for name,path in paths.items()}
    config={name:json.loads(data) for name,data in raw.items()};worker=config['worker']
    if sha(paths['worker'])!=args.expected_worker_sha or sha(paths['api'])!=args.expected_api_sha:raise ValueError('Live configuration drift')
    if worker['runtime']['image']!=OLD_IMAGE:raise ValueError('Unexpected prior execution image')
    if any(value.get('environment_network_profile')!='claude-only' or value.get('environment_secret_refs')!=['claude-subscription'] for value in config.values()):raise ValueError('Unexpected live environment policy')
    base_reference=verified_base_reference(args.base_image_id)
    backup=Path('/var/lib/cloud-workbench/operator-backups')/args.checkpoint
    registry_path=Path('/var/lib/cloud-workbench/environments')/(args.checkpoint+'.db')
    if backup.exists() or (registry_path.exists() and not args.resume_qualified_receipt):raise ValueError('Checkpoint path already exists; do not reuse blindly')
    database=Path(worker['database']);shared_gid=grp.getgrnam('cloud-workbench').gr_gid
    def pending():
        with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
            return db.execute("SELECT id,state FROM attempts WHERE state NOT IN ('completed','failed','cancelled','interrupted','paused')").fetchall()
    def clients():
        with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
            return db.execute('SELECT id,name,projects,scopes,revoked_at FROM clients ORDER BY id').fetchall()
    owner=worker['runtime'].get('owner','primary')
    if pending() or owned_live_containers(owner):raise ValueError('V2 database and owned runtime inventory must be idle')
    before_clients=clients()
    receipt={'schema_version':1,'checkpoint':args.checkpoint,'started_at':now(),'host':socket.gethostname(),
        'driver_sha256':sha(__file__),'snapshot_manifest_sha256':sha(source/'manifest.json'),
        'source_sha256':{name:manifest['files'][name] for name in files},'base_image_id':args.base_image_id,
        'base_image_reference':base_reference,
        'old_image_id':OLD_IMAGE,'config_sha256_before':{name:sha(path) for name,path in paths.items()},
        'legacy_pid_before':run(['systemctl','show','cloudd','--property=MainPID','--value']),
        'passed':False,'execute':args.execute,'stage':'validated','backup_path':str(backup),
        'services_started':[],'stopped_services':[]}
    candidate=next_configs(config,'sha256:'+'0'*64,registry_path)
    receipt['config_synthesis_validated']=True
    receipt['credential_preflight']=credential_probe(source)
    receipt['gateway']=gateway_provenance(candidate['worker']['runtime']['egress_image'],manifest['files']['src/cloudworkbench/egress.py'])
    resume=None
    if args.resume_qualified_receipt:
        prior_path=args.resume_qualified_receipt
        if prior_path.is_symlink() or prior_path.stat().st_uid!=0 or prior_path.stat().st_mode & 0o022 or sha(prior_path)!=args.resume_receipt_sha:
            raise ValueError('Resume receipt must be immutable root-owned and hash-bound')
        prior=json.loads(prior_path.read_text())
        sys.path.insert(0,str(source/'src'))
        from cloudworkbench.environments import EnvironmentRegistry
        image_id=prior['new_image_id']
        actual=json.loads(run(['/usr/bin/docker','image','inspect',image_id]))
        if len(actual)!=1 or actual[0]['Id']!=image_id:raise ValueError('Resume image unavailable')
        resume=validate_qualified_resume(prior,receipt,manifest,EnvironmentRegistry(registry_path),EnvironmentRegistry(Path(worker['environment_registry'])),image_id)
        receipt['resumed_from_receipt_sha256']=sha(prior_path)
        receipt['reused_qualified_image']=True
    if not args.execute:
        receipt['service_states']=service_states()
        print(json.dumps(receipt,indent=2));return 0
    for path in (source,*source.rglob('*')):
        if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode & 0o022:
            raise ValueError('Execution requires a root-owned non-writable source stage')
    stopped=[]
    receipt_path=source.parent/'auth-deployment-receipt.json'
    try:
        if resume:
            new_image,new_records=resume
            receipt['new_image_id']=new_image
            receipt['qualified_environments']=new_records
        else:
            receipt['stage']='build'
            iidfile=source.parent/'auth-image.id'
            if iidfile.exists():raise ValueError('Image receipt path exists')
            verified_base_reference(args.base_image_id)
            run(['/usr/bin/docker','build','--network=none','--pull=false','--build-arg','BASE_IMAGE='+base_reference,
                '--iidfile',str(iidfile),'-f',str(source/'deploy/Dockerfile.runtime'),'-t','cwb2-runtime:'+args.checkpoint,str(source)],timeout=600)
            verified_base_reference(args.base_image_id)
            receipt['base_identity_checked_before_and_after_build']=True
            new_image=iidfile.read_text().strip()
            if not re.fullmatch(r'sha256:[0-9a-f]{64}',new_image):raise ValueError('Invalid built image ID')
            receipt['new_image_id']=new_image
            sys.path.insert(0,str(source/'src'))
            from cloudworkbench.environments import EnvironmentRegistry,docker_qualifier
            old_registry=Path(worker['environment_registry'])
            with sqlite3.connect(f'file:{old_registry}?mode=ro',uri=True) as existing,sqlite3.connect(registry_path) as copied:
                existing.backup(copied)
            registry=EnvironmentRegistry(registry_path)
            old_snapshots={project:registry.resolve(project,versions[0]) for project,versions in VERSIONS.items()}
            new_records={}
            for project,(old,new) in VERSIONS.items():
                candidate=copy.deepcopy(old_snapshots[project]['manifest'])
                candidate.update(version=new,image_digest=new_image,base_image_digest=args.base_image_id)
                # No credentials/network/host mounts enter these readiness probes.
                for name in ('adapters','entrypoint','egress'):
                    candidate['readiness_probes'].append({'id':'source-'+name,'argv':['python3','-c',
                        'import hashlib,pathlib; assert hashlib.sha256(pathlib.Path("/opt/cloudworkbench/'+name+'.py").read_bytes()).hexdigest()=="'+manifest['files']['src/cloudworkbench/'+name+'.py']+'"']})
                registry.register(candidate);new_records[project]=registry.qualify(project,new,docker_qualifier)
                registry.activate(project,new,expected_active=old)
                if registry.resolve(project,old)!=old_snapshots[project]:raise ValueError('Prior manifest mutated')
            os.chown(registry_path,0,shared_gid);registry_path.chmod(0o640)
            receipt['qualified_environments']=new_records
        updated=next_configs(config,new_image,registry_path)
        if pending() or owned_live_containers(owner) or any(sha(paths[name])!=receipt['config_sha256_before'][name] for name in paths):raise ValueError('Work/config changed during qualification')
        receipt['stage']='credential_state'
        receipt['credential_initialization']=credential_probe(source,initialize=True)
        if not receipt['credential_initialization']['private_state_ready']:raise ValueError('Credential state not ready')
        receipt['credential_state_initialized_as']='cloud-worker'
        receipt['stage']='quiesce'
        backup.mkdir(mode=0o700)
        for name,data in raw.items():atomic(backup/(name+'.json'),data,0o600)
        for name in files:
            if (TARGET/name).exists():
                atomic(backup/name,(TARGET/name).read_bytes(),0o600)
        run(['systemctl','stop',SERVICES[0]]);stopped.append(SERVICES[0])
        if pending():raise ValueError('Work arrived before admission stopped')
        run(['systemctl','stop',SERVICES[1]]);stopped.append(SERVICES[1])
        receipt['owned_live_containers_after_stop']=owned_live_containers(owner)
        if pending() or receipt['owned_live_containers_after_stop']:raise ValueError('Work/runtime exists after quiescence')
        if clients()!=before_clients:raise ValueError('Client policy changed during qualification')
        with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as live,sqlite3.connect(backup/'state.db') as saved:live.backup(saved)
        (backup/'state.db').chmod(0o600)
        receipt['stage']='publish'
        for name in files:atomic(TARGET/name,(source/name).read_bytes())
        for name,path in paths.items():atomic(path,(json.dumps(updated[name],indent=2)+'\n').encode(),0o640,shared_gid)
        receipt['stage']='restart'
        run(['systemctl','start',SERVICES[1]]);stopped.remove(SERVICES[1]);receipt['services_started'].append(SERVICES[1])
        run(['systemctl','start',SERVICES[0]]);stopped.remove(SERVICES[0]);receipt['services_started'].append(SERVICES[0])
        token=Path('/var/lib/cloud-workbench/control/client.token').read_text().strip()
        ready,error=wait_readiness(token)
        receipt['readiness']=ready;receipt['readiness_error']=error
        if error:raise ValueError('New worker readiness failed')
        if clients()!=before_clients:raise ValueError('Client grants changed')
        if pending():raise ValueError('Unexpected work during deployment proof')
        receipt['readiness']=ready
        receipt['client_grants_unchanged']=True
        receipt['cli_entrypoint_metadata_updated']=False
        receipt['compat_invocation']='Existing venv Python -m cloudworkbench.compat; console entrypoint installation is separate'
        receipt['config_sha256_after']={name:sha(path) for name,path in paths.items()}
        receipt['deployed_source_sha256']={name:sha(TARGET/name) for name in files}
        if receipt['deployed_source_sha256']!=receipt['source_sha256']:raise ValueError('Deployed source mismatch')
        receipt['legacy_pid_after']=run(['systemctl','show','cloudd','--property=MainPID','--value'])
        if receipt['legacy_pid_after']!=receipt['legacy_pid_before']:raise ValueError('Legacy PID changed')
        receipt['passed']=True;receipt['stage']='complete'
    except Exception as error:
        receipt['error_type']=type(error).__name__
        receipt['stopped_services']=stopped
        receipt['recovery']='Inspect checkpoint and backups; no automatic rollback or mixed-version restart'
    finally:
        receipt['stopped_services']=stopped
        receipt['service_states']=service_states()
        receipt['services_live_unverified']=not receipt['passed'] and any(value=='active' for value in receipt['service_states'].values())
        receipt['finished_at']=now()
        atomic(receipt_path,(json.dumps(receipt,indent=2)+'\n').encode(),0o600)
        print(json.dumps(receipt,indent=2))
    return 0 if receipt['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
