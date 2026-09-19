import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from cloudworkbench import provider_bootstrap as bootstrap,provider_main
from cloudworkbench.native_responses import NativeProfile,NativeResult
from cloudworkbench.inference_transport import TransportError

PROFILE=NativeProfile('openai-codex','gpt-6-astra','high')


def credential(tmp_path,*,expiry=None,change=None):
    payload={'exp':time.time()+3600 if expiry is None else expiry,'https://api.openai.com/auth':{'chatgpt_account_id':'account_1'}}
    encoded=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    value={'auth_mode':'chatgpt','OPENAI_API_KEY':None,'tokens':{'access_token':'header.'+encoded+'.signature',
        'refresh_token':'synthetic-refresh-only','id_token':'synthetic-id-only','account_id':'account_1'},'last_refresh':'synthetic'}
    if change:change(value)
    path=tmp_path/'native-auth.json';path.write_text(json.dumps(value));path.chmod(0o600)
    return path,value


class Transport:
    seen=[];result=NativeResult('ok',response={'id':'resp_1','model':PROFILE.model,'status':'completed','output':[]})
    def __init__(self,profile,**kwargs):self.profile=profile;self.kwargs=kwargs
    def execute(self,request,**kwargs):self.__class__.seen.append((self.profile,request,kwargs));return self.__class__.result


@pytest.fixture(autouse=True)
def reset():
    Transport.seen=[];Transport.result=NativeResult('ok',response={'id':'resp_1','model':PROFILE.model,'status':'completed','output':[]})


def test_dedicated_file_only_injected_inside_provider_no_write_or_sse(tmp_path,monkeypatch):
    path,value=credential(tmp_path);before=path.read_bytes()
    monkeypatch.setenv('OPENAI_API_KEY','ambient-ignored');monkeypatch.setenv('CODEX_HOME','/personal/do-not-read')
    result=bootstrap.run_native_request(PROFILE,{'synthetic':True},credential_path=path,transport_factory=Transport)
    assert result['status']=='ok' and set(result['result'])=={'profile_digest','response','transport_stopped'}
    assert result['result']['profile_digest']==PROFILE.digest and result['container_cleanup_required']
    assert not result['credential_reuse_authorized'] and path.read_bytes()==before
    assert Transport.seen[0][2]['credential']==value['tokens']['access_token']
    assert Transport.seen[0][2]['account_id']=='account_1'
    for key in ('access_token','refresh_token','id_token'):assert value['tokens'][key] not in json.dumps(result)


@pytest.mark.parametrize('case',['missing','symlink','hardlink','world','groupwrite','large','duplicate_json','bad_json'])
def test_invalid_file_prevents_network(tmp_path,case):
    path,_=credential(tmp_path)
    if case=='missing':path=tmp_path/'missing'
    elif case=='symlink':target=tmp_path/'link';target.symlink_to(path);path=target
    elif case=='hardlink':os.link(path,tmp_path/'second')
    elif case=='world':path.chmod(0o644)
    elif case=='groupwrite':path.chmod(0o660)
    elif case=='large':path.write_text('x'*65537)
    elif case=='duplicate_json':path.write_text('{"tokens":{},"tokens":{}}')
    else:path.write_text('not-json-secret-sentinel')
    result=bootstrap.run_native_request(PROFILE,{},credential_path=path,transport_factory=Transport)
    assert result['status']=='error' and result['result'] is None and not Transport.seen
    assert result['error']==('auth_missing' if case=='missing' else 'auth_invalid')
    assert 'secret-sentinel' not in json.dumps(result)


@pytest.mark.parametrize('change,code',[
    (lambda v:v.update(auth_mode='api_key'),'auth_schema_unsupported'),
    (lambda v:v.update(OPENAI_API_KEY='api-key-no-fallback'),'auth_schema_unsupported'),
    (lambda v:v.update(new_schema=True),'auth_schema_unsupported'),
    (lambda v:v['tokens'].update(account_id='different'),'auth_invalid'),
    (lambda v:v['tokens'].update(access_token='opaque'),'auth_invalid'),
    (lambda v:v['tokens'].update(refresh_token=None),'auth_invalid'),
])
def test_unsupported_or_invalid_auth_never_falls_back(tmp_path,change,code):
    path,_=credential(tmp_path,change=change)
    result=bootstrap.run_native_request(PROFILE,{},credential_path=path,transport_factory=Transport)
    assert result['error']==code and not Transport.seen


def test_expired_and_missing_grok_credentials_are_fail_closed(tmp_path):
    path,_=credential(tmp_path,expiry=time.time()-1)
    assert bootstrap.run_native_request(PROFILE,{},credential_path=path,transport_factory=Transport)['error']=='auth_expired'
    grok=NativeProfile('xai-oauth','grok-4.6','xhigh')
    assert bootstrap.run_native_request(grok,{},credential_path=tmp_path/'not-read',transport_factory=Transport)['error']=='auth_missing'
    assert not Transport.seen


