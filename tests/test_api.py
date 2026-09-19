from pathlib import Path
import hashlib
import pytest
from fastapi.testclient import TestClient
from cloudworkbench.api import create_app
from cloudworkbench.store import Store

@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    principal=store.add_client('owner','a'*40,['submit','observe','retrieve','cancel'],['project'])
    store.add_client('other','b'*40,['submit','observe','retrieve','cancel'],['project'])
    settings={'projects':{'project':{'allowed_agents':['claude'],'models':{'claude':['approved-model']},'environment_versions':['env1']}},'artifact_root':tmp_path/'artifacts','input_root':tmp_path/'inputs','input_max_bytes':1024,'sse_poll_seconds':0.001}
    settings['artifact_root'].mkdir()
    client=TestClient(create_app(store,settings))
    return store,principal,client,settings

def headers(key='create', token='a'*40):
    return {'Authorization':'Bearer '+token,'Idempotency-Key':key}

def create(client,**kwargs):
    return client.post('/v1/sessions',json={'project_id':'project','goal':'test','agent':'claude','environment_version':'env1',**kwargs},headers=headers())

def test_auth_scope_config_and_idempotency(setup):
    store,p,client,cfg=setup
    assert client.get('/health').status_code==200
    assert client.get('/v1/sessions').status_code==401
    assert client.get('/v1/ready',headers=headers()).status_code==503
    assert create(client,model='not-configured').status_code==422
    assert create(client,project_id='arbitrary-url').status_code==422
    assert create(client,agent='fixture').status_code==422
    assert create(client,command='rm something').status_code==422
    first=create(client)
    assert first.status_code==201
    assert create(client).json()==first.json()
    assert create(client,goal='different').status_code==409
    sid=first.json()['session_id']
    assert client.get('/v1/sessions/'+sid,headers=headers(token='b'*40)).status_code==404
    assert client.get('/v1/sessions/'+sid+'/events?follow=false',headers=headers(token='b'*40)).status_code==404
    assert client.post('/v1/sessions',json={'project_id':'project','goal':'test','agent':'claude'},headers={'Authorization':'Bearer '+'a'*40}).status_code==422

def test_sse_cursor_and_followup(setup):
    store,p,client,cfg=setup
    created=create(client).json()
    aid,sid=created['attempt_id'],created['session_id']
    store.claim_next()
    store.transition(aid,'failed',reason='setup', expected_generation=store.get_attempt(aid)['generation'])
    response=client.get(f'/v1/sessions/{sid}/events?follow=false',headers={**headers(),'Last-Event-ID':'1'})
    assert response.status_code==200 and 'id: 1\n' not in response.text and 'id: 2\n' in response.text
    assert client.get(f'/v1/sessions/{sid}/events',headers={**headers(),'Last-Event-ID':'bad'}).status_code==422
    assert client.post(f'/v1/sessions/{sid}/messages',json={'message':'next'},headers=headers('follow')).json()['delivery']=='queued'
    assert client.post(f'/v1/sessions/{sid}/takeover',headers=headers('take')).status_code==501
    assert client.delete(f'/v1/sessions/{sid}',headers=headers('delete')).status_code==501

