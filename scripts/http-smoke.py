"""HTTP + real controller/worker/job UID synthetic acceptance, no provider call."""
import hashlib
import json
import time
import uuid
import urllib.request
from pathlib import Path

BASE='http://127.0.0.1:7780'
TOKEN=Path('/var/lib/cloud-workbench/control/client.token').read_text().strip()


def call(method,path,data=None,key=None,raw=False):
    body=data if isinstance(data,bytes) else (json.dumps(data).encode() if data is not None else None)
    headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/octet-stream' if isinstance(data,bytes) else 'application/json'}
    if method!='GET':headers['Idempotency-Key']=key or str(uuid.uuid4())
    req=urllib.request.Request(BASE+path,data=body,headers=headers,method=method)
    with urllib.request.urlopen(req,timeout=15) as response:
        if raw:return response.read(2*1024*1024),{k.lower():v for k,v in response.headers.items()}
        return json.load(response)


def wait(sid,turns):
    end=time.monotonic()+120
    while time.monotonic()<end:
        data=call('GET','/v1/sessions/'+sid)
        if len(data.get('turns',[]))>=turns and data['state'] in ('completed','failed','cancelled','interrupted'):return data
        time.sleep(.5)
    raise RuntimeError('Smoke timeout: '+data['state'])


def assert_verified(result):
    assert result['state']=='completed', 'Synthetic execution did not complete'
    assert result.get('outcome')=='verified', 'Protected verifier did not accept task'


def downloads(sid,expected_name,expected_bytes,expected_attempt):
    items=call('GET','/v1/sessions/'+sid+'/artifacts')['artifacts']
    matches=[item for item in items if item['path']==expected_name and item['attempt_id']==expected_attempt]
    assert len(matches)==1,'Expected one exported binary input artifact per attempt'
    item=matches[0]
    body,headers=call('GET','/v1/artifacts/'+item['id']+'/content',raw=True)
    digest=hashlib.sha256(body).hexdigest()
    assert body==expected_bytes,'Binary download differs from supplied input'
    assert digest==item['sha256'],'Binary download SHA does not match manifest'
    assert len(body)==item['bytes'],'Binary download size does not match manifest'
    assert headers.get('content-disposition','').startswith('attachment'),'Unsafe download disposition'
    assert 'storage_path' not in item,'Private worker path leaked'
    return {'artifact_id':item['id'],'attempt_id':item['attempt_id'],'path':item['path'],'bytes':len(body),'sha256':digest,'authenticated_download':True}


def main():
    receipt={'schema_version':1,'synthetic':True,'provider_called':False,'passed':False,'checks':{}}
    try:
        content=b'\x00\xffCloud Workbench synthetic upload\r\n'+uuid.uuid4().bytes+b'\x80\x00'
        item=call('POST','/v1/inputs',{'name':'qualification.bin'})
        upload_key=str(uuid.uuid4())
        ready=call('PUT','/v1/inputs/'+item['id']+'/content',content,upload_key)
        retry=call('PUT','/v1/inputs/'+item['id']+'/content',content,upload_key)
        assert ready==retry and ready['state']=='ready','Upload idempotency failed'
        assert ready['bytes']==len(content) and ready['sha256']==hashlib.sha256(content).hexdigest(),'Upload integrity failed'
        receipt['input']={'id':item['id'],'bytes':len(content),'sha256':ready['sha256']}
        receipt['checks']['binary_upload_integrity_and_retry']=True
        request={'project_id':'sample-web','goal':'Synthetic qualification: repair sample booking validator and preserve supplied binary input','agent':'fixture','environment_version':'demo-v1','input_ids':[item['id']],'acceptance':[{'id':'booking-validity','description':'Valid dates pass; malformed and nonexistent dates fail.','mandatory':True}]}
        key=str(uuid.uuid4())
        first=call('POST','/v1/sessions',request,key)
        again=call('POST','/v1/sessions',request,key)
        assert first==again,'Session idempotency failed'
        receipt['checks']['session_idempotency']=True
        sid=first['session_id'];receipt['session_id']=sid
        result=wait(sid,1);assert_verified(result)
        receipt['first_result']={'state':result['state'],'outcome':result['outcome'],'attempt_id':first['attempt_id']}
        expected_name='input-'+item['id']+'.bin'
        receipt['first_download']=downloads(sid,expected_name,content,first['attempt_id'])
        receipt['checks']['controller_worker_job_binary_roundtrip']=True
        follow=call('POST','/v1/sessions/'+sid+'/messages',{'message':'Synthetic follow-up: retain valid-date behavior and the same supplied binary input'})
        result=wait(sid,2);assert_verified(result)
        assert len(result['turns'])==2,'Follow-up did not create exactly one new turn'
        assert first['attempt_id']!=follow['attempt_id'],'Follow-up reused attempt'
        receipt['followup_result']={'state':result['state'],'outcome':result['outcome'],'attempt_id':follow['attempt_id'],'turns':len(result['turns'])}
        receipt['followup_download']=downloads(sid,expected_name,content,follow['attempt_id'])
        receipt['checks']['followup_preserves_input_and_verification']=True
        receipt['passed']=all(receipt['checks'].values())
    except Exception as exc:
        receipt['error']=type(exc).__name__+': '+str(exc)[:300]
    print(json.dumps(receipt,indent=2),flush=True)
    return 0 if receipt['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