@pytest.mark.parametrize('secret',['access_token','refresh_token','id_token'])
def test_exact_private_material_never_exported(tmp_path,secret):
    path,value=credential(tmp_path);Transport.result=replace(Transport.result,response={'text':'prefix'+value['tokens'][secret]})
    result=bootstrap.run_native_request(PROFILE,{},credential_path=path,transport_factory=Transport)
    assert result['error']=='credential_exposure_detected' and result['result'] is None


def test_unstopped_transport_cannot_emit_success(tmp_path):
    path,_=credential(tmp_path);Transport.result=replace(Transport.result,transport_stopped=False)
    assert bootstrap.run_native_request(PROFILE,{},credential_path=path,transport_factory=Transport)['error']=='transport_cleanup_unconfirmed'


def test_native_profile_file_exact_trusted_discriminator(tmp_path,monkeypatch):
    original=os.fstat
    def root(fd):
        v=original(fd);return SimpleNamespace(st_mode=v.st_mode,st_uid=0,st_nlink=v.st_nlink,st_size=v.st_size)
    monkeypatch.setattr(provider_main.os,'fstat',root)
    path=tmp_path/'profile.json';value={'transport':'native-responses-v1','provider':PROFILE.provider,'model':PROFILE.model,'effort':PROFILE.effort}
    path.write_text(json.dumps(value));path.chmod(0o644)
    assert provider_main.read_profile(path)==PROFILE
    for patch in ({'effort':'max'},{'endpoint':'http://localhost'},{'transport':'other'}):
        path.write_text(json.dumps({**value,**patch}))
        with pytest.raises(TransportError,match='invalid_profile_file'):provider_main.read_profile(path)


def test_main_selects_native_only_from_profile(monkeypatch,capsys):
    monkeypatch.setattr(provider_main,'read_profile',lambda _:PROFILE)
    monkeypatch.setattr(provider_main,'read_request',lambda _: {'synthetic':True})
    monkeypatch.setattr(provider_main.sys,'stdin',SimpleNamespace(fileno=lambda:0))
    monkeypatch.setattr(provider_main,'run_token_request',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('CLI must not run')))
    seen=[]
    def native(profile,request,**kwargs):
        seen.append((profile,request,kwargs));return {'version':1,'status':'error','error':'auth_missing','result':None,'container_cleanup_required':True,'credential_reuse_authorized':False}
    monkeypatch.setattr(provider_main,'run_native_request',native)
    assert provider_main.main(['--proxy','http://10.0.0.2:8080'])==1
    assert json.loads(capsys.readouterr().out)['error']=='auth_missing' and seen[0][0]==PROFILE


def grok_credential(tmp_path):
    from datetime import datetime,timezone,timedelta
    issuer='https://auth.x.ai';client='b1a00492-073a-47ea-816f-4c329264a828'
    record={'key':'synthetic-oidc-access','auth_mode':'oidc','refresh_token':'synthetic-oidc-refresh',
        'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
        'oidc_issuer':issuer,'oidc_client_id':client,'email':'synthetic@example.invalid'}
    value={issuer+'::'+client:record};path=tmp_path/'grok-auth.json';path.write_text(json.dumps(value));path.chmod(0o600)
    return path,value,record


def test_observed_grok_oidc_namespace_injects_access_only_no_api_fallback(tmp_path):
    path,value,record=grok_credential(tmp_path);before=path.read_bytes()
    profile=NativeProfile('xai-oauth','grok-4.6','xhigh')
    result=bootstrap.run_native_request(profile,{},credential_path=path,transport_factory=Transport)
    assert result['status']=='ok' and Transport.seen[0][2]['credential']==record['key']
    assert Transport.seen[0][2]['account_id'] is None and path.read_bytes()==before
    assert record['key'] not in json.dumps(result) and record['refresh_token'] not in json.dumps(result)


@pytest.mark.parametrize('case',['namespace','mode','client','issuer','expired','naive','missing_key','extra'])
def test_grok_namespace_and_oidc_shape_are_exact(tmp_path,case):
    path,value,record=grok_credential(tmp_path)
    if case=='namespace':value={'https://api.x.ai':record}
    elif case=='mode':record['auth_mode']='api_key'
    elif case=='client':record['oidc_client_id']='different-client'
    elif case=='issuer':record['oidc_issuer']='https://attacker.invalid'
    elif case=='expired':record['expires_at']='2020-01-01T00:00:00Z'
    elif case=='naive':record['expires_at']='2099-01-01T00:00:00'
    elif case=='missing_key':record.pop('key')
    else:record['unknown']=True
    path.write_text(json.dumps(value))
    result=bootstrap.run_native_request(NativeProfile('xai-oauth','grok-4.6','xhigh'),{},credential_path=path,transport_factory=Transport)
    assert result['status']=='error' and not Transport.seen
