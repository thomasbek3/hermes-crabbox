#!/usr/bin/env python3
"""Prepare, or explicitly run as Linux root, an isolated numeric-UID bootstrap proof.

No SSH, Docker, provider inference, credential import or service mutation. The
prepared bundle freezes this helper and the relative-import source closure.
"""

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

import argparse
import ast
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
START_MODULES = ('routed_bootstrap', 'worker_service', 'workflow_revisions', 'store',
                 'scheduler', 'hermes_adapter', 'native_responses', 'workflow_instructions',
                 # Bootstrap copies these data-declared payloads; imports alone
                 # cannot discover that dependency. Keep parity with SOURCE_FILES.
                 '__init__', 'routed_caller', 'pstack_routing', 'inference_relay',
                 'inference_service', 'adapters')
SERVICES = ('cloud-workbench-api.service', 'cloud-workbench-worker.service', 'cloudd.service')
WORKER_GROUPS = (959, 966)
MARKER = 'synthetic bootstrap permission fixture\n'
CHILDREN = []


def sha(value): return hashlib.sha256(value).hexdigest()


def source_closure(root):
    pending = [*START_MODULES, '__init__']; result = {}
    while pending:
        name = pending.pop()
        if name + '.py' in result: continue
        if not re.fullmatch('[a-z_]+', name): raise ValueError('invalid_module_name')
        body = (root / 'src/cloudworkbench' / (name + '.py')).read_text()
        result[name + '.py'] = body
        for node in ast.walk(ast.parse(body)):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.level != 1: raise ValueError('unsupported_relative_import')
                pending.extend([node.module.split('.')[0]] if node.module else [a.name for a in node.names])
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                for module in names:
                    if module and module.startswith('cloudworkbench.'):
                        pending.append(module.split('.')[1])
    return result


def validate_bundle(value):
    if (type(value) is not dict or set(value) != {'version','run_id','sources','hashes','harness','harness_sha256'}
            or type(value['version']) is not int or value['version'] != 1 or type(value['run_id']) is not str
            or not re.fullmatch('[0-9a-f]{16}', value['run_id'])
            or type(value['sources']) is not dict or type(value['hashes']) is not dict
            or set(value['sources']) != set(value['hashes'])
            or not {n+'.py' for n in (*START_MODULES,'__init__')} <= set(value['sources'])
            or type(value['harness']) is not str or sha(value['harness'].encode()) != value['harness_sha256']):
        raise ValueError('frozen_bundle_mismatch')
    for name, body in value['sources'].items():
        if (not re.fullmatch(r'[a-z_]+\.py', name) or type(body) is not str or len(body.encode()) > 2*1024**2
                or sha(body.encode()) != value['hashes'][name]): raise ValueError('frozen_source_mismatch')
        for node in ast.walk(ast.parse(body)):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.level != 1: raise ValueError('unsupported_relative_import')
                dependencies = [node.module.split('.')[0]] if node.module else [a.name for a in node.names]
                if any(n+'.py' not in value['sources'] for n in dependencies): raise ValueError('incomplete_source_closure')
    return value


def prepare(output):
    import secrets
    sources = source_closure(ROOT); harness = Path(__file__).read_text()
    value = {'version':1, 'run_id':secrets.token_hex(8), 'sources':sources,
             'hashes':{n:sha(t.encode()) for n,t in sources.items()},
             'harness':harness, 'harness_sha256':sha(harness.encode())}
    validate_bundle(value)
    with Path(output).open('x') as stream: json.dump(value,stream,indent=2); stream.write('\n')
    return {'prepared':True, 'run_id':value['run_id'], 'modules':len(sources),
            'bundle_sha256':sha(Path(output).read_bytes()), 'harness_sha256':value['harness_sha256']}


def command(args):
    return subprocess.check_output(args, text=True, timeout=5, env={'PATH':'/usr/bin:/bin','LANG':'C'}).strip()


def identities():
    passwd = command(['getent','passwd','959']).split(':')
    if len(passwd) != 7 or passwd[2:4] != ['959','960']: raise ValueError('worker_identity_mismatch')
    groups = sorted(int(v) for v in command(['id','-G',passwd[0]]).split())
    if groups != [959,960,966]: raise ValueError('worker_groups_mismatch')
    group_records = {str(n):command(['getent','group',str(n)]).split(':')[:3] for n in (959,960,966)}
    if any(v[2] != n for n,v in group_records.items()): raise ValueError('group_identity_mismatch')
    return {'name':passwd[0], 'uid':959, 'primary_gid':960, 'groups':groups,'group_records':group_records}


