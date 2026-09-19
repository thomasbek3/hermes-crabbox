#!/usr/bin/env python3
"""Opt-in synthetic snapshot/Docker qualification. --prepare never invokes SSH."""

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

from pathlib import Path
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid

MODULES = ('__init__.py','models.py','store.py','retention.py','provider_leases.py',
    'provider_dispatch.py','provider_docker.py','provider_auth_snapshot.py',
    'snapshot_provider_docker.py','provider_bootstrap.py','inference_transport.py',
    'native_responses.py','adapters.py','profile_binding.py','egress.py','inference_budget.py')
REMOTE = 'operator@worker.example.invalid'
LABEL = 'io.cloudworkbench.snapshot-qualification'
SERVICES = ('cloud-workbench-api.service','cloud-workbench-worker.service','cloudd.service')
WORKER_PHASES = frozenset({'bootstrap','setup','normal_request','normal_cleanup','missing_request',
    'missing_cleanup','old_owner_request','old_owner_cleanup','new_owner_request','old_material_cleanup',
    'new_owner_cleanup','final_checks','complete'})
WORKER_PHASE = 'bootstrap'
WORKER_SUBPHASE = None
SUBPHASES = frozenset({'grant','admit','scope_journal','launch','collect','response','inspect','mount'})
ERROR_TYPES = frozenset({'TypeError','ValueError','AssertionError','OSError','PermissionError','RuntimeError',
    'LeaseError','DispatchError','SnapshotError','DockerError','JSONDecodeError','unknown'})
FAILURES = frozenset({'assertion_failed','permission_denied','filesystem_error','timeout',
    'lease_error','invalid_value','unexpected_error'})
FIXTURE = '''import os,json,errno,hashlib
from pathlib import Path
request=json.load(__import__('sys').stdin)
assert request=={'synthetic_snapshot_probe':True}
p=Path('/run/secrets/native-auth.json')
raw=p.read_bytes(); value=json.loads(raw)
assert value['tokens']['refresh_token']=='SYNTHETIC-SNAPSHOT-ONLY'
assert os.getuid()==958 and os.getgid()==959
s=p.stat(); assert s.st_uid==959 and s.st_gid==959 and s.st_mode&0o777==0o440
try: p.write_bytes(b'not-allowed')
except OSError as e: write_errno=e.errno
else: raise AssertionError('write_allowed')
try: p.chmod(0o600)
except OSError as e: chmod_errno=e.errno
else: raise AssertionError('chmod_allowed')
assert write_errno in (errno.EROFS,errno.EACCES,errno.EPERM)
assert chmod_errno in (errno.EROFS,errno.EPERM)
print(json.dumps({'fixture_only':True,'uid':os.getuid(),'gid':os.getgid(),'source_uid':s.st_uid,
 'source_gid':s.st_gid,'mode':s.st_mode&0o777,'sha256':hashlib.sha256(raw).hexdigest(),
 'write_errno':write_errno,'chmod_errno':chmod_errno,'provider_calls':False}))
'''


def digest(data): return hashlib.sha256(data).hexdigest()


def base_alias_name(run_id):
    if not re.fullmatch(r'snapshotqual-[0-9a-f]{32}',run_id):raise ValueError('invalid_run_id')
    return 'cwb2-snapshot-base:'+run_id


def assert_alias_absent(docker, alias):
    if docker(['image','ls','--format','{{json .}}','--filter','reference='+alias]).strip():
        raise RuntimeError('base_alias_exists')


def verify_alias(docker, alias, image):
    rows=json.loads(docker(['image','inspect',alias]))
    if len(rows)!=1 or rows[0].get('Id')!=image or alias not in rows[0].get('RepoTags',[]):
        raise RuntimeError('base_alias_changed')
    return rows[0]


def cleanup_base_alias(docker, alias, image):
    if not docker(['image','ls','--format','{{json .}}','--filter','reference='+alias]).strip():return
    verify_alias(docker,alias,image)
    docker(['image','rm',alias])
    assert_alias_absent(docker,alias)
    if json.loads(docker(['image','inspect',image]))[0].get('Id')!=image:
        raise RuntimeError('base_image_preservation_unproven')


def require_existing_base_tag(base):
    tags=base.get('RepoTags')
    if type(tags) is not list or not any(type(tag) is str and tag and tag!='<none>:<none>' for tag in tags):
        raise RuntimeError('base_existing_tag_required')


def verify_base_layers(base, derived):
    layers=base.get('RootFS',{}).get('Layers')
    actual=derived.get('RootFS',{}).get('Layers')
    if (type(layers) is not list or not layers or type(actual) is not list
            or len(actual)<len(layers) or actual[:len(layers)]!=layers):
        raise RuntimeError('base_layers_changed')


