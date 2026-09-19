"""Real MCP HTTP protocol -> real task API/store, without running a worker/model."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

import httpx
import pytest
from cloudworkbench.api import create_app
from cloudworkbench.store import Store

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('omarchy_gateway', ROOT/'integrations/omarchy-mcp/server.py')
gateway = importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path/'state.db')
    owner = store.add_client('owner', 'a'*40, ['submit','observe','retrieve','cancel'], ['hermes-tasks'])
    store.add_client('other', 'b'*40, ['submit','observe','retrieve','cancel'], ['hermes-tasks'])
    store.add_client('reader', 'c'*40, ['observe'], ['hermes-tasks'])
    artifacts = tmp_path/'artifacts';artifacts.mkdir()
    settings={'hermes_enabled':True,'projects':{'hermes-tasks':{'allowed_agents':['hermes'],
        'models':{'hermes':['grok-4.6']},'environment_versions':[gateway.DEFAULT_ENV,gateway.ROUTED_ENV]}},
        'artifact_root':artifacts,'input_root':tmp_path/'inputs','sse_poll_seconds':0.001}
    api = create_app(store,settings)
    backend = gateway.Backend(httpx.ASGITransport(app=api))
    mcp,app = gateway.create_server(backend)
    return store, owner, artifacts, backend, app


@asynccontextmanager
async def client(app,token='a'*40):
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=gateway.ORIGIN,
            headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'}) as c:
            yield c


async def rpc(c,method,params=None,**kwargs):
    return await c.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}},**kwargs)


async def call(c,name,args=None,**kwargs):
    r=await rpc(c,'tools/call',{'name':name,'arguments':args or {}},**kwargs)
    assert r.status_code==200,r.text
    return r.json()['result']


def data(result):
    assert not result.get('isError'),result
    return result['structuredContent']


@pytest.mark.asyncio
async def test_discovery_auth_and_package(setup):
    *_,app=setup
    async with client(app) as c:
        init=await rpc(c,'initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
        assert init.status_code==200 and init.json()['result']['serverInfo']['name']=='Omarchy Cloud'
        tools=(await rpc(c,'tools/list')).json()['result']['tools']
        assert len(tools)==10
        assert {t['name'] for t in tools}>={'submit_task','get_task','get_results','get_delegation_guide'}
        guide=data(await call(c,'get_delegation_guide'))
        assert 'CONTRIBUTING.md' in guide['skill'] and 'Tailscale' in guide['skill']
        package=await c.get('/mcp/skill.zip');assert package.status_code==200
        z=zipfile.ZipFile(io.BytesIO(package.content))
        assert 'omarchy-cloud-delegate/SKILL.md' in z.namelist()
        for headers,status in [({'Authorization':''},401),({'Authorization':'Bearer '+'z'*40},401),
                               ({'Origin':'https://evil.example'},403),({'Host':'evil.example'},403)]:
            assert (await rpc(c,'tools/list',headers=headers)).status_code==status
        # Tailscale's stripped path and direct /mcp both work.
        assert (await c.post('/',json={'jsonrpc':'2.0','id':1,'method':'tools/list'})).status_code==200


@pytest.mark.asyncio
async def test_submit_ownership_idempotency_events_cancel_followup(setup):
    store,_,_,_,app=setup
    async with client(app) as c:
        args={'goal':'Build a bounded fixture','idempotency_key':'mcp-test-1'}
        first=data(await call(c,'submit_task',args));sid=first['session_id'];aid=first['attempt_id']
        assert data(await call(c,'submit_task',args))==first
        assert (await call(c,'submit_task',{**args,'goal':'Different'}))['isError']
        status=data(await call(c,'get_task',{'session_id':sid}));assert status['state']=='queued'
        assert store.get_attempt(aid)['request']['environment_version']==gateway.DEFAULT_ENV
        for token in ['b'*40,'c'*40]:
            assert (await call(c,'get_task',{'session_id':sid},headers={'Authorization':'Bearer '+token}))['isError']
        assert (await call(c,'submit_task',args,headers={'Authorization':'Bearer '+'c'*40}))['isError']
        events=data(await call(c,'get_events',{'session_id':sid,'limit':1}));assert len(events['events'])==1
        assert events['next_after']>0
        assert data(await call(c,'list_tasks'))['sessions'][0]['id']==sid
        cancelled=data(await call(c,'cancel_task',{'attempt_id':aid,'idempotency_key':'cancel-1'}))
        assert store.get_attempt(aid)['state']=='cancelled'
        follow=data(await call(c,'follow_up',{'session_id':sid,'message':'Continue','idempotency_key':'follow-1'}))
        assert follow['delivery']=='queued'
        # Per-request identity cannot bleed across concurrent calls.
        responses=await asyncio.gather(*[call(c,'list_tasks',headers={'Authorization':'Bearer '+t*40}) for t in ['a','b','a','b']])
        assert [len(data(x)['sessions']) for x in responses]==[1,0,1,0]


@pytest.mark.asyncio
async def test_results_text_integrity_and_inputs(setup):
    store,_,root,backend,app=setup
    async with client(app) as c:
        first=data(await call(c,'submit_task',{'goal':'Fixture','idempotency_key':'artifacts-1'}))
        raw=b'Changed the form. Checks passed.';path=root/'notes.md';path.write_bytes(raw)
        artifact=store.register_artifact(first['attempt_id'],{'path':'notes.md','storage_path':str(path),
            'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'mime':'text/markdown'},expected_generation=first['generation'])
        results=data(await call(c,'get_results',{'session_id':first['session_id']}))
        assert results['artifacts'][0]['download_url'].startswith(gateway.ORIGIN+'/v1/artifacts/')
        text=data(await call(c,'read_artifact',{'artifact_id':artifact['id']}));assert text['content']==raw.decode()
        assert (await call(c,'read_artifact',{'artifact_id':artifact['id']},headers={'Authorization':'Bearer '+'b'*40}))['isError']
        inp=data(await call(c,'prepare_input',{'name':'source.txt','idempotency_key':'input-1'}))
        assert inp['upload_idempotency_key']=='input-1:content'
        async with httpx.AsyncClient(transport=backend.transport,base_url=gateway.UPSTREAM) as up:
            upload=await up.put('/v1/inputs/'+inp['id']+'/content',content=b'source bytes',headers={
                'Authorization':'Bearer '+'a'*40,'Idempotency-Key':inp['upload_idempotency_key']})
            assert upload.status_code==200
        admitted=data(await call(c,'submit_task',{'goal':'Use the input','input_ids':[inp['id']],'idempotency_key':'with-input'}))
        assert store.get_attempt(admitted['attempt_id'])['request']['input_ids']==[inp['id']]


@pytest.mark.asyncio
async def test_invalid_schema_and_bounded_body(setup):
    store,*_,app=setup
    async with client(app) as c:
        for name,args in [('get_task',{'session_id':'../health'}),
                          ('submit_task',{'goal':'x','idempotency_key':'bad\nkey'}),
                          ('submit_task',{'goal':'x','idempotency_key':'x','workflow':'other'}),
                          ('get_events',{'session_id':'00000000-0000-0000-0000-000000000000','after':-1})]:
            assert (await call(c,name,args))['isError']
        r=await c.post('/mcp',content=b'x'*(gateway.MAX_BODY+1));assert r.status_code==413
        assert not data(await call(c,'list_tasks'))['sessions']