def service_pids():
    result={}
    for name in SERVICES:
        values=dict(line.split('=',1) for line in command(['systemctl','show',name,'--property=MainPID','--property=ActiveState']).splitlines())
        if set(values)!={'MainPID','ActiveState'} or values['ActiveState']!='active' or not values['MainPID'].isdigit() or int(values['MainPID'])<=0:
            raise ValueError('service_identity_unavailable')
        result[name]={'MainPID':int(values['MainPID']),'ActiveState':'active'}
    return result


def cleanup_safe(root, identity, mountinfo):
    import stat
    info=root.lstat()
    if (info.st_dev,info.st_ino)!=identity or info.st_uid!=0 or root.is_symlink():return False
    for line in mountinfo.splitlines():
        fields=line.split()
        if len(fields)<6:raise ValueError('invalid_mountinfo')
        decoded=re.sub(r'\\([0-7]{3})',lambda match:chr(int(match[1],8)),fields[4])
        target=Path(decoded)
        if target==root or root in target.parents:return False
    for directory,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            value=(Path(directory)/name).lstat()
            if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):return False
    return True


def _validate_receipt(value,bundle):
    validate_bundle(bundle)
    if (value.get('status')!='passed' or value.get('hostname')!='archived-worker.invalid'
            or value.get('run_id')!=bundle['run_id'] or value.get('source_hashes')!=bundle['hashes']
            or value.get('harness_sha256')!=bundle['harness_sha256']
            or value.get('identity_before')!=value.get('identity_after')
            or value.get('service_pids_before')!=value.get('service_pids_after')
            or value.get('remote_inference') is not False or value.get('docker_used') is not False
            or value.get('scoped_fd_entry_not_actual_bind_mount') is not True
            or any(value.get(k) is not True for k in ('identities_unchanged','service_pids_unchanged','children_reaped','child_groups_absent','fixture_absent'))
            or value.get('cleanup')!='exact_fixture_removed'):
        raise ValueError('qualification_receipt_invalid')
    identity=value['identity_before']
    if (identity.get('uid')!=959 or identity.get('primary_gid')!=960 or identity.get('groups')!=[959,960,966]
            or not isinstance(identity.get('name'),str) or not identity['name']):raise ValueError('qualification_identity_invalid')
    states=value['service_pids_before']
    if set(states)!=set(SERVICES) or any(v.get('ActiveState')!='active' or type(v.get('MainPID')) is not int or v['MainPID']<=0 for v in states.values()):
        raise ValueError('qualification_services_invalid')
    worker=value['worker'];relay=value['relay'];tool=value['tool'];service=value['service'];closed=service['closed']
    if (worker.get('ready') is not True or worker.get('worker_uid')!=959 or worker.get('worker_gid')!=960
            or sorted(worker.get('worker_groups',[]))!=list(WORKER_GROUPS)
            or worker.get('socket_gid')!=1001 or worker.get('socket_mode')!=0o660
            or json.dumps(relay,sort_keys=True)!=json.dumps({'uid':1001,'gid':1001,'connected':True,'status':200},sort_keys=True)
            or json.dumps(tool,sort_keys=True)!=json.dumps({'uid':1000,'gid':1000,'task_read':True,'worker_config_denied':True,'source_read':True,'source_write_denied':True,'scratch_write':True},sort_keys=True)
            or type(service.get('synthetic_calls')) is not int or service.get('synthetic_calls')!=1 or sorted(service.get('worker_groups_after',[]))!=list(WORKER_GROUPS)
            or any(closed.get(k) is not True for k in ('started','stop_requested','thread_stopped','listener_closed','resources_closed'))
            or type(closed.get('active_callbacks')) is not int or closed.get('active_callbacks')!=0 or closed.get('timed_out') is not False or closed.get('error') is not None
            or not re.fullmatch('[0-9a-f]{32}',closed.get('service_id',''))):raise ValueError('qualification_permission_invalid')
    binding=closed.get('binding',{})
    if (binding.get('attempt_id')!=value.get('attempt_id') or type(binding.get('generation')) is not int or binding.get('generation')!=1
            or binding.get('profile_digest')!=value.get('profile_digest')
            or any(not re.fullmatch('[0-9a-f]{64}',value.get(k,'')) for k in ('profile_digest','bootstrap_binding_digest','bootstrap_receipt_sha256'))):
        raise ValueError('qualification_binding_invalid')
    parent=Path('/tmp')/('cwb2-workerqual-'+bundle['run_id'])/'b'/(sha(value['attempt_id'].encode())[:24]+'.1')/'stages/review'
    if worker.get('source')!=str(parent/'source') or worker.get('scratch')!=str(parent/'scratch'):
        raise ValueError('qualification_paths_invalid')
    return value



