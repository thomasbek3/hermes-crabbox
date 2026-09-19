#!/usr/bin/env python3
"""One fresh isolated normal-API Hermes job and exact-session follow-up on Omarchy.

Operator-only. Run with the deployed Python environment as root, after review.
Uses dedicated auth by path only; never reads or emits its credential values.
Keeps evidence/workspaces for inspection. No services/configuration are modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

IMAGE='sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
SERVICES=('cloud-workbench-api.service','cloud-workbench-worker.service','cloudd.service')
CHECK='''import importlib.util,json,sys
from pathlib import Path
p=Path('/workspace/arithmetic.py')
spec=importlib.util.spec_from_file_location('subject',p)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
count=0
names=['add']+(['subtract'] if hasattr(m,'subtract') or '--followup' in sys.argv else [])
for name in names:
    fn=getattr(m,name)
    for a,b in [(2,3),(-5,3),(0,0),(10**20,1)]:
        assert fn(a,b)==(a+b if name=='add' else a-b);count+=1
    for bad in [True,False,1.5,'2',None]:
        for pair in [(bad,1),(1,bad)]:
            try:fn(*pair)
            except TypeError:count+=1
            else:raise AssertionError('expected TypeError')
print(json.dumps({'accepted':True,'cases':count,'functions':names}))
'''
PROMPTS=(
    'Use pstack:tdd to create arithmetic.py containing add(a,b). Accept exactly Python ints; '
    'bool, float, string and None in either argument must raise TypeError. Return the integer sum. '
    'Write test_arithmetic.py using stdlib unittest, run it, and write README.md. '
    'Work only in /workspace. No network or external dependencies. Do not edit protected checks.',
    'Continue this exact session and existing arithmetic.py. Use pstack:tdd. Preserve add(a,b) '
    'and add subtract(a,b), accepting exactly ints (bool and every other type raise TypeError). '
    'Return a-b. Extend stdlib tests, run all of them and update README.md. Work only in /workspace; '
    'no network, external dependencies or protected-check changes.')


class ProofError(Exception):pass


def require(value,code):
    if not value:raise ProofError(code)


def sha(raw):return hashlib.sha256(raw).hexdigest()


def command(argv,timeout=20):
    result=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    require(result.returncode==0,'operator_command_failed')
    require(len(result.stdout)<=2*1024**2,'operator_output_limit')
    return result.stdout


def services():
    return {name:command(['/usr/bin/systemctl','show',name,'-p','ActiveState','-p','MainPID','--value']).decode().splitlines()
        for name in SERVICES}


def headroom():
    info=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
    fs=os.statvfs('/var/lib/cloud-workbench')
    return {'available_memory_bytes':int(info['MemAvailable'].split()[0])*1024,
            'available_disk_bytes':fs.f_bavail*fs.f_frsize}


def write(path,value):
    raw=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
    temporary=path.with_suffix(path.suffix+'.new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def source_hashes(root):
    for directory in (root/'cloudworkbench',root,*root.parents):
        info=directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==0
            and (not info.st_mode&0o022 or bool(info.st_mode&stat.S_ISVTX)), 'source_directory_untrusted')
    files=sorted((root/'cloudworkbench').glob('*.py'))
    require(5<=len(files)<=512,'source_closure_unavailable')
    result={}
    for path in files:
        info=path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==0 and not info.st_mode&0o022
            and info.st_size<=2*1024**2,'source_file_untrusted')
        result[str(path.relative_to(root))]=sha(path.read_bytes())
    return result


def owned_ids(owner):
    return command(['/usr/bin/docker','ps','-aq','--no-trunc','--filter',
        'label=io.cloudworkbench.owner='+owner]).decode().split()


def independent(proof,image,owner,directory,followup):
    name=owner+'-check-'+('2' if followup else '1')
    argv=['/usr/bin/docker','run','--rm','--name',name,'--label',
        'io.cloudworkbench.hermes-api-proof='+owner,'--network','none','--read-only',
        '--user','959:960','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--cpus','1','--memory','256m','--memory-swap','256m','--pids-limit','64',
        '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777',
        '--mount',f'type=bind,src={directory},dst=/workspace,readonly',
        '--mount',f'type=bind,src={proof}/protected/check.py,dst=/check.py,readonly',
        '--workdir','/workspace','--entrypoint','python3',image,'-B','/check.py']
    if followup:argv.append('--followup')
    try:
        raw=command(argv,timeout=30)
        value=json.loads(raw.splitlines()[-1])
        require(value=={'accepted':True,'cases':28 if followup else 14,
            'functions':['add','subtract'] if followup else ['add']},'independent_check_failed')
        return value
    finally:
        inspected=subprocess.run(['/usr/bin/docker','inspect',name],capture_output=True,timeout=10)
        if inspected.returncode==0:
            item=json.loads(inspected.stdout)[0]
            require(item['Config']['Labels'].get('io.cloudworkbench.hermes-api-proof')==owner,
                'independent_cleanup_identity_changed')
            command(['/usr/bin/docker','rm','-f',item['Id']])


def observe(owner,image,stop,records,errors):
    """Sample only the exact proof owner; never persist raw inspect Config/Env."""
    from cloudworkbench.hermes_coordinator_runtime import CACHE_PATHS
    seen=set();seen_states=set()
    while not stop.is_set():
        try:
            for rid in owned_ids(owner):
                if rid in seen:continue
                response=subprocess.run(['/usr/bin/docker','inspect',rid],capture_output=True,timeout=10)
                if response.returncode:
                    require(rid not in owned_ids(owner),'live_inspect_failed')
                    continue
                data=json.loads(response.stdout)[0];labels=data['Config'].get('Labels') or {}
                if labels.get('io.cloudworkbench.role')!='hermes-tool' and labels.get('io.cloudworkbench.hermes-coordinator')!='1':
                    continue
                host=data['HostConfig'];mounts=[{'source':v['Source'],'destination':v['Destination'],
                    'readonly':not v['RW'],'type':v['Type']} for v in data['Mounts']]
                projected={'id':rid,'observed_at':time.time(),'running':data['State']['Running'],
                    'image':data['Image'],'labels':{k:v for k,v in labels.items() if k.startswith('io.cloudworkbench.')},
                    'user':data['Config']['User'],'network':host['NetworkMode'],
                    'nano_cpus':host['NanoCpus'],'memory':host['Memory'],'pids_limit':host.get('PidsLimit'),
                    'privileged':host['Privileged'],'mounts':mounts}
                key=(rid,bool(data['State']['Running']))
                if key not in seen_states:
                    require(len(records)<256,'runtime_observation_limit')
                    records.append(projected);seen_states.add(key)
                if data['State']['Running']:seen.add(rid)
                require(labels.get('io.cloudworkbench.owner')==owner and data['Image']==image,'observed_scope_changed')
                expected_memory_mib=1536 if labels.get('io.cloudworkbench.role')=='hermes-tool' else 1024
                require(host['NanoCpus']==1000000000 and host['Memory']==expected_memory_mib*1024**2
                    and not host['Privileged'],'observed_resource_policy_changed')
                if labels['io.cloudworkbench.role']=='hermes-tool':
                    require(data['Config']['User']=='1000:1000' and host['NetworkMode']=='none','observed_tool_identity_changed')
                    sid=labels['io.cloudworkbench.session']
                    require(re.fullmatch(r'[a-f0-9-]{36}',sid),'observed_session_invalid')
                    root=Path('/var/lib/cloud-workbench/workspaces')/sid
                    allowed={(str(root/'work'),'/workspace',False)}
                    allowed.update((str(root/'native/hermes-job/hermes'/relative),'/root/.hermes/'+relative,True)
                        for relative in CACHE_PATHS)
                    binds={(v['source'],v['destination'],v['readonly']) for v in mounts if v['type']=='bind'}
                    require((str(root/'work'),'/workspace',False) in binds and binds<=allowed
                        and all(v['type'] in ('bind','tmpfs') for v in mounts),'observed_tool_mount_policy_changed')
                else:
                    require(data['Config']['User']=='959:960' and host['NetworkMode']=='bridge',
                        'observed_coordinator_identity_changed')
                    # The facade independently validates its complete immutable mount journal.
        except Exception as exc:
            errors.append(str(exc) if type(exc) is ProofError else type(exc).__name__)
            stop.set()
        stop.wait(.25)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source-root',type=Path,required=True,help='Frozen src directory containing cloudworkbench')
    ap.add_argument('--proof-root',type=Path,required=True,help='Fresh directory under /var/lib/cloud-workbench/qualifications')
    ap.add_argument('--workspace-helper',type=Path,required=True)
    ap.add_argument('--grok-auth',type=Path,required=True)
    ap.add_argument('--image',default=IMAGE)
    args=ap.parse_args()
    require(os.geteuid()==0 and socket.gethostname()=='omarchy','root_on_omarchy_required')
    require(args.image==IMAGE,'only_qualified_image_supported')
    for path in (args.source_root,args.proof_root,args.workspace_helper,args.grok_auth):
        require(path.is_absolute() and path==path.resolve(),'canonical_operator_path_required')
    require(args.proof_root.parent==Path('/var/lib/cloud-workbench/qualifications')
        and re.fullmatch(r'hermes-api-proof-[a-f0-9]{12}',args.proof_root.name),'proof_path_invalid')
    require(not args.proof_root.exists(),'fresh_proof_required')
    require(args.workspace_helper.is_file() and args.workspace_helper.stat().st_uid==0
        and not args.workspace_helper.stat().st_mode&0o022,'untrusted_workspace_helper')
    auth=args.grok_auth.lstat()
    require(stat.S_ISREG(auth.st_mode) and auth.st_nlink==1 and auth.st_uid==959
        and not auth.st_mode&0o077 and 0<auth.st_size<=65536,'dedicated_auth_metadata_invalid')
    source=source_hashes(args.source_root)
    image=json.loads(command(['/usr/bin/docker','image','inspect',args.image]))[0]
    require(image['Id']==args.image,'image_identity_changed')
    before=services();capacity=headroom()
    require(capacity['available_memory_bytes']>=10*1024**3 and capacity['available_disk_bytes']>=21*1024**3,
        'insufficient_host_headroom')
    owner='hermes-api-proof-'+secrets.token_hex(6)
    require(not owned_ids(owner),'owner_already_exists')
    args.proof_root.parent.mkdir(mode=0o755,exist_ok=True)
    args.proof_root.mkdir(mode=0o700)
    os.chown(args.proof_root,959,960)
    for name in ('state','inputs','downloads','protected'):
        p=args.proof_root/name;p.mkdir(mode=0o750);os.chown(p,959,1000 if name=='protected' else 960)
    check=args.proof_root/'protected/check.py';check.write_text(CHECK);os.chown(check,959,1000);check.chmod(0o440)
    os.setgroups([966,959,1000]);os.setgid(960);os.setuid(959);os.umask(0o007)
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(args.source_root))
    import uvicorn
    from cloudworkbench.api import create_app
    from cloudworkbench.runner import Runner
    from cloudworkbench.store import Store
    from cloudworkbench.server import worker_readiness
    state=args.proof_root/'state'
    config={'state_root':str(state),'database':str(state/'state.db'),'input_root':str(args.proof_root/'inputs'),
        'artifact_root':str(state/'artifacts'),'resource_sampling_enabled':False,
        'agents':['hermes'],'hermes_enabled':True,'claude_enabled':False,'capacity':2,
        'execution_seconds':600,'verification_seconds':60,
        'runtime':{'image':args.image,'root':'/var/lib/cloud-workbench/workspaces',
            'owner':owner,'uid':958,'gid':959,'cpus':1,'memory_mib':1024,'pids':128,'workspace_mib':256,
            'workspace_helper':str(args.workspace_helper),'network_enabled':False,
            'approved_mount_roots':[str(args.proof_root),'/var/lib/cloud-workbench/workspaces'],
            'approved_writable_mount_roots':['/var/lib/cloud-workbench/workspaces']},
        'hermes_runtime':{'hermes_source_root':str(args.source_root/'cloudworkbench'),
            'hermes_grok_auth':str(args.grok_auth),'coordinator_uid':959,'coordinator_gid':960,
            'docker_gid':966,'tool_gid':1000,'tool_shared_gid':959},
        'projects':{'hermes-api-proof':{'allowed_agents':['hermes'],'models':{'hermes':['grok-4.6']},
            'environment_versions':['hermes-api-proof-v1'],'checks':[{'id':'arithmetic',
                'description':'Integer arithmetic and type validation pass the protected checks.',
                'argv':['python3','/run/task/check.py'],'script_source':str(check),
                'script_name':'check.py','script_sha256':sha(check.read_bytes()),'timeout':20}]}}}
    store=Store(state/'state.db');bearer=secrets.token_hex(32)
    principal=store.add_client('TEST isolated Hermes API',bearer,['submit','observe','retrieve','cancel'],['hermes-api-proof'])
    receipt={'schema_version':1,'host':socket.gethostname(),'uid':os.geteuid(),'groups':os.getgroups(),
        'image':args.image,'owner':owner,'source_sha256':source,'services_before':before,'headroom_before':capacity,
        'started_at':time.time(),'turns':[],'production_services_modified':False,'full_platform_complete':False}
    path=args.proof_root/'receipt.json';write(path,receipt)
    runner=None;server=None;thread=None;sock=None;observer=None
    observer_stop=threading.Event();observations=[];observation_errors=[]
    def checkpoint():write(path,receipt)
    def request(method,route,body=None,key=None,binary=False):
        headers={'Authorization':'Bearer '+bearer}
        if key:headers['Idempotency-Key']=key
        data=None if body is None else json.dumps(body).encode()
        if data is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request(base+route,data=data,headers=headers,method=method)
        try:
            with urllib.request.urlopen(req,timeout=10) as response:
                raw=response.read(1024**2+1)
        except urllib.error.HTTPError as exc:
            raise ProofError('http_status_'+str(exc.code)) from None
        require(len(raw)<=1024**2,'http_response_limit')
        return raw if binary else json.loads(raw)
    try:
        runner=Runner(store,config)
        observer=threading.Thread(target=observe,args=(owner,args.image,observer_stop,observations,observation_errors),daemon=True)
        observer.start()
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
        base='http://127.0.0.1:'+str(sock.getsockname()[1]);receipt['loopback_port']=sock.getsockname()[1]
        settings={**config,'readiness':lambda:worker_readiness(config)}
        server=uvicorn.Server(uvicorn.Config(create_app(store,settings),log_level='critical',access_log=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        end=time.monotonic()+10
        while not server.started and time.monotonic()<end:time.sleep(.05)
        require(server.started,'temporary_api_not_started')
        runner.tick()
        require('hermes' not in runner.blocked(),'hermes_runtime_not_ready')
        require(request('GET','/v1/ready').get('ready') is True,'temporary_api_not_ready')
        previous_native=None
        for number,prompt in enumerate(PROMPTS,1):
            require('hermes' not in runner.blocked(),'hermes_runtime_not_ready')
            if number==1:
                created=request('POST','/v1/sessions',{'project_id':'hermes-api-proof','agent':'hermes',
                    'model':'grok-4.6','environment_version':'hermes-api-proof-v1','goal':prompt,
                    'acceptance':[{'id':'arithmetic','description':config['projects']['hermes-api-proof']['checks'][0]['description'],
                        'mandatory':True}]},key='first')
                sid=created['session_id']
            else:created=request('POST',f'/v1/sessions/{sid}/messages',{'message':prompt},key='followup')
            turn={'number':number,'session_id':sid,'attempt_id':created['attempt_id'],
                'generation':created['generation'],'submitted_at':time.time()}
            receipt['turns'].append(turn);checkpoint()
            end=time.monotonic()+600
            while True:
                require(time.monotonic()<end,'turn_deadline_exceeded')
                require(not observation_errors,'live_observer_refused')
                runner.tick()
                detail=request('GET',f'/v1/sessions/{sid}')
                attempt=next(v for v in detail['attempts'] if v['id']==created['attempt_id'])
                turn.update(state=attempt['state'],reason=attempt.get('reason'))
                if attempt['state'] in ('completed','failed','cancelled','interrupted','paused'):break
                time.sleep(.5)
            require(attempt['state']=='completed' and attempt['outcome']=='verified','attempt_not_verified')
            provider=attempt['result']['provider_result'];native=provider.get('native_session_id')
            require(provider['is_error'] is False and type(native) is str and
                re.fullmatch(r'[0-9]{8}_[0-9]{6}_[0-9a-f]{6}',native),'native_identity_unavailable')
            if previous_native is not None:require(native==previous_native,'native_followup_identity_changed')
            previous_native=native
            turn.update(native_session_id=native,outcome=attempt['outcome'],finished_at=time.time())
            downloaded=args.proof_root/'downloads'/str(number);downloaded.mkdir(mode=0o750)
            rows=request('GET',f'/v1/sessions/{sid}/artifacts')['artifacts']
            selected=[v for v in rows if v['attempt_id']==created['attempt_id'] and v['path'] in ('arithmetic.py','test_arithmetic.py','README.md')]
            require({v['path'] for v in selected}=={'arithmetic.py','test_arithmetic.py','README.md'},'expected_artifacts_missing')
            turn['artifacts']=[]
            for artifact in selected:
                raw=request('GET',f"/v1/artifacts/{artifact['id']}/content",binary=True)
                require(len(raw)==artifact['bytes'] and sha(raw)==artifact['sha256'],'download_hash_mismatch')
                p=downloaded/artifact['path'];p.write_bytes(raw);p.chmod(0o640)
                turn['artifacts'].append({k:artifact[k] for k in ('id','path','bytes','sha256')})
            require(any(v['labels'].get('io.cloudworkbench.attempt')==created['attempt_id']
                and v['labels'].get('io.cloudworkbench.role')=='hermes-tool' and v['running'] for v in observations),
                'running_tool_not_observed')
            require(any(v['labels'].get('io.cloudworkbench.attempt')==created['attempt_id']
                and v['labels'].get('io.cloudworkbench.hermes-coordinator')=='1' and v['running'] for v in observations),
                'running_coordinator_not_observed')
            events=request('GET',f'/v1/sessions/{sid}/events?follow=false',binary=True)
            events_path=args.proof_root/('turn-'+str(number)+'.events.sse')
            with events_path.open('xb') as out:out.write(events);out.flush();os.fsync(out.fileno())
            events_path.chmod(0o600)
            turn['api_events']={'file':events_path.name,'sha256':sha(events),'bytes':len(events)}
            turn['independent']=independent(args.proof_root,args.image,owner,downloaded,number==2)
            write(args.proof_root/('turn-'+str(number)+'.json'),{'detail':detail,'artifacts':turn['artifacts']})
            checkpoint()
        receipt['result']='passed'
    except Exception as exc:
        receipt['result']='failed'
        receipt['failure']=str(exc) if type(exc) is ProofError else type(exc).__name__
    finally:
        if runner is not None:
            try:
                for attempt in store.active_attempts():
                    store.cancel(principal,attempt['id'],'cleanup-'+attempt['id'])
                for turn in receipt['turns']:
                    store.cancel(principal,turn['attempt_id'],'cleanup-'+turn['attempt_id'])
                end=time.monotonic()+30
                while store.active_attempts() and time.monotonic()<end:
                    runner.tick();time.sleep(.25)
                receipt['remaining_active_attempts']=[a['id'] for a in store.active_attempts()]
                receipt['remaining_container_ids']=owned_ids(owner)
                receipt['cleanup_confirmed']=not receipt['remaining_active_attempts'] and not receipt['remaining_container_ids']
            except Exception as exc:
                receipt['cleanup_confirmed']=False;receipt['cleanup_error']=type(exc).__name__
            runner.close()
        observer_stop.set()
        if observer is not None:observer.join(timeout=25)
        receipt['runtime_observations']=observations
        receipt['runtime_observation_errors']=observation_errors
        receipt['observer_stopped']=observer is None or not observer.is_alive()
        if server is not None:server.should_exit=True
        if thread is not None:thread.join(timeout=10);receipt['api_thread_stopped']=not thread.is_alive()
        if sock is not None:sock.close()
        try:
            receipt['services_after']=services();receipt['services_unchanged']=receipt['services_after']==before
            receipt['headroom_after']=headroom();receipt['source_unchanged']=source_hashes(args.source_root)==source
        except Exception as exc:receipt['final_readback_error']=type(exc).__name__
        receipt['finished_at']=time.time()
        if not all(receipt.get(k) is True for k in ('cleanup_confirmed','api_thread_stopped','services_unchanged','source_unchanged','observer_stopped')):
            receipt['result']='failed_held'
        if observation_errors:receipt['result']='failed_held'
        checkpoint()
    print(json.dumps({'receipt':str(path),'result':receipt['result'],'session_ids':[t['session_id'] for t in receipt['turns']]}))
    return 0 if receipt['result']=='passed' else 1


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print(json.dumps({'preflight_failure':str(exc) if type(exc) is ProofError else type(exc).__name__}))
        sys.exit(1)
