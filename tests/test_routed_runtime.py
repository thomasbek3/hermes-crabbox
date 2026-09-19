from dataclasses import asdict
import hashlib
import json
import os
import stat
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from cloudworkbench.runtime import Runtime, RuntimeError
from cloudworkbench.routed_runtime import RoutedRuntime
from cloudworkbench.hermes_adapter import build_routed_launch
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.workflow_instructions import StageInstructions, STAGE_BOUNDARY

RID='b'*64
IMAGE='sha256:'+'a'*64

class Docker:
    def __init__(self):self.calls=[];self.objects={};self.fail_create=False;self.fail_exec=False
    def __call__(self,args,**kwargs):
        self.calls.append(args)
        def one(key):return args[args.index(key)+1]
        def many(key):return [args[i+1] for i,value in enumerate(args) if value==key]
        if args[0]=='create':
            binds=[]
            for text in many('--mount'):
                v=dict(piece.split('=',1) for piece in text.split(',') if '=' in piece)
                binds.append(dict(Type='bind',Source=v['src'],Destination=v['dst'],RW='readonly' not in text.split(',')))
            self.objects[RID]={'Id':RID,'Name':'/'+one('--name'),'Image':IMAGE,'State':{'Status':'created','Running':False},
                'Config':{'Labels':dict(v.split('=',1) for v in many('--label')),'User':one('--user'),'WorkingDir':one('--workdir'),'Entrypoint':[one('--entrypoint')],'Cmd':args[args.index(IMAGE)+1:]},
                'HostConfig':{'NetworkMode':one('--network'),'ReadonlyRootfs':True,'User':one('--user'),'CapDrop':many('--cap-drop'),
                    'SecurityOpt':many('--security-opt'),'IpcMode':one('--ipc'),'Dns':[one('--dns')],
                    'RestartPolicy':{'Name':one('--restart'),'MaximumRetryCount':0},
                    'Memory':int(one('--memory')[:-1])*1024**2,'MemorySwap':int(one('--memory-swap')[:-1])*1024**2,
                    'PidsLimit':int(one('--pids-limit')),'NanoCpus':round(float(one('--cpus'))*1e9),
                    'Tmpfs':dict(v.split(':',1) for v in many('--tmpfs')),
                    'LogConfig':{'Type':one('--log-driver'),'Config':dict(v.split('=',1) for v in many('--log-opt'))}},
                'Mounts':binds,'NetworkSettings':{'Networks':{'none':{}}}}
            if self.fail_create:raise RuntimeError('lost create response')
            return RID
        if args[0]=='inspect':
            obj=self.objects[args[-1]]
            if '--format' in args:
                return json.dumps(obj['Config']['Labels'] if '.Config.Labels' in one('--format') else obj['State'])
            return json.dumps([obj])
        if args[:2]==['container','ls']:
            found=[]
            for rid,obj in self.objects.items():
                if all((rid==v[3:] if v.startswith('id=') else obj['Config']['Labels'].get(v[6:].split('=',1)[0])==v[6:].split('=',1)[1]) for v in many('--filter')):found.append(rid)
            return '\n'.join(found)
        if args[0]=='start':self.objects[args[-1]]['State'].update(Status='running',Running=True);return RID
        if args[0]=='stop':self.objects[args[-1]]['State'].update(Status='exited',Running=False);return RID
        if args[0]=='rm':del self.objects[args[-1]];return RID
        if args[0]=='exec':
            if '-I' in args:return json.dumps({'present':False})
            if self.fail_exec:raise RuntimeError('lost exec response')
            return ''
        raise AssertionError(args)


