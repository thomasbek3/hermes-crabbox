from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

import pytest
from cloudworkbench import routed_caller as caller
from cloudworkbench.hermes_adapter import build_routed_launch
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.workflow_instructions import StageInstructions, STAGE_BOUNDARY


@pytest.fixture
def plan_files(monkeypatch):
    text='Review the assigned fixture.'
    stage=StageInstructions('review','code_review','fixture',hashlib.sha256(text.encode()).hexdigest(),text,STAGE_BOUNDARY)
    plan=build_routed_launch(NativeProfile('openai-codex','gpt-6-astra','high'),stage,task='Review',input_revision_sha256='a'*64,workspace_readonly=True)
    files={'/run/task/launch.json':json.dumps(asdict(plan)).encode(), '/run/task/prompt.txt':plan.prompt.encode(),
           '/run/tool/hermes/config.yaml':plan.config_json.encode()}
    monkeypatch.setattr(caller,'_read',lambda path,limit:files[str(path)])
    return plan,files


def test_bound_caller_plan_loads_without_credentials(plan_files):
    plan,_=plan_files
    assert caller.load_plan()==plan
    assert 'CWB_INFERENCE_CAPABILITY' not in dict(plan.environment)


@pytest.mark.parametrize('kind',['prompt','config','argv','environment','endpoint','extra_field'])
def test_mismatched_readonly_material_refused(plan_files,kind):
    plan,files=plan_files
    if kind=='prompt':files['/run/task/prompt.txt']=b'other'
    elif kind=='config':files['/run/tool/hermes/config.yaml']=b'{}'
    else:
        value=json.loads(files['/run/task/launch.json'])
        if kind=='argv':value['argv']=['/bin/sh','-c','anything']
        elif kind=='environment':value['environment'].append(['OPENAI_API_KEY','synthetic-unapproved'])
        elif kind=='endpoint':value['request_path']='/elsewhere'
        else:value['extra']='unapproved'
        files['/run/task/launch.json']=json.dumps(value).encode()
    with pytest.raises(caller.CallerError):caller.load_plan()


@pytest.mark.parametrize('name',['run_hermes','run_relay'])
def test_wrong_runtime_uid_refused_before_file_read(monkeypatch,name):
    monkeypatch.setattr(caller.os,'getuid',lambda:0)
    monkeypatch.setattr(caller,'_read',lambda *args:pytest.fail('must not read capability'))
    with pytest.raises(caller.CallerError,match='identity'):getattr(caller,name)()


def test_safe_file_reader_rejects_symlink_hardlink_and_oversize(tmp_path):
    file=tmp_path/'file';file.write_bytes(b'123')
    assert caller._read(file,3)==b'123'
    with pytest.raises(caller.CallerError):caller._read(file,2)
    link=tmp_path/'link';link.symlink_to(file)
    with pytest.raises(caller.CallerError):caller._read(link,3)
    link.unlink();link.hardlink_to(file)
    with pytest.raises(caller.CallerError):caller._read(file,3)


def test_worker_record_is_complete_when_published(tmp_path,monkeypatch):
    target=tmp_path/'result.json';replace=caller.os.replace
    value={'status':'completed','provenance':'worker_reported'}
    def observe(source,destination):
        assert not target.exists()
        assert json.loads(Path(source).read_text())==value
        assert Path(source).stat().st_mode & 0o777 == 0o600
        return replace(source,destination)
    monkeypatch.setattr(caller.os,'replace',observe)
    caller._publish_record(target,value)
    assert json.loads(target.read_text())==value
    assert list(tmp_path.iterdir())==[target]
    with pytest.raises(FileExistsError):caller._publish_record(target,{'status':'failed'})
    assert json.loads(target.read_text())==value
    assert list(tmp_path.iterdir())==[target]


def test_main_never_emits_exception_secrets(monkeypatch,capsys):
    monkeypatch.setattr(caller.sys,'argv',['caller','hermes'])
    def fail():raise ValueError('synthetic-secret')
    monkeypatch.setattr(caller,'run_hermes',fail)
    assert caller.main()==1
    output=capsys.readouterr()
    assert output.err=='routed_caller_bootstrap_failed\n' and not output.out


