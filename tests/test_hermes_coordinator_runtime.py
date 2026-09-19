import copy
import json
import os
from pathlib import Path
import socket
import tempfile
import uuid

import pytest

from cloudworkbench.hermes_coordinator_runtime import HermesCoordinatorRuntime, HERMES_ARGV, tool_labels, MARKER, PREFIX
from cloudworkbench.runtime import Runtime, RuntimeError

IMAGE = 'sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
RID = 'b'*64
TOOL = 'c'*64


class Docker:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail = None

    def __call__(self,args,**kwargs):
        self.calls.append(list(args))
        def value(key): return args[args.index(key)+1]
        def values(key): return [args[n+1] for n,v in enumerate(args) if v==key]
        if args[0]=='create':
            binds=[]
            for m in values('--mount'):
                parts=m.split(','); kv=dict(p.split('=',1) for p in parts if '=' in p)
                binds.append(dict(Type='bind',Source=kv['src'],Destination=kv['dst'],RW='readonly' not in parts))
            labels=dict(v.split('=',1) for v in values('--label'))
            self.objects[RID]=dict(Id=RID,Name='/'+value('--name'),Image=IMAGE,
                Config=dict(Labels=labels,User=value('--user')),
                HostConfig=dict(NetworkMode=value('--network'),Privileged=False,ReadonlyRootfs=True,
                    PidMode='',IpcMode='private',CapAdd=None,CapDrop=['ALL'],SecurityOpt=['no-new-privileges'],
                    GroupAdd=values('--group-add'),Memory=int(value('--memory')[:-1])*1024**2,
                    MemorySwap=int(value('--memory-swap')[:-1])*1024**2,NanoCpus=int(float(value('--cpus'))*10**9),
                    PidsLimit=int(value('--pids-limit'))),Mounts=binds,
                State=dict(Running=False,Status='created',ExitCode=0,OOMKilled=False))
            if self.fail=='create': raise RuntimeError('lost create ACK')
            return RID
        if args[0] in {'start','stop','rm'}:
            if self.fail==args[0]: raise RuntimeError('lost operation ACK')
            rid=args[-1]
            if args[0]=='rm': self.objects.pop(rid)
            else:
                self.objects[rid]['State']['Running']=args[0]=='start'
                self.objects[rid]['State']['Status']='running' if args[0]=='start' else 'exited'
            return ''
        if args[:2]==['container','ls']:
            result=[]
            for rid,obj in self.objects.items():
                if '--all' not in args and not obj['State']['Running']: continue
                match=True
                for f in values('--filter'):
                    if f.startswith('id='): match &= rid==f[3:]
                    if f.startswith('label='):
                        k,v=f[6:].split('=',1);match &= obj['Config']['Labels'].get(k)==v
                if match: result.append(json.dumps({'ID':rid}) if '{{json .}}' in args else rid)
            return '\n'.join(result)
        if args[0]=='inspect':
            obj=self.objects[args[-1]]
            if '--format' not in args: return json.dumps([obj])
            return json.dumps(obj['Config']['Labels'] if '.Config.Labels' in args[2] else obj['State'])
        if args[:2]==['network','ls']: return ''
        raise AssertionError(args)


