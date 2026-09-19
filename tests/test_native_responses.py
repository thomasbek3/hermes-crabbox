import copy
import json
from pathlib import Path
import threading
import time

import pytest
from cloudworkbench.native_responses import NativeProfile,NativeResponses,NativeError,ResponsesDecoder,validate_request

PROFILES=[NativeProfile('openai-codex','gpt-6-astra','high'),NativeProfile('openai-codex','gpt-5.6-sol','max'),NativeProfile('xai-oauth','grok-4.6','xhigh')]
TOOLS=[{'type':'function',**t['function']} for t in json.loads((Path(__file__).parent/'fixtures/hermes/base-tool-schemas.json').read_text())]


def request(p):
    return {'model':p.model,'reasoning':{'effort':p.effort},'store':False,'stream':True,'instructions':'Use Hermes tools.',
        'include':['reasoning.encrypted_content'],'tools':copy.deepcopy(TOOLS),'input':[{'role':'user','content':'Read file x and explain it.'}]}


def message(text='content was 42'):
    return {'type':'message','id':'msg_1','role':'assistant','status':'completed','content':[{'type':'output_text','text':text,'annotations':[]}]}


def wire(p,items,*,terminal_output=None,model=None,usage=None):
    events=[{'type':'response.output_item.done','output_index':i,'item':item} for i,item in enumerate(items)]
    events.append({'type':'response.completed','response':{'id':'resp_test','status':'completed','model':model or p.model,'output':terminal_output,'usage':usage}})
    return b''.join(b'event: '+v['type'].encode()+b'\ndata: '+json.dumps(v).encode()+b'\n\n' for v in events)


class Response:
    status=200
    def __init__(self,body,fragment=17):self.body=body;self.fragment=fragment;self.headers={'Content-Type':'text/event-stream'}
    def close(self):pass
    def getheader(self,name,default=''):return self.headers.get(name,default)
    def read1(self,n):chunk,self.body=self.body[:min(n,self.fragment)],self.body[min(n,self.fragment):];return chunk


class Connection:
    def __init__(self,response):self.response=response;self.calls=[];self.closed=False;self.sock=None
    def connect(self):pass
    def request(self,*args,**kwargs):self.calls.append((args,kwargs))
    def getresponse(self):return self.response
    def close(self):self.closed=True
    def set_tunnel(self,*args):self.tunnel=args


def transport(p,raw):
    conn=Connection(Response(raw));seen=[]
    def factory(*args,**kwargs):seen.append((args,kwargs));return conn
    return NativeResponses(p,connection_factory=factory),conn,seen


@pytest.mark.parametrize('p',PROFILES)
def test_exact_native_two_turn_tool_roundtrip_preserves_encrypted_reasoning(p):
    req=request(p)
    reasoning={'type':'reasoning','id':'rs_1','summary':[],'encrypted_content':'opaque-state-fixture'}
    call={'type':'function_call','id':'fc_1','call_id':'call_1','name':'read_file','arguments':'{"path":"x"}','status':'completed'}
    api,conn,seen=transport(p,wire(p,[reasoning,call]))
    first=api.execute(req,credential='synthetic-only')
    assert first.status=='ok' and first.container_cleanup_required and not first.credential_reuse_authorized
    assert first.response['output']==[reasoning,call] and first.usage_status=='unknown'
    req['input']+=first.response['output']+[{'type':'function_call_output','call_id':'call_1','output':'1|42'}]
    conn.response=Response(wire(p,[message()],usage={'input_tokens':12,'output_tokens':8}))
    second=api.execute(req,credential='synthetic-only')
    assert second.status=='ok' and second.usage_status=='known'
    body=json.loads(conn.calls[1][1]['body'])
    assert body['model']==p.model and body['reasoning']['effort']==p.effort
    assert body['input'][-3]['encrypted_content']=='opaque-state-fixture'
    assert body['input'][-1]['call_id']=='call_1'
    assert b'response.output_item.done' in second.sse and b'response.completed' in second.sse
    assert len(conn.calls)==2 and conn.closed and 'synthetic-only' not in repr(second)
    assert seen[0][0][0]==('api.x.ai' if p.provider=='xai-oauth' else 'chatgpt.com')