@pytest.mark.parametrize('value',[b'F'*64,b'a'*63,b'a'*65,b'a'*64+b'\n',b'\xff'])
def test_capability_exact_shape_only(monkeypatch,value):
    monkeypatch.setattr(caller,'_read',lambda *args:value)
    with pytest.raises(caller.CallerError):caller._capability(Path('fixture'))


def test_detached_supervisor_persists_bounded_public_events(plan_files,tmp_path):
    import sys
    from dataclasses import replace
    plan,_=plan_files
    secret='f'*64
    code="""import json,sys
print(json.dumps({'type':'system','subtype':'init','model':'gpt-6-astra','session_id':'fixture'}))
print(json.dumps({'type':'text','text':'hello '+sys.argv[1]}))
print(json.dumps({'type':'result','exit_code':0,'text':'done '+sys.argv[1],'tokens':{'input':0,'output':0}}))
print(sys.argv[1],file=sys.stderr)
"""
    plan=replace(plan,argv=(sys.executable,'-c',code,secret,'--run-budget','10'))
    assert caller.supervise_hermes(plan,{'PATH':'/usr/bin:/bin'},secret,tmp_path,cwd=tmp_path)==0
    event_text=(tmp_path/'events.jsonl').read_text()
    assert secret not in event_text and '[REDACTED]' in event_text
    events=[json.loads(line) for line in event_text.splitlines()]
    assert events[-1]['payload']['usage'] is None
    receipt=json.loads((tmp_path/'result.json').read_text())
    assert receipt['status']=='completed' and receipt['outer_cleanup_required'] and not receipt['verification_pass']
    assert receipt['stderr_bytes']==65


def test_detached_supervisor_rejects_bad_stdout(plan_files,tmp_path):
    import sys
    from dataclasses import replace
    plan,_=plan_files
    plan=replace(plan,argv=(sys.executable,'-c',"print('not json')",'--run-budget','10'))
    assert caller.supervise_hermes(plan,{'PATH':'/usr/bin:/bin'},'f'*64,tmp_path,cwd=tmp_path)==1
    receipt=json.loads((tmp_path/'result.json').read_text())
    assert receipt['status']=='failed' and receipt['failure']=='caller_execution_failed'


def test_detached_supervisor_no_result_is_not_success(plan_files,tmp_path):
    import sys
    from dataclasses import replace
    plan,_=plan_files
    plan=replace(plan,argv=(sys.executable,'-c','pass','--run-budget','10'))
    assert caller.supervise_hermes(plan,{'PATH':'/usr/bin:/bin'},'f'*64,tmp_path,cwd=tmp_path)==1
    assert json.loads((tmp_path/'result.json').read_text())['status']=='failed'


@pytest.mark.parametrize('emit_result',[True,False])
def test_exited_hermes_does_not_wait_for_inherited_grandchild_pipes(plan_files,tmp_path,emit_result):
    import sys,time
    from dataclasses import replace
    plan,_=plan_files
    code="""import os,time,json
print(json.dumps({'type':'system','subtype':'init','model':'gpt-6-astra'}),flush=True)
if %r: print(json.dumps({'type':'result','exit_code':0,'text':'done'}),flush=True)
if os.fork()==0: time.sleep(60)
os._exit(0)
""" % emit_result
    plan=replace(plan,argv=(sys.executable,'-c',code,'--run-budget','10'))
    started=time.monotonic()
    result=caller.supervise_hermes(plan,{'PATH':'/usr/bin:/bin'},'f'*64,tmp_path,cwd=tmp_path)
    assert time.monotonic()-started < 5
    assert result == (0 if emit_result else 1)
    receipt=json.loads((tmp_path/'result.json').read_text())
    assert receipt['status']==('completed' if emit_result else 'failed')
    assert receipt['outer_cleanup_required'] is True


