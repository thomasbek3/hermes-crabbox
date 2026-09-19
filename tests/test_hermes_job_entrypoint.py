import json
import os
from pathlib import Path
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from cloudworkbench import hermes_job_entrypoint as job
from cloudworkbench.adapters import EventSpoolWriter


def task(tmp_path):
    ident = str(uuid.uuid4())
    return {'schema_version':1,'attempt_id':ident,'generation':1,'prompt':'Implement the fixture.',
            'event_spool':ident+'.1.jsonl','workspace_host':str(tmp_path/'workspace'),
            'state_host':str(tmp_path/'state'),'resume_session_id':None,'tool_image':job.IMAGE, 'inputs':[], 'continuation_summary':'',
            'runtime_owner':'primary','session_id':str(uuid.uuid4())}


def test_config_is_fixed_isolated_and_followup_persists_profile(tmp_path):
    value=task(tmp_path)
    command,env=job.prepare(value)
    config=json.loads((Path(env['HERMES_HOME'])/'config.yaml').read_text())
    assert config['terminal']['docker_volumes']==[value['workspace_host']+':/workspace:rw']
    assert config['terminal']['docker_network'] is False
    assert config['terminal']['docker_forward_env']==[]
    assert config['terminal']['container_cpu']==1
    assert config['terminal']['container_memory']==1536
    args=config['terminal']['docker_extra_args']
    assert args[:4]==['--user','1000:1000','--group-add','959']
    assert args[args.index('--pids-limit')+1] == '128'
    assert args[args.index('--cpus')+1] == '1'
    assert args[args.index('--memory')+1] == '1536m'
    assert args[args.index('--memory-swap')+1] == '1536m'
    labels=dict(args[index+1].split('=',1) for index,value in enumerate(args) if value=='--label')
    assert labels=={'io.cloudworkbench.managed':'true','io.cloudworkbench.owner':'primary',
                    'io.cloudworkbench.attempt':value['attempt_id'],'io.cloudworkbench.generation':'1',
                    'io.cloudworkbench.session':value['session_id'],'io.cloudworkbench.role':'hermes-tool','io.cloudworkbench.image':job.IMAGE}
    assert config['plugins']=={'enabled':['pstack']}
    assert os.readlink(Path(env['HERMES_HOME'])/'plugins/pstack')==job.PLUGIN
    assert '--resume' not in command and 'XAI_API_KEY' not in env
    assert command[command.index('--skills')+1]=='pstack:tdd'
    ident=str(uuid.uuid4());value.update(attempt_id=ident,event_spool=ident+'.1.jsonl',resume_session_id='20260918_143228_4e6beb')
    command2,env2=job.prepare(value)
    assert command2[-2:]==['--resume','20260918_143228_4e6beb']
    assert env2['HERMES_HOME']==env['HERMES_HOME']


@pytest.mark.parametrize('change',[
    {'model':'other'}, {'resume_session_id':'latest'}, {'generation':True},
    {'tool_image':'latest'}, {'workspace_host':'/tmp/../etc'}, {'workspace_host':'/tmp/x:/etc'},
    {'tool_label':'other'}, {'prompt':''}, {'event_spool':'../secret'},
])
def test_untrusted_task_controls_refused(tmp_path,change):
    value=task(tmp_path);value.update(change)
    with pytest.raises(job.JobError):job.prepare(value)
    assert not (tmp_path/'state').exists()