@pytest.mark.parametrize('model,effort',[('gpt-6-astra','max'),('gpt-5.6-sol','high'),('gpt-5.5','high'),('gpt-6-astra',[])])
def test_profile_substitution_refused(model,effort):
    with pytest.raises(NativeError,match='unsupported_profile'):NativeProfile('openai-codex',model,effort)


@pytest.mark.parametrize('change',['model','effort','store','stream','headers','builtin','missing_output','url','duplicate_tool','malformed_args'])
def test_untrusted_request_cannot_select_auth_endpoint_or_provider_tools(change):
    p=PROFILES[0];req=request(p)
    if change=='model':req['model']='gpt-5.5'
    elif change=='effort':req['reasoning']['effort']='max'
    elif change=='store':req['store']=True
    elif change=='stream':req['stream']=False
    elif change=='headers':req['extra_headers']={'Authorization':'attacker'}
    elif change=='builtin':req['tools']=[{'type':'web_search'}]
    elif change=='url':req['endpoint']='http://127.0.0.1'
    elif change=='duplicate_tool':req['tools'].append(req['tools'][0])
    else:req['input'].append({'type':'function_call','call_id':'c1','name':'read_file','arguments':'{"path":"x"}' if change=='missing_output' else '{"path":1}'})
    with pytest.raises(NativeError):validate_request(p,req)


@pytest.mark.parametrize('status,code',[(401,'provider_auth_rejected'),(403,'provider_auth_rejected'),(429,'rate_limited'),(307,'redirect_refused'),(500,'provider_http_error')])
def test_safe_http_failure_no_retry(status,code):
    p=PROFILES[0];api,conn,_=transport(p,b'secret raw error');conn.response.status=status
    out=api.execute(request(p),credential='synthetic-only')
    assert out.error==code and out.response is None and len(conn.calls)==1 and conn.closed


@pytest.mark.parametrize('terminal_output',[None,[],"not-an-array"])
def test_item_done_is_authoritative_when_terminal_output_unusable(terminal_output):
    p=PROFILES[0];api,conn,_=transport(p,wire(p,[message()],terminal_output=terminal_output))
    assert api.execute(request(p),credential='fixture').status=='ok'


@pytest.mark.parametrize('mutation',['model','duplicate','truncated','missing_terminal','bad_args','reused_call','nan','terminal_disagreement'])
def test_bad_stream_refused_without_partial_delivery(mutation):
    p=PROFILES[0];items=[message()];req=request(p)
    if mutation=='bad_args':items=[{'type':'function_call','call_id':'c1','name':'read_file','arguments':'{"path":false}'}]
    if mutation=='reused_call':
        call={'type':'function_call','call_id':'c1','name':'read_file','arguments':'{"path":"x"}'}
        req['input'] += [call,{'type':'function_call_output','call_id':'c1','output':'x'}];items=[call]
    raw=wire(p,items,model='other' if mutation=='model' else None,terminal_output=[message('different')] if mutation=='terminal_disagreement' else None)
    if mutation=='duplicate':raw=raw.replace(b'"id": "resp_test"',b'"id":"x","id":"resp_test"')
    if mutation=='truncated':raw=raw[:-3]
    if mutation=='missing_terminal':raw=raw.split(b'event: response.completed')[0]
    if mutation=='nan':raw=raw.replace(b'"usage": null',b'"usage":NaN')
    api,conn,_=transport(p,raw);result=api.execute(req,credential='fixture')
    assert result.status=='error' and not result.sse and result.response is None


