import dataclasses
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

from cloudworkbench.inference_transport import (
    InferenceTransport, PinnedCLI, TransportError, TransportLimits,
    child_environment, validate_transcript,
)

MODEL='claude-opus-4-6'
REQUEST={'messages':[{'role':'user','content':'synthetic request $(not-a-command)'}],'tools':[]}
FINAL={'kind':'final','text':'synthetic answer','tool_calls':[]}
RESULT={'type':'result','subtype':'success','is_error':False,'structured_output':FINAL}
TOOL={'type':'function','function':{'name':'read_file','parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}}}


@pytest.fixture
def factory(tmp_path):
    home=tmp_path/'home';scratch=tmp_path/'scratch'
    home.mkdir(mode=0o700);scratch.mkdir(mode=0o700)
    def make(body=None, result=None, **limits):
        binary=tmp_path/'fake-cli'
        script='#!'+sys.executable+'\nimport os,sys,json,time,signal\n'
        script+=body if body is not None else 'sys.stdin.read()\nprint('+repr(json.dumps(RESULT if result is None else result))+')\n'
        binary.write_text(script);binary.chmod(0o700)
        profile=PinnedCLI(binary,hashlib.sha256(binary.read_bytes()).hexdigest(),'2.1.274',MODEL,'high')
        return InferenceTransport(profile,home=home,scratch=scratch,limits=TransportLimits(wall_seconds=limits.pop('wall_seconds',2),terminate_seconds=.15,cleanup_seconds=.2,**limits))
    return make


def test_exact_command_environment_stdin_and_identity(factory,monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-secret')
    monkeypatch.setenv('ANTHROPIC_BASE_URL','https://unapproved.invalid')
    t=factory('data=sys.stdin.read()\nopen("capture.json","w").write(json.dumps({"argv":sys.argv,"env":dict(os.environ),"stdin":data}))\nprint('+repr(json.dumps(RESULT))+')\n')
    result=t.run(REQUEST)
    capture=json.loads((t.scratch/'capture.json').read_text());argv=capture['argv']
    assert result.status=='ok' and result.decision==FINAL
    assert json.loads(capture['stdin'])==REQUEST
    assert REQUEST['messages'][0]['content'] not in ' '.join(argv)
    for flag,value in [('--tools',''),('--disallowedTools','*'),('--mcp-config','{"mcpServers":{}}'),('--setting-sources',''),('--model',MODEL),('--effort','high'),('--max-turns','1')]:
        assert argv[argv.index(flag)+1]==value
    assert {'--safe-mode','--strict-mcp-config','--no-session-persistence','--no-chrome'}<=set(argv)
    assert not any('fallback' in arg or 'resume' in arg for arg in argv)
    assert 'ANTHROPIC_API_KEY' not in capture['env'] and 'ANTHROPIC_BASE_URL' not in capture['env']
    assert set(capture['env'])<=set(t.env)|{'LC_CTYPE','__CF_USER_TEXT_ENCODING'}
    assert result.reported_identity['status']=='unknown'
    assert result.usage is None and result.usage_status['state']=='unknown'
    assert result.provenance=='worker_reported'
    assert result.cleanup=={'process_group_stopped':True,'scope':'process_group','container_cleanup_required':True,'credential_reuse_authorized':False}
    with pytest.raises(TransportError,match='outer_cleanup_required'):t.run(REQUEST)


@pytest.mark.parametrize('effort',[[],{},None,True,'ultra'])
def test_bad_effort_is_stable_error(factory,effort):
    with pytest.raises(TransportError,match='unsupported_effort'):
        dataclasses.replace(factory().profile,effort=effort)


@pytest.mark.parametrize('model',['opus','fable','sonnet','claude-x --fallback-model sonnet'])
def test_native_model_no_alias_or_injection(factory,model):
    with pytest.raises(TransportError,match='invalid_native_model'):
        dataclasses.replace(factory().profile,native_model=model)


def test_transcript_tool_roundtrip_and_new_decision(factory):
    request={'messages':[{'role':'system','content':'Read the file.'},{'role':'user','content':'go'},
      {'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'read_file','arguments':'{"path":"a.txt"}'}}]},
      {'role':'tool','tool_call_id':'call_1','content':'file contents'}],'tools':[TOOL]}
    decision={'kind':'tool_calls','text':None,'tool_calls':[{'id':'call_2','name':'read_file','arguments':{'path':'b.txt'}}]}
    assert factory(result={**RESULT,'structured_output':decision}).run(request).decision==decision
    invalid={**decision,'tool_calls':[{**decision['tool_calls'][0],'id':'call_1'}]}
    assert factory(result={**RESULT,'structured_output':invalid}).run(request).error_code=='invalid_decision'


@pytest.mark.parametrize('decision',[
 {'kind':'tool_calls','text':None,'tool_calls':[{'id':'c','name':'shell','arguments':{}}]},
 {'kind':'tool_calls','text':None,'tool_calls':[{'id':'c','name':'read_file','arguments':{'path':3}}]},
 {'kind':'tool_calls','text':None,'tool_calls':[{'id':'c','name':'read_file','arguments':{'path':'x','escape':True}}]},
 {'kind':'final','text':'ok','tool_calls':[{}]},
 {'kind':'final','text':'ok','tool_calls':[],'unexpected':True},
])
def test_invalid_decisions_fail_closed(factory,decision):
    result=factory(result={**RESULT,'structured_output':decision}).run({**REQUEST,'tools':[TOOL]})
    assert result.status=='error' and result.decision is None


@pytest.mark.parametrize('messages',[
 [{'role':'user','content':[{'type':'image'}]}],
 [{'role':'tool','tool_call_id':[],'content':'x'}],
 [{'role':'assistant','content':None,'tool_calls':[{'id':'c','type':'function','function':{'name':[],'arguments':'{}'}}]}],
 [{'role':'assistant','content':None,'tool_calls':[{'id':'c','type':'function','function':{'name':'read_file','arguments':'{"path":"x"}'}}]}],
 [{'role':'user','content':'x'},{'role':'system','content':'late'}],
])
def test_invalid_transcript_before_launch(factory,messages):
    t=factory('open("launched","w").write("bad")')
    with pytest.raises(TransportError) as caught:t.run({'messages':messages,'tools':[TOOL]})
    assert caught.value.code in {'invalid_transcript','invalid_tool_result','invalid_tool_calls','missing_tool_result'}
    assert not (t.scratch/'launched').exists()


@pytest.mark.parametrize('schema',[
 {'type':'object','anyOf':[]}, {'type':'object','additionalProperties':True},
 {'type':'string','minimum':1}, {'type':'string','minLength':5,'maxLength':2},
 {'type':['string','null']}, {'type':'array'},
])
def test_unsupported_schema_is_explicit(factory,schema):
    tool={'type':'function','function':{'name':'x','parameters':schema}}
    with pytest.raises(TransportError,match='unsupported_tool_schema'):factory().run({**REQUEST,'tools':[tool]})


@pytest.mark.parametrize('mutation,code',[
 ({'model':'claude-other'},'reported_model_mismatch'),
 ({'modelUsage':{'claude-other':{}}},'reported_model_mismatch'),
 ({'effort':'low'},'reported_effort_mismatch'),
 ({'permission_denials':[{'tool_name':'Bash'}]},'unexpected_cli_tool_activity'),
 ({'is_error':True,'api_error_status':401},'provider_auth_rejected'),
 ({'is_error':True,'api_error_status':429},'rate_limited'),
 ({'is_error':True,'result':'401 unauthorized'},'provider_failed'),
 ({'is_error':'false'},'invalid_cli_result'),
 ({'structured_output':None},'invalid_decision'),
])
def test_result_protocol(factory,mutation,code):
    result=factory(result={**RESULT,**mutation}).run(REQUEST)
    assert result.error_code==code and result.decision is None


def test_reported_usage_cost_not_billing_or_trusted_identity(factory):
    result=factory(result={**RESULT,'usage':{'input_tokens':4,'output_tokens':0,'cache_read_input_tokens':True},'total_cost_usd':.001,'modelUsage':{MODEL:{}},'effort':'high'}).run(REQUEST)
    assert result.usage=={'input_tokens':4,'output_tokens':0}
    assert result.cost_basis=='api_equivalent_estimate'
    assert result.reported_identity['model']==MODEL
    assert result.provenance=='worker_reported'


@pytest.mark.parametrize('body,code',[
 ('sys.stdin.read(); print("noisy log"); print('+repr(json.dumps(RESULT))+')','invalid_json'),
 ('sys.stdin.read(); print(\'{"type":"result","type":"result"}\')','duplicate_json_key'),
 ('sys.stdin.read(); print('+repr(json.dumps(RESULT))+'); sys.exit(7)','cli_exit_error'),
 ('sys.stdin.read(); print("x"*2000)','stdout_limit'),
 ('sys.stdin.read(); sys.stderr.write("synthetic-secret"*200)','stderr_limit'),
])
def test_process_failures_are_bounded_and_safe(factory,body,code):
    result=factory(body,stdout_bytes=1024,stderr_bytes=1024).run(REQUEST)
    assert result.error_code==code and result.cleanup['process_group_stopped']
    assert 'synthetic-secret' not in repr(result)


def test_wall_timeout_is_bounded_and_cleans_child(factory):
    start=time.monotonic()
    result=factory('sys.stdin.read(); time.sleep(10)',wall_seconds=.25).run(REQUEST)
    assert result.error_code=='wall_timeout' and result.cleanup['process_group_stopped']
    assert time.monotonic()-start<2


@pytest.mark.parametrize('close_pipes',[False,True])
def test_cancel_kills_child_even_after_pipes_close(factory,close_pipes):
    body='sys.stdin.read()\n'
    if close_pipes:body+='os.close(0); os.close(1); os.close(2)\n'
    body+='time.sleep(10)\n'
    t=factory(body);event=threading.Event();timer=threading.Timer(.15,event.set)
    timer.start();start=time.monotonic()
    try:result=t.run(REQUEST,cancel=event)
    finally:timer.cancel()
    assert result.status=='cancelled' and result.cleanup['process_group_stopped']
    assert time.monotonic()-start<1


def test_cancel_before_start(factory):
    event=threading.Event();event.set()
    with pytest.raises(TransportError,match='cancelled_before_start'):factory().run(REQUEST,cancel=event)


def test_digest_and_mode_rejected(factory):
    t=factory();t.profile.path.write_text('changed')
    with pytest.raises(TransportError,match='binary_digest_mismatch'):t.run(REQUEST)
    t=factory();t.profile.path.chmod(0o777)
    with pytest.raises(TransportError,match='unsafe_binary'):t.run(REQUEST)


def test_input_limit(factory):
    with pytest.raises(TransportError,match='input_limit'):factory(input_bytes=16).run(REQUEST)


def test_private_dirs_accept_setgid_not_group_access(factory):
    t=factory();t.home.chmod(0o2700)
    assert child_environment(t.home,t.scratch)['HOME']==str(t.home)
    t.home.chmod(0o2750)
    with pytest.raises(TransportError,match='unsafe_private_directory'):child_environment(t.home,t.scratch)


@pytest.mark.parametrize('proxy',['http://127.0.0.1:8080','http://0.1.1.1:8080','https://10.0.0.2:8080','http://user:pass@10.0.0.2:8080','http://10.0.0.2:8080/path','http://example.com:8080'])
def test_proxy_validation(factory,proxy):
    t=factory()
    with pytest.raises(TransportError,match='invalid_proxy'):child_environment(t.home,t.scratch,proxy)


def test_registry_setup_failure_still_cleans_child(factory,monkeypatch):
    import cloudworkbench.inference_transport as mod
    t=factory('time.sleep(10)')
    def broken(*args):raise OSError('synthetic-secret')
    monkeypatch.setattr(mod.os,'set_blocking',broken)
    result=t.run(REQUEST)
    assert result.error_code=='cli_io_error' and result.cleanup['process_group_stopped']


def test_cleanup_unknown_prevents_success(factory,monkeypatch):
    import cloudworkbench.inference_transport as mod
    original=mod._stop_group
    def incomplete(proc,limits):
        result=original(proc,limits);result['process_group_stopped']=False;return result
    monkeypatch.setattr(mod,'_stop_group',incomplete)
    result=factory().run(REQUEST)
    assert result.error_code=='process_group_cleanup_unconfirmed' and result.decision is None


def test_actual_pinned_hermes_tool_shapes_supported(factory):
    tools=json.loads((Path(__file__).parent/'fixtures/hermes/base-tool-schemas.json').read_text())
    request={**REQUEST,'tools':tools}
    cases=[('read_file',{'path':'x','offset':1,'limit':2000}),
           ('write_file',{'path':'x','content':'y'}),
           ('patch',{'path':'x','old_string':'a','new_string':'b','replace_all':False}),
           ('search_files',{'pattern':'foo','order':'modified'}),
           ('terminal',{'command':'pytest','background':True,'notify':['ready']}),
           ('terminal',{'command':'pytest','notify':True})]
    for name,arguments in cases:
        decision={'kind':'tool_calls','text':None,'tool_calls':[{'id':'c','name':name,'arguments':arguments}]}
        assert factory(result={**RESULT,'structured_output':decision}).run(request).decision==decision
    bad={'kind':'tool_calls','text':None,'tool_calls':[{'id':'c','name':'terminal','arguments':{'command':'x','notify':[1]}}]}
    assert factory(result={**RESULT,'structured_output':bad}).run(request).error_code=='invalid_tool_arguments'


def test_defaults_are_annotations_not_injected(factory):
    tool={'type':'function','function':{'name':'x','parameters':{'type':'object','properties':{'n':{'type':'integer','default':3}}}}}
    decision={'kind':'tool_calls','text':None,'tool_calls':[{'id':'c','name':'x','arguments':{}}]}
    assert factory(result={**RESULT,'structured_output':decision}).run({**REQUEST,'tools':[tool]}).decision==decision


@pytest.mark.parametrize('schema',[
 {'anyOf':[{'type':'string'}]*5},
 {'anyOf':[{'type':'string'}],'type':'string'},
 {'type':'integer','default':'wrong'},
 {'anyOf':[{'type':'boolean'},{'type':'array','items':{'type':'string'}}],'default':[1]},
])
def test_invalid_union_defaults_rejected(factory,schema):
    tool={'type':'function','function':{'name':'x','parameters':{'type':'object','properties':{'n':schema}}}}
    with pytest.raises(TransportError,match='unsupported_tool_schema'):factory().run({**REQUEST,'tools':[tool]})


def test_cancel_process_group_with_descendant(factory):
    # Parent reaps its child on TERM, so proof does not depend on host PID1 zombie reaping.
    body='''sys.stdin.read()
time.sleep(.35)  # Exercise startup slower than the former 250ms cancellation timer.
pid=os.fork()
if pid==0:
    signal.signal(signal.SIGTERM,signal.SIG_DFL)
    while True: time.sleep(1)
def stop(sig,frame):
    try: os.kill(pid,signal.SIGTERM)
    except ProcessLookupError: pass
    os.waitpid(pid,0)
    sys.exit(0)
signal.signal(signal.SIGTERM,stop)
open('child.pid','w').write(str(pid))
while True: time.sleep(1)
'''
    t=factory(body);event=threading.Event();finished=threading.Event()
    observed_pids=[]
    def cancel_when_descendant_ready():
        deadline=time.monotonic()+1.5
        try:
            while time.monotonic()<deadline and not finished.is_set():
                try:
                    pid=int((t.scratch/'child.pid').read_text())
                    os.kill(pid,0)
                except (FileNotFoundError,ValueError,ProcessLookupError):
                    finished.wait(.01)
                    continue
                observed_pids.append(pid)
                return
        finally:
            event.set()
    waiter=threading.Thread(target=cancel_when_descendant_ready)
    waiter.start()
    try:result=t.run(REQUEST,cancel=event)
    finally:
        finished.set()
        waiter.join(2)
    assert not waiter.is_alive()
    assert observed_pids, 'descendant did not become ready before the startup deadline'
    assert result.status=='cancelled' and result.cleanup['process_group_stopped']
    pid=int((t.scratch/'child.pid').read_text())
    assert observed_pids==[pid]
    with pytest.raises(ProcessLookupError):os.kill(pid,0)


def test_surrogate_output_is_safe_protocol_error(factory):
    result=factory(result={**RESULT,'structured_output':{**FINAL,'text':'\ud800'}}).run(REQUEST)
    assert result.status=='error' and result.error_code=='invalid_json'


def test_stdout_before_large_stdin_does_not_deadlock(factory):
    body='sys.stdout.write(" "*131072); sys.stdout.flush()\nsys.stdin.read()\nprint('+repr(json.dumps(RESULT))+')\n'
    result=factory(body).run({'messages':[{'role':'user','content':'x'*131072}],'tools':[]})
    assert result.status=='ok'


def test_symlink_binary_rejected(factory):
    t=factory();link=t.profile.path.parent/'link';link.symlink_to(t.profile.path)
    t.profile=dataclasses.replace(t.profile,path=link)
    with pytest.raises(TransportError,match='binary_unavailable'):t.run(REQUEST)


def test_root_anyof_rejected_with_stable_code(factory):
    tool={'type':'function','function':{'name':'x','parameters':{'anyOf':[{'type':'object'}]}}}
    with pytest.raises(TransportError,match='unsupported_tool_schema'):factory().run({**REQUEST,'tools':[tool]})


@pytest.mark.parametrize('status,expected',[(401,'provider_auth_rejected'),(429,'rate_limited')])
def test_explicit_error_survives_unrelated_metadata_fault(factory,status,expected):
    result=factory(result={**RESULT,'is_error':True,'api_error_status':status,'model':'claude-other'}).run(REQUEST)
    assert result.error_code==expected and result.decision is None


@pytest.mark.parametrize('schema',[{'type':'string','enum':[1]},{'type':'integer','enum':[True]},{'type':'integer','minimum':2,'enum':[1]}])
def test_impossible_enum_rejected_before_launch(factory,schema):
    tool={'type':'function','function':{'name':'x','parameters':{'type':'object','properties':{'n':schema}}}}
    with pytest.raises(TransportError,match='unsupported_tool_schema'):factory().run({**REQUEST,'tools':[tool]})


def test_oversized_decision_has_output_specific_code(factory):
    decision={'kind':'tool_calls','text':None,'tool_calls':[{'id':str(n),'name':'read_file','arguments':{'path':'x'*10000}} for n in range(8)]}
    result=factory(result={**RESULT,'structured_output':decision}).run({**REQUEST,'tools':[TOOL]})
    assert result.error_code=='decision_limit'


def test_sigkill_escalation_for_term_ignoring_process(factory):
    t=factory('signal.signal(signal.SIGTERM,signal.SIG_IGN)\nsys.stdin.read()\nopen("leader.pid","w").write(str(os.getpid()))\nwhile True:time.sleep(1)\n',wall_seconds=.25)
    started=time.monotonic();result=t.run(REQUEST)
    assert result.error_code=='wall_timeout' and result.cleanup['process_group_stopped']
    assert time.monotonic()-started<1.5
    with pytest.raises(ProcessLookupError):os.kill(int((t.scratch/'leader.pid').read_text()),0)


def test_sigkill_descendant_and_truthful_zombie_uncertainty(factory):
    import subprocess
    body='''sys.stdin.read()
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pid=os.fork()
if pid==0:
    while True:time.sleep(1)
open('processes.json','w').write(json.dumps([os.getpid(),pid]))
while True:time.sleep(1)
'''
    t=factory(body,wall_seconds=.3);started=time.monotonic();result=t.run(REQUEST)
    assert result.error_code in ('wall_timeout','process_group_cleanup_unconfirmed')
    assert time.monotonic()-started<1.5
    assert result.cleanup['credential_reuse_authorized'] is False
    for pid in json.loads((t.scratch/'processes.json').read_text()):
        observed=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True,timeout=1)
        # An orphan zombie depends on the host reaper; never call it a running process or proven cleanup.
        assert not observed.stdout.strip() or observed.stdout.strip().startswith('Z')
        if observed.stdout.strip():assert result.cleanup['process_group_stopped'] is False