@pytest.fixture
def setup(tmp_path,monkeypatch):
    workspace=tmp_path/'workspace';workspace.mkdir()
    scratch=tmp_path/'scratch';scratch.mkdir()
    task=tmp_path/'task';task.mkdir(mode=0o751)
    socket=tmp_path/'worker';socket.mkdir(mode=0o750)
    (socket/'client.json').write_text('{"synthetic":true}');(socket/'client.json').chmod(0o440)
    (socket/'socket').touch();(socket/'socket').chmod(0o660)
    # Metadata-only fake Linux UID/group/socket fixture; no host chown or real Docker.
    original_lstat=Path.lstat
    def metadata(path):
        info=original_lstat(path)
        if path==task or path.is_relative_to(task):
            values=list(info);values[5]=1000;return os.stat_result(values)
        if path==socket or path.parent==socket:
            values=list(info);values[5]=1001
            if path.name=='socket':values[0]=stat.S_IFSOCK|0o660
            return os.stat_result(values)
        return info
    monkeypatch.setattr(Path,'lstat',metadata)
    instructions=StageInstructions('review','plan_review','fixed.md',hashlib.sha256(b'fixture').hexdigest(),'fixture',STAGE_BOUNDARY)
    plan=build_routed_launch(NativeProfile('openai-codex','gpt-6-astra','high'),instructions,task='test',input_revision_sha256='c'*64,workspace_readonly=True)
    for name,body in [('prompt.txt',plan.prompt),('config.yaml',plan.config_json),('launch.json',json.dumps(asdict(plan))),('http-capability','f'*64)]:
        p=task/name;p.write_text(body);p.chmod(0o440)
    source=task/'source'/'cloudworkbench';source.mkdir(parents=True)
    for name in ('__init__.py','routed_caller.py','hermes_adapter.py','pstack_routing.py','inference_relay.py','inference_service.py','adapters.py'):
        (source/name).write_text('# synthetic immutable module\n');(source/name).chmod(0o444)
    runtime=Runtime({'root':tmp_path,'image':IMAGE,'test_path_workspace':True,'approved_mount_roots':[tmp_path],'approved_writable_mount_roots':[tmp_path]})
    docker=Docker();monkeypatch.setattr(runtime,'_run',docker)
    routed=RoutedRuntime(runtime,journal_root=tmp_path/'journal')
    spec=routed.prepare_caller('child','session',generation=1,plan=plan,workspace=workspace,scratch=scratch,task_dir=task,worker_socket_dir=socket)
    return routed,spec,docker


def test_create_is_stopped_then_distinct_fixed_role_bootstraps(setup):
    routed,spec,docker=setup
    assert routed.create_caller(spec)==RID
    assert not any(c[0] in ('start','exec') for c in docker.calls)
    obj=docker.objects[RID]
    assert obj['Config']['User']=='1002:1002'
    mounts={m['Destination']:m for m in obj['Mounts']}
    assert mounts['/workspace']['RW'] is False and mounts['/scratch']['RW'] is True
    assert mounts['/run/worker-inference']['RW'] is False
    assert 'uid=1000,gid=1000,mode=0700' in obj['HostConfig']['Tmpfs']['/run/tool/hermes']
    routed.start_caller(spec,RID)
    assert routed.start_caller_process(spec,RID,role='relay')['uid']==1001
    assert routed.start_caller_process(spec,RID,role='hermes')['uid']==1000
    commands=[c for c in docker.calls if c[0]=='exec']
    assert commands[0][-3:]==['-m','cloudworkbench.routed_caller','relay']
    assert 'f'*64 not in json.dumps(docker.calls)
    assert 'f'*64 not in (routed._folder(spec)/'journal.json').read_text()
    assert not any('/var/run/docker.sock' in str(c) or '/run/secrets' in str(c) for c in docker.calls)


def test_lost_create_reconciles_exact_stopped_object_without_duplicate(setup):
    routed,spec,docker=setup;docker.fail_create=True
    with pytest.raises(RuntimeError,match='lost create'):routed.create_caller(spec)
    with pytest.raises(RuntimeError,match='unresolved'):routed.create_caller(spec)
    assert routed.reconcile_create(spec)==RID
    assert routed.create_caller(spec)==RID
    assert sum(c[0]=='create' for c in docker.calls)==1


def test_absence_does_not_authorize_create_retry(setup):
    routed,spec,docker=setup;docker.fail_create=True
    with pytest.raises(RuntimeError):routed.create_caller(spec)
    docker.objects.clear()
    assert routed.reconcile_create(spec) is None
    with pytest.raises(RuntimeError,match='unresolved'):routed.create_caller(spec)
    assert sum(c[0]=='create' for c in docker.calls)==1