def synthetic_build(folder, build, tag, run_id, *, executable='/usr/bin/docker', timeout=120):
    # Only this fixed, pre-credential synthetic context may retain build output.
    import selectors,signal
    home=folder/'build-home';config=folder/'build-cli'
    home.mkdir(mode=0o700);config.mkdir(mode=0o700)
    argv=[executable,'--host','unix:///var/run/docker.sock','--config',str(config),
          'build','--network','none','--pull=false','--progress','plain',
          '--label',LABEL+'='+run_id,'-t',tag,str(build)]
    result={'exit_code':None,'error':None,'stdout':'','stderr':'','truncated':False,'output_limit_bytes':65536}
    try:
        process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':str(home),'DOCKER_CONFIG':str(config)},
            start_new_session=True,cwd=str(build))
    except OSError:
        result['error']='build_unavailable';return result
    selector=selectors.DefaultSelector();buffers={'stdout':bytearray(),'stderr':bytearray()}
    deadline=time.monotonic()+timeout;total=0
    try:
        for name in buffers:
            pipe=getattr(process,name);os.set_blocking(pipe.fileno(),False)
            selector.register(pipe,selectors.EVENT_READ,name)
        while selector.get_map():
            if time.monotonic()>=deadline:result['error']='build_timeout';break
            for key,_ in selector.select(min(.1,max(0,deadline-time.monotonic()))):
                part=os.read(key.fileobj.fileno(),8192)
                if not part:selector.unregister(key.fileobj);continue
                available=max(0,65536-total);buffers[key.data].extend(part[:available]);total+=min(available,len(part))
                if len(part)>available:result['truncated']=True;result['error']='build_output_limit';break
            if result['error']:break
        if result['error'] is None:
            try:result['exit_code']=process.wait(timeout=max(.001,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:result['error']='build_timeout'
            if result['exit_code'] not in (0,None):result['error']='build_command_failed'
    finally:
        if process.poll() is None:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait(timeout=2)
        result['exit_code']=process.returncode
        selector.close();process.stdout.close();process.stderr.close()
    for name,value in buffers.items():result[name]=value.decode('utf-8',errors='replace')
    return result


def validate_services(value):
    if type(value) is not dict or set(value)!=set(SERVICES):raise ValueError('service_identity_unproven')
    for unit,info in value.items():
        if (type(info) is not dict or set(info)!={'ActiveState','MainPID'}
                or info['ActiveState']!='active' or type(info['MainPID']) is not int or info['MainPID']<=0):
            raise ValueError('service_identity_unproven')
    return value


def service_snapshot(run=None):
    run=run or subprocess.run;result={}
    for unit in SERVICES:
        p=run(['systemctl','show',unit,'--property=Id','--property=ActiveState','--property=MainPID','--no-pager'],
              capture_output=True,text=True,timeout=5,env={'PATH':'/usr/bin:/bin'})
        if p.returncode or len(p.stdout)>4096:raise RuntimeError('service_snapshot_failed')
        rows=[line.split('=',1) for line in p.stdout.splitlines() if line]
        if any(len(row)!=2 for row in rows) or len(rows)!=3:raise RuntimeError('service_snapshot_failed')
        fields=dict(rows)
        if set(fields)!={'Id','ActiveState','MainPID'} or fields['Id']!=unit or not fields['MainPID'].isdigit():
            raise RuntimeError('service_snapshot_failed')
        result[unit]={'ActiveState':fields['ActiveState'],'MainPID':int(fields['MainPID'])}
    return validate_services(result)


def safe_worker_failure(exc):
    if isinstance(exc,AssertionError):return 'assertion_failed'
    if isinstance(exc,PermissionError):return 'permission_denied'
    if isinstance(exc,TimeoutError):return 'timeout'
    if isinstance(exc,OSError):return 'filesystem_error'
    if type(exc).__name__ in ('LeaseError','DispatchError','SnapshotError','DockerError'):return 'lease_error'
    if isinstance(exc,ValueError):return 'invalid_value'
    return 'unexpected_error'


def worker_mark(folder, phase, error=None, *, subphase=None, error_type=None):
    global WORKER_PHASE,WORKER_SUBPHASE
    if (phase not in WORKER_PHASES or error is not None and error not in FAILURES
            or subphase is not None and subphase not in SUBPHASES
            or error_type is not None and error_type not in ERROR_TYPES):
        raise ValueError('invalid_worker_diagnostic')
    WORKER_PHASE=phase;WORKER_SUBPHASE=subphase
    value={'phase':phase,'error':error}
    if subphase is not None:value['subphase']=subphase
    if error_type is not None:value['error_type']=error_type
    body=json.dumps(value).encode()
    fd=os.open(folder/'work/worker-diagnostic.json',os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    try:os.write(fd,body);os.fsync(fd)
    finally:os.close(fd)


def read_worker_diagnostic(folder):
    import stat
    fallback={'phase':'unknown','error':'diagnostic_unavailable'}
    try:
        fd=os.open(folder/'work/worker-diagnostic.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            meta=os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=959 or meta.st_nlink!=1 or not 0<meta.st_size<=1024:return fallback
            value=json.loads(os.read(fd,1025))
        finally:os.close(fd)
        if (type(value) is not dict or not {'phase','error'}<=set(value)<= {'phase','error','subphase','error_type'}
                or value['phase'] not in WORKER_PHASES
                or value['error'] is not None and value['error'] not in FAILURES
                or 'subphase' in value and value['subphase'] not in SUBPHASES
                or 'error_type' in value and value['error_type'] not in ERROR_TYPES):return fallback
        return value
    except (OSError,ValueError,TypeError):return fallback


def scoped_ids(docker, scope, *, network=False):
    args=['network','ls','-q','--no-trunc'] if network else ['ps','-aq','--no-trunc']
    args+=['--filter','label=io.cloudworkbench.provider-reservation='+scope['lease']['reservation']['reservation_id'],
           '--filter','label=io.cloudworkbench.provider-request='+scope['lease']['request_id']]
    return docker(args).decode().splitlines()


def validate_bundle(bundle):
    if set(bundle['sources']) != set(MODULES) or set(bundle['hashes']) != set(MODULES):
        raise ValueError('invalid_source_set')
    if any(digest(bundle['sources'][name].encode()) != bundle['hashes'][name] for name in MODULES):
        raise ValueError('source_binding_mismatch')
    if digest(bundle['harness'].encode()) != bundle['harness_sha256']:
        raise ValueError('harness_binding_mismatch')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', bundle['base_image']): raise ValueError('invalid_image')
    if not re.fullmatch(r'snapshotqual-[0-9a-f]{32}', bundle['run_id']): raise ValueError('invalid_run_id')


def prepare(root, output, image):
    sources = {name:(root/'src/cloudworkbench'/name).read_text() for name in MODULES}
    harness = Path(__file__).read_text()
    bundle = {'run_id':'snapshotqual-'+uuid.uuid4().hex,'base_image':image,'sources':sources,
        'hashes':{name:digest(value.encode()) for name,value in sources.items()},
        'harness':harness,'harness_sha256':digest(harness.encode()),'scope':'synthetic only'}
    validate_bundle(bundle)
    with output.open('x') as stream: json.dump(bundle,stream,indent=2);stream.write('\n')
    return bundle


def validate_receipt(receipt, bundle):
    validate_services(receipt.get('services_before'));validate_services(receipt.get('services_after'))
    if (receipt.get('run_id') != bundle['run_id'] or receipt.get('host') != 'archived-worker.invalid'
            or receipt.get('source_hashes') != bundle['hashes']
            or receipt.get('base_image') != bundle['base_image']
            or receipt.get('harness_sha256') != bundle['harness_sha256']):
        raise ValueError('receipt_binding_mismatch')
    if (receipt.get('passed') is not True or receipt.get('provider_calls') is not False
            or receipt.get('real_credentials') is not False
            or receipt.get('services_before') != receipt.get('services_after')
            or receipt.get('worker_diagnostic') != {'phase':'complete','error':None}
            or receipt.get('worker_exit_code') != 0
            or receipt.get('base_alias') != base_alias_name(bundle['run_id'])
            or receipt.get('base_alias_removed') is not True
            or receipt.get('base_layers_verified') is not True
            or receipt.get('build',{}).get('exit_code') != 0
            or receipt.get('build',{}).get('error') is not None
            or receipt.get('build',{}).get('truncated') is not False
            or receipt.get('direct_source_errno') != 13
            or receipt.get('cleanup',{}).get('complete') is not True
            or receipt['cleanup'].get('remaining') != []):
        raise ValueError('qualification_incomplete')
    worker=receipt.get('worker',{})
    checks=worker.get('checks',{})
    if worker.get('uid')!=959 or worker.get('gid')!=960 or 959 not in worker.get('groups',[]):
        raise ValueError('worker_identity_unproven')
    required={'normal_release_removes_snapshot','missing_auth_recovery','newer_owner_preserved',
              'source_original_unchanged','readonly_mounts','no_owned_resources'}
    if set(checks)!=required or any(v is not True for v in checks.values()):
        raise ValueError('checks_incomplete')
    outputs=worker.get('synthetic_results',[])
    if len(outputs)!=4: raise ValueError('synthetic_result_count')
    for item in outputs:
        if (item.get('fixture_only') is not True or item.get('provider_calls') is not False
                or item.get('uid')!=958 or item.get('gid')!=959 or item.get('source_uid')!=959
                or item.get('source_gid')!=959 or item.get('mode')!=0o440
                or item.get('sha256')!=worker.get('synthetic_source_sha256')
                or item.get('write_errno') not in (1,13,30) or item.get('chmod_errno') not in (1,30)):
            raise ValueError('mount_identity_unproven')
    return True


def synthetic_collected_result(dispatch, store, request_id):
    # Qualification evidence only: never a client response or production delivery.
    if dispatch.collect(request_id) is not True:raise RuntimeError('fixture_collection_refused')
    with store._connect() as db:
        row=db.execute('SELECT state,response,response_digest FROM provider_dispatch WHERE request_id=?',
                       (request_id,)).fetchone()
    if row is None or row['state']!='collected':raise RuntimeError('fixture_not_collected')
    raw=row['response']
    if type(raw) is not bytes or not 0<len(raw)<=4096 or digest(raw)!=row['response_digest']:
        raise RuntimeError('fixture_response_binding_failed')
    value=json.loads(raw)
    keys={'fixture_only','uid','gid','source_uid','source_gid','mode','sha256','write_errno','chmod_errno','provider_calls'}
    if (type(value) is not dict or set(value)!=keys or value['fixture_only'] is not True
            or value['provider_calls'] is not False):raise RuntimeError('fixture_response_shape_failed')
    return value


def worker_main(folder):
    import socket
    if socket.gethostname()!='archived-worker.invalid' or os.geteuid()!=959 or os.getegid()!=960:
        raise RuntimeError('wrong_worker_identity')
    worker_mark(folder,'setup')
    config=json.loads((folder/'worker-config.json').read_text())
    sys.path.insert(0,str(folder/'source'))
    from dataclasses import asdict
    from cloudworkbench.store import Store
    from cloudworkbench.provider_leases import ProviderLeases
    from cloudworkbench.provider_dispatch import ProviderDispatch
    from cloudworkbench.provider_docker import DockerConfig
    from cloudworkbench.provider_auth_snapshot import AuthSnapshots
    from cloudworkbench.snapshot_provider_docker import SnapshotProviderDocker
    from cloudworkbench.native_responses import NativeProfile
    work=folder/'work';source=work/'dedicated/auth.json';original=source.read_bytes()
    result={'uid':os.geteuid(),'gid':os.getegid(),'groups':os.getgroups(),'checks':{},
            'synthetic_source_sha256':digest(original),'synthetic_results':[]}
    store=Store(work/'controller.db')
    principal=store.add_client('synthetic','x'*40,['submit'],['synthetic'])
    attempt=store.create_session(principal,{'project_id':'synthetic','agent':'hermes','goal':'synthetic fixture'},'one')
    store.claim_next();holder={};targets=[]
    def verifier(target):
        targets.append(target)
        return holder['runtime'].cleanup(target)
    leases=ProviderLeases(store,cleanup_verifier=verifier,inspector_id='snapshot-qualification')
    leases.register_account(config['run_id'],legacy_agent='claude',persistent_owner_id='synthetic-owner')
    profile=NativeProfile('openai-codex','gpt-6-astra','high')
    snapshots=AuthSnapshots(leases,source=source,dedicated_root=source.parent,destination_root=work/'snapshots',
        profile=profile,source_uid=959,provider_gid=959,account_id=config['run_id'],persistent_owner_id='synthetic-owner')
    runtime=SnapshotProviderDocker(DockerConfig(config['image'],folder/'profile.json',config['profile_sha256'],
        profile.digest,source,work/'docker',('snapshot.invalid',),inspector_id='snapshot-qualification'),
        snapshots,cancel_check=lambda spec:False)
    holder['runtime']=runtime;dispatch=ProviderDispatch(leases,runtime);scopes=[]
    payload=b'{"synthetic_snapshot_probe":true}'
    def save_scopes():
        path=work/'owned-scopes.json';temporary=work/'owned-scopes.next'
        temporary.write_text(json.dumps(scopes));temporary.chmod(0o600)
        with temporary.open('rb') as f:os.fsync(f.fileno())
        temporary.replace(path)
    def request(owner):
        def mark(subphase):worker_mark(folder,WORKER_PHASE,subphase=subphase)
        mark('grant')
        grant=leases.issue_grant(owner,attempt_id=attempt['attempt_id'],generation=1)
        mark('admit')
        spec=dispatch.admit(owner,grant,request_nonce=uuid.uuid4().hex+uuid.uuid4().hex,payload=payload,profile_digest=profile.digest)
        mark('scope_journal')
        scopes.append(asdict(spec));save_scopes()  # exact fallback scope before any Docker RPC
        mark('launch')
        resources=dispatch.launch(spec.lease.request_id,payload)
        mark('collect')
        value=synthetic_collected_result(dispatch,store,spec.lease.request_id)
        mark('response')
        assert value['fixture_only'] and value['sha256']==digest(original)
        result['synthetic_results'].append(value)
        mark('inspect')
        obj=json.loads(runtime.command(['inspect',resources.provider_id],deadline=time.monotonic()+5))[0]
        mark('mount')
        mount=next(m for m in obj['Mounts'] if m['Destination']=='/run/secrets/native-auth.json')
        path=snapshots.expected_path(spec.lease,attempt_id=spec.attempt_id,generation=spec.generation)
        assert mount['Source']==str(path) and mount['RW'] is False
        return spec,path
    owner=leases.reserve(config['run_id'],persistent_owner_id='synthetic-owner')
    worker_mark(folder,'normal_request');first,path=request(owner)
    worker_mark(folder,'normal_cleanup');dispatch.cleanup(first.lease.request_id)
    assert not path.parent.exists();result['checks']['normal_release_removes_snapshot']=True
    worker_mark(folder,'missing_request');missing,path=request(owner);path.unlink()
    worker_mark(folder,'missing_cleanup');dispatch.cleanup(missing.lease.request_id)
    assert not path.parent.exists();result['checks']['missing_auth_recovery']=True
    worker_mark(folder,'old_owner_request');old,old_path=request(owner)
    worker_mark(folder,'old_owner_cleanup');leases.cleanup_owner(owner);old_target=targets[-1]
    assert old_path.exists()
    newer=leases.reserve(config['run_id'],persistent_owner_id='synthetic-owner')
    worker_mark(folder,'new_owner_request');current,new_path=request(newer)
    worker_mark(folder,'old_material_cleanup')
    runtime.after_owner_cleanup(old_target)
    assert not old_path.exists() and new_path.exists() and leases.active_request(newer)==current.lease
    for args in (['ps','-aq','--no-trunc'],['network','ls','-q','--no-trunc']):
        owned=runtime.command(args+['--filter','label=io.cloudworkbench.provider-reservation='+newer.reservation_id],deadline=time.monotonic()+5)
        assert len(owned.decode().splitlines())==2
    result['checks']['newer_owner_preserved']=True
    worker_mark(folder,'new_owner_cleanup');dispatch.cleanup(current.lease.request_id);leases.cleanup_owner(newer)
    runtime.after_owner_cleanup(targets[-1])
    result['checks']['readonly_mounts']=True
    worker_mark(folder,'final_checks')
    info=source.stat();assert source.read_bytes()==original and info.st_uid==959 and info.st_gid==960 and info.st_mode&0o777==0o600
    result['checks']['source_original_unchanged']=True
    for scope in scopes:
        reservation=scope['lease']['reservation']['reservation_id']
        for args in (['ps','-aq','--no-trunc'],['network','ls','-q','--no-trunc']):
            assert not runtime.command(args+['--filter','label=io.cloudworkbench.provider-reservation='+reservation],deadline=time.monotonic()+5).strip()
    result['checks']['no_owned_resources']=True
    worker_mark(folder,'complete')
    print(json.dumps(result))


def owned_object(obj, scope, image, network=False):
    """Exact per-request cleanup scope; no broad daemon prune."""
    lease=scope['lease'];owner=lease['reservation']
    labels=obj.get('Labels',{}) if network else obj.get('Config',{}).get('Labels',{})
    common={'io.cloudworkbench.provider-reservation':owner['reservation_id'],
            'io.cloudworkbench.provider-epoch':str(owner['epoch']),
            'io.cloudworkbench.provider-request':lease['request_id'],
            'io.cloudworkbench.provider-launch':scope['launch_nonce']}
    if any(labels.get(k)!=v for k,v in common.items()):raise ValueError('cleanup_owner_mismatch')
    if not re.fullmatch('[0-9a-f]{64}',obj.get('Id','')):raise ValueError('invalid_runtime_id')
    if network:
        role=labels.get('io.cloudworkbench.provider-network')
        if role not in ('internal','external') or obj.get('Name')!=scope[role+'_network_name']:
            raise ValueError('cleanup_network_mismatch')
    elif obj.get('Image')!=image or obj.get('Name') not in ('/'+scope['provider_name'],'/'+scope['gateway_name']):
        raise ValueError('cleanup_container_mismatch')
    return obj['Id']


def remote_main(bundle):
    import errno,pwd,signal,socket,stat,tempfile
    validate_bundle(bundle)
    if socket.gethostname()!='archived-worker.invalid' or os.geteuid()!=0:raise RuntimeError('wrong_target')
    account=pwd.getpwnam('cloud-worker')
    groups=os.getgrouplist(account.pw_name,account.pw_gid)
    if account.pw_uid!=959 or account.pw_gid!=960 or 959 not in groups:raise RuntimeError('worker_identity_changed')
    folder=Path(tempfile.mkdtemp(prefix='cwb2-'+bundle['run_id']+'-')).resolve();folder.chmod(0o755)
    package=folder/'source/cloudworkbench';package.mkdir(parents=True)
    for name,source in bundle['sources'].items():
        (package/name).write_text(source);(package/name).chmod(0o444)
    sys.path.insert(0,str(package.parent))
    from cloudworkbench.provider_docker import BoundedDocker
    cli=BoundedDocker('/usr/bin/docker',folder/'root-docker-cli')
    def docker(args,seconds=15):return cli(args,deadline=time.monotonic()+seconds,limit=2*1024*1024)
    receipt={'run_id':bundle['run_id'],'host':socket.gethostname(),'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'folder':str(folder),'source_hashes':bundle['hashes'],'harness_sha256':bundle['harness_sha256'],
        'base_image':bundle['base_image'],'phase':'preflight','passed':False,'provider_calls':False,'real_credentials':False}
    build_started=False;build_confirmed=False;alias_attempted=False
    alias=base_alias_name(bundle['run_id'])
    image=None;tag='cwb2-snapshot-qualification:'+bundle['run_id'];direct_name='cwb2-'+bundle['run_id']+'-dac'
    source=None;uncertain=False;cleanup=[]
    try:
        receipt['services_before']=service_snapshot()
        available=int(next(v.split()[1] for v in Path('/proc/meminfo').read_text().splitlines() if v.startswith('MemAvailable:')))
        fs=os.statvfs('/var/lib/docker')
        if available<2*1024*1024 or fs.f_bavail*fs.f_frsize<2*1024**3:raise RuntimeError('insufficient_headroom')
        base=json.loads(docker(['image','inspect',bundle['base_image']]))[0]
        if base['Id']!=bundle['base_image']:raise RuntimeError('base_image_changed')
        require_existing_base_tag(base)
        assert_alias_absent(docker,alias)
        alias_attempted=True
        docker(['image','tag',bundle['base_image'],alias])
        verify_alias(docker,alias,bundle['base_image'])
        receipt['base_alias']=alias
        build=folder/'build';build.mkdir(mode=0o700)
        (build/'provider_main.py').write_text(FIXTURE)
        (build/'Dockerfile').write_text('FROM '+alias+'\nUSER 0\nCOPY --chmod=0444 provider_main.py /opt/cloud-provider/cloudworkbench/provider_main.py\nRUN rm -rf /opt/cloud-provider/cloudworkbench/__pycache__\nUSER 958:959\n')
        receipt['phase']='image_build'
        verify_alias(docker,alias,bundle['base_image'])
        build_started=True
        receipt['build']=synthetic_build(folder,build,tag,bundle['run_id'])
        if receipt['build']['error']:raise RuntimeError(receipt['build']['error'])
        metadata=json.loads(docker(['image','inspect',tag]))[0];image=metadata['Id']
        if metadata['Config']['Labels'].get(LABEL)!=bundle['run_id']:raise RuntimeError('image_owner_mismatch')
        verify_base_layers(base,metadata)
        receipt['base_layers_verified']=True
        build_confirmed=True
        receipt['synthetic_image']=image;receipt['fixture_sha256']=digest(FIXTURE.encode())
        receipt['phase']='synthetic_setup'
        work=folder/'work';work.mkdir(mode=0o700);os.chown(work,959,960)
        for name in ('dedicated','snapshots','docker'):
            p=work/name;p.mkdir(mode=0o700);os.chown(p,959,960)
        source=work/'dedicated/auth.json'
        claims=base64.urlsafe_b64encode(json.dumps({'exp':time.time()+3600}).encode()).decode().rstrip('=')
        source.write_text(json.dumps({'auth_mode':'chatgpt','tokens':{'access_token':'header.'+claims+'.signature','refresh_token':'SYNTHETIC-SNAPSHOT-ONLY'}}))
        os.chown(source,959,960);source.chmod(0o600)
        body=json.dumps({'transport':'native-responses-v1','provider':'openai-codex','model':'gpt-6-astra','effort':'high'}).encode()
        (folder/'profile.json').write_bytes(body);(folder/'profile.json').chmod(0o444)
        (folder/'worker-config.json').write_text(json.dumps({'run_id':bundle['run_id'],'image':image,'profile_sha256':digest(body)}))
        receipt['phase']='direct_source_probe'
        probe="import os,json,errno\ntry: os.open('/auth',os.O_RDONLY)\nexcept OSError as e: assert e.errno==errno.EACCES; print(json.dumps({'errno':e.errno}))\nelse: raise AssertionError('source readable')"
        docker(['create','--name',direct_name,'--label',LABEL+'='+bundle['run_id'],'--network','none','--read-only',
            '--user','958:959','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','64m','--pids-limit','16',
            '--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2',
            '--mount','type=bind,src='+str(source)+',dst=/auth,readonly','--entrypoint','/opt/hermes/venv/bin/python',image,'-c',probe])
        docker(['start','--attach',direct_name],20)
        obj=json.loads(docker(['inspect',direct_name]))[0]
        assert obj['Image']==image and obj['Config']['Labels'].get(LABEL)==bundle['run_id'] and obj['State']['ExitCode']==0
        assert obj['Config']['User']=='958:959' and obj['HostConfig']['NetworkMode']=='none'
        assert len(obj['Mounts'])==1 and obj['Mounts'][0]['Source']==str(source) and obj['Mounts'][0]['Destination']=='/auth' and obj['Mounts'][0]['RW'] is False
        receipt['direct_source_errno']=json.loads(docker(['logs',obj['Id']]))['errno']
        docker(['rm',obj['Id']])
        script=folder/'harness.py';script.write_text(bundle['harness']);script.chmod(0o444)
        def worker_identity():os.setgroups(groups);os.setgid(960);os.setuid(959)
        receipt['phase']='worker'
        child=subprocess.Popen([sys.executable,str(script),'--worker-entry',str(folder)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':str(work),'PYTHONDONTWRITEBYTECODE':'1'},
            preexec_fn=worker_identity,start_new_session=True,cwd=str(work))
        try:out,err=child.communicate(timeout=150)
        except subprocess.TimeoutExpired:
            uncertain=True;os.killpg(child.pid,signal.SIGKILL);child.communicate(timeout=5);raise RuntimeError('worker_timeout_unknown')
        receipt['worker_diagnostic']=read_worker_diagnostic(folder)
        receipt['worker_exit_code']=child.returncode
        if len(out)>262144 or len(err)>262144:raise RuntimeError('worker_output_limit')
        if child.returncode:raise RuntimeError('worker_fixture_failed')
        receipt['worker']=json.loads(out)
    except Exception as exc:
        receipt['failure_phase']=receipt['phase']
        receipt['worker_diagnostic']=read_worker_diagnostic(folder)
        receipt['error']=str(exc) if str(exc) in ('worker_timeout_unknown','worker_fixture_failed','insufficient_headroom','build_unavailable','build_timeout','build_command_failed','build_output_limit','base_alias_exists','base_alias_changed','base_layers_changed','docker_command_failed','docker_timeout','docker_output_limit','docker_unavailable') else type(exc).__name__
    finally:
        receipt['phase']='cleanup'
        alias_error=None
        if alias_attempted:
            try:
                cleanup_base_alias(docker,alias,bundle['base_image'])
                receipt['base_alias_removed']=True
            except Exception:
                alias_error='base_alias_cleanup_unconfirmed'
                receipt['base_alias_removed']=False
        if build_started and not build_confirmed:uncertain=True
        remaining=[alias_error] if alias_error else []
        try:
            receipt['cleanup_phase']='dispatch_objects'
            scopes_path=folder/'work/owned-scopes.json'
            if scopes_path.exists() and scopes_path.stat().st_size>=262144:raise RuntimeError('scope_file_limit')
            scopes=json.loads(scopes_path.read_text()) if scopes_path.exists() else []
            if len(scopes)>8:raise RuntimeError('scope_limit')
            for scope in scopes:
                for network in (False,True):
                    identities=scoped_ids(docker,scope,network=network)
                    for identity in identities:
                        obj=json.loads(docker((['network','inspect'] if network else ['inspect'])+[identity]))[0]
                        actual=owned_object(obj,scope,image,network)
                        docker((['network','rm'] if network else ['rm','-f'])+[actual]);cleanup.append(actual)
                    remaining.extend(scoped_ids(docker,scope,network=network))
            # The only non-dispatch container has its own exact name+image+label.
            receipt['cleanup_phase']='direct_probe'
            direct=docker(['ps','-aq','--no-trunc','--filter','label='+LABEL+'='+bundle['run_id']]).decode().splitlines()
            for identity in direct:
                obj=json.loads(docker(['inspect',identity]))[0]
                if obj['Name']!='/'+direct_name or obj['Image']!=image or obj['Config']['Labels'].get(LABEL)!=bundle['run_id']:
                    raise RuntimeError('unresolved_owned_container')
                docker(['rm','-f',identity]);cleanup.append(identity)
            remaining.extend(docker(['ps','-aq','--no-trunc','--filter','label='+LABEL+'='+bundle['run_id']]).decode().splitlines())
            if image and not remaining and not uncertain:
                receipt['cleanup_phase']='image'
                current=json.loads(docker(['image','inspect',tag]))[0]
                if current['Id']!=image or current['Config']['Labels'].get(LABEL)!=bundle['run_id']:raise RuntimeError('image_cleanup_mismatch')
                docker(['image','rm',tag]);cleanup.append(image)
                remaining.extend(docker(['image','ls','-aq','--no-trunc','--filter','label='+LABEL+'='+bundle['run_id']]).decode().splitlines())
            if not remaining and not uncertain and source is not None:
                receipt['cleanup_phase']='synthetic_files'
                # Fresh isolated synthetic-only tree; do not recurse or touch unrelated paths.
                paths=[source]
                snapshot_root=folder/'work/snapshots'
                if snapshot_root.exists():
                    for entry in snapshot_root.iterdir():
                        if entry.is_symlink() or not entry.is_dir():raise RuntimeError('unsafe_snapshot_residual')
                        p=entry/'auth.json'
                        if p.exists():paths.append(p)
                for p in paths:
                    if p.exists():
                        st=p.lstat()
                        if not stat.S_ISREG(st.st_mode) or st.st_uid!=959 or st.st_nlink!=1:raise RuntimeError('unsafe_synthetic_cleanup')
                        p.unlink()
            receipt['cleanup_phase']='service_readback'
            receipt['services_after']=service_snapshot()
            receipt['cleanup_phase']='complete'
        except Exception as exc:
            receipt['cleanup_failure_phase']=receipt['cleanup_phase']
            receipt['cleanup_error']=safe_worker_failure(exc)
            remaining.append('unconfirmed:'+type(exc).__name__)
        receipt['cleanup']={'complete':not remaining and not uncertain,'remaining':remaining,'removed_exact_ids':cleanup,'unknown_worker_effects':uncertain}
        receipt['passed']=bool('worker' in receipt and not receipt.get('error') and receipt['cleanup']['complete'])
        if receipt['passed']:
            try:validate_receipt(receipt,bundle)
            except ValueError as exc:receipt['passed']=False;receipt['error']=str(exc)
        receipt['phase']='complete' if receipt['passed'] else 'failed'
        (folder/'qualification.json').write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt))
    return receipt


def main():
    if sys.argv[1:2]==['--remote-entry']:
        return remote_main(QUALIFICATION_BUNDLE)
    if sys.argv[1:2]==['--worker-entry']:
        folder=Path(sys.argv[2])
        try:return worker_main(folder)
        except Exception as exc:
            try:worker_mark(folder,WORKER_PHASE,safe_worker_failure(exc),subphase=WORKER_SUBPHASE,
                            error_type=type(exc).__name__ if type(exc).__name__ in ERROR_TYPES else 'unknown')
            except Exception:pass
            raise SystemExit(1) from None
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare',action='store_true');mode.add_argument('--remote',action='store_true')
    parser.add_argument('--prepared',type=Path);parser.add_argument('--image');parser.add_argument('--output',type=Path)
    args=parser.parse_args();root=Path(__file__).resolve().parents[2]
    if args.prepare:
        image=args.image or json.loads((root/'evidence/provider-image-candidate.json').read_text())['image_id']
        path=args.output or root/'evidence'/('snapshot-linux-prepared-'+uuid.uuid4().hex+'.json')
        bundle=prepare(root,path,image)
        print(json.dumps({'prepared':str(path),'run_id':bundle['run_id'],'remote_executed':False}))
    else:
        if args.prepared is None:parser.error('--remote requires inspected --prepared artifact')
        bundle=json.loads(args.prepared.read_text());validate_bundle(bundle)
        program='QUALIFICATION_BUNDLE='+repr(bundle)+'\n'+bundle['harness']
        result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',REMOTE,
            'sudo -n /opt/cloud-workbench/.venv/bin/python - --remote-entry'],input=program,capture_output=True,text=True,timeout=360)
        output=args.output or root/'evidence'/('snapshot-linux-'+bundle['run_id']+'.json')
        with output.open('x') as stream:stream.write(result.stdout)
        if result.returncode or len(result.stdout)>1024*1024:raise RuntimeError('remote_qualification_failed')
        receipt=json.loads(result.stdout);validate_receipt(receipt,bundle)
        print(json.dumps({'receipt':str(output),'passed':True,'scope':'synthetic Linux mount/lifecycle only'}))


if __name__=='__main__':main()