def test_binary_artifact_no_path_disclosure_and_symlink_denial(setup,tmp_path):
    store,p,client,cfg=setup
    created=create(client).json()
    body=b'\x00\xffdata'
    path=cfg['artifact_root']/'binary.bin'
    path.write_bytes(body)
    artifact=store.register_artifact(created['attempt_id'],{'path':'binary.bin','storage_path':str(path),'sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body),'mime':'application/octet-stream'}, expected_generation=created['generation'])
    listing=client.get(f"/v1/sessions/{created['session_id']}/artifacts",headers=headers())
    assert 'storage_path' not in listing.text
    route=f"/v1/artifacts/{artifact['id']}/content"
    response=client.get(route,headers=headers())
    assert response.content==body and response.headers['content-disposition'].startswith('attachment')
    assert client.get(route,headers=headers(token='b'*40)).status_code==404
    path.unlink()
    outside=tmp_path/'outside';outside.write_bytes(body)
    path.symlink_to(outside)
    assert client.get(route,headers=headers()).status_code==404

def test_upload_immutable_retry_limit_and_foreign_input(setup):
    store,p,client,cfg=setup
    item=client.post('/v1/inputs',json={'name':'input.bin'},headers=headers('reserve')).json()
    route=f"/v1/inputs/{item['id']}/content"
    assert client.put(route,content=b'data',headers=headers('put',token='b'*40)).status_code==404
    result=client.put(route,content=b'data',headers=headers('put'))
    assert result.status_code==200 and result.json()['state']=='ready'
    assert 'storage_path' not in result.text
    staged=Path(store.get_input(p,item['id'])['storage_path'])
    assert staged.stat().st_mode & 0o777 == 0o440
    assert client.put(route,content=b'data',headers=headers('put')).status_code==200
    assert client.put(route,content=b'changed',headers=headers('put2')).status_code==409
    assert len(list(cfg['input_root'].iterdir()))==1
    assert create(client,input_ids=[item['id']]).status_code==201
    another=client.post('/v1/inputs',json={'name':'large'},headers=headers('reserve2')).json()
    assert client.put(f"/v1/inputs/{another['id']}/content",content=b'x'*1025,headers=headers('put3')).status_code==413
    assert store.get_input(p,another['id'])['state']=='pending'

def test_json_limit(setup):
    _,_,client,_=setup
    assert client.post('/v1/sessions',content=b'x'*1048577,headers=headers()).status_code==413

def test_admission_requires_explicit_environment_and_canonical_allowlist(setup):
    store,p,client,cfg=setup
    assert create(client,environment_version=None).status_code==422
    assert create(client,environment_version='unknown').status_code==422
    cfg['projects']['project'].pop('allowed_agents')
    cfg['projects']['project']['agents']=['claude']
    cfg['agents']=['claude']
    assert create(client).status_code==422
    assert store.list_sessions(p)==[]
    cfg['projects']['project']['allowed_agents']=['claude']
    cfg['projects']['project']['models']=['approved-model']
    assert create(client,model='approved-model').status_code==422


def test_blocked_sqlite_read_does_not_block_health_event_loop(setup,monkeypatch):
    import asyncio
    import threading
    import httpx
    store,p,_,cfg=setup
    entered=threading.Event()
    release=threading.Event()
    original=store.list_sessions
    def blocking(*args,**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args,**kwargs)
    monkeypatch.setattr(store,'list_sessions',blocking)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store,cfg)),base_url='http://test') as client:
            pending=asyncio.create_task(client.get('/v1/sessions',headers=headers()))
            assert await asyncio.to_thread(entered.wait,2)
            try:
                health=await asyncio.wait_for(client.get('/health'),timeout=1)
                assert health.status_code==200
            finally:
                release.set()
            assert (await pending).status_code==200
    asyncio.run(check())


def test_api_reports_quota_rejection_without_partial_session(tmp_path):
    store=Store(tmp_path/'state.db',policy={'max_pending_attempts':1,'input_max_bytes':10,'input_owner_bytes':10})
    p=store.add_client('owner','a'*40,['submit','observe','retrieve','cancel'],['project'])
    cfg={'projects':{'project':{'allowed_agents':['claude'],'environment_versions':['env1']}}}
    client=TestClient(create_app(store,cfg))
    assert create(client).status_code==201
    second=client.post('/v1/sessions',json={'project_id':'project','goal':'second','agent':'claude','environment_version':'env1'},headers=headers('second'))
    assert second.status_code==503 and second.headers['retry-after']=='5'
    assert len(store.list_sessions(p))==1
    assert client.post('/v1/inputs',json={'name':'a'},headers=headers('input1')).status_code==201
    denied=client.post('/v1/inputs',json={'name':'b'},headers=headers('input2'))
    assert denied.status_code==429 and denied.headers['retry-after']=='5'


def test_diff_is_latest_attempt_scoped_and_requires_owned_delivery(setup):
    store,p,client,cfg=setup
    created=create(client).json();sid,aid=created['session_id'],created['attempt_id']
    assert client.get(f'/v1/sessions/{sid}/diff',headers=headers()).status_code==409
    attempt=store.claim_next()
    delivery={'base_commit':'a'*40,'patch_sha256':'b'*64,'changed_files':[],'complete_text_patch':True}
    store.transition(aid,'failed',reason='test',result={'delivery':delivery},expected_generation=attempt['generation'])
    artifact=store.register_artifact(aid,{'path':'@delivery/changes.patch','storage_path':str(cfg['artifact_root']/'patch'),'sha256':'b'*64,'bytes':1},expected_generation=attempt['generation'])
    response=client.get(f'/v1/sessions/{sid}/diff',headers=headers())
    assert response.status_code==200 and response.json()['patch_artifact_id']==artifact['id']
    assert client.get(f'/v1/sessions/{sid}/diff',headers=headers(token='b'*40)).status_code==404
    store.add_message(p,sid,'Follow up','next')
    assert client.get(f'/v1/sessions/{sid}/diff',headers=headers()).status_code==409


