#!/usr/bin/env python3
"""Prepared, exact-owned Linux Docker UID0600 export proof. No implicit remote run."""

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

import argparse
import ast
from dataclasses import asdict, fields
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import socket
import subprocess
import sys
import types

ROOT=Path(__file__).resolve().parents[2]
SUPPORT='qualify-routed-worker-bootstrap-linux.py'
IMAGE='sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
SEEDS=('routed_export','routed_export_protocol','routed_runtime','runtime')
EXPECTED={'answer.txt':(b'private-tool-output\n\x00\xff',False),
          'nested/data.bin':(bytes(range(32)),False),'run.sh':(b'echo synthetic\n',True)}
SELECTED=('answer.txt','nested','run.sh')


def sha(data):return hashlib.sha256(data).hexdigest()

WORKER_PHASE='initialization'
WORKER_FAILURE=None
PHASES=frozenset({'initialization','prepare_caller','create_caller','start_caller','tool_exec',
    'host_read_denial','stop_caller','remove_caller','export','validate_export','compare_payload',
    'reload','validate_reload','assemble_receipt','cleanup','service_closed','root','worker_transport'})
ERROR_TYPES=frozenset({'ValueError','RuntimeError','WorkspaceExportError','ExportError','WorkerServiceError',
    'RelayError','OSError','PermissionError','FileNotFoundError','JSONDecodeError','AttributeError',
    'TypeError','KeyError','AssertionError','TimeoutExpired','Exception'})


def diagnostic_policy():
    # Only literals in the inspected helper/app error branches can leave the process.
    package=Path(__file__).parent/'frozen/cloudworkbench'
    if not package.is_dir():
        loaded=sys.modules.get('cloudworkbench')
        package=Path(loaded.__file__).parent if loaded is not None and loaded.__file__ else ROOT/'src/cloudworkbench'
    paths=[Path(__file__),*(package/name for name in ('runtime.py','routed_runtime.py','routed_export.py',
        'routed_export_protocol.py','worker_service.py','inference_relay.py'))]
    codes={'detail_redacted','worker_failed','worker_timeout','worker_output_bound','worker_invalid_output',
        'runtime_error','docker_unavailable','docker_timeout','docker_daemon_unavailable','docker_image_missing',
        'docker_mount_invalid','docker_name_conflict','docker_start_failed','docker_command_failed'}
    files={}
    for path in paths:
        if not path.is_file():continue
        body=path.read_text()
        if len(body)>2*1024**2:continue
        files[path.name]=len(body.splitlines())
        for node in ast.walk(ast.parse(body)):
            if isinstance(node,ast.Raise) and isinstance(node.exc,ast.Call):
                for arg in (*node.exc.args,*(kw.value for kw in node.exc.keywords if kw.arg=='code')):
                    if isinstance(arg,ast.Constant) and type(arg.value) is str and re.fullmatch(r'[a-zA-Z0-9 _;.-]{1,128}',arg.value):
                        codes.add(arg.value)
    return codes,files


def safe_failure(exc,phase):
    codes,files=diagnostic_policy();message=str(exc);code=getattr(exc,'code',None)
    frames=[];tb=exc.__traceback__
    while tb is not None:
        name=Path(tb.tb_frame.f_code.co_filename).name;line=tb.tb_lineno
        if name in files and 0<line<=files[name]:frames.append({'file':name,'line':line})
        tb=tb.tb_next
    return {'phase':phase if phase in PHASES else 'worker_transport',
        'type':type(exc).__name__ if type(exc).__name__ in ERROR_TYPES else 'Exception',
        'code':message if message in codes else code if code in codes else 'detail_redacted','frames':frames[-8:]}