def test_unknown_exec_never_repeats_or_starts_hermes(setup):
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID);docker.fail_exec=True
    with pytest.raises(RuntimeError,match='lost exec'):routed.start_caller_process(spec,RID,role='relay')
    with pytest.raises(RuntimeError,match='no replay'):routed.start_caller_process(spec,RID,role='relay')
    with pytest.raises(RuntimeError,match='not launched'):routed.start_caller_process(spec,RID,role='hermes')
    assert sum(c[0]=='exec' for c in docker.calls)==1


@pytest.mark.parametrize('tamper', ['workspace_rw','uid','network','extra_mount','duplicate_mount','tmpfs','image','capadd','command','memory','labels'])
def test_inspect_rejects_policy_drift_before_start(setup,tamper):
    routed,spec,docker=setup;routed.create_caller(spec);obj=docker.objects[RID]
    if tamper=='workspace_rw':obj['Mounts'][0]['RW']=True
    elif tamper=='uid':obj['Config']['User']='0:0'
    elif tamper=='network':obj['NetworkSettings']['Networks']['external']={}
    elif tamper=='extra_mount':obj['Mounts'].append(dict(obj['Mounts'][0]))
    elif tamper=='duplicate_mount':obj['Mounts'][1]=dict(obj['Mounts'][0])
    elif tamper=='tmpfs':obj['HostConfig']['Tmpfs']['/run/tool/hermes']='rw,size=32m,mode=0755'
    elif tamper=='image':obj['Image']='sha256:'+'c'*64
    elif tamper=='capadd':obj['HostConfig']['CapAdd']=['SYS_ADMIN']
    elif tamper=='command':obj['Config']['Cmd']=['untrusted']
    elif tamper=='memory':obj['HostConfig']['Memory']*=2
    else:obj['Config']['Labels']['io.cloudworkbench.generation']='2'
    with pytest.raises(RuntimeError):routed.start_caller(spec,RID)
    assert not any(c[0]=='start' for c in docker.calls)


def test_task_change_prevents_launch(setup):
    routed,spec,docker=setup;routed.create_caller(spec)
    Path(spec.task_dir,'prompt.txt').chmod(0o640)
    Path(spec.task_dir,'prompt.txt').write_text('changed')
    with pytest.raises(RuntimeError,match='material changed'):routed.start_caller(spec,RID)
    assert not any(c[0]=='start' for c in docker.calls)


def test_concurrent_create_and_process_are_serialized(setup):
    routed,spec,docker=setup
    with ThreadPoolExecutor(max_workers=4) as pool:assert list(pool.map(lambda _:routed.create_caller(spec),range(4)))==[RID]*4
    assert sum(c[0]=='create' for c in docker.calls)==1
    routed.start_caller(spec,RID)
    def launch(_):
        try:routed.start_caller_process(spec,RID,role='relay');return True
        except RuntimeError:return False
    with ThreadPoolExecutor(max_workers=4) as pool:assert sum(pool.map(launch,range(4)))==1
    assert sum(c[0]=='exec' for c in docker.calls)==1


def test_reconstructed_controller_replays_create_binding_not_process(setup):
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID);routed.start_caller_process(spec,RID,role='relay')
    fresh=RoutedRuntime(routed.runtime,journal_root=routed.root)
    assert fresh.create_caller(spec)==RID
    with pytest.raises(RuntimeError,match='no replay'):fresh.start_caller_process(spec,RID,role='relay')


def test_task_tree_refuses_extra_secret_or_symlink(setup):
    routed,spec,docker=setup
    path=Path(spec.task_dir,'extra-auth.json');path.write_text('SYNTHETIC');path.chmod(0o444)
    with pytest.raises(RuntimeError,match='unexpected routed task material'):routed.create_caller(spec)
    path.unlink();path.symlink_to(Path(spec.task_dir,'launch.json'))
    with pytest.raises(RuntimeError,match='untrusted routed task material'):routed.create_caller(spec)
    assert docker.calls==[]


def test_start_ack_loss_confirms_running_without_second_start(setup):
    routed,spec,docker=setup;routed.create_caller(spec)
    original=routed.runtime._run
    def fail(args,**kwargs):
        result=original(args,**kwargs)
        if args[0]=='start':raise RuntimeError('start response lost')
        return result
    routed.runtime._run=fail
    with pytest.raises(RuntimeError,match='response lost'):routed.start_caller(spec,RID)
    routed.runtime._run=original
    routed.start_caller(spec,RID)
    assert sum(c[0]=='start' for c in docker.calls)==1