def validate_receipt(value,bundle):
    try:return _validate_receipt(value,bundle)
    except (TypeError,KeyError,AttributeError,ValueError):raise ValueError('qualification_receipt_invalid') from None


def drop_identity(uid,gid,groups):
    def child():
        import resource
        os.setgroups(groups); os.setgid(gid); os.setuid(uid)
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        resource.setrlimit(resource.RLIMIT_CPU,(45,45))
        resource.setrlimit(resource.RLIMIT_AS,(512*1024**2,512*1024**2))
        resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
    return child


def spawn(args, *, uid, gid, groups=(), pass_fds=()):
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'PATH':'/usr/bin:/bin','LANG':'C','HOME':'/nonexistent','PYTHONDONTWRITEBYTECODE':'1'},
        preexec_fn=drop_identity(uid,gid,groups), pass_fds=pass_fds, start_new_session=True)
    CHILDREN.append(process)
    return process


def finish(child, *, data=None, timeout=15):
    try: out, err = child.communicate(input=data, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:os.killpg(child.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        child.communicate(timeout=5)
        raise ValueError('bounded_child_timeout') from None
    if child.returncode != 0 or len(out)>65536 or len(err)>65536: raise ValueError('fixture_child_failed')
    return out


def ready_line(child, timeout=15):
    deadline=time.monotonic()+timeout; data=bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout,selectors.EVENT_READ)
        while b'\n' not in data:
            left=deadline-time.monotonic()
            if left<=0 or not selector.select(left): raise ValueError('worker_ready_timeout')
            part=os.read(child.stdout.fileno(),1)
            if not part or len(data)>=65536: raise ValueError('worker_ready_invalid')
            data.extend(part)
    return json.loads(data)


def fixture_worker(root, mode):
    if os.getuid()!=959 or os.getgid()!=960 or sorted(os.getgroups())!=list(WORKER_GROUPS):
        raise ValueError('child_worker_identity_mismatch')
    sys.path.insert(0,str(root/'frozen'))
    state=root/'state'
    from cloudworkbench.store import Store
    from cloudworkbench.scheduler import RoleScheduler
    from cloudworkbench.workflow_revisions import RevisionBinding, WorkspaceRevision, capture_revision, materialize_stage
    if mode=='prepare':
        store=Store(state/'state.db');owner=store.add_client('synthetic fixture','x'*40,['submit'],['synthetic'])
        store.migrate_scheduler();scheduler=RoleScheduler(store,None)
        frozen={'accounts':{'synthetic':'owner'},'role_plans':{},'provenance':{'synthetic':True}}
        task=scheduler.enqueue_root(owner,{'project_id':'synthetic','agent':'hermes','goal':'permissions only'},'root',frozen=frozen)
        binding=RevisionBinding(owner['id'],'synthetic',task['session_id'],task['turn_id'],task['attempt_id'],1,task['attempt_id'],1)
        work=state/'work';revisions=state/'revisions';work.mkdir();revisions.mkdir()
        (work/'deliverable.txt').write_text(MARKER)
        revision=capture_revision(store,binding,work,revisions,selected_paths=('deliverable.txt',),controller_attests_quiesced=True)
        value={'binding':asdict(binding),'revision_path':str(revision.path),'revision_sha256':revision.sha256}
        (state/'revision.json').write_text(json.dumps(value));return value
    if mode!='serve':raise ValueError('invalid_worker_phase')
    from cloudworkbench.inference_relay import AttemptBinding,WorkerDispatcher,WorkerSocketServer,DispatchResult
    from cloudworkbench.inference_service import ServiceResponse
    from cloudworkbench.worker_service import WorkerService
    config=json.loads((root/'fixture.json').read_text());revision_meta=json.loads((state/'revision.json').read_text())
    binding=RevisionBinding(**revision_meta['binding']);store=Store(state/'state.db')
    revision=WorkspaceRevision(Path(revision_meta['revision_path']),revision_meta['revision_sha256'],binding)
    attempt=Path(config['attempt_root']);task=attempt/'task';socket_dir=attempt/'worker'
    client=json.loads((socket_dir/'client.json').read_text())
    # Worker reads owner files and pinned bootstrap source without tool/relay groups.
    assert (task/'launch.json').read_bytes() and (task/'http-capability').read_bytes()
    assert (task/'source/cloudworkbench/routed_caller.py').read_bytes()
    stage=materialize_stage(store,revision,binding,attempt/'stages'/'review',readonly=True,
        required_tool_gid=1000,before_publish=lambda b:b==binding)
    socket_binding=AttemptBinding(**client['binding']);calls=[]
    def execute(payload,cancel):
        assert payload=={'synthetic':'worker-bootstrap'} and not cancel.is_set()
        calls.append(1);return DispatchResult(ServiceResponse(200,'application/json',b'{"synthetic":true}'),True)
    dispatcher=WorkerDispatcher(journal_path=state/'worker.db',binding=socket_binding,
        capability_sha256=sha(client['worker_capability'].encode()),authorize=lambda b:b==socket_binding,execute=execute)
    server=WorkerSocketServer(socket_dir/'socket',dispatcher,socket_gid=1001,deadline_seconds=5)
    service=WorkerService(server);service.start()
    try:
        info=(socket_dir/'socket').stat()
        print(json.dumps({'ready':True,'worker_uid':os.getuid(),'worker_gid':os.getgid(),'worker_groups':os.getgroups(),
            'socket_gid':info.st_gid,'socket_mode':info.st_mode&0o777,'source':str(stage.source),'scratch':str(stage.scratch)}),flush=True)
        if sys.stdin.readline().strip()!='stop':raise ValueError('root_control_lost')
    finally:
        receipt=service.close(5)
    assert receipt.resources_closed and receipt.thread_stopped and receipt.active_callbacks==0
    assert calls==[1] and not (socket_dir/'socket').exists()
    return {'closed':asdict(receipt),'synthetic_calls':len(calls),'worker_groups_after':os.getgroups()}


RELAY_PROBE = r'''
import os,sys,json,socket,struct
assert os.getuid()==1001 and os.getgid()==1001 and os.getgroups()==[]
p='/proc/self/fd/'+sys.argv[1]
client=json.load(open(p+'/client.json'))
payload={'synthetic':'worker-bootstrap'}
import hashlib
raw=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
request={'binding':client['binding'],'capability':client['worker_capability'],'nonce':'c'*64,'payload_digest':hashlib.sha256(raw).hexdigest(),'payload':payload}
body=json.dumps(request).encode()
with socket.socket(socket.AF_UNIX) as sock:
 sock.settimeout(5);sock.connect(p+'/socket');sock.sendall(struct.pack('!I',len(body))+body)
 def exact(n):
  result=b''
  while len(result)<n:
   part=sock.recv(n-len(result));assert part;result+=part
  return result
 size=struct.unpack('!I',exact(4))[0];assert size<=2**20
 value=json.loads(exact(size));assert value['response']['status']==200
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'connected':True,'status':200}))
'''

TOOL_PROBE = r'''
import os,sys,json
assert os.getuid()==1000 and os.getgid()==1000 and os.getgroups()==[]
task,worker,source,scratch=['/proc/self/fd/'+v for v in sys.argv[1:]]
assert open(task+'/prompt.txt').read() and open(task+'/http-capability').read()
try:open(worker+'/client.json').read()
except PermissionError:pass
else:raise AssertionError('worker capability readable')
assert open(source+'/deliverable.txt').read()=='synthetic bootstrap permission fixture\n'
try:open(source+'/deliverable.txt','w')
except PermissionError:pass
else:raise AssertionError('review source writable')
with open(scratch+'/tool-note','x') as out:out.write('synthetic note')
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'task_read':True,'worker_config_denied':True,'source_read':True,'source_write_denied':True,'scratch_write':True}))
'''


def run(bundle, receipt_path):
    validate_bundle(bundle)
    if sys.platform!='linux' or os.geteuid()!=0:raise ValueError('linux_root_required')
    if sha(Path(__file__).read_bytes())!=bundle['harness_sha256']:raise ValueError('executed_helper_not_frozen')
    if Path(receipt_path).exists():raise ValueError('receipt_exists')
    if socket.gethostname()!='archived-worker.invalid':raise ValueError('wrong_qualification_host')
    if Path('/tmp').resolve()!=Path('/tmp'):raise ValueError('noncanonical_fixture_parent')
    before_identity=identities();before_pids=service_pids()
    root=Path('/tmp')/('cwb2-workerqual-'+bundle['run_id'])
    root.mkdir(mode=0o750);os.chown(root,0,960);root.chmod(0o750)
    root_identity=(root.stat().st_dev,root.stat().st_ino)
    child=None;descriptors=[]
    result={'hostname':socket.gethostname(),'run_id':bundle['run_id'],'source_hashes':bundle['hashes'],'harness_sha256':bundle['harness_sha256'],
        'identity_before':before_identity,'service_pids_before':before_pids,'status':'failed','remote_inference':False,
        'docker_used':False,'scoped_fd_entry_not_actual_bind_mount':True}
    try:
        package=root/'frozen/cloudworkbench';package.mkdir(parents=True)
        for name,body in bundle['sources'].items():(package/name).write_text(body)
        helper=root/'helper.py';helper.write_text(bundle['harness'])
        for directory in (root/'frozen',package):directory.chmod(0o555)
        for path in package.iterdir():path.chmod(0o444)
        helper.chmod(0o444)
        state=root/'state';state.mkdir(mode=0o700);os.chown(state,959,960)
        prepared=json.loads(finish(spawn([sys.executable,'-I',str(helper),'--worker','prepare','--root',str(root)],uid=959,gid=960,groups=WORKER_GROUPS)))
        sys.path.insert(0,str(root/'frozen'))
        from cloudworkbench.routed_bootstrap import RoutedBootstrap,BootstrapRequest,SOURCE_FILES
        from cloudworkbench.hermes_adapter import build_routed_launch
        from cloudworkbench.native_responses import NativeProfile
        from cloudworkbench.workflow_instructions import StageInstructions,STAGE_BOUNDARY
        profile=NativeProfile('openai-codex','gpt-6-astra','high')
        text='Synthetic permission fixture; no inference is executed.'
        instructions=StageInstructions('review','code_review','synthetic',sha(text.encode()),text,STAGE_BOUNDARY)
        plan=build_routed_launch(profile,instructions,task='Check permissions',input_revision_sha256=prepared['revision_sha256'],workspace_readonly=True,run_budget_seconds=10)
        binding=prepared['binding'];request=BootstrapRequest(binding['attempt_id'],binding['session_id'],1,profile.digest,plan)
        dest=root/'b';dest.mkdir(mode=0o750);os.chown(dest,0,960);dest.chmod(0o750)
        pinned={name:bundle['hashes'][name] for name in SOURCE_FILES}
        helper_api=RoutedBootstrap(destination_root=dest,source_root=package,source_hashes=pinned,
            authorize=lambda candidate:candidate.digest==request.digest,authorize_discard=lambda _:False)
        material=helper_api.provision(request)
        (root/'fixture.json').write_text(json.dumps({'attempt_root':str(material.root)}));os.chown(root/'fixture.json',0,960);(root/'fixture.json').chmod(0o440)
        assert len(os.fsencode(material.worker_socket_dir/'socket'))<108
        child=spawn([sys.executable,'-I',str(helper),'--worker','serve','--root',str(root)],uid=959,gid=960,groups=WORKER_GROUPS)
        ready=ready_line(child);assert ready['ready'] and ready['socket_gid']==1001 and ready['socket_mode']==0o660
        expected_stage=material.materialization_parent/'review'
        if ready.get('source')!=str(expected_stage/'source') or ready.get('scratch')!=str(expected_stage/'scratch'):
            raise ValueError('worker_materialization_path_mismatch')
        def descriptor(path):
            fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(fd);return fd
        task_fd=descriptor(material.task_dir);worker_fd=descriptor(material.worker_socket_dir)
        source_fd=descriptor(Path(ready['source']));scratch_fd=descriptor(Path(ready['scratch']))
        relay=json.loads(finish(spawn([sys.executable,'-I','-c',RELAY_PROBE,str(worker_fd)],uid=1001,gid=1001,pass_fds=(worker_fd,))))
        tool_fds=(task_fd,worker_fd,source_fd,scratch_fd)
        tool=json.loads(finish(spawn([sys.executable,'-I','-c',TOOL_PROBE,*map(str,tool_fds)],uid=1000,gid=1000,pass_fds=tool_fds)))
        final=json.loads(finish(child,data=b'stop\n'));child=None
        result.update(status='passed',attempt_id=request.attempt_id,profile_digest=request.profile_digest,bootstrap_binding_digest=material.binding_digest,
            bootstrap_receipt_sha256=material.receipt_sha256,worker=ready,relay=relay,tool=tool,service=final)
    except BaseException as exc:
        result['error']=type(exc).__name__
    finally:
        if child is not None:
            if child.poll() is None:
                try:os.killpg(child.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            child.communicate(timeout=5)
        for fd in descriptors:os.close(fd)
        try:
            result['identity_after']=identities();result['service_pids_after']=service_pids()
            result['identities_unchanged']=result['identity_after']==before_identity
            result['service_pids_unchanged']=result['service_pids_after']==before_pids
            if not result['identities_unchanged'] or not result['service_pids_unchanged']:result['status']='failed'
        except Exception:
            result['postflight']='unconfirmed';result['status']='failed'
        result['children_reaped']=all(process.poll() is not None for process in CHILDREN)
        result['child_pids']=[process.pid for process in CHILDREN]
        result['child_groups_absent']=True
        for process in CHILDREN:
            try:os.killpg(process.pid,0)
            except ProcessLookupError:pass
            else:result['child_groups_absent']=False
        if not result['children_reaped'] or not result['child_groups_absent']:result['status']='failed'
        import shutil
        try:
            with Path('/proc/self/mountinfo').open() as stream:mountinfo=stream.read(4*1024**2+1)
            if not result['children_reaped'] or not result['child_groups_absent'] or len(mountinfo)>4*1024**2 or not cleanup_safe(root,root_identity,mountinfo) or not shutil.rmtree.avoids_symlink_attacks:
                result['cleanup']='identity_mount_or_special_file_refused';result['status']='failed'
            else:
                shutil.rmtree(root);result['cleanup']='exact_fixture_removed';result['fixture_absent']=not root.exists()
        except Exception:
            result['cleanup']='cleanup_unconfirmed';result['status']='failed'
        if result['status']=='passed':
            try:validate_receipt(result,bundle)
            except Exception:result['status']='failed';result['error']='receipt_validation_failed'
        with Path(receipt_path).open('x') as out:json.dump(result,out,indent=2);out.write('\n')
    return result


def main():
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare',type=Path);mode.add_argument('--run',type=Path);mode.add_argument('--worker',choices=('prepare','serve'))
    parser.add_argument('--receipt',type=Path);parser.add_argument('--root',type=Path);args=parser.parse_args()
    if args.prepare: result=prepare(args.prepare)
    elif args.worker:
        result=fixture_worker(args.root,args.worker)
    else:
        if args.receipt is None:parser.error('--run requires --receipt')
        try:
            result=run(json.loads(args.run.read_text()),args.receipt)
        except Exception as exc:
            if args.receipt.exists():raise
            result={'status':'failed','phase':'preflight_or_unconfirmed_cleanup','error':type(exc).__name__,
                    'executed_helper_sha256':sha(Path(__file__).read_bytes()),
                    'cleanup':'unconfirmed','remote_inference':False,'docker_used':False}
            with args.receipt.open('x') as out:json.dump(result,out,indent=2);out.write('\n')
    print(json.dumps(result,sort_keys=True))
    return 0 if result.get('status','passed')=='passed' else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'failed','error':type(exc).__name__}),file=sys.stderr)
        raise SystemExit(1)
