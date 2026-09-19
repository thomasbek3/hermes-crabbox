"""Local HTTP+codec+synthetic CLI integration; no native Hermes/provider proof."""
import hashlib
import http.client
import json
from pathlib import Path
import sys
import threading

from cloudworkbench.hermes_inference_protocol import admit_request, encode_response, ProtocolError
from cloudworkbench.inference_service import InferenceService, ServiceResponse
from cloudworkbench.inference_transport import InferenceTransport, PinnedCLI


def test_http_tool_decision_and_result_roundtrip(tmp_path):
    cli=tmp_path/'fake-cli'
    cli.write_text('#!'+sys.executable+'\n'+'''import json,sys
r=json.load(sys.stdin)
if r['messages'][-1]['role']=='tool':
 d={'kind':'final','text':r['messages'][-1]['content'],'tool_calls':[]}
else:
 d={'kind':'tool_calls','text':None,'tool_calls':[{'id':'read1','name':'read_file','arguments':{'path':'fixture.txt'}}]}
print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':d}))
''');cli.chmod(0o700)
    profile=PinnedCLI(cli,hashlib.sha256(cli.read_bytes()).hexdigest(),'2.1.274','claude-fable-5-1','high')
    token='a'*64;calls=[]
    def execute(raw,cancel):
        try:
            request=admit_request(raw,profile)
            ordinal=len(calls);calls.append(request)
            home=tmp_path/('home'+str(ordinal));scratch=tmp_path/('scratch'+str(ordinal))
            home.mkdir(mode=0o700);scratch.mkdir(mode=0o700)
            result=InferenceTransport(profile,home=home,scratch=scratch).run(request.transport_request,cancel=cancel)
            encoded=encode_response(request,result,completion_id='chatcmpl-test'+str(ordinal),created=1)
            return ServiceResponse(encoded.status_code,encoded.content_type,encoded.body)
        except ProtocolError as exc:
            return ServiceResponse(exc.status_code,'application/json',json.dumps({'error':{'code':exc.code}}).encode())
    server=InferenceService(('127.0.0.1',0),capability_sha256=hashlib.sha256(token.encode()).hexdigest(),authorize=lambda:True,execute=execute)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    def post(value):
        conn=http.client.HTTPConnection(*server.server_address,timeout=5)
        conn.request('POST','/v1/chat/completions',json.dumps(value),{'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        response=conn.getresponse();status,body=response.status,response.read();conn.close();return status,body
    try:
        tools=json.loads((Path(__file__).parent/'fixtures/hermes/base-tool-schemas.json').read_text())
        tools=[t for t in tools if t['function']['name']=='read_file']
        messages=[{'role':'user','content':'Read the fixture'}]
        request={'model':profile.native_model,'messages':messages,'tools':tools}
        status,body=post(request);assert status==200
        first=json.loads(body);message=first['choices'][0]['message']
        assert first['choices'][0]['finish_reason']=='tool_calls'
        assert message['tool_calls'][0]['id']=='read1'
        assert 'usage' not in first
        # The caller supplies tool output; the provider side never executes the tool.
        messages.extend([message,{'role':'tool','tool_call_id':'read1','content':'synthetic fixture contents'}])
        status,body=post({**request,'stream':True,'stream_options':{'include_usage':True}})
        assert status==200 and body.endswith(b'data: [DONE]\n\n')
        chunks=[json.loads(line[6:]) for line in body.splitlines() if line.startswith(b'data: ') and line!=b'data: [DONE]']
        assert any(c['choices'] and c['choices'][0]['delta'].get('content')=='synthetic fixture contents' for c in chunks)
        assert any(c['choices'] and c['choices'][0].get('finish_reason')=='stop' for c in chunks)
        status,_=post({**request,'model':'different'})
        assert status==400 and len(calls)==2
    finally:
        server.shutdown();server.server_close();thread.join(2)