def test_result_bundle_authorized_terminal_hashes_and_provenance(setup):
    import io
    import json
    import zipfile
    store, principal, client, cfg = setup
    task = create(client, model='approved-model').json()
    sid, aid = task['session_id'], task['attempt_id']
    route = f'/v1/sessions/{sid}/bundle'
    assert client.get(route, headers=headers()).status_code == 409
    content = b'\x00\xffBundle fixture'
    path = cfg['artifact_root'] / 'bundle.bin'; path.write_bytes(content)
    store.register_artifact(aid, {'path': 'bundle.bin', 'storage_path': str(path), 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'mime': 'application/octet-stream'}, expected_generation=task['generation'])
    store.append_event(aid, 'adapter.provenance', {'model': 'actual-model-version', 'cli_version': '2.1.274'}, expected_generation=task['generation'])
    store.cancel(principal, aid, 'cancel-bundle')
    response = client.get(route, headers=headers())
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/zip'
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.headers['x-cloud-attempt-id'] == aid
    assert response.headers['etag'] == '"' + hashlib.sha256(response.content).hexdigest() + '"'
    assert int(response.headers['content-length']) == len(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read('files/bundle.bin') == content
        result = json.loads(archive.read('result.json'))
        assert result['state'] == 'cancelled' and result['unresolved_issues']
        assert result['provider_provenance']['resolved_model']['value'] == 'actual-model-version'
        assert result['provider_provenance']['requested_model']['value'] == 'approved-model'
        assert result['provider_provenance']['cli_version']['value'] == '2.1.274'
    assert client.get(route).status_code == 401
    assert client.get(route, headers=headers(token='b'*40)).status_code == 404
    for scope in ('observe', 'retrieve'):
        with store._tx() as db:
            db.execute('UPDATE clients SET scopes=? WHERE id=?', (json.dumps([scope]), principal['id']))
        assert client.get(route, headers=headers()).status_code == 403
    with store._tx() as db:
        db.execute('UPDATE clients SET scopes=? WHERE id=?', (json.dumps(['observe', 'retrieve', 'submit']), principal['id']))
    path.write_bytes(b'x' * len(content))
    refused = client.get(route, headers=headers())
    assert refused.status_code == 409 and refused.headers['content-type'] == 'application/json'
    assert 'Bundle fixture' not in refused.text
    store.add_message(principal, sid, 'new attempt', 'bundle-next')
    assert client.get(route, headers=headers()).status_code == 409


def test_bundle_paused_is_immutable_resume_new_attempt_and_timing_stable(setup):
    import io
    import json
    import zipfile
    from cloudworkbench.result_bundle import authorized_snapshot, build_bundle
    from cloudworkbench.store import StoreError
    store, principal, client, cfg = setup
    task = create(client).json(); sid, aid = task['session_id'], task['attempt_id']
    store.claim_next()
    for state in ('running', 'checkpointing', 'paused'):
        store.transition(aid, state, expected_generation=task['generation'])
    snapshot, artifacts = authorized_snapshot(store, principal, sid)
    before = store.get_attempt(aid)['updated_at']
    path = cfg['artifact_root']/'late'; path.write_bytes(b'late')
    store.register_artifact(aid, {'path': 'late', 'storage_path': str(path), 'bytes': 4, 'sha256': hashlib.sha256(b'late').hexdigest()}, expected_generation=task['generation'])
    assert store.get_attempt(aid)['updated_at'] == before
    with pytest.raises(StoreError, match='transition|immutable'):
        store.transition(aid, 'running', expected_generation=task['generation'])
    resumed = store.resume(principal, sid, 'resume-paused')
    assert resumed['attempt_id'] != aid and resumed['generation'] > task['generation']
    assert client.get(f'/v1/sessions/{sid}/bundle', headers=headers()).status_code == 409
    with zipfile.ZipFile(io.BytesIO(build_bundle(snapshot, artifacts, cfg['artifact_root']))) as archive:
        result = json.loads(archive.read('result.json'))
        assert result['attempt_id'] == aid and result['state'] == 'paused'
        assert result['timing']['finished_at'] == before
        assert 'files/late' not in archive.namelist()


def test_bundle_same_owner_project_revocation_hides_session(setup):
    import json
    store, principal, client, cfg = setup
    task = create(client).json()
    store.cancel(principal, task['attempt_id'], 'stop')
    with store._tx() as db:
        db.execute('UPDATE clients SET projects=? WHERE id=?', (json.dumps(['another-project']), principal['id']))
    assert client.get(f"/v1/sessions/{task['session_id']}/bundle", headers=headers()).status_code == 404
