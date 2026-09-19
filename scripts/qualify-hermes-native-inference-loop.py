"""Actual Hermes CLI -> private HTTP/SSE -> codec -> synthetic inference CLI only."""
import hashlib,importlib.metadata,json,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
from cloudworkbench.inference_transport import InferenceTransport,PinnedCLI,TransportLimits,TransportError
from cloudworkbench.hermes_inference_protocol import admit_request,encode_response,ProtocolError
from cloudworkbench.inference_service import InferenceService,ServiceResponse,_Handler

started=time.monotonic();stage=Path(__file__).resolve().parents[1]
version=importlib.metadata.version('hermes-agent');assert version=='0.21.3'
fixture_schemas=json.loads((stage/'tests/fixtures/hermes/base-tool-schemas.json').read_text())
if isinstance(fixture_schemas,dict):fixture_schemas=fixture_schemas['tools']
expected_schemas={t['function']['name']:t for t in fixture_schemas if t['function']['name'] in {'read_file','write_file','patch','search_files'}}
root=Path(tempfile.mkdtemp(prefix='native-loop-',dir='/tmp'))
for key,suffix in [('HOME','home'),('HERMES_HOME','hermes'),('CODEX_HOME','codex'),('CLAUDE_CONFIG_DIR','claude')]:
    path=root/suffix;path.mkdir();os.environ[key]=str(path)
os.environ.update({'HERMES_BUNDLED_PLUGINS':'/opt/hermes/empty-bundled','HERMES_INTERACTIVE':'0','HERMES_ENABLE_PROJECT_PLUGINS':'0','TERMINAL_CWD':str(root),'SYNTHETIC_CAPABILITY':'b'*64})
fixture=root/'fixture.txt';fixture.write_text('SYNTHETIC_NATIVE_FILE_CONTENT\n')
query=root/'query.txt';query.write_text('Read '+str(fixture)+' using read_file. Report SYNTHETIC_NATIVE_TOOL_LOOP_OK only after receiving the file contents.')
fake=root/'fake-cli'
fake.write_text('#!'+sys.executable+'\n'+'''import json,sys
open('fixture-argv.json','w').write(json.dumps(sys.argv))
request=json.load(sys.stdin)
seen=any(m.get('role')=='tool' and 'SYNTHETIC_NATIVE_FILE_CONTENT' in m.get('content','') for m in request['messages'])
if seen:decision={'kind':'final','text':'SYNTHETIC_NATIVE_TOOL_LOOP_OK','tool_calls':[]}
else:decision={'kind':'tool_calls','text':None,'tool_calls':[{'id':'call_native_loop_1','name':'read_file','arguments':{'path':%r}}]}
print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':decision}))
'''%str(fixture));fake.chmod(0o700)
profile=PinnedCLI(fake,hashlib.sha256(fake.read_bytes()).hexdigest(),'2.1.274','claude-opus-4-6','high')
requests=[];results=[];refusals=[];http_events=[];http_requests=[]
class AuditHandler(_Handler):
    def _event(self,status,code):
        http_requests.append({'method':self.command,'path':self.path[:256],'status':status,'code':code})
        super()._event(status,code)
def execute(body,cancel):
    requests.append(body)
    try:
        admitted=admit_request(body,profile)
        assert {t['function']['name']:t for t in body.get('tools',[])}==expected_schemas
        attempt=root/('fake-request-'+str(len(requests)));attempt.mkdir();home=attempt/'home';scratch=attempt/'scratch';home.mkdir(mode=0o700);scratch.mkdir(mode=0o700)
        value=InferenceTransport(profile,home=home,scratch=scratch,limits=TransportLimits(wall_seconds=3,terminate_seconds=.2,cleanup_seconds=.3)).run(admitted.transport_request,cancel=cancel)
        results.append({'status':value.status,'error_code':value.error_code,'cleanup':value.cleanup,'decision_kind':value.decision.get('kind') if value.decision else None})
        response=encode_response(admitted,value,completion_id='chatcmpl-native-'+str(len(requests)),created=1)
        argv=json.loads((scratch/'fixture-argv.json').read_text())
        assert argv[argv.index('--model')+1]==profile.native_model and argv[argv.index('--effort')+1]==profile.effort
        assert '--safe-mode' in argv and argv[argv.index('--tools')+1]==''
        assert response.content_type=='text/event-stream' and response.metadata['usage_status']['state']=='unknown'
        results[-1].update(content_type=response.content_type,metadata=response.metadata,wire_schemas_match_fixture=True,cli_flags={'model':argv[argv.index('--model')+1],'effort':argv[argv.index('--effort')+1],'safe_mode':True,'tools':''})
        return ServiceResponse(response.status_code,response.content_type,response.body)
    except (ProtocolError,TransportError) as exc:
        status=getattr(exc,'status_code',502)
        refusals.append({'code':exc.code,'status':status,'request':body})
        return ServiceResponse(status,'application/json',json.dumps({'error':{'code':exc.code,'message':exc.code}}).encode())