def test_workspace_paths_are_distinct_and_approved(setup):
    from dataclasses import replace
    routed,spec,docker=setup
    forged=replace(spec,workspace=str(Path('/etc').resolve()))
    with pytest.raises(RuntimeError,match='workspace source not approved'):routed._mounts(forged)
    assert docker.calls==[]


def test_foreign_tool_uid_cannot_be_given_worker_secret_mount(setup):
    routed,spec,docker=setup
    Path(spec.worker_socket_dir,'client.json').chmod(0o444)
    with pytest.raises(RuntimeError,match='unsafe worker socket material'):routed.create_caller(spec)
    assert docker.calls==[]


def test_collection_is_fixed_path_bounded_and_not_cleanup_authority(setup):
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    assert routed.read_caller_file(spec,RID,name='result')=={'present':False}
    call=docker.calls[-1]
    assert call[call.index('--user')+1]=='1000:1000' and '-I' in call
    assert call[-3:]==['/scratch/.cwb-observations/result.json','0','65536']
    with pytest.raises(RuntimeError,match='invalid caller collection'):routed.read_caller_file(spec,RID,name='/run/task/http-capability')
    with pytest.raises(RuntimeError,match='invalid caller collection'):routed.read_caller_file(spec,RID,name='result',max_bytes=65537)


def test_create_never_reports_running_container_as_stopped(setup):
    routed,spec,docker=setup;original=routed.runtime._run
    def unexpectedly_started(args,**kwargs):
        result=original(args,**kwargs)
        if args[0]=='create':docker.objects[RID]['State']['Status']='running'
        return result
    routed.runtime._run=unexpectedly_started
    with pytest.raises(RuntimeError,match='not stopped'):routed.create_caller(spec)
    assert routed._load(spec)['runtime_id'] is None


def test_actual_collection_program_refuses_symlink_and_hardlink(setup,tmp_path):
    import subprocess,sys,base64
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    routed.read_caller_file(spec,RID,name='result')
    command=docker.calls[-1];program=command[command.index('-c')+1]
    path=tmp_path/'output';path.write_bytes(b'synthetic output')
    def run():return subprocess.run([sys.executable,'-I','-c',program,str(path),'0','8'],capture_output=True)
    result=run();assert result.returncode==0
    value=json.loads(result.stdout);assert base64.b64decode(value['data_b64'])==b'syntheti'
    os.link(path,tmp_path/'hardlinked')
    assert run().returncode!=0
    path.unlink();path.symlink_to(tmp_path/'hardlinked')
    assert run().returncode!=0


def test_real_program_base64_one_mib_roundtrip_through_runtime(setup,tmp_path):
    import subprocess,sys
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    path=tmp_path/'events';data=bytes(range(256))*4096+b'tail';path.write_bytes(data)
    original=routed.runtime._run
    def local_program(args,**kwargs):
        if args[0]=='exec' and '-I' in args:
            assert args[-3]=='/scratch/.cwb-observations/events.jsonl'
            program=args[args.index('-c')+1]
            return subprocess.check_output([sys.executable,'-I','-c',program,str(path.resolve()),*args[-2:]],text=True,timeout=5)
        return original(args,**kwargs)
    routed.runtime._run=local_program
    first=routed.read_caller_file(spec,RID,name='events',max_bytes=1024**2)
    second=routed.read_caller_file(spec,RID,name='events',offset=first['next_offset'],max_bytes=1024**2)
    assert first['data']+second['data']==data
    assert first['inode']==second['inode'] and first['mtime_ns']==second['mtime_ns']
    assert first['provenance']=='worker_reported' and first['size']==len(data)


@pytest.mark.parametrize('name,maximum',[('events',1024**2),('result',65536),('relay-ready',4096),('relay-fenced',4096)])
def test_collection_individual_path_read_caps(setup,name,maximum):
    routed,spec,_=setup
    with pytest.raises(RuntimeError,match='invalid caller collection'):
        routed.read_caller_file(spec,RID,name=name,max_bytes=maximum+1)


