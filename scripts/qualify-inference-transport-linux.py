#!/usr/bin/env python3
"""Stdlib fake-CLI Linux qualification, inside a disposable private PID namespace."""
import dataclasses,hashlib,importlib.util,json,os,signal,sys,threading,time
from pathlib import Path
from cloudworkbench.inference_transport import InferenceTransport,PinnedCLI,TransportLimits,TransportError
ROOT=Path(__file__).resolve().parents[1]
REQUEST={'messages':[{'role':'user','content':'synthetic literal $(not-a-command)'}],'tools':[]}
FINAL={'kind':'final','text':'synthetic answer','tool_calls':[]}
RESULT={'type':'result','subtype':'success','is_error':False,'structured_output':FINAL}
TOOLS=json.loads((ROOT/'tests/fixtures/hermes/base-tool-schemas.json').read_text())
records=[];counter=0

def make(body=None,result=None,**limits):
 global counter
 counter+=1;case=ROOT/('case-'+str(counter));case.mkdir(mode=0o700)
 home=case/'home';scratch=case/'scratch';home.mkdir(mode=0o700);scratch.mkdir(mode=0o700)
 binary=case/'fake-cli';script='#!'+sys.executable+'\nimport os,sys,json,time,signal\n'
 script+=body if body is not None else 'sys.stdin.read()\nprint('+repr(json.dumps(RESULT if result is None else result))+')\n'
 binary.write_text(script);binary.chmod(0o700)
 profile=PinnedCLI(binary,hashlib.sha256(binary.read_bytes()).hexdigest(),'2.1.274','claude-opus-4-6','high')
 return InferenceTransport(profile,home=home,scratch=scratch,limits=TransportLimits(wall_seconds=limits.pop('wall_seconds',2),terminate_seconds=.15,cleanup_seconds=.25,**limits))

def check(name,fn):
 start=time.monotonic()
 try:
  details=fn() or {};records.append({'case':name,'status':'passed','seconds':time.monotonic()-start,'details':details})
 except Exception as exc:
  records.append({'case':name,'status':'failed','seconds':time.monotonic()-start,'error_type':type(exc).__name__,'error':str(exc)[:300]})

def procstate(pid):
 try:return Path('/proc/'+str(pid)+'/stat').read_text().split(') ',1)[1].split()[0]
 except FileNotFoundError:return None

def exact():
 os.environ['ANTHROPIC_API_KEY']='synthetic-not-real';os.environ['ANTHROPIC_BASE_URL']='https://blocked.invalid'
 t=make('data=sys.stdin.read()\nopen("capture.json","w").write(json.dumps({"argv":sys.argv,"env":dict(os.environ),"stdin":data}))\nprint('+repr(json.dumps(RESULT))+')\n')
 r=t.run(REQUEST);c=json.loads((t.scratch/'capture.json').read_text());args=c['argv']
 assert r.status=='ok' and r.decision==FINAL and json.loads(c['stdin'])==REQUEST
 assert 'ANTHROPIC_API_KEY' not in c['env'] and 'ANTHROPIC_BASE_URL' not in c['env']
 assert REQUEST['messages'][0]['content'] not in ' '.join(args)
 for flag in ['--safe-mode','--json-schema','--system-prompt','--no-session-persistence']:assert flag in args
 for flag,value in [('--tools',''),('--disallowedTools','*'),('--mcp-config','{"mcpServers":{}}'),('--setting-sources',''),('--model','claude-opus-4-6'),('--effort','high'),('--max-turns','1')]:assert args[args.index(flag)+1]==value
 assert r.cleanup['process_group_stopped'] and r.cleanup['container_cleanup_required'] and not r.cleanup['credential_reuse_authorized']
 try:t.run(REQUEST);raise AssertionError('reuse accepted')
 except TransportError as exc:assert exc.code=='outer_cleanup_required'
 return {'cleanup':r.cleanup,'requested_identity':r.requested_identity,'reported_identity':r.reported_identity}