def checked_failure(value):
    codes,files=diagnostic_policy()
    if type(value) is not dict or set(value)!={'phase','type','code','frames'}:raise ValueError('worker_invalid_output')
    if value['phase'] not in PHASES or value['type'] not in ERROR_TYPES or value['code'] not in codes:raise ValueError('worker_invalid_output')
    if type(value['frames']) is not list or len(value['frames'])>8:raise ValueError('worker_invalid_output')
    for frame in value['frames']:
        if (type(frame) is not dict or set(frame)!={'file','line'} or frame['file'] not in files
            or type(frame['line']) is not int or not 0<frame['line']<=files[frame['file']]):raise ValueError('worker_invalid_output')
    return value


class WorkerFailed(ValueError):
    def __init__(self,detail,cleanup=None):
        super().__init__('worker_failed');self.detail=detail;self.cleanup=cleanup


def finish_worker(child,*,timeout=100):
    try:out,err=child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:os.killpg(child.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        child.communicate(timeout=5)
        raise WorkerFailed({'phase':'worker_transport','type':'TimeoutExpired','code':'worker_timeout','frames':[]}) from None
    generic={'phase':'worker_transport','type':'ValueError','code':'worker_failed','frames':[]}
    if len(out)>65536 or len(err)>16384:
        raise WorkerFailed({**generic,'code':'worker_output_bound'})
    if child.returncode:
        try:
            envelope=json.loads(err)
            if type(envelope) is not dict or set(envelope)!={'worker_failure','cleanup_failure'}:raise ValueError()
            detail=checked_failure(envelope['worker_failure'])
            cleanup=checked_failure(envelope['cleanup_failure']) if envelope['cleanup_failure'] is not None else None
        except (TypeError,ValueError,KeyError):detail,cleanup=generic,None
        raise WorkerFailed(detail,cleanup)
    try:
        value=json.loads(out)
        if type(value) is not dict or value.get('passed') is not True or err:raise ValueError()
    except (ValueError,TypeError):raise WorkerFailed({**generic,'code':'worker_invalid_output'}) from None
    return value


def support_module(value):
    module=types.ModuleType('frozen_worker_fixture');module.__file__=str(ROOT/'scripts/legacy'/SUPPORT)
    exec(compile(value['support'],SUPPORT,'exec'),module.__dict__)
    return module


def validate_bundle(value):
    if (type(value) is not dict or set(value)!={'version','run_id','image','sources','hashes','harness','harness_sha256','support','support_sha256'}
            or value.get('image')!=IMAGE or type(value.get('support')) is not str
            or sha(value['support'].encode())!=value.get('support_sha256')):raise ValueError('invalid_export_bundle')
    support=support_module(value)
    support.START_MODULES=tuple(dict.fromkeys((*support.START_MODULES,*SEEDS)))
    support.validate_bundle({key:value[key] for key in ('version','run_id','sources','hashes','harness','harness_sha256')})
    return support


def prepare(path):
    import secrets
    source=(ROOT/'scripts/legacy'/SUPPORT).read_text();harness=Path(__file__).read_text()
    support=support_module({'support':source});support.START_MODULES=tuple(dict.fromkeys((*support.START_MODULES,*SEEDS)))
    modules=support.source_closure(ROOT)
    value={'version':1,'run_id':secrets.token_hex(8),'image':IMAGE,'sources':modules,
           'hashes':{name:sha(body.encode()) for name,body in modules.items()},
           'harness':harness,'harness_sha256':sha(harness.encode()),'support':source,'support_sha256':sha(source.encode())}
    validate_bundle(value)
    with Path(path).open('x') as out:json.dump(value,out,indent=2);out.write('\n')
    return {'prepared':True,'run_id':value['run_id'],'modules':len(modules),'bundle_sha256':sha(Path(path).read_bytes()),'harness_sha256':value['harness_sha256']}


def docker(args,timeout=10):
    result=subprocess.run(['/usr/bin/docker',*args],capture_output=True,timeout=timeout,
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    if result.returncode or len(result.stdout)>2*1024**2 or len(result.stderr)>65536:raise ValueError('qualification_docker_failed')
    return result.stdout.decode().strip()


def inventory(owner):
    values=docker(['container','ls','--all','--no-trunc','--filter','label=io.cloudworkbench.managed=true',
        '--filter','label=io.cloudworkbench.owner='+owner,'--format','{{.ID}}']).splitlines()
    if len(values)>2 or any(not re.fullmatch('[0-9a-f]{64}',v) for v in values):raise ValueError('qualification_inventory_invalid')
    return values


def headroom():
    memory={k:int(v.split()[0]) for k,v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())}
    root=Path(docker(['info','--format','{{.DockerRootDir}}']))
    stat=os.statvfs(root)
    value={'memory_available_kib':memory['MemAvailable'],'docker_available_bytes':stat.f_bavail*stat.f_frsize}
    if value['memory_available_kib']<2*1024**2 or value['docker_available_bytes']<1024**3:raise ValueError('qualification_headroom_low')
    return value


def plan():
    from cloudworkbench.hermes_adapter import build_routed_launch
    from cloudworkbench.native_responses import NativeProfile
    from cloudworkbench.workflow_instructions import StageInstructions,STAGE_BOUNDARY
    profile=NativeProfile('openai-codex','gpt-6-astra','high')
    text='Synthetic permissions only. Hermes and provider inference are not launched.'
    instructions=StageInstructions('review','code_review','synthetic',sha(text.encode()),text,STAGE_BOUNDARY)
    return build_routed_launch(profile,instructions,task='Export synthetic output',input_revision_sha256='a'*64,
        workspace_readonly=False,run_budget_seconds=10),profile


TOOL_PROGRAM=r'''
import os,json
assert os.getuid()==1000 and os.getgid()==1000
os.mkdir('/workspace/nested',0o700)
items=[('/workspace/answer.txt',b'private-tool-output\n\x00\xff',0o600),('/workspace/nested/data.bin',bytes(range(32)),0o600),('/workspace/run.sh',b'echo synthetic\n',0o700),('/workspace/.env',b'synthetic-unselected',0o600)]
for path,data,mode in items:
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
 try:os.write(fd,data);os.fchmod(fd,mode)
 finally:os.close(fd)
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'answer_mode':os.stat('/workspace/answer.txt').st_mode&0o777,'answer_uid':os.stat('/workspace/answer.txt').st_uid}))
'''


WORKER_ADDRESS_SPACE=4*1024**3


def worker_child_setup():
    os.setgroups((959,966));os.setgid(960);os.setuid(959)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_CPU,(45,45))
    # Docker's Go runtime reserves more virtual address space than the Python-only
    # bootstrap fixture. This is not a 4 GiB physical-memory allocation.
    resource.setrlimit(resource.RLIMIT_AS,(WORKER_ADDRESS_SPACE,WORKER_ADDRESS_SPACE))
    resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))