@pytest.mark.parametrize('change', ['extra','bool_inode','short','bad_base64','size_cap','offset','encoded_cap','duplicate'])
def test_collection_wire_envelope_strict_validation(setup,change):
    import base64
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    original=routed.runtime._run
    value={'present':True,'offset':0,'next_offset':4,'size':4,'inode':12,'mtime_ns':42,
           'data_b64':base64.b64encode(b'test').decode()}
    if change=='extra':value['verification_pass']=True
    elif change=='bool_inode':value['inode']=True
    elif change=='short':value['size']=5
    elif change=='bad_base64':value['data_b64']='????'
    elif change=='size_cap':value['size']=65537
    elif change=='offset':value['offset']=1
    elif change=='encoded_cap':value['data_b64']='x'*100000
    wire=json.dumps(value)
    if change=='duplicate':wire=wire[:-1]+',"present":true}'
    routed.runtime._run=lambda args,**kwargs:wire if args[0]=='exec' else original(args,**kwargs)
    with pytest.raises(RuntimeError,match='collection'):
        routed.read_caller_file(spec,RID,name='result')


def test_collection_program_refuses_parent_symlink_and_result_size(setup,tmp_path):
    import subprocess,sys
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    routed.read_caller_file(spec,RID,name='result')
    command=docker.calls[-1];program=command[command.index('-c')+1]
    directory=tmp_path/'real';directory.mkdir();path=directory/'result';path.write_bytes(b'x'*65537)
    def run(target):return subprocess.run([sys.executable,'-I','-c',program,str(target),'0','65536'],capture_output=True,timeout=5)
    assert run(path.resolve()).returncode!=0
    path.write_bytes(b'{}');link=tmp_path/'linked';link.symlink_to(directory,target_is_directory=True)
    assert run(link/'result').returncode!=0


def test_existing_runtime_can_stop_and_remove_routed_full_id(setup):
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    routed.runtime.stop(RID,expected_generation=1)
    routed.runtime.cleanup(RID,expected_generation=1)
    assert RID not in docker.objects
    assert [c[0] for c in docker.calls if c[0] in ('stop','rm')]==['stop','rm']


def test_routed_cleanup_fences_launches_and_replays_after_restart(setup):
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    stopped=routed.stop_caller(spec,RID)
    assert stopped['caller_stopped'] and not stopped['caller_removed']
    for operation in (lambda:routed.create_caller(spec),lambda:routed.start_caller(spec,RID),
                      lambda:routed.start_caller_process(spec,RID,role='relay'),lambda:routed.reconcile_create(spec)):
        with pytest.raises(RuntimeError,match='cleanup already begun'):operation()
    fresh=RoutedRuntime(routed.runtime,journal_root=routed.root)
    assert fresh.remove_caller(spec,RID)['caller_removed']
    assert fresh.remove_caller(spec,RID)['caller_removed']
    assert fresh.stop_caller(spec,RID)['caller_removed']
    assert sum(c[0]=='stop' for c in docker.calls)==sum(c[0]=='rm' for c in docker.calls)==1


@pytest.mark.parametrize('operation',['stop','rm'])
def test_cleanup_lost_ack_reconciles_physical_state(setup,operation):
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    if operation=='rm':routed.stop_caller(spec,RID)
    original=routed.runtime._run
    def lose(args,**kwargs):
        result=original(args,**kwargs)
        if args[0]==operation:raise RuntimeError('lost cleanup ack')
        return result
    routed.runtime._run=lose
    method=routed.stop_caller if operation=='stop' else routed.remove_caller
    with pytest.raises(RuntimeError,match='lost cleanup ack'):method(spec,RID)
    routed.runtime._run=original
    fresh=RoutedRuntime(routed.runtime,journal_root=routed.root)
    receipt=(fresh.stop_caller if operation=='stop' else fresh.remove_caller)(spec,RID)
    assert receipt['caller_stopped']
    assert sum(c[0]==operation for c in docker.calls)==1