def roundtrip():
 request={'messages':[{'role':'user','content':'go'},{'role':'assistant','content':None,'tool_calls':[{'id':'prior','type':'function','function':{'name':'read_file','arguments':'{"path":"a.txt"}'}}]},{'role':'tool','tool_call_id':'prior','content':'synthetic data'}],'tools':TOOLS}
 d={'kind':'tool_calls','text':None,'tool_calls':[{'id':'next','name':'read_file','arguments':{'path':'b.txt'}}]}
 r=make(result={**RESULT,'structured_output':d}).run(request);assert r.decision==d
 d['tool_calls'][0]['id']='prior';assert make(result={**RESULT,'structured_output':d}).run(request).error_code=='invalid_decision'
 return {'returned_tool_call_only':True,'tool_execution':False}

def shapes():
 cases=[('read_file',{'path':'x','offset':1,'limit':2000}),('write_file',{'path':'x','content':'y'}),('patch',{'path':'x','old_string':'a','new_string':'b','replace_all':False}),('search_files',{'pattern':'foo','order':'modified'}),('terminal',{'command':'pytest','background':True,'notify':['ready']})]
 for name,args in cases:
  decision={'kind':'tool_calls','text':None,'tool_calls':[{'id':'next','name':name,'arguments':args}]}
  assert make(result={**RESULT,'structured_output':decision}).run({**REQUEST,'tools':TOOLS}).decision==decision
 bad={'kind':'tool_calls','text':None,'tool_calls':[{'id':'bad','name':'delete_file','arguments':{'path':'x'}}]}
 assert make(result={**RESULT,'structured_output':bad}).run({**REQUEST,'tools':TOOLS}).error_code=='invalid_decision'
 return {'negative_undeclared_tool_refused':True,'admitted_schema_names':[name for name,_ in cases],'tools_executed':False}

def process_error(body,code):
 start=time.monotonic();r=make(body,wall_seconds=.25 if code=='wall_timeout' else 2,stdout_bytes=1024,stderr_bytes=1024).run(REQUEST)
 assert r.error_code==code and r.cleanup['process_group_stopped'];assert time.monotonic()-start<3
 assert 'synthetic-secret' not in repr(r)
 return {'error_code':r.error_code,'cleanup':r.cleanup}

def cancel(close):
 body='sys.stdin.read()\n'+('os.close(0);os.close(1);os.close(2)\n' if close else '')+'time.sleep(10)\n'
 t=make(body);e=threading.Event();timer=threading.Timer(.15,e.set);timer.start();start=time.monotonic()
 try:r=t.run(REQUEST,cancel=e)
 finally:timer.cancel()
 assert r.status=='cancelled' and r.cleanup['process_group_stopped'] and time.monotonic()-start<1
 return {'pipes_closed':close,'cleanup':r.cleanup}

def descendant(reap):
 body='''sys.stdin.read()
pid=os.fork()
if pid==0:
    signal.signal(signal.SIGTERM,signal.SIG_DFL if REAP else signal.SIG_IGN)
    while True:time.sleep(1)
open('pids.json','w').write(json.dumps([os.getpid(),pid]))
def stop(sig,frame):
    os.kill(pid,signal.SIGTERM);os.waitpid(pid,0);sys.exit(0)
signal.signal(signal.SIGTERM,stop if REAP else signal.SIG_IGN)
while True:time.sleep(1)
'''.replace('REAP',repr(reap))
 t=make(body,wall_seconds=.3);start=time.monotonic();r=t.run(REQUEST)
 states=[procstate(pid) for pid in json.loads((t.scratch/'pids.json').read_text())]
 assert time.monotonic()-start<1.5 and all(s is None or s=='Z' for s in states)
 assert not r.cleanup['credential_reuse_authorized'] and r.cleanup['container_cleanup_required']
 if reap:assert r.cleanup['process_group_stopped'] and states==[None,None]
 if 'Z' in states:assert not r.cleanup['process_group_stopped']
 return {'remaining_states':states,'error_code':r.error_code,'cleanup':r.cleanup}

