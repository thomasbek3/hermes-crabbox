import hashlib
import http.client
import json
import threading
import pytest
from cloudworkbench.inference_service import InferenceService,ServiceResponse


def test_listener_raw_responses_roundtrip_keeps_protocol_and_tool_ids():
    calls=[]; token='a'*64
    def execute(request,cancel):
        calls.append(request)
        out=({'type':'function_call','id':'fc_1','call_id':'call_1','name':'read_file','arguments':'{"path":"sample.txt"}'}
             if len(calls)==1 else {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Observed sample data'}]})
        return ServiceResponse(200,'application/json',json.dumps({'id':f'resp_{len(calls)}','object':'response','status':'completed',
                              'model':'gpt-6-astra','output':[out]}).encode())
    server=InferenceService(('127.0.0.1',0),request_path='/v1/responses',capability_sha256=hashlib.sha256(token.encode()).hexdigest(),authorize=lambda:True,execute=execute)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        def send(path,request):
            conn=http.client.HTTPConnection(*server.server_address,timeout=5)
            conn.request('POST',path,json.dumps(request),{'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            response=conn.getresponse();result=(response.status,json.loads(response.read()));conn.close();return result
        req={'model':'gpt-6-astra','reasoning':{'effort':'high'},'input':[{'role':'user','content':'Read sample.txt'}]}
        status,first=send('/v1/responses',req); assert status==200
        call=first['output'][0]
        req2=dict(req,input=req['input']+[call,{'type':'function_call_output','call_id':'call_1','output':'sample data'}])
        status,second=send('/v1/responses',req2)
        assert status==200 and second['output'][0]['content'][0]['text']=='Observed sample data'
        assert calls==[req,req2]
        assert send('/v1/chat/completions',req)[0]==404 and len(calls)==2
    finally:
        server.shutdown();server.server_close();thread.join()


def test_path_cannot_be_arbitrary_or_multiple():
    with pytest.raises(ValueError,match='protocol path'):
        InferenceService(('127.0.0.1',0),request_path='/anything',capability_sha256='a'*64,authorize=lambda:True,execute=lambda *_:None)
