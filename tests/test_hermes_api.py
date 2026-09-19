"""Normal authenticated Hermes session API; execution is synthetic in these tests."""
import hashlib
import pytest
from fastapi.testclient import TestClient
from cloudworkbench.api import create_app
from cloudworkbench.store import Store


def headers(key='create', token='a'*40):
    return {'Authorization':'Bearer '+token,'Idempotency-Key':key}


@pytest.fixture
def api(tmp_path):
    store=Store(tmp_path/'state.db')
    owner=store.add_client('TEST Hermes owner','a'*40,['submit','observe','retrieve','cancel'],['project'])
    store.add_client('TEST foreign owner','b'*40,['submit','observe','retrieve'],['project'])
    store.add_client('TEST observer','c'*40,['observe'],['project'])
    artifacts=tmp_path/'artifacts';artifacts.mkdir()
    settings={'hermes_enabled':True,'artifact_root':str(artifacts),
        'projects':{'project':{'allowed_agents':['hermes','claude'],
            'models':{'hermes':['grok-4.6'],'claude':['legacy-approved']},
            'environment_versions':['hermes-v1','legacy-v1']}}}
    return store,owner,TestClient(create_app(store,settings)),settings


def create(client, *, key='create', token='a'*40, **changes):
    return client.post('/v1/sessions',headers=headers(key,token),json={
        'project_id':'project','goal':'Create a status summary','agent':'hermes',
        'model':'grok-4.6','environment_version':'hermes-v1',**changes})


def test_authenticated_hermes_admission_is_idempotent_and_records_identity(api):
    store,owner,client,_=api
    assert client.post('/v1/sessions',json={}).status_code==401
    assert create(client,token='c'*40).status_code==403
    first=create(client)
    assert first.status_code==201
    assert create(client).json()==first.json()
    assert create(client,goal='Different work').status_code==409
    attempt=store.get_attempt(first.json()['attempt_id'])
    assert attempt['agent']=='hermes' and attempt['state']=='queued'
    assert attempt['request']['model']=='grok-4.6'
    assert attempt['request']['environment_version']=='hermes-v1'
    assert store.claim_next()['id']==attempt['id']


@pytest.mark.parametrize('enabled',[False,None,1,'true'])
def test_disabled_hermes_refuses_before_durable_admission_without_disabling_claude(api,enabled):
    store,_,client,settings=api
    if enabled is None:settings.pop('hermes_enabled')
    else:settings['hermes_enabled']=enabled
    response=create(client)
    assert response.status_code==503
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==0
    legacy=create(client,key='legacy',agent='claude',model='legacy-approved',environment_version='legacy-v1')
    assert legacy.status_code==201
    assert store.get_attempt(legacy.json()['attempt_id'])['agent']=='claude'


@pytest.mark.parametrize('changes',[
    {'model':'unapproved'}, {'environment_version':'missing'},
    {'project_id':'unregistered'}, {'command':'arbitrary command'},
])
def test_hermes_config_cannot_be_overridden_by_request(api,changes):
    store,_,client,_=api
    assert create(client,**changes).status_code==422
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0


def test_hermes_same_session_followup_preserves_request_and_owner_scope(api):
    store,_,client,_=api
    first=create(client).json();aid=first['attempt_id'];sid=first['session_id']
    store.claim_next()
    store.transition(aid,'failed',expected_generation=first['generation'],reason='synthetic_setup')
    route=f'/v1/sessions/{sid}/messages'
    assert client.post(route,headers=headers('foreign','b'*40),json={'message':'Add strict mode'}).status_code==404
    next_response=client.post(route,headers=headers('followup'),json={'message':'Add strict mode'})
    assert next_response.status_code==200
    follow=next_response.json()
    assert client.post(route,headers=headers('followup'),json={'message':'Add strict mode'}).json()==follow
    child=store.get_attempt(follow['attempt_id'])
    assert child['session_id']==sid and child['agent']=='hermes'
    assert child['request']['goal']=='Add strict mode'
    assert child['request']['model']=='grok-4.6'
    assert child['request']['environment_version']=='hermes-v1'
    assert follow['delivery']=='queued'
    assert child['generation']>first['generation']


def test_hermes_result_and_file_contract_does_not_expose_private_paths(api):
    store,_,client,settings=api
    first=create(client).json();aid=first['attempt_id'];sid=first['session_id'];gen=first['generation']
    store.claim_next();store.transition(aid,'running',expected_generation=gen)
    body=b'def summary():\n    return "ready"\n'
    from pathlib import Path
    path=Path(settings['artifact_root'])/'status_summary.py';path.write_bytes(body)
    artifact=store.register_artifact(aid,{'path':'status_summary.py','storage_path':str(path),
        'sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body),'mime':'text/x-python'},expected_generation=gen)
    # Controller fixture writes an explicitly unverified result, not a provider claim.
    store.transition(aid,'verifying',expected_generation=gen)
    store.transition(aid,'completed',expected_generation=gen,outcome='unverified',result={
        'summary':'Synthetic fixture output','native_session_id':'native-test-session',
        'model_reported':'grok-4.6','configured_effort':'xhigh','worker_reported':True})
    detail=client.get(f'/v1/sessions/{sid}',headers=headers())
    assert detail.status_code==200 and 'storage_path' not in detail.text
    saved=next(x for x in detail.json()['attempts'] if x['id']==aid)
    assert saved['outcome']=='unverified' and saved['result']['worker_reported'] is True
    listed=client.get(f'/v1/sessions/{sid}/artifacts',headers=headers())
    assert listed.status_code==200 and 'storage_path' not in listed.text
    route=f"/v1/artifacts/{artifact['id']}/content"
    assert client.get(route,headers=headers()).content==body
    assert client.get(route,headers=headers(token='b'*40)).status_code==404