def spawn_worker(support,args):
    process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        env={'PATH':'/usr/bin:/bin','LANG':'C','HOME':'/nonexistent','PYTHONDONTWRITEBYTECODE':'1'},
        preexec_fn=worker_child_setup,start_new_session=True)
    support.CHILDREN.append(process)
    return process


def worker(root):
    global WORKER_PHASE,WORKER_FAILURE
    WORKER_PHASE='initialization';WORKER_FAILURE=None
    if os.getuid()!=959 or os.getgid()!=960 or sorted(os.getgroups())!=[959,966]:raise ValueError('worker_identity_mismatch')
    sys.path.insert(0,str(root/'frozen'))
    from cloudworkbench.runtime import Runtime
    from cloudworkbench.routed_runtime import RoutedRuntime
    from cloudworkbench.inference_relay import AttemptBinding,WorkerDispatcher,WorkerSocketServer
    from cloudworkbench.worker_service import WorkerService
    from cloudworkbench import routed_export as export
    from cloudworkbench.routed_export_protocol import ExportLimits
    config=json.loads((root/'fixture.json').read_text());owner=config['owner'];attempt=config['attempt'];session=config['session']
    task=Path(config['attempt_root'])/'task';worker_dir=Path(config['attempt_root'])/'worker';state=root/'state'
    workspace=state/'workspace';scratch=state/'scratch'
    launch,profile=plan();client=json.loads((worker_dir/'client.json').read_text())
    binding=AttemptBinding(attempt,1,profile.digest)
    def never_execute(*args):raise AssertionError('inference_must_not_execute')
    dispatcher=WorkerDispatcher(journal_path=state/'worker.db',binding=binding,
        capability_sha256=sha(client['worker_capability'].encode()),authorize=lambda _:False,execute=never_execute)
    server=WorkerSocketServer(worker_dir/'socket',dispatcher,socket_gid=1001)
    service=WorkerService(server);service.start()
    base=Runtime({'root':state,'image':IMAGE,'owner':owner,'cpus':.5,'memory_mib':256,'pids':64,
        'approved_mount_roots':[root],'approved_writable_mount_roots':[root]})
    routed=RoutedRuntime(base,journal_root=state/'journal');spec=None;runtime_id=None;result=None
    observations=[];rpc_calls=[];original=export._bounded_run
    def observed_runtime(runtime,args,**kwargs):
        rpc_calls.append(args[0])
        body=original(runtime,args,**kwargs)
        if args[0]=='inspect':
            objects=json.loads(body)
            for item in objects:
                if (item.get('Config',{}).get('Labels') or {}).get('io.cloudworkbench.role')=='routed-export':
                    observations.append({'id':item['Id'],'image':item['Image'],'user':item['Config']['User'],
                        'network':item['HostConfig']['NetworkMode'],'readonly_root':item['HostConfig']['ReadonlyRootfs'],
                        'mounts':item['Mounts'],'state':item['State']['Status']})
        return body
    export._bounded_run=observed_runtime
    try:
        WORKER_PHASE='prepare_caller'
        spec=routed.prepare_caller(attempt,session,generation=1,plan=launch,workspace=workspace,scratch=scratch,
            task_dir=task,worker_socket_dir=worker_dir)
        original_identity=(workspace.stat().st_dev,workspace.stat().st_ino)
        sock_info=(worker_dir/'socket').lstat()
        ready={'ready':True,'spec':asdict(spec),'spec_digest':spec.digest,'workspace_identity':original_identity,
            'socket_identity':[sock_info.st_dev,sock_info.st_ino],'profile_digest':profile.digest}
        (state/'intent.json').write_text(json.dumps(ready));print(json.dumps(ready),flush=True)
        WORKER_PHASE='create_caller';runtime_id=routed.create_caller(spec)
        WORKER_PHASE='start_caller';routed.start_caller(spec,runtime_id)
        WORKER_PHASE='tool_exec'
        tool=json.loads(base._run(['exec','--user','1000:1000',runtime_id,'/usr/bin/env','-i','PATH=/usr/bin:/bin',
            '/opt/hermes/venv/bin/python','-I','-S','-c',TOOL_PROGRAM],timeout=10))
        WORKER_PHASE='host_read_denial'
        host_denied=False
        try:(workspace/'answer.txt').read_bytes()
        except PermissionError:host_denied=True
        if not host_denied:raise ValueError('host_worker_unexpected_read_access')
        WORKER_PHASE='stop_caller';stopped=routed.stop_caller(spec,runtime_id)
        WORKER_PHASE='remove_caller';removed=routed.remove_caller(spec,runtime_id)
        if not removed['caller_removed']:raise ValueError('caller_not_removed')
        WORKER_PHASE='export'
        result=export.export_stopped_workspace(routed,spec,runtime_id,removed,selected_paths=SELECTED,
            authorize=lambda action:True,limits=ExportLimits(max_bytes=4096,max_entries=10,max_depth=4,max_seconds=10),
            expected_workspace_identity=original_identity)
        WORKER_PHASE='validate_export'
        if export.validate_workspace_export(routed,spec,result,authorize=lambda _:True) is not True:raise ValueError('export_not_current')
        WORKER_PHASE='compare_payload'
        actual={file.path:(file.data,file.executable) for file in result.tree.files}
        if actual!=EXPECTED or result.tree.directories!=('nested',):raise ValueError('export_bytes_mismatch')
        WORKER_PHASE='reload'
        reload_start=len(rpc_calls)
        fresh=RoutedRuntime(Runtime(dict(base.config)),journal_root=state/'journal')
        loaded=export.load_workspace_export(fresh,spec,authorize=lambda _:True)
        WORKER_PHASE='validate_reload'
        if loaded!=result or export.validate_workspace_export(fresh,spec,loaded,authorize=lambda _:True) is not True:
            raise ValueError('durable_reload_mismatch')
        reload_calls=rpc_calls[reload_start:]
        if 'create' in reload_calls or 'start' in reload_calls:raise ValueError('reload_launched_collector')
        reload_proof={'fresh_runtime_view':fresh is not routed and fresh.runtime is not base,
            'equal_complete_export':loaded==result,'current_validation':True,
            'receipt_sha256':loaded.receipt_sha256,'stream_sha256':loaded.stream_sha256,
            'tree_sha256':loaded.tree.sha256,'collector_id':loaded.collector_id,
            'create_calls':reload_calls.count('create'),'start_calls':reload_calls.count('start')}
        WORKER_PHASE='assemble_receipt'
        result_meta={field.name:getattr(result,field.name) for field in fields(result) if field.name!='tree'}
        value={'passed':True,'worker_uid':os.getuid(),'worker_gid':os.getgid(),'worker_groups':os.getgroups(),
            'tool':tool,'host_worker_read_denied':host_denied,'workspace_identity_before_start':original_identity,
            'caller_id':runtime_id,'stop_receipt':stopped,'remove_receipt':removed,'export':result_meta,
            'tree':{'directories':result.tree.directories,'sha256':result.tree.sha256,
                'files':[{'path':f.path,'sha256':sha(f.data),'bytes':len(f.data),'executable':f.executable} for f in result.tree.files]},
            'export_current_validation':True,'durable_reload':reload_proof,'collector_observations':observations,'no_inference_requests':dispatcher.journal.db.execute('SELECT count(*) FROM requests').fetchone()[0]==0}
    except BaseException as exc:
        WORKER_FAILURE={'worker_failure':safe_failure(exc,WORKER_PHASE),'cleanup_failure':None}
        raise
    finally:
        try:cleanup_worker(routed,spec,runtime_id,result,export,service,original)
        except BaseException as exc:
            if WORKER_FAILURE is None:WORKER_FAILURE={'worker_failure':safe_failure(exc,'cleanup'),'cleanup_failure':None}
            else:WORKER_FAILURE['cleanup_failure']=safe_failure(exc,'cleanup')
            raise
    WORKER_PHASE='service_closed'
    value['service_closed']=asdict(service.closed_receipt())
    return value