@pytest.fixture
def configured(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir()
    auth=tmp_path/'auth';auth.write_text('synthetic-only');auth.chmod(0o600)
    socket_dir=tempfile.TemporaryDirectory(prefix='hc-', dir='/tmp')
    sock=socket.socket(socket.AF_UNIX);socket_path=Path(socket_dir.name).resolve()/'docker.sock';sock.bind(str(socket_path))
    taskdir=tmp_path/'task';taskdir.mkdir()
    rt=HermesCoordinatorRuntime(dict(root=tmp_path/'workspaces',image=IMAGE,test_path_workspace=True,
        hermes_source_root=source,hermes_grok_auth=auth,hermes_journal_root=tmp_path/'journal',
        docker_socket=socket_path,coordinator_uid=os.geteuid(),coordinator_gid=960,
        docker_gid=966,tool_gid=1000,tool_shared_gid=959,approved_mount_roots=[tmp_path],approved_writable_mount_roots=[tmp_path]))
    attempt=str(uuid.uuid4());session=str(uuid.uuid4())
    work=rt.make_workspace(session);native=rt.native_state(session)
    task=dict(schema_version=1,attempt_id=attempt,generation=1,session_id=session,runtime_owner=rt.owner,
        workspace_host=str(work),state_host=str(native),tool_image=IMAGE,inputs=[],prompt='test',
        event_spool=attempt+'.1.jsonl',resume_session_id=None,continuation_summary='')
    (taskdir/'task.json').write_text(json.dumps(task))
    mounts=[dict(source=str(taskdir),target='/run/task',readonly=True),dict(source=str(native),target='/state',readonly=False)]
    docker=Docker();monkeypatch.setattr(rt,'_run',docker)
    yield rt,docker,attempt,session,mounts,task
    sock.close();socket_dir.cleanup()


def launch(case):
    rt,docker,attempt,session,mounts,task=case
    assert rt.launch(attempt,session,list(HERMES_ARGV),{},mounts,generation=1)==RID
    return rt,docker


def add_tool(case,*,running=True):
    rt,docker,attempt,session,mounts,task=case
    docker.objects[TOOL]=dict(Id=TOOL,Image=IMAGE,Name='/hermes-tool',
        Config=dict(Labels=tool_labels(rt.owner,attempt,session,1,IMAGE),User='1000:1000'),
        HostConfig=dict(NetworkMode='none',Privileged=False,GroupAdd=['959'],PidsLimit=128,
            NanoCpus=10**9,Memory=1536*1024**2,MemorySwap=1536*1024**2,CapDrop=['ALL'],
            CapAdd=['DAC_OVERRIDE','CHOWN','FOWNER','SETUID','SETGID','SYS_CHROOT'],
            SecurityOpt=['no-new-privileges'],PidMode='',IpcMode='private',Devices=[],DeviceRequests=[]),
        Mounts=[dict(Type='bind',Source=task['workspace_host'],Destination='/workspace',RW=True)],
        State=dict(Running=running,Status='running' if running else 'exited',ExitCode=0))


def exited(docker):
    docker.objects[RID]['State'].update(Running=False,Status='exited')


def test_launch_exact_identity_mounts_and_no_secret_read(configured,monkeypatch):
    rt,docker=launch(configured)
    args=docker.calls[0]
    assert args[0]=='create'
    assert '/run/secrets/grok-auth.json,readonly' in ' '.join(args)
    assert 'synthetic-only' not in ' '.join(args)
    assert {'959','966','1000'} <= set(docker.objects[RID]['HostConfig']['GroupAdd'])
    assert docker.objects[RID]['Config']['Labels'][MARKER]=='1'
    assert args[-4:]==[IMAGE,'-m','cloudworkbench.hermes_job_entrypoint','/run/task/task.json']
    assert docker.calls[-1]==['start',RID]
    assert rt._record(configured[2],1)['workspace']==configured[-1]['workspace_host']


def test_running_keeps_tools_and_completion_quiesces_before_return(configured):
    rt,docker=launch(configured);add_tool(configured)
    assert rt.status(RID,1)['state']=='running' and TOOL in docker.objects
    exited(docker)
    assert rt.status(RID,1)['state']=='exited'
    assert TOOL not in docker.objects and RID in docker.objects
    assert ['stop','--time','10',TOOL] in docker.calls
    assert ['rm',TOOL] in docker.calls


@pytest.mark.parametrize('operation',['stop','rm'])
def test_tool_cleanup_failure_never_exposes_exited_and_retry_no_relaunch(configured,operation):
    rt,docker=launch(configured);add_tool(configured);exited(docker);docker.fail=operation
    with pytest.raises(RuntimeError):rt.status(RID,1)
    assert TOOL in docker.objects
    docker.fail=None
    assert rt.status(RID,1)['state']=='exited'
    assert sum(c[0]=='create' for c in docker.calls)==1
    assert sum(c[0]=='start' for c in docker.calls)==1


def test_stop_fences_coordinator_before_tools(configured):
    rt,docker=launch(configured);add_tool(configured)
    rt.stop(RID,expected_generation=1)
    assert docker.calls.index(['stop','--time','10',RID]) < docker.calls.index(['stop','--time','10',TOOL])
    assert not docker.objects[RID]['State']['Running'] and TOOL not in docker.objects


def test_missing_coordinator_cleanup_uses_saved_exact_identity_without_auth(configured):
    rt,docker=launch(configured);add_tool(configured)
    docker.objects.pop(RID);rt.hermes_auth.unlink()
    rt.cleanup_infrastructure(configured[2],expected_generation=1)
    assert not docker.objects


def test_live_coordinator_prevents_infrastructure_cleanup(configured):
    rt,docker=launch(configured);add_tool(configured)
    with pytest.raises(RuntimeError,match='live'):
        rt.cleanup_infrastructure(configured[2],expected_generation=1)
    assert TOOL in docker.objects


def test_lost_create_ack_reconciles_created_without_start_or_second_launch(configured):
    rt,docker,attempt,session,mounts,task=configured;docker.fail='create'
    with pytest.raises(RuntimeError,match='lost create'):launch(configured)
    assert rt.status(RID,1)['state']=='created'
    with pytest.raises(RuntimeError,match='already attempted'):
        rt.launch(attempt,session,HERMES_ARGV,{},mounts)
    docker.fail=None;rt.cleanup_attempt(attempt,expected_generation=1)
    assert not docker.objects
    assert not any(c[0]=='start' for c in docker.calls)


def test_lost_start_ack_no_automatic_restart(configured):
    rt,docker,attempt,session,mounts,task=configured;docker.fail='start'
    with pytest.raises(RuntimeError):launch(configured)
    docker.fail=None;rt.cleanup_attempt(attempt,expected_generation=1)
    assert sum(c[0]=='start' for c in docker.calls)==1


@pytest.mark.parametrize('field,value',[('NetworkMode','bridge'),('Privileged',True)])
def test_tool_policy_mismatch_stops_exact_owned_writer_but_refuses_clean_claim(configured,field,value):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    docker.objects[TOOL]['HostConfig'][field]=value
    with pytest.raises(RuntimeError,match='isolation mismatch'):rt.status(RID,1)
    assert not docker.objects[TOOL]['State']['Running'] and TOOL in docker.objects


def test_tool_auth_mount_rejected(configured):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    docker.objects[TOOL]['Mounts'].append(dict(Type='bind',Source=str(rt.hermes_auth),Destination='/secret',RW=False))
    with pytest.raises(RuntimeError,match='isolation mismatch'):rt.status(RID,1)
    assert TOOL in docker.objects


def test_other_generation_tool_preserved(configured):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    docker.objects[TOOL]['Config']['Labels'][PREFIX+'generation']='2'
    assert rt.status(RID,1)['state']=='exited'
    assert TOOL in docker.objects and docker.objects[TOOL]['State']['Running']


def test_stale_generation_refuses_stop(configured):
    rt,docker=launch(configured)
    with pytest.raises(RuntimeError):rt.stop(RID,expected_generation=2)
    assert docker.objects[RID]['State']['Running']


@pytest.mark.parametrize('field,value',[('Image','sha256:'+'d'*64),('Name','/wrong')])
def test_coordinator_identity_drift_refuses_status(configured,field,value):
    rt,docker=launch(configured);docker.objects[RID][field]=value
    with pytest.raises(RuntimeError,match='identity or isolation'):rt.status(RID,1)


@pytest.mark.parametrize('target',['workspace_host','state_host','tool_image','attempt_id','generation','session_id','runtime_owner'])
def test_task_identity_mismatch_before_create(configured,target):
    rt,docker,attempt,session,mounts,task=configured
    task[target]=False
    Path(mounts[0]['source'],'task.json').write_text(json.dumps(task))
    with pytest.raises(RuntimeError,match='task'):launch(configured)
    assert not docker.calls


@pytest.mark.parametrize('kind',['symlink','hardlink','world-readable','large'])
def test_bad_auth_metadata_refuses_without_launch(configured,kind):
    rt,docker,*_=configured
    if kind=='symlink':
        original=rt.hermes_auth.with_name('original');rt.hermes_auth.rename(original);rt.hermes_auth.symlink_to(original)
    elif kind=='hardlink':os.link(rt.hermes_auth,rt.hermes_auth.with_name('other'))
    elif kind=='world-readable':rt.hermes_auth.chmod(0o644)
    else:rt.hermes_auth.write_bytes(b'x'*65537)
    with pytest.raises(RuntimeError):launch(configured)
    assert not docker.calls


def test_ordinary_launch_delegates_and_clone_preserves_facade(configured,monkeypatch):
    rt,*_=configured
    seen=[]
    monkeypatch.setattr(Runtime,'launch',lambda self,*a,**k:seen.append((a,k)) or RID)
    assert rt.launch('a','s',['python3','/opt/cloudworkbench/entrypoint.py','/run/task/verify.json'],{},[])==RID
    assert len(seen)==1
    clone=rt.with_image('sha256:'+'e'*64)
    assert isinstance(clone,HermesCoordinatorRuntime) and clone.hermes_journal_root==rt.hermes_journal_root


def test_cleanup_removes_coordinator_after_tools_and_is_replayable(configured):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    rt.cleanup(RID,expected_generation=1)
    assert docker.calls.index(['rm',TOOL])<docker.calls.index(['rm',RID])
    rt.cleanup_attempt(configured[2],expected_generation=1)
    assert not docker.objects and rt._record(configured[2],1)


def test_readonly_inputs_exact_mounts(configured):
    rt,docker,attempt,session,mounts,task=configured
    inputs=Path(mounts[0]['source']).parent/'inputs';inputs.mkdir();iid=str(uuid.uuid4());(inputs/iid).write_text('input')
    task['inputs']=[dict(id=iid,host_path=str(inputs/iid))]
    Path(mounts[0]['source'],'task.json').write_text(json.dumps(task))
    mounts.append(dict(source=str(inputs),target='/inputs',readonly=True))
    launch(configured);add_tool(configured);exited(docker)
    docker.objects[TOOL]['Mounts'].append(dict(Type='bind',Source=str(inputs/iid),Destination='/inputs/'+iid,RW=False))
    assert rt.status(RID,1)['state']=='exited'


def test_pinned_hermes_cache_child_readonly_mounts_are_allowed(configured):
    from cloudworkbench.hermes_coordinator_runtime import CACHE_PATHS
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    for relative in CACHE_PATHS:
        path=Path(configured[-1]['state_host'])/'hermes-job'/'hermes'/relative
        path.mkdir(parents=True,exist_ok=True)
        docker.objects[TOOL]['Mounts'].append(dict(Type='bind',Source=str(path),Destination='/root/.hermes/'+relative,RW=False))
    assert rt.status(RID,1)['state']=='exited' and TOOL not in docker.objects


@pytest.mark.parametrize('kind',['writable','symlink','parent','wrong-destination'])
def test_cache_mount_cannot_expand_to_private_profile(configured,kind):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    profile=Path(configured[-1]['state_host'])/'hermes-job'/'hermes'
    path=profile/'cache/images';path.mkdir(parents=True)
    mount=dict(Type='bind',Source=str(path),Destination='/root/.hermes/cache/images',RW=False)
    if kind=='writable': mount['RW']=True
    if kind=='symlink':
        path.rmdir();path.symlink_to(profile,target_is_directory=True)
    if kind=='parent': mount['Source']=str(profile)
    if kind=='wrong-destination': mount['Destination']='/root/.hermes'
    docker.objects[TOOL]['Mounts'].append(mount)
    with pytest.raises(RuntimeError):rt.status(RID,1)
    assert TOOL in docker.objects


def test_readiness_safe_errors_and_recovery_does_not_call_auth(configured,monkeypatch):
    from cloudworkbench import hermes_job_entrypoint as entry
    rt,docker=launch(configured)
    monkeypatch.setattr(entry,'read_auth',lambda p:('synthetic-secret',[]))
    assert rt.readiness_reason() is None
    def invalid(p):raise entry.JobError('synthetic-secret')
    monkeypatch.setattr(entry,'read_auth',invalid)
    assert rt.readiness_reason()=='hermes_auth_invalid'
    exited(docker)
    assert rt.status(RID,1)['state']=='exited'
    def unavailable(p):raise OSError('synthetic-secret')
    monkeypatch.setattr(entry,'read_auth',unavailable)
    assert rt.readiness_reason()=='hermes_auth_unavailable'


def test_saved_hermes_attempt_cannot_be_downgraded_to_ordinary_job(configured):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    docker.objects[RID]['Config']['Labels'].pop(MARKER)
    with pytest.raises(RuntimeError,match='marker missing'):rt.status(RID,1)
    assert TOOL in docker.objects


def test_ordinary_verifier_status_preserves_base_contract(configured):
    rt,docker=launch(configured)
    labels=docker.objects[RID]['Config']['Labels']
    labels.pop(MARKER);labels[PREFIX+'attempt']+='-verify'
    exited(docker)
    assert rt.status(RID,1)['state']=='exited'
    rt.cleanup(RID,expected_generation=1)
    assert not docker.objects


@pytest.mark.parametrize('field,value',[
    ('PidsLimit',0),('PidsLimit',-1),('PidsLimit',256),('NanoCpus',0),
    ('NanoCpus',2*10**9),('Memory',0),('Memory',2048*1024**2),
    ('MemorySwap',-1),('MemorySwap',3072*1024**2),('CapDrop',[]),('CapAdd',['SYS_ADMIN']),('SecurityOpt',[]),
    ('GroupAdd',[]),('GroupAdd',['959','966']),('PidMode','host'),('IpcMode','host'),
    ('Devices',[{'PathOnHost':'/dev/sda'}]),('DeviceRequests',[{'Count':-1}]),
])
def test_unbounded_or_privileged_owned_tool_is_stopped_but_not_credited_clean(configured,field,value):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    docker.objects[TOOL]['HostConfig'][field]=value
    with pytest.raises(RuntimeError,match='isolation mismatch'):rt.status(RID,1)
    assert not docker.objects[TOOL]['State']['Running'] and TOOL in docker.objects
    assert ['rm',TOOL] not in docker.calls


def test_root_tool_user_is_refused(configured):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    docker.objects[TOOL]['Config']['User']='0:0'
    with pytest.raises(RuntimeError,match='isolation mismatch'):rt.status(RID,1)
    assert not docker.objects[TOOL]['State']['Running']


def test_actual_linux_tool_capability_spelling_and_startup_skills_mount(configured):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    obj=docker.objects[TOOL]
    obj['HostConfig']['CapAdd']=['CAP_CHOWN','CAP_DAC_OVERRIDE','CAP_FOWNER','CAP_SETGID','CAP_SETUID']
    skills=Path(configured[-1]['state_host'])/'hermes-job'/'hermes'/'skills'
    skills.mkdir(parents=True,mode=0o700)
    obj['Mounts'].append(dict(Type='bind',Source=str(skills),Destination='/root/.hermes/skills',RW=False))
    assert rt.status(RID,1)['state']=='exited' and TOOL not in docker.objects


@pytest.mark.parametrize('capabilities', [['CAP_SYS_ADMIN'],['CAP_CAP_SYS_ADMIN'],[None],[1],['cap_chown']])
def test_capability_prefix_normalization_does_not_expand_allowlist(configured,capabilities):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    docker.objects[TOOL]['HostConfig']['CapAdd']=capabilities
    with pytest.raises(RuntimeError,match='isolation mismatch'):rt.status(RID,1)
    assert TOOL in docker.objects and not docker.objects[TOOL]['State']['Running']


@pytest.mark.parametrize('kind',['writable','symlink','parent','wrong-destination'])
def test_skills_child_exception_cannot_mount_private_profile(configured,kind):
    rt,docker=launch(configured);add_tool(configured);exited(docker)
    profile=Path(configured[-1]['state_host'])/'hermes-job'/'hermes'
    skills=profile/'skills';skills.mkdir(parents=True)
    mount=dict(Type='bind',Source=str(skills),Destination='/root/.hermes/skills',RW=False)
    if kind=='writable':mount['RW']=True
    elif kind=='symlink':skills.rmdir();skills.symlink_to(profile,target_is_directory=True)
    elif kind=='parent':mount['Source']=str(profile)
    else:mount['Destination']='/root/.hermes'
    docker.objects[TOOL]['Mounts'].append(mount)
    with pytest.raises(RuntimeError):rt.status(RID,1)
    assert TOOL in docker.objects