def escaped():
 body='''sys.stdin.read()
pid=os.fork()
if pid==0:
    os.setsid();os.close(0);os.close(1);os.close(2)
    open('escaped.pid','w').write(str(os.getpid()))
    while True:time.sleep(1)
while not os.path.exists('escaped.pid'):time.sleep(.01)
print(RESPONSE)
'''.replace('RESPONSE',repr(json.dumps(RESULT)))
 t=make(body);r=t.run(REQUEST);pid=int((t.scratch/'escaped.pid').read_text());state=procstate(pid)
 assert r.status=='ok' and state not in (None,'Z')
 assert r.cleanup['process_group_stopped'] and r.cleanup['container_cleanup_required'] and not r.cleanup['credential_reuse_authorized']
 # Intentionally leave this child alive until PID1 exits; the outer runner verifies container exit/removal.
 return {'escaped_pid':pid,'state_before_outer_cleanup':state,'cleanup':r.cleanup,'outer_cleanup_required':True}

def duplex():
 body='sys.stdout.write(" "*131072);sys.stdout.flush()\nsys.stdin.read()\nprint('+repr(json.dumps(RESULT))+')\n'
 r=make(body).run({'messages':[{'role':'user','content':'x'*131072}],'tools':[]});assert r.status=='ok'
 return {'stdin_bytes':131072,'stdout_prefix_bytes':131072}

check('exact_argv_stdin_environment_one_use',exact)
check('tool_result_roundtrip_and_id_reuse_rejection',roundtrip)
check('pinned_five_base_tool_schemas',shapes)
for name,body,code in [
 ('stdout_cap','sys.stdin.read();print("x"*2000)','stdout_limit'),
 ('stderr_cap','sys.stdin.read();sys.stderr.write("synthetic-secret"*200)','stderr_limit'),
 ('wall_timeout','sys.stdin.read();time.sleep(10)','wall_timeout'),
 ('term_ignoring_leader','signal.signal(signal.SIGTERM,signal.SIG_IGN);sys.stdin.read();time.sleep(10)','wall_timeout'),
 ('noise','sys.stdin.read();print("noise");print('+repr(json.dumps(RESULT))+')','invalid_json'),
 ('duplicate_json','sys.stdin.read();print(\'{"type":"result","type":"result"}\')','duplicate_json_key'),
 ('nonzero_exit','sys.stdin.read();print('+repr(json.dumps(RESULT))+');sys.exit(7)','cli_exit_error')]:
 check(name,lambda body=body,code=code:process_error(body,code))
check('cancel_open_pipes',lambda:cancel(False));check('cancel_closed_pipes',lambda:cancel(True))
check('descendant_reaped',lambda:descendant(True));check('descendant_sigkill_zombie_truthfulness',lambda:descendant(False))
check('duplex_pipe_no_deadlock',duplex)
check('escaped_session_requires_outer_cleanup',escaped)
receipt={'platform':sys.platform,'python':sys.version,'uid':os.getuid(),'tmpfs_noexec':bool(os.statvfs('/tmp').f_flag & os.ST_NOEXEC),'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in json.loads((ROOT/'source-manifest.json').read_text())},
 'pytest_available':importlib.util.find_spec('pytest') is not None,'suite':'bounded stdlib Linux subset; full76-testpytest suite not run','cases':records,
 'passed':sum(r['status']=='passed' for r in records),'failed':sum(r['status']=='failed' for r in records),
 'provider_calls':False,'real_credentials_used':False,'real_cli_executed':False,
 'uncovered':['full pytest parameter matrix','real CLI argv/schema acceptance','Hermes inference adapter integration','real provider auth/model/effort','auth capsule lease/refresh','service cancellation/recovery'],
 'outer_container_cleanup_verified_by_parent':False}
print(json.dumps(receipt,indent=2),flush=True)
raise SystemExit(0 if receipt['failed']==0 else 1)