def test_cleanup_refuses_unknown_create_and_never_masks_label_drift(setup):
    routed,spec,docker=setup;docker.fail_create=True
    with pytest.raises(RuntimeError):routed.create_caller(spec)
    with pytest.raises(RuntimeError,match='confirmed identity'):routed.stop_caller(spec,RID)
    assert not any(c[0]=='stop' for c in docker.calls)
    routed.reconcile_create(spec)
    docker.objects[RID]['Config']['Labels']['io.cloudworkbench.generation']='2'
    with pytest.raises(RuntimeError,match='policy mismatch'):routed.stop_caller(spec,RID)
    assert not any(c[0] in ('stop','rm') for c in docker.calls)


def test_remove_refuses_live_or_unproven_stop(setup):
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    with pytest.raises(RuntimeError,match='stop not confirmed'):routed.remove_caller(spec,RID)
    routed.stop_caller(spec,RID)
    docker.objects[RID]['State'].update(Status='running',Running=True)
    with pytest.raises(RuntimeError,match='live routed'):routed.remove_caller(spec,RID)
    assert not any(c[0]=='rm' for c in docker.calls)


def test_created_caller_cleanup_needs_no_stop_command(setup):
    routed,spec,docker=setup;routed.create_caller(spec)
    routed.stop_caller(spec,RID);assert routed.remove_caller(spec,RID)['caller_removed']
    assert not any(c[0]=='stop' for c in docker.calls)


def test_failed_stop_retains_fence_and_no_cleanup_success(setup):
    routed,spec,docker=setup;routed.create_caller(spec);routed.start_caller(spec,RID)
    original=routed.runtime._run
    routed.runtime._run=lambda args,**kw: '' if args[0]=='stop' else original(args,**kw)
    with pytest.raises(RuntimeError,match='stop unconfirmed'):routed.stop_caller(spec,RID)
    with pytest.raises(RuntimeError,match='stop not confirmed'):routed.remove_caller(spec,RID)
    with pytest.raises(RuntimeError,match='cleanup already begun'):routed.start_caller(spec,RID)


def test_inherited_build_labels_are_inert_and_init_cwd_not_workspace(setup):
    routed,spec,docker=setup;routed.create_caller(spec)
    obj=docker.objects[RID]
    obj['Config']['Labels'].update({'io.cloudworkbench.build-owner':'hermesbuild-4e555a944e','io.cloudworkbench.candidate':'true','org.opencontainers.image.version':'24.04'})
    assert obj['Config']['WorkingDir']=='/'
    routed.start_caller(spec,RID)
    routed.stop_caller(spec,RID);routed.remove_caller(spec,RID)


@pytest.mark.parametrize('label,value',[('io.cloudworkbench.candidate','false'),('io.cloudworkbench.build-owner','bad label'),('io.cloudworkbench.extra-authority','true'),('io.cloudworkbench.generation','99')])
def test_inherited_labels_cannot_expand_authority(setup,label,value):
    routed,spec,docker=setup;routed.create_caller(spec)
    docker.objects[RID]['Config']['Labels'][label]=value
    with pytest.raises(RuntimeError):routed.start_caller(spec,RID)
    assert not any(c[0]=='start' for c in docker.calls)


@pytest.mark.parametrize('target',['task','source_directory','source_file','private_file'])
def test_prepare_rejects_relay_import_or_private_boundary_mismatch(setup,target):
    routed,spec,docker=setup
    if target=='task':path=Path(spec.task_dir);mode=0o750
    elif target=='source_directory':path=Path(spec.task_dir,'source');mode=0o550
    elif target=='source_file':path=Path(spec.task_dir,'source/cloudworkbench/routed_caller.py');mode=0o440
    else:path=Path(spec.task_dir,'http-capability');mode=0o444
    path.chmod(mode)
    with pytest.raises(RuntimeError,match='relay'):routed._task_hashes(Path(spec.task_dir))
    assert docker.calls==[]


def test_relay_can_traverse_public_bootstrap_but_not_private_inputs(setup):
    routed,spec,docker=setup
    assert routed._relay_access(Path(spec.task_dir).lstat(),traverse_only=True)
    assert routed._relay_access(Path(spec.task_dir,'source/cloudworkbench/routed_caller.py').lstat())
    for name in ('prompt.txt','config.yaml','launch.json','http-capability'):
        assert not routed._relay_access(Path(spec.task_dir,name).lstat())
    routed.create_caller(spec)