def test_caps_and_credentials_and_precancel():
    p=PROFILES[0];api,conn,_=transport(p,b'')
    with pytest.raises(NativeError):api.execute(request(p),credential='bad\r\nHeader: x')
    req=request(p);req['instructions']='x'*262144
    with pytest.raises(NativeError,match='request_limit'):api.execute(req,credential='fixture')
    cancel=threading.Event();cancel.set()
    assert api.execute(request(p),credential='fixture',cancel=cancel).error=='cancelled'
    assert not conn.calls
    decoder=ResponsesDecoder(p,{},set(),limit=32)
    with pytest.raises(NativeError,match='response_limit'):decoder.feed(b'x'*33)


def test_timeout_returns_unknown_thread_cleanup_until_outer_container_fence():
    p=PROFILES[0];api,conn,_=transport(p,b'');api.timeout=.04
    release=threading.Event()
    conn.getresponse=lambda:(release.wait(2),conn.response)[1]
    before=time.monotonic()
    try:
        result=api.execute(request(p),credential='fixture')
        assert result.error=='wall_timeout' and not result.transport_stopped and result.container_cleanup_required
        assert time.monotonic()-before<.6 and len(conn.calls)==1
    finally:release.set()


@pytest.mark.parametrize('proxy',['http://127.0.0.1:8080','https://10.0.0.2:8080','http://gateway:8080','http://user@10.0.0.2:8080','http://10.0.0.2:8080/path'])
def test_proxy_policy(proxy):
    with pytest.raises(NativeError,match='invalid_proxy'):NativeResponses(PROFILES[0],proxy=proxy)


def test_explicit_private_connect_proxy_no_ambient_proxy(monkeypatch):
    p=PROFILES[0];api,conn,seen=transport(p,wire(p,[message()]))
    api.proxy=('10.0.0.2',8080);monkeypatch.setenv('HTTPS_PROXY','http://attacker.invalid:1')
    result=api.execute(request(p),credential='fixture',account_id='account_1')
    assert result.status=='ok' and seen[0][0]==('10.0.0.2',8080) and conn.tunnel==('chatgpt.com',443)
    assert conn.calls[0][1]['headers']['ChatGPT-Account-ID']=='account_1'

@pytest.mark.parametrize('field,value',[('call_id',[]),('name',{}),('arguments',None)])
def test_malformed_unhashable_call_has_stable_error(field,value):
    p=PROFILES[0];req=request(p)
    req['input'].append({'type':'function_call','call_id':'call_1','name':'read_file','arguments':'{"path":"x"}',field:value})
    with pytest.raises(NativeError):validate_request(p,req)


def test_profile_digest_binds_transport_endpoint_and_exact_policy():
    assert len({p.digest for p in PROFILES})==3
    assert len(PROFILES[0].digest)==64 and PROFILES[0].digest==NativeProfile('openai-codex','gpt-6-astra','high').digest


def test_assistant_refusal_roundtrip():
    p=PROFILES[0];item=message();item['content']=[{'type':'refusal','refusal':'No.'}]
    api,conn,_=transport(p,wire(p,[item]));req=request(p)
    first=api.execute(req,credential='fixture');assert first.status=='ok'
    req['input']+=first.response['output']+[{'role':'user','content':'Explain.'}]
    conn.response=Response(wire(p,[message('I cannot do that.')]))
    assert api.execute(req,credential='fixture').status=='ok'


def test_terminal_comments_and_unneeded_provider_fields_not_delivered():
    p=PROFILES[0];raw=wire(p,[message()]);raw=raw.replace(b'"usage": null',b'"usage":null,"unexpected_provider_blob":"omitted"')+b': partial ping'
    api,conn,_=transport(p,raw);conn.response.fragment=len(raw)
    result=api.execute(request(p),credential='fixture')
    assert result.status=='ok' and b'omitted' not in result.sse and 'unexpected_provider_blob' not in result.response