server=InferenceService(('127.0.0.1',0),capability_sha256=hashlib.sha256(('b'*64).encode()).hexdigest(),authorize=lambda:True,execute=execute,event=lambda status,code:http_events.append({'status':status,'code':code}),execution_timeout=8)
server.RequestHandlerClass=AuditHandler
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
config={'security':{'tirith_enabled':False},'plugins':{'enabled':[]},'model':{'default':profile.native_model,'provider':'native-proof'},
 'providers':{'native-proof':{'base_url':'http://127.0.0.1:'+str(server.server_port)+'/v1','transport':'chat_completions','key_env':'SYNTHETIC_CAPABILITY','default_model':profile.native_model}},
 'agent':{'max_turns':3,'api_max_retries':1},'compression':{'enabled':False},'auxiliary':{'transient_retries':0,'title_generation':{'enabled':False,'model_upgrade_enabled':False}},'terminal':{'backend':'local','cwd':str(root)}}
(Path(os.environ['HERMES_HOME'])/'config.yaml').write_text(json.dumps(config))
argv=['/opt/hermes/venv/bin/hermes','chat','--query-file',str(query),'--format','stream-json','--oneshot','--provider','native-proof','--model',profile.native_model,'--reasoning','high','--toolsets','file']
cli=subprocess.run(argv,cwd=root,capture_output=True,text=True,timeout=50)
server.shutdown();server.server_close();thread.join(2)
events=[];invalid=[]
for line in cli.stdout.splitlines():
    try:events.append(json.loads(line))
    except ValueError:invalid.append(line)
terminal=[e for e in events if e.get('type')=='result']
checks=[]
if cli.returncode==0 and not invalid and len(terminal)==1 and terminal[0].get('exit_code')==0:checks.append('native_cli_terminal_success')
if any(e.get('type')=='tool_result' and e.get('name')=='read_file' for e in events):checks.append('native_file_tool_executed')
if any(any(m.get('role')=='tool' and 'SYNTHETIC_NATIVE_FILE_CONTENT' in str(m.get('content','')) for m in r.get('messages',[])) for r in requests):checks.append('tool_result_returned_to_inference')
if terminal and 'SYNTHETIC_NATIVE_TOOL_LOOP_OK' in terminal[0].get('text',''):checks.append('inference_used_actual_tool_result')
if len(results)==2 and not refusals and all(r['status']=='ok' and r['cleanup']['process_group_stopped'] for r in results) and all(r.get('stream') is True for r in requests):checks.append('two_fresh_synthetic_transports_http_sse')
receipt={'passed':len(checks),'failed':5-len(checks),'checks':checks,'suite':'actual native Hermes CLI private HTTP codec fake inference tool loop','source_sha256':json.loads((stage/'source-manifest.json').read_text()),'cli_argv':argv,'cli_exit':cli.returncode,'config':config,'events':events,'invalid_stdout_lines':invalid,'stderr':cli.stderr[-12000:],'requests':requests,'results':results,'refusals':refusals,'http_events':http_events,'http_requests':http_requests,'hermes_version':version,'synthetic':True,'real_provider_calls':False,'real_credentials':False,'auth_lease_qualified':False,'outer_container_cleanup_required':True,'seconds':time.monotonic()-started}
print(json.dumps(receipt))
raise SystemExit(0 if receipt['failed']==0 else 1)