def cleanup_worker(routed,spec,runtime_id,result,export,service,original):
    # Every layer gets its bounded teardown even when an earlier layer refuses.
    try:
        try:
            if spec is not None and runtime_id is not None:
                routed.stop_caller(spec,runtime_id);routed.remove_caller(spec,runtime_id)
        finally:
            if spec is not None and result is None and (routed._folder(spec)/'workspace-export.json').exists():
                export.reconcile_workspace_export(routed,spec,authorize=lambda _:True)
    finally:
        try:service.close(5)
        finally:export._bounded_run=original


def expected_files():
    return [{'path':name,'sha256':sha(data),'bytes':len(data),'executable':executable} for name,(data,executable) in sorted(EXPECTED.items())]


def validate_receipt(value,bundle):
    validate_bundle(bundle)
    try:
        proof=value['proof'];report=proof['export'];caller=proof['caller_id'];collector=report['collector_id']
        ready=value['ready'];spec=ready['spec'];run=bundle['run_id']
        root=Path('/tmp')/('cwb2-exportqual-'+run)
        assert ready['ready'] is True and spec['owner']=='exportqual-'+run
        assert spec['attempt_id']=='export-'+run and spec['session_id']=='session-'+run and type(spec['generation']) is int and spec['generation']==1
        assert spec['image']==IMAGE and spec['workspace']==str(root/'state/workspace') and spec['scratch']==str(root/'state/scratch')
        assert spec['workspace_readonly'] is False and spec['limits']==[.5,256,64]
        attempt_root=root/'b'/(sha(spec['attempt_id'].encode())[:24]+'.1')
        assert spec['task_dir']==str(attempt_root/'task') and spec['worker_socket_dir']==str(attempt_root/'worker')
        assert ready['spec_digest']==sha(json.dumps(spec,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
        assert tuple(proof['workspace_identity_before_start'])==tuple(ready['workspace_identity'])
        assert len(ready['workspace_identity'])==2 and all(type(n) is int and n>0 for n in ready['workspace_identity'])
        assert re.fullmatch('[0-9a-f]{64}',ready['profile_digest'])
        assert value['passed'] is True and value['host']=='archived-worker.invalid' and value['image']==IMAGE
        assert value['run_id']==bundle['run_id'] and value['source_hashes']==bundle['hashes']
        assert value['harness_sha256']==bundle['harness_sha256'] and value['support_sha256']==bundle['support_sha256']
        assert value['identity_before']==value['identity_after'] and value['identity_before']['groups']==[959,960,966]
        assert value['services_before']==value['services_after'] and set(value['services_before'])==set(validate_bundle(bundle).SERVICES)
        assert all(v['ActiveState']=='active' and type(v['MainPID']) is int and v['MainPID']>0 for v in value['services_before'].values())
        assert value['image_after']==IMAGE and value['child_groups_absent'] is True
        assert value['identity_before']['uid']==959 and value['identity_before']['primary_gid']==960
        assert value['remaining']==[] and value['fixture_absent'] is True and value['children_reaped'] is True
        assert value['real_provider_calls'] is False and value['real_credentials_used'] is False
        assert proof['passed'] is True and proof['worker_uid']==959 and proof['worker_gid']==960 and sorted(proof['worker_groups'])==[959,966]
        assert proof['host_worker_read_denied'] is True and proof['export_current_validation'] is True and proof['no_inference_requests'] is True
        assert proof['tool']=={'uid':1000,'gid':1000,'answer_mode':0o600,'answer_uid':1000}
        assert re.fullmatch('[0-9a-f]{64}',caller) and re.fullmatch('[0-9a-f]{64}',collector) and caller!=collector
        assert report['runtime_id']==caller and tuple(report['workspace_identity'])==tuple(proof['workspace_identity_before_start'])
        assert report['spec_digest']==value['ready']['spec_digest']
        assert report['selected_paths']==list(SELECTED) or report['selected_paths']==SELECTED
        assert proof['remove_receipt']['caller_removed'] is True and proof['remove_receipt']['runtime_id']==caller
        assert proof['stop_receipt']['caller_stopped'] is True and proof['stop_receipt']['runtime_id']==caller
        assert proof['tree']['files']==expected_files() and list(proof['tree']['directories'])==['nested']
        assert all(re.fullmatch('[0-9a-f]{64}',report[key]) for key in ('stream_sha256','receipt_sha256','caller_cleanup_sha256'))
        for key in ('stop_receipt','remove_receipt'):
            cleanup=proof[key]
            assert cleanup['attempt_id']==spec['attempt_id'] and type(cleanup['generation']) is int and cleanup['generation']==1
            assert cleanup['spec_digest']==ready['spec_digest'] and cleanup['authority']=='controller_observed_caller_only' and cleanup['provider_cleanup_qualified'] is False
        assert report['caller_cleanup_sha256']==sha(json.dumps(proof['remove_receipt'],sort_keys=True,separators=(',',':'),allow_nan=False).encode())
        assert re.fullmatch('[0-9a-f]{64}',proof['tree']['sha256'])
        closed=proof['service_closed']
        assert all(closed[k] is True for k in ('started','stop_requested','resources_closed','thread_stopped','listener_closed'))
        assert type(closed['active_callbacks']) is int and closed['active_callbacks']==0 and closed['timed_out'] is False and closed['error'] is None
        assert re.fullmatch('[0-9a-f]{32}',closed['service_id']) and closed['binding']['profile_digest']==ready['profile_digest']
        assert closed['binding']['attempt_id']==value['ready']['spec']['attempt_id'] and closed['binding']['generation']==1
        reload_proof=proof['durable_reload']
        assert all(reload_proof[k] is True for k in ('fresh_runtime_view','equal_complete_export','current_validation'))
        assert all(type(reload_proof[k]) is int and reload_proof[k]==0 for k in ('create_calls','start_calls'))
        assert reload_proof['receipt_sha256']==report['receipt_sha256'] and reload_proof['stream_sha256']==report['stream_sha256']
        assert reload_proof['tree_sha256']==proof['tree']['sha256'] and reload_proof['collector_id']==collector
        observations=proof['collector_observations'];assert {v['state'] for v in observations}>={'created','exited'}
        for observed in observations:
            assert observed['id']==collector and observed['image']==IMAGE and observed['user']=='1000:1000'
            assert observed['network']=='none' and observed['readonly_root'] is True
            mounts=observed['mounts'];assert len(mounts)==1 and mounts[0]['Type']=='bind' and mounts[0]['RW'] is False
            assert mounts[0]['Source']==value['ready']['spec']['workspace'] and mounts[0]['Destination']=='/workspace'
    except (AssertionError,KeyError,TypeError,ValueError):raise ValueError('export_qualification_receipt_invalid') from None
    return value


def root_run(bundle,receipt_path):
    support=validate_bundle(bundle)
    if sys.platform!='linux' or os.geteuid()!=0 or socket.gethostname()!='archived-worker.invalid' or Path('/tmp').resolve()!=Path('/tmp'):raise ValueError('linux_omarchy_root_required')
    if sha(Path(__file__).read_bytes())!=bundle['harness_sha256'] or Path(receipt_path).exists():raise ValueError('helper_or_receipt_mismatch')
    identity=support.identities();services=support.service_pids();capacity=headroom()
    if docker(['image','inspect',IMAGE,'--format','{{.Id}}'])!=IMAGE:raise ValueError('image_mismatch')
    owner='exportqual-'+bundle['run_id']
    if inventory(owner):raise ValueError('owner_collision')
    root=Path('/tmp')/('cwb2-exportqual-'+bundle['run_id']);root.mkdir(mode=0o750);os.chown(root,0,960);root.chmod(0o750)
    root_identity=(root.stat().st_dev,root.stat().st_ino);child=None;material=None;ready=None
    result={'passed':False,'host':'archived-worker.invalid','run_id':bundle['run_id'],'image':IMAGE,'source_hashes':bundle['hashes'],
        'harness_sha256':bundle['harness_sha256'],'support_sha256':bundle['support_sha256'],
        'identity_before':identity,'services_before':services,'headroom_before':capacity,'real_provider_calls':False,'real_credentials_used':False}
    try:
        package=root/'frozen/cloudworkbench';package.mkdir(parents=True)
        for name,body in bundle['sources'].items():(package/name).write_text(body);(package/name).chmod(0o444)
        for directory in (root/'frozen',package):directory.chmod(0o555)
        helper=root/'helper.py';helper.write_text(bundle['harness']);helper.chmod(0o444)
        state=root/'state';state.mkdir(mode=0o700);os.chown(state,959,960)
        for name in ('workspace','scratch'):
            path=state/name;path.mkdir();os.chown(path,959,1000);path.chmod(0o2770)
        sys.path.insert(0,str(root/'frozen'))
        from cloudworkbench.routed_bootstrap import RoutedBootstrap,BootstrapRequest,SOURCE_FILES
        launch,profile=plan();attempt='export-'+bundle['run_id'];session='session-'+bundle['run_id']
        request=BootstrapRequest(attempt,session,1,profile.digest,launch)
        dest=root/'b';dest.mkdir();os.chown(dest,0,960);dest.chmod(0o750)
        bootstrap=RoutedBootstrap(destination_root=dest,source_root=package,
            source_hashes={name:bundle['hashes'][name] for name in SOURCE_FILES},
            authorize=lambda current:current.digest==request.digest,authorize_discard=lambda _:False)
        material=bootstrap.provision(request)
        config={'owner':owner,'attempt':attempt,'session':session,'attempt_root':str(material.root)}
        fixture=root/'fixture.json';fixture.write_text(json.dumps(config));os.chown(fixture,0,960);fixture.chmod(0o440)
        child=spawn_worker(support,[sys.executable,'-I',str(helper),'--worker',str(root)])
        ready=support.ready_line(child);result['ready']=ready
        if ready.get('ready') is not True or ready['spec']['workspace']!=str(state/'workspace') or ready['spec']['owner']!=owner:
            raise ValueError('worker_ready_invalid')
        proof=finish_worker(child,timeout=100);child=None
        result['proof']=proof;result['passed']=True
    except BaseException as exc:
        result['error']=type(exc).__name__
        if isinstance(exc,WorkerFailed):
            result['worker_failure']=exc.detail;result['worker_cleanup_failure']=exc.cleanup
        else:result['failure']=safe_failure(exc,'root')
    finally:
        if child is not None:
            if child.poll() is None:
                try:os.killpg(child.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            child.communicate(timeout=5)
        # Preserve uncertainty. Root does not guess a broad Docker stop/delete from an owner label.
        try:
            result['remaining']=inventory(owner)
            result['identity_after']=support.identities();result['services_after']=support.service_pids()
            result['image_after']=docker(['image','inspect',IMAGE,'--format','{{.Id}}'])
            result['children_reaped']=all(p.poll() is not None for p in support.CHILDREN)
            result['child_groups_absent']=True
            for process in support.CHILDREN:
                try:os.killpg(process.pid,0)
                except ProcessLookupError:pass
                else:result['child_groups_absent']=False
            if result['remaining'] or result['identity_after']!=identity or result['services_after']!=services or result['image_after']!=IMAGE or not result['child_groups_absent']:
                result['passed']=False
            import shutil
            mountinfo=Path('/proc/self/mountinfo').read_text()
            if (not result['remaining'] and result['children_reaped'] and result['child_groups_absent']
                    and len(mountinfo)<4*1024**2 and support.cleanup_safe(root,root_identity,mountinfo)
                    and shutil.rmtree.avoids_symlink_attacks):
                shutil.rmtree(root);result['fixture_absent']=not root.exists()
            else:result['cleanup']='reconciliation_required';result['passed']=False
        except Exception:
            result['cleanup']='unconfirmed';result['passed']=False
        if result['passed']:
            try:validate_receipt(result,bundle)
            except ValueError:result['passed']=False;result['error']='receipt_validation_failed'
        with Path(receipt_path).open('x') as out:json.dump(result,out,indent=2);out.write('\n')
    return result


def main():
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare',type=Path);mode.add_argument('--run',type=Path);mode.add_argument('--worker',type=Path)
    parser.add_argument('--receipt',type=Path);args=parser.parse_args()
    if args.prepare:value=prepare(args.prepare)
    elif args.worker:
        try:value=worker(args.worker)
        except BaseException as exc:
            detail=WORKER_FAILURE or {'worker_failure':safe_failure(exc,WORKER_PHASE),'cleanup_failure':None}
            print(json.dumps(detail,sort_keys=True),file=sys.stderr);return 1
    else:
        if args.receipt is None:parser.error('--receipt required')
        try:value=root_run(json.loads(args.run.read_text()),args.receipt)
        except Exception as exc:
            if args.receipt.exists():raise
            value={'passed':False,'phase':'preflight_or_unconfirmed_cleanup','error':type(exc).__name__}
            with args.receipt.open('x') as out:json.dump(value,out);out.write('\n')
    print(json.dumps(value,sort_keys=True));return 0 if value.get('passed',True) else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'passed':False,'error':type(exc).__name__}),file=sys.stderr);raise SystemExit(1)