def test_observation_root_fresh_private_and_retained_in_scratch(tmp_path):
    scratch=tmp_path/'scratch';scratch.mkdir(mode=0o770)
    output=caller.create_observation_root(scratch)
    assert output==scratch/'.cwb-observations'
    assert output.stat().st_mode&0o777==0o700
    assert output.stat().st_uid==os.getuid() and output.stat().st_gid==os.getgid()
    (output/'events.jsonl').write_text('retained fixture\n')
    with pytest.raises(caller.CallerError,match='observation_directory_exists'):
        caller.create_observation_root(scratch)
    assert (output/'events.jsonl').read_text()=='retained fixture\n'


def test_observation_root_refuses_symlinks_and_existing_entries(tmp_path):
    scratch=tmp_path/'scratch';scratch.mkdir(mode=0o770)
    target=tmp_path/'target';target.mkdir()
    (scratch/'.cwb-observations').symlink_to(target,target_is_directory=True)
    with pytest.raises(caller.CallerError,match='observation_directory_exists'):
        caller.create_observation_root(scratch)
    assert list(target.iterdir())==[]
    alias=tmp_path/'scratch-link';alias.symlink_to(scratch,target_is_directory=True)
    with pytest.raises(caller.CallerError,match='observation_directory_unavailable'):
        caller.create_observation_root(alias)


def test_observation_root_refuses_world_accessible_scratch(tmp_path):
    scratch=tmp_path/'scratch';scratch.mkdir();scratch.chmod(0o777)
    with pytest.raises(caller.CallerError,match='untrusted_observation_scratch'):
        caller.create_observation_root(scratch)
    assert not (scratch/'.cwb-observations').exists()


@pytest.mark.parametrize('field',['owner','group'])
def test_observation_root_refuses_untrusted_owner_or_group(tmp_path,monkeypatch,field):
    from types import SimpleNamespace
    scratch=tmp_path/'scratch';scratch.mkdir(mode=0o770)
    original=os.fstat
    def changed(fd):
        value=original(fd)
        return SimpleNamespace(st_mode=value.st_mode,st_uid=os.getuid()+10000 if field=='owner' else value.st_uid,
                               st_gid=os.getgid()+10000 if field=='group' else value.st_gid)
    monkeypatch.setattr(os,'fstat',changed)
    with pytest.raises(caller.CallerError,match='untrusted_observation_scratch'):
        caller.create_observation_root(scratch)
    assert not (scratch/'.cwb-observations').exists()


def test_run_hermes_uses_durable_observation_root(plan_files,monkeypatch,tmp_path):
    from dataclasses import replace
    plan,_=plan_files
    plan=replace(plan,required_empty_directories=())
    monkeypatch.setattr(caller.os,'getuid',lambda:1000)
    monkeypatch.setattr(caller.os,'getgid',lambda:1000)
    monkeypatch.setattr(caller,'load_plan',lambda:plan)
    monkeypatch.setattr(caller,'_capability',lambda _: 'a'*64)
    expected=tmp_path/'scratch/.cwb-observations'
    monkeypatch.setattr(caller,'create_observation_root',lambda:expected)
    seen=[]
    def supervise(plan,env,capability,output_root,*,cwd):
        seen.append((output_root,cwd));return 0
    monkeypatch.setattr(caller,'supervise_hermes',supervise)
    assert caller.run_hermes()==0 and seen==[(expected,'/workspace')]


def test_observation_root_accepts_trusted_controller_owned_scratch(tmp_path,monkeypatch):
    from types import SimpleNamespace
    scratch=tmp_path/'scratch';scratch.mkdir(mode=0o770)
    original_fstat,original_lstat=os.fstat,Path.lstat
    root_info=scratch.stat()
    def metadata(value):
        if (value.st_dev,value.st_ino)==(root_info.st_dev,root_info.st_ino):
            fields={k:getattr(value,k) for k in ('st_mode','st_uid','st_gid','st_dev','st_ino')};fields['st_uid']=959
            return SimpleNamespace(**fields)
        return value
    monkeypatch.setattr(os,'fstat',lambda fd:metadata(original_fstat(fd)))
    monkeypatch.setattr(Path,'lstat',lambda p,*a,**kw:metadata(original_lstat(p,*a,**kw)))
    assert caller.create_observation_root(scratch)==scratch/'.cwb-observations'