def test_malformed_error_event_is_stable():
    p=PROFILES[0];decoder=ResponsesDecoder(p,{},set())
    with pytest.raises(NativeError,match='invalid_error_event'):decoder.feed(b'data: {"type":"response.failed","response":"x"}\n\n')


def test_cancel_shuts_response_owned_socket_after_connection_detaches_it():
    p=PROFILES[0];api,conn,_=transport(p,b'');released=threading.Event();reading=threading.Event()
    class Sock:
        def shutdown(self,*unused):released.set()
    sock=Sock();conn.connect=lambda:setattr(conn,'sock',sock)
    def response():
        conn.sock=None
        def read(n):reading.set();released.wait(3);return b''
        conn.response.read1=read
        return conn.response
    conn.getresponse=response;cancel=threading.Event()
    timer=threading.Thread(target=lambda:(reading.wait(2),cancel.set()));timer.start()
    result=api.execute(request(p),credential='fixture',cancel=cancel);timer.join(3)
    assert result.error=='cancelled' and result.transport_stopped and released.is_set()


def test_cancel_during_connect_prevents_late_bearer_post():
    p=PROFILES[0];api,conn,_=transport(p,b'');api.timeout=.03
    released=threading.Event();finished=threading.Event()
    conn.connect=lambda:released.wait(3)
    original_close=conn.close
    def close():original_close();finished.set()
    conn.close=close
    result=api.execute(request(p),credential='fixture')
    assert result.error=='wall_timeout' and not result.transport_stopped and not conn.calls
    finished.clear();released.set();assert finished.wait(1)
    assert not conn.calls


def test_real_stdlib_connection_close_stream_cancel_releases_response_socket():
    import http.client
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    ready=threading.Event();release=threading.Event();cancel=threading.Event();observed=[]
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            observed.append((self.path,self.headers['Authorization']))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close');self.end_headers()
            self.wfile.write(b': waiting\n\n');self.wfile.flush();ready.set();release.wait(3)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    canceller=threading.Thread(target=lambda:(ready.wait(2),cancel.set()));canceller.start()
    try:
        api=NativeResponses(PROFILES[0],timeout=2,connection_factory=lambda *args,**kwargs:http.client.HTTPConnection('127.0.0.1',server.server_port,**kwargs))
        result=api.execute(request(PROFILES[0]),credential='fixture',cancel=cancel)
        assert result.error=='cancelled' and result.transport_stopped
        assert observed==[('/backend-api/codex/responses','Bearer fixture')]
    finally:release.set();server.shutdown();server.server_close();thread.join(2);canceller.join(2)


@pytest.mark.parametrize('profile', PROFILES)
def test_pinned_hermes_cli_cache_and_summary_fields(profile):
    req=request(profile)
    req['reasoning']['summary']='auto'
    req['prompt_cache_key']='pck_'+'a'*24
    body,_,_=validate_request(profile,req)
    assert json.loads(body)['reasoning']=={'effort':profile.effort,'summary':'auto'}
    assert json.loads(body)['prompt_cache_key']==req['prompt_cache_key']


@pytest.mark.parametrize('key',[None,True,'','pck_'+'a'*23,'pck_'+'a'*25,'pck_'+'A'*24,'https://example.com'])
def test_pinned_cache_key_shape_refused(key):
    req=request(PROFILES[0]);req['prompt_cache_key']=key
    with pytest.raises(NativeError,match='invalid_prompt_cache_key'):validate_request(PROFILES[0],req)


@pytest.mark.parametrize('reasoning',[{'effort':'high','summary':'detailed'}, {'effort':'max','summary':'auto'},
    {'effort':'high','summary':'auto','endpoint':'http://elsewhere'}])
def test_summary_option_does_not_relax_profile(reasoning):
    req=request(PROFILES[0]);req['reasoning']=reasoning
    with pytest.raises(NativeError,match='profile_mismatch'):validate_request(PROFILES[0],req)
