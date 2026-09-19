#!/usr/bin/env python3
"""Prepare generic Hermes project files; never publish configs, restart services or run probes.

Run using the deployed cloud-workbench Python. Input configs and registry remain
unchanged. The new registry is qualified only by explicitly reusing image-only
readiness evidence, with its full provenance retained in a private sidecar.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

PROJECT='hermes-tasks'
VERSION='hermes-tasks-v1'
IMAGE='sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
SOURCE_PROJECT='sample-web'
SOURCE_VERSION='hermes-grok-v1'
SOURCE_MANIFEST='552804d2c3873487b5cd7dd399f833a59f5fcd2f9c672876f63f2b5c6ba546bf'
RESOURCES={'cpus':1,'memory_mib':1024,'pids':128,'workspace_mib':256}
ALLOWED_DIFFERENCES={'project_id','version','checks','legacy_template_id','expected_deliverables'}

class PreparationError(Exception):pass

def require(ok,code):
    if not ok:raise PreparationError(code)

def raw_json(value):return (json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()

def read_config(path):
    require(path.is_absolute() and path.resolve()==path,'config_path_not_canonical')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=1024**2,'unsafe_config_file')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(1024**2+1)
        require(len(raw)<=1024**2,'config_size_limit')
        return raw,json.loads(raw)
    finally:os.close(fd)

def write_new(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as output:
        output.write(raw_json(value));output.flush();os.fsync(output.fileno())

def prepare(args):
    from cloudworkbench.environments import EnvironmentRegistry,QualificationReceipt,validate_manifest
    require(args.reuse_image_readiness,'explicit_image_readiness_reuse_required')
    require(args.output.is_absolute() and args.output.resolve()==args.output and not args.output.exists(),'fresh_output_directory_required')
    require(args.registry_destination.is_absolute() and args.registry_destination.resolve()==args.registry_destination,
        'registry_destination_not_canonical')
    require(args.registry_destination.parent==Path('/var/lib/cloud-workbench/environments')
        and args.registry_destination.suffix=='.db' and not args.registry_destination.exists(),'new_registry_destination_required')
    raw={};configs={}
    for name,path in (('api',args.api_config),('worker',args.worker_config)):
        raw[name],configs[name]=read_config(path)
        require(configs[name].get('hermes_enabled') is True and 'hermes' in configs[name].get('agents',[]),'activated_hermes_required')
        require(PROJECT not in configs[name]['projects'],'project_already_configured')
    worker=configs['worker'];source_path=Path(worker['environment_registry'])
    require(source_path.is_absolute() and source_path.resolve()==source_path
        and configs['api']['environment_registry']==str(source_path),'source_registry_mismatch')
    require(args.registry_destination!=source_path,'source_registry_cannot_be_destination')
    require({k:worker['runtime'].get(k) for k in RESOURCES}==RESOURCES,'worker_resource_profile_changed')
    policy=worker.get('hermes_runtime',{})
    require(policy.get('environment_network_profile')=='hermes-coordinator-bridge-tools-none'
        and policy.get('environment_secret_refs')==['grok-dedicated-oauth']
        and IMAGE in worker.get('qualified_images',[]),'worker_image_policy_changed')
    # A fresh output is deliberately retained on any later exception; never retry
    # into a partially prepared registry or silently replace prior material.
    args.output.mkdir(mode=0o700)
    copied=args.output/'environments.db'
    with sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(copied) as destination:
        source.backup(destination)
    copied.chmod(0o600)
    registry=EnvironmentRegistry(copied)
    existing=registry.resolve(SOURCE_PROJECT,SOURCE_VERSION)
    manifest,digest=validate_manifest(existing['manifest'])
    require(digest==SOURCE_MANIFEST and existing['manifest_sha256']==digest,'source_manifest_changed')
    require(manifest['image_digest']==IMAGE and manifest['base_image_digest']==IMAGE
        and manifest['resources']==RESOURCES and not manifest['repositories']
        and not manifest['install_commands'] and not manifest['startup_commands'],'source_image_profile_changed')
    prior=existing['qualification']
    qualified={key:value for key,value in prior.items() if key not in ('qualification_id','qualified_at')}
    QualificationReceipt.model_validate(qualified)
    require(prior['source']=='docker_image_readiness'
        and all(prior[key]==expected for key,expected in (
            ('manifest_sha256',digest),('image_digest',IMAGE),('os',manifest['os']),
            ('architecture',manifest['architecture']),('cli_versions',manifest['cli_versions'])))
        and sorted(p['id'] for p in prior['probes'])==sorted(p['id'] for p in manifest['readiness_probes'])
        and all(p['exit_code']==0 for p in prior['probes']), 'original_image_readiness_invalid')
    candidate=copy.deepcopy(manifest)
    candidate.update(project_id=PROJECT,version=VERSION,checks=[],legacy_template_id=None,expected_deliverables=[])
    differences=[{'field':key,'previous':manifest[key],'current':candidate[key]}
        for key in sorted(manifest) if manifest[key]!=candidate[key]]
    require({item['field'] for item in differences}<=ALLOWED_DIFFERENCES,'runtime_profile_reuse_forbidden')
    with registry.db() as db:
        require(not db.execute('SELECT 1 FROM environments WHERE project=? OR version=?',(PROJECT,VERSION)).fetchone(),
            'candidate_environment_already_exists')
    registered=registry.register(candidate)
    sidecar={'schema_version':1,'basis':'inherited_image_readiness','probes_run':0,'provider_calls':0,
        'source_registry':str(source_path),'source_project':SOURCE_PROJECT,'source_version':SOURCE_VERSION,
        'original_manifest_sha256':digest,'original_qualification_id':prior['qualification_id'],
        'original_qualified_at':prior['qualified_at'],'original_qualification':prior,
        'original_readiness_probes':manifest['readiness_probes'],'differences':differences,
        'new_manifest_sha256':registered['manifest_sha256'],'new_project_workflow':'unverified',
        'no_checks_outcome':'unverified','automatic_deployment':False}
    write_new(args.output/'image-readiness-provenance.json',sidecar)
    def reuse(record):
        require(record['manifest']==registered['manifest'] and record['manifest_sha256']==registered['manifest_sha256'],
            'candidate_changed_during_registration')
        return {**qualified,'manifest_sha256':record['manifest_sha256'],'source':'trusted_builder'}
    qualified_record=registry.qualify(PROJECT,VERSION,reuse)
    # Only the new project's default is set. Existing project active versions
    # remain untouched and each existing session keeps its frozen environment.
    registry.activate(PROJECT,VERSION,expected_active=None)
    copied.chmod(0o600)
    project={'display_name':'Hermes tasks','allowed_agents':['hermes'],'models':{'hermes':['grok-4.6']},
        'environment_versions':[VERSION],'checks':[]}
    reserve=worker.get('host_memory_reserve_mib',8192)
    require(type(reserve) is int and reserve>=0,'invalid_host_memory_reserve')
    worker['hermes_capacity']=3
    worker['host_memory_reserve_mib']=max(reserve,8192)
    for name,config in configs.items():
        config['projects'][PROJECT]=copy.deepcopy(project)
        config['environment_registry']=str(args.registry_destination)
        config['capacity']=3
        write_new(args.output/(name+'.json'),config)
    write_new(args.output/'environment.json',qualified_record)
    write_new(args.output/'preparation.json',{'schema_version':1,'project':PROJECT,'environment_version':VERSION,
        'source_config_sha256':{k:sha(value) for k,value in raw.items()},
        'source_registry':str(source_path),'registry_destination':str(args.registry_destination),
        'prepared_files_sha256':{p.name:sha(p.read_bytes()) for p in args.output.iterdir() if p.is_file()},
        'capacity':3,'hermes_capacity':3,'host_memory_reserve_mib':worker['host_memory_reserve_mib'],'runtime_resources':RESOURCES,'probes_run':0,'provider_calls':0,
        'deployed':False,'client_grants_modified':False,'new_project_workflow':'unverified',
        'remaining_operator_actions':['Publish prepared registry and API/worker configs using the existing service procedure.',
            'Explicitly grant hermes-tasks to the dedicated delegating client; preserve its other project scopes.',
            'Do not replace existing task histories, profiles, credentials or native sessions.']})
    fd=os.open(args.output,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return {'prepared':str(args.output),'project':PROJECT,'environment':VERSION,'deployed':False,'probes_run':0}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--api-config',type=Path,default=Path('/etc/cloud-workbench/api.json'))
    p.add_argument('--worker-config',type=Path,default=Path('/etc/cloud-workbench/worker.json'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--registry-destination',type=Path,required=True)
    p.add_argument('--reuse-image-readiness',action='store_true',help='Explicitly inherit the already recorded image-only readiness; run zero new probes')
    try:print(json.dumps(prepare(p.parse_args())));return 0
    except Exception as exc:
        print(json.dumps({'prepared':False,'error':str(exc) if type(exc) is PreparationError else type(exc).__name__,
            'recovery':'Preserve partial output; use a new output path after resolving the cause.'}));return 1

if __name__=='__main__':sys.exit(main())