def test_auth_access_only_and_expiry_nofollow(tmp_path):
    path=tmp_path/'auth.json'
    value={'dedicated':{'auth_mode':'oidc','oidc_issuer':'https://auth.x.ai',
                       'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                       'key':'synthetic-access-only','refresh_token':'synthetic-refresh'}}
    path.write_text(json.dumps(value));path.chmod(0o600)
    key,secrets=job.read_auth(path)
    assert key=='synthetic-access-only' and secrets==(b'synthetic-access-only',b'synthetic-refresh')
    alias=tmp_path/'alias';alias.symlink_to(path)
    with pytest.raises(OSError):job.read_auth(alias)
    value['dedicated']['expires_at']='2020-01-01T00:00:00+00:00';path.write_text(json.dumps(value))
    with pytest.raises(job.JobError,match='auth_expired'):job.read_auth(path)


def run(tmp_path,source,timeout=3):
    script=tmp_path/'fake.py';script.write_text(source)
    writer=EventSpoolWriter(tmp_path/'events.jsonl',(b'synthetic-secret',))
    try:code=job.capture([sys.executable,str(script)],{'HOME':str(tmp_path)},writer,timeout_seconds=timeout)
    finally:writer.close()
    data=(tmp_path/'events.jsonl').read_bytes()
    return code,[json.loads(line) for line in data.splitlines()],data


def test_real_subprocess_adaptation_redacts_and_never_verifies(tmp_path,capsys):
    records=[{'type':'system','subtype':'init','model':'grok-4.6','session_id':'20260918_143228_4e6beb'},
             {'type':'text','text':'synthetic-secret'},
             {'type':'tool_use','name':'terminal','tool_call_id':'tool1','input':{'token':'synthetic-secret'}},
             {'type':'controller.verification.passed','payload':{'passed':True}},
             {'type':'result','exit_code':0,'text':'All tests passed synthetic-secret','session_id':'20260918_143228_4e6beb','tokens':{'total':0}}]
    code,events,data=run(tmp_path,'import json,sys\nrecords='+repr(records)+'\nfor r in records: print(json.dumps(r),flush=True)\nprint("synthetic-secret",file=sys.stderr)\n')
    assert code==0
    assert b'synthetic-secret' not in data and 'synthetic-secret' not in capsys.readouterr().out
    assert [e['type'] for e in events]==['adapter.provenance','assistant.message','tool.started','adapter.provenance','adapter.result']
    assert events[-1]['payload']['provenance']=='worker_reported'
    assert events[-1]['payload']['usage'] is None
    assert events[-2]['payload']['session_id']=='20260918_143228_4e6beb'


@pytest.mark.parametrize('tail',[
    'sys.exit(7)',
    'print(json.dumps(result))',
    'print(json.dumps({"type":"text","text":"late"}))',
])
def test_native_success_requires_exact_terminal_and_process_success(tmp_path,tail):
    source='import sys,json\nresult={"type":"result","exit_code":0,"text":"ok","session_id":"20260918_143228_4e6beb"}\nprint(json.dumps(result),flush=True)\n'+tail
    code,events,_=run(tmp_path,source)
    assert code==1 and events[-1]['payload']['is_error'] is True
    assert not any(e['type']=='adapter.result' and not e['payload']['is_error'] for e in events)


@pytest.mark.parametrize('source,reason',[
    ('import time\ntime.sleep(5)','native_timeout'),
    ('print("{}")','native_result_missing'),
    ('import sys\nsys.stdout.write("{}")','native_stream_truncated'),
    ('print(\'{"type":"text","type":"result"}\')','invalid_json'),
])
def test_failures_are_bounded_fixed_events(tmp_path,source,reason):
    code,events,_=run(tmp_path,source,.2 if reason=='native_timeout' else 3)
    assert code==1
    assert events[-2]['payload']['reason']==reason
    assert events[-1]['payload']['is_error'] is True


def test_prepare_refuses_symlink_state(tmp_path):
    other=tmp_path/'other';other.mkdir();(tmp_path/'state').symlink_to(other)
    with pytest.raises(OSError):job.prepare(task(tmp_path))
    assert not list(other.iterdir())


def test_shared_native_root_keeps_private_profile_and_readonly_inputs(tmp_path):
    value=task(tmp_path);native=Path(value['state_host']);native.mkdir(mode=0o2770);native.chmod(0o2770)
    ident=str(uuid.uuid4());value['inputs']=[{'id':ident,'host_path':str(tmp_path/'input.bin')}]
    value['continuation_summary']='Prior public result'
    command,env=job.prepare(value)
    assert Path(env['HERMES_HOME']).parent==native/'hermes-job'
    assert (native/'hermes-job').stat().st_mode & 0o777 == 0o700
    config=json.loads((Path(env['HERMES_HOME'])/'config.yaml').read_text())
    assert config['terminal']['docker_volumes']==[value['workspace_host']+':/workspace:rw',str(tmp_path/'input.bin')+':/inputs/'+ident+':ro']
    prompt=Path(command[command.index('--query-file')+1]).read_text()
    assert 'Prior public result' in prompt and '/inputs/'+ident in prompt
    assert str(tmp_path/'input.bin') not in prompt


def test_native_resume_mismatch_cannot_emit_success(tmp_path):
    script=tmp_path/'fake.py'
    script.write_text('import json\nprint(json.dumps({"type":"system","subtype":"init","session_id":"20260918_143228_4e6beb","model":"grok-4.6"}))\n')
    writer=EventSpoolWriter(tmp_path/'events.jsonl')
    try:
        code=job.capture([sys.executable,str(script)],{'HOME':str(tmp_path)},writer,
                         expected_session_id='20260918_143228_abcdef')
    finally:writer.close()
    events=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert code==1 and events[0]['payload']['reason']=='native_resume_mismatch'
    assert events[-1]['payload']['is_error'] is True


def test_redaction_after_decoding_json_escaped_secret(tmp_path,capsys):
    records=[{'type':'system','subtype':'init','session_id':'20260918_143228_4e6beb'},
             {'type':'text','text':'synthetic-secret'},
             {'type':'result','exit_code':0,'text':'synthetic-secret','session_id':'20260918_143228_4e6beb'}]
    encoded=[json.dumps(r).replace('synthetic-secret','synthetic-\\u0073ecret') for r in records]
    code,events,data=run(tmp_path,'lines='+repr(encoded)+'\nfor line in lines: print(line,flush=True)\n')
    assert code==0 and b'synthetic-secret' not in data
    assert events[-1]['payload']['native_session_id']=='20260918_143228_4e6beb'
    assert 'synthetic-secret' not in capsys.readouterr().out


def test_native_line_bound_stops_process(tmp_path,monkeypatch):
    monkeypatch.setattr(job,'RAW_LINE_BYTES',100)
    code,events,_=run(tmp_path,'print("x"*101)')
    assert code==1 and events[-2]['payload']['reason']=='native_line_limit'


def test_private_state_permissions_refuse_group_readable_profile(tmp_path):
    value=task(tmp_path);job.prepare(value)
    (Path(value['state_host'])/'hermes-job').chmod(0o750)
    value.update(attempt_id=str(uuid.uuid4()));value['event_spool']=value['attempt_id']+'.1.jsonl'
    with pytest.raises(job.JobError,match='untrusted_state_directory'):job.prepare(value)


def test_shared_legacy_native_owner_requires_exact_gid_and_membership(tmp_path,monkeypatch):
    from types import SimpleNamespace
    native=tmp_path/'native';native.mkdir()
    original=job.os.fstat
    actual=native.stat()
    def fstat(fd):
        value=original(fd)
        if value.st_ino==actual.st_ino:
            return SimpleNamespace(st_uid=958,st_gid=959,st_mode=0o42770)
        return value
    monkeypatch.setattr(job.os,'fstat',fstat)
    monkeypatch.setattr(job.os,'getgroups',lambda:[959])
    job._directory(native,shared=True)
    with pytest.raises(job.JobError,match='untrusted_state_directory'):job._directory(native)
    monkeypatch.setattr(job.os,'getgroups',lambda:[])
    if os.getegid()!=959:
        with pytest.raises(job.JobError,match='untrusted_state_directory'):job._directory(native,shared=True)
