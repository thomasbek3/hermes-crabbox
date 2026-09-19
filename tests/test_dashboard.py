import hashlib
from pathlib import Path
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from cloudworkbench.dashboard import COOKIE_NAME, mount_dashboard, principal_from_cookie
from cloudworkbench.store import Store, StoreError


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    owner = store.add_client('owner', 'a' * 40, ['observe', 'submit', 'retrieve', 'cancel'], ['one'])
    settings = {'dashboard_origin': 'https://workbench.example', 'projects': {'one': {'allowed_agents': ['claude'], 'models': {'claude': ['configured']}}}, 'test_mode': True}
    app = FastAPI()
    @app.exception_handler(StoreError)
    async def error(request, exc):
        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
    @app.api_route('/protected', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
    async def protected(request: Request):
        return principal_from_cookie(request)
    manager = mount_dashboard(app, store, settings)
    client = TestClient(app, base_url=settings['dashboard_origin'])
    return app, store, owner, settings, manager, client


def login(client, origin='https://workbench.example', token='a' * 40):
    return client.post('/auth/session', headers={'Origin': origin, 'Authorization': 'Bearer ' + token})


def test_exchange_secure_cookie_and_no_reusable_bearer_storage(setup):
    app, store, owner, settings, manager, client = setup
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Secure' in cookie
    assert 'Domain=' not in cookie
    assert response.headers['cache-control'] == 'no-store'
    token = client.cookies.get(COOKIE_NAME)
    assert token not in repr(manager.sessions)
    assert 'a' * 40 not in repr(manager.sessions)
    assert hashlib.sha256(token.encode()).hexdigest() in manager.sessions
    assert client.get('/protected').json()['id'] == owner['id']
    assert client.get('/auth/session').json()['csrf'] == response.json()['csrf']


@pytest.mark.parametrize('method', ['post', 'put', 'patch', 'delete'])
def test_cookie_mutations_require_both_origin_and_csrf(setup, method):
    *_, client = setup
    csrf = login(client).json()['csrf']
    request = getattr(client, method)
    assert request('/protected').status_code == 403
    assert request('/protected', headers={'Origin': 'https://workbench.example'}).status_code == 403
    assert request('/protected', headers={'Origin': 'https://evil.example', 'X-CSRF-Token': csrf}).status_code == 403
    assert request('/protected', headers={'Origin': 'https://workbench.example', 'X-CSRF-Token': '0' * 64}).status_code == 403
    assert request('/protected', headers={'Origin': 'https://workbench.example', 'X-CSRF-Token': csrf}).status_code == 200


def test_login_csrf_and_host_rebinding_denied(setup):
    *_, client = setup
    assert client.post('/auth/session', headers={'Authorization': 'Bearer ' + 'a' * 40}).status_code == 403
    assert login(client, origin='https://evil.example').status_code == 403
    assert client.post('/auth/session', headers={'Origin': 'https://workbench.example', 'Authorization': 'Bearer ' + 'a' * 40, 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.get('/', headers={'Host': 'evil.example'}).status_code == 403
    login(client)
    assert client.get('/protected', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/protected', headers={'Host': 'evil.example'}).status_code in {401, 403}


def test_logout_revokes_server_session(setup):
    *_, manager, client = setup
    csrf = login(client).json()['csrf']
    token = client.cookies.get(COOKIE_NAME)
    assert client.delete('/auth/session', headers={'Origin': 'https://workbench.example', 'X-CSRF-Token': csrf}).status_code == 200
    assert manager.sessions == {}
    assert client.get('/protected', headers={'Cookie': COOKIE_NAME + '=' + token}).status_code == 401


def test_expiry_and_underlying_principal_revocation(setup):
    app, store, owner, settings, manager, client = setup
    login(client)
    for record in manager.sessions.values():
        record['expires'] = time.monotonic() - 1
    assert client.get('/protected').status_code == 401
    assert manager.sessions == {}
    login(client)
    with store._connect() as db:
        db.execute("UPDATE clients SET revoked_at='now' WHERE id=?", (owner['id'],))
        db.commit()
    assert client.get('/protected').status_code == 401
    assert manager.sessions == {}


def test_scopes_and_projects_refreshed_not_cached(setup):
    app, store, owner, settings, manager, client = setup
    login(client)
    with store._connect() as db:
        db.execute('UPDATE clients SET scopes=?,projects=? WHERE id=?', ('["observe"]', '[]', owner['id']))
        db.commit()
    response = client.get('/auth/config')
    assert response.json()['scopes'] == ['observe']
    assert response.json()['projects'] == []


def test_session_limit_and_login_attempt_limit(setup):
    app, store, owner, settings, manager, client = setup
    manager.maximum = 1
    assert login(client).status_code == 200
    other = TestClient(app, base_url=settings['dashboard_origin'])
    assert login(other).status_code == 503
    for _ in range(20):
        assert login(other, token='wrong').status_code == 401
    assert login(other, token='wrong').status_code == 429
    assert len(manager.sessions) == 1
    assert len(manager.login_attempts) <= 20


@pytest.mark.parametrize('origin,allow', [('http://workbench.example', True), ('http://127.0.0.1:7788', False), ('https://*.example', False), ('https://workbench.example/path', False), ('https://name:password@workbench.example', False)])
def test_disallowed_origins(origin, allow, tmp_path):
    with pytest.raises(ValueError):
        mount_dashboard(FastAPI(), Store(tmp_path / 'db'), {'dashboard_origin': origin, 'dashboard_allow_http_loopback': allow})


def test_explicit_loopback_mode(tmp_path):
    store = Store(tmp_path / 'db')
    app = FastAPI()
    manager = mount_dashboard(app, store, {'dashboard_origin': 'http://127.0.0.1:7788', 'dashboard_allow_http_loopback': True})
    assert manager.secure is False
    assert TestClient(app, base_url='http://127.0.0.1:7788').get('/').status_code == 200


def test_dashboard_csp_and_inert_render_patterns(setup):
    *_, client = setup
    response = client.get('/')
    assert response.status_code == 200
    policy = response.headers['content-security-policy']
    assert "frame-ancestors 'none'" in policy
    assert 'unsafe-inline' not in policy and 'unsafe-eval' not in policy
    nonce = policy.split("script-src 'nonce-", 1)[1].split("'", 1)[0]
    assert f'<script nonce="{nonce}">' in response.text
    assert f'<style nonce="{nonce}">' in response.text
    for prohibited in ('.innerHTML', 'insertAdjacentHTML', 'document.write(', 'localStorage', 'sessionStorage', 'eval(', 'src="http', 'onclick='):
        assert prohibited not in response.text
    assert '.textContent=' in response.text
    assert "link.setAttribute('download','')" in response.text
    assert '__CSP_NONCE__' not in response.text


def test_project_content_not_injected_into_html(setup):
    app, store, owner, settings, manager, client = setup
    payload = '<img src=x onerror=alert(1)>'
    settings['projects']['one']['display_name'] = payload
    assert payload not in client.get('/').text
    login(client)
    assert client.get('/auth/config').json()['projects'][0]['name'] == payload


@pytest.fixture
def integrated(tmp_path):
    from cloudworkbench.api import create_app
    store = Store(tmp_path / 'integrated.sqlite')
    store.add_client('owner', 'a' * 40, ['observe', 'submit', 'retrieve', 'cancel'], ['one'])
    settings = {'dashboard_origin': 'https://workbench.example', 'projects': {'one': {'allowed_agents': ['claude'], 'models': {'claude': ['approved']}, 'environment_versions': ['env1']}}, 'artifact_root': tmp_path / 'artifacts', 'sse_poll_seconds': 0.001}
    settings['artifact_root'].mkdir()
    app = create_app(store, settings)
    client = TestClient(app, base_url=settings['dashboard_origin'])
    return app, store, settings, client


def test_real_api_cookie_create_read_events_and_origin_protection(integrated):
    app, store, settings, client = integrated
    response = login(client)
    assert response.status_code == 200
    csrf = response.json()['csrf']
    payload = {'project_id': 'one', 'goal': '<script>untrusted goal</script>', 'agent': 'claude', 'environment_version': 'env1'}
    headers = {'Origin': settings['dashboard_origin'], 'X-CSRF-Token': csrf, 'Idempotency-Key': 'browser-task'}
    assert client.post('/v1/sessions', json=payload, headers={'Idempotency-Key': 'missing-guard'}).status_code == 403
    assert client.post('/v1/sessions', json=payload, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    created = client.post('/v1/sessions', json=payload, headers=headers)
    assert created.status_code == 201
    session_id = created.json()['session_id']
    assert client.get('/v1/sessions').json()['sessions'][0]['id'] == session_id
    detail = client.get('/v1/sessions/' + session_id)
    assert detail.json()['goal'] == payload['goal']
    assert "sandbox" in detail.headers['content-security-policy']
    assert client.get(f'/v1/sessions/{session_id}/events?follow=false').status_code == 200
    assert client.get(f'/v1/sessions/{session_id}/artifacts').json() == {'artifacts': []}
    message = client.post(f'/v1/sessions/{session_id}/messages', json={'message': 'follow up'}, headers={**headers, 'Idempotency-Key': 'browser-message'})
    assert message.status_code == 200
    assert message.json()['delivery'] == 'queued'
    assert 'dashboard_cookie_auth' not in client.get('/v1/capabilities').json()['unsupported']


def test_real_api_invalid_bearer_does_not_fallback_to_cookie(integrated):
    app, store, settings, client = integrated
    assert login(client).status_code == 200
    assert client.get('/v1/sessions').status_code == 200
    for value in ('', 'Basic abc', 'Bearer wrong', 'Bearer ' + 'x' * 2000):
        assert client.get('/v1/sessions', headers={'Authorization': value}).status_code == 401
    # Header-authenticated CLI calls remain independent of browser CSRF.
    payload = {'project_id': 'one', 'goal': 'bearer task', 'agent': 'claude', 'environment_version': 'env1'}
    assert client.post('/v1/sessions', json=payload, headers={'Authorization': 'Bearer ' + 'a' * 40, 'Idempotency-Key': 'cli'}).status_code == 201


def test_real_api_html_csp_survives_middleware_and_cookie_expires(integrated):
    app, store, settings, client = integrated
    page = client.get('/')
    assert page.status_code == 200
    assert "script-src 'nonce-" in page.headers['content-security-policy']
    assert "sandbox" not in page.headers['content-security-policy']
    assert page.headers['cache-control'] == 'no-store'
    assert page.headers['x-content-type-options'] == 'nosniff'
    assert 'Provider disabled' in page.text
    login(client)
    assert 'Secure' in login(client).headers['set-cookie']
    for record in app.state.browser_sessions.sessions.values():
        record['expires'] = 0
    assert client.get('/v1/sessions').status_code == 401


def test_dashboard_is_opt_in_and_bearer_only_api_unchanged(tmp_path):
    from cloudworkbench.api import create_app
    store = Store(tmp_path / 'no-dashboard.sqlite')
    store.add_client('owner', 'a' * 40, ['observe'], ['one'])
    app = create_app(store)
    client = TestClient(app)
    assert client.get('/').status_code == 404
    assert client.get('/auth/session').status_code == 404
    assert not hasattr(app.state, 'browser_sessions')
    assert client.get('/v1/sessions').status_code == 401
    headers = {'Authorization': 'Bearer ' + 'a' * 40}
    assert client.get('/v1/sessions', headers=headers).status_code == 200
    assert 'dashboard_cookie_auth' in client.get('/v1/capabilities', headers=headers).json()['unsupported']


def test_integrated_cookie_artifact_download(integrated):
    app, store, settings, client = integrated
    csrf = login(client).json()['csrf']
    response = client.post('/v1/sessions', json={'project_id': 'one', 'goal': 'download', 'agent': 'claude', 'environment_version': 'env1'}, headers={'Origin': settings['dashboard_origin'], 'X-CSRF-Token': csrf, 'Idempotency-Key': 'artifact-task'})
    created = response.json()
    content = b'\x00binary\xff'
    path = settings['artifact_root'] / 'binary'
    path.write_bytes(content)
    artifact = store.register_artifact(created['attempt_id'], {'path': 'binary', 'storage_path': str(path), 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content), 'mime': 'text/html'}, expected_generation=created['generation'])
    response = client.get('/v1/artifacts/' + artifact['id'] + '/content')
    assert response.status_code == 200 and response.content == content
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.headers['content-type'] == 'application/octet-stream'


def test_invalid_signin_flood_cannot_lock_out_valid_owner(integrated):
    app, store, settings, owner = integrated
    attacker = TestClient(app, base_url=settings['dashboard_origin'])
    for _ in range(20):
        assert login(attacker, token='invalid').status_code == 401
    for _ in range(10):
        assert login(attacker, token='invalid').status_code == 429
    assert len(app.state.browser_sessions.login_attempts) == 20
    assert login(owner).status_code == 200
    assert owner.get('/v1/sessions').status_code == 200
    assert len(app.state.browser_sessions.login_attempts) == 20
    assert len(app.state.browser_sessions.sessions) == 1
    # Valid sessions do not reset the bounded failed-attempt window.
    assert login(attacker, token='invalid').status_code == 429


def test_hsts_on_secure_entry_and_auth_responses_only(setup, tmp_path):
    app, store, principal, settings, manager, client = setup
    entry = client.get('/')
    assert entry.headers['strict-transport-security'] == 'max-age=31536000'
    response = login(client)
    csrf = response.json()['csrf']
    assert response.headers['strict-transport-security'] == 'max-age=31536000'
    assert client.get('/auth/session').headers['strict-transport-security'] == 'max-age=31536000'
    assert client.get('/auth/config').headers['strict-transport-security'] == 'max-age=31536000'
    assert client.delete('/auth/session', headers={'Origin': settings['dashboard_origin'], 'X-CSRF-Token': csrf}).headers['strict-transport-security'] == 'max-age=31536000'
    development = FastAPI()
    mount_dashboard(development, Store(tmp_path / 'http.sqlite'), {'dashboard_origin': 'http://127.0.0.1:7788', 'dashboard_allow_http_loopback': True})
    assert 'strict-transport-security' not in TestClient(development, base_url='http://127.0.0.1:7788').get('/').headers


def test_proxy_requires_preserved_host_and_trusted_https_scheme(integrated):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    app, store, settings, _ = integrated
    proxy = ProxyHeadersMiddleware(app, trusted_hosts=['127.0.0.1'])
    client = TestClient(proxy, base_url='http://127.0.0.1:7780', client=('127.0.0.1', 40000))
    correct = {'Host': 'workbench.example', 'X-Forwarded-Proto': 'https'}
    response = client.get('/', headers=correct)
    assert response.status_code == 200
    assert response.headers['strict-transport-security'] == 'max-age=31536000'
    assert client.get('/', headers={'Host': '127.0.0.1:7780', 'X-Forwarded-Proto': 'https'}).status_code == 403
    assert client.get('/', headers={'Host': 'workbench.example'}).status_code == 403
    untrusted = TestClient(proxy, base_url='http://127.0.0.1:7780', client=('192.0.2.10', 40000))
    assert untrusted.get('/', headers=correct).status_code == 403


@pytest.mark.parametrize('active,expected',[('new','new'),('unadmitted',None),(None,None),('unavailable',None)])
def test_dashboard_defaults_only_to_active_admitted_environment(setup,monkeypatch,active,expected):
    import cloudworkbench.dashboard as module
    app,store,owner,settings,manager,client=setup
    settings['projects']['one']['environment_versions']=['old','new']
    settings['environment_registry']='fixture-registry'
    class Registry:
        def __init__(self,path,read_only):assert path=='fixture-registry' and read_only
        def resolve(self,project,version,**kwargs):
            return {'manifest':{'cli_versions':{'claude':'test'}}}
        def active(self,project):
            assert project=='one'
            if active=='unavailable':raise OSError('unavailable')
            return {'manifest':{'version':active}} if active else None
    monkeypatch.setattr(module,'EnvironmentRegistry',Registry)
    login(client)
    project=client.get('/auth/config').json()['projects'][0]
    assert project['default_environment_version']==expected
    assert project['environment_versions']==['old','new']


def test_configuration_filters_disabled_fixture_and_bounds_browser_uploads(setup):
    _, store, _, settings, _, client = setup
    settings['projects']['one']['allowed_agents'] = ['fixture', 'claude']
    settings['test_mode'] = False
    settings['input_max_bytes'] = 32 * 1024 * 1024
    login(client)
    config = client.get('/auth/config').json()
    assert config['projects'][0]['agents'] == ['claude']
    assert config['input_max_bytes'] == 16 * 1024 * 1024
    store.policy['input_max_bytes'] = 4096
    assert client.get('/auth/config').json()['input_max_bytes'] == 4096
    settings['input_max_bytes'] = 512
    settings['test_mode'] = True
    config = client.get('/auth/config').json()
    assert config['input_max_bytes'] == 512
    assert config['projects'][0]['agents'] == ['fixture', 'claude']


def test_cookie_binary_input_upload_requires_csrf_and_keeps_owner_binding(tmp_path):
    from cloudworkbench.api import create_app
    store = Store(tmp_path / 'state.db')
    owner = store.add_client('uploader', 'u' * 40, ['submit', 'observe'], ['one'])
    store.add_client('other', 'v' * 40, ['submit', 'observe'], ['one'])
    app = create_app(store, {'dashboard_origin':'https://workbench.example', 'input_root':str(tmp_path/'inputs'), 'test_mode':True, 'projects':{'one':{'allowed_agents':['fixture'],'environment_versions':['v1']}}})
    client = TestClient(app, base_url='https://workbench.example')
    csrf = login(client, token='u'*40).json()['csrf']
    headers = {'Origin':'https://workbench.example','X-CSRF-Token':csrf,'Idempotency-Key':'reserve'}
    item = client.post('/v1/inputs',json={'name':'binary.bin'},headers=headers).json()
    payload = b'\x00\xff\r\n\x80'
    path = '/v1/inputs/'+item['id']+'/content'
    assert client.put(path,content=payload).status_code == 403
    headers['Idempotency-Key'] = 'upload'
    uploaded = client.put(path,content=payload,headers=headers)
    assert uploaded.status_code == 200
    assert uploaded.json()['sha256'] == hashlib.sha256(payload).hexdigest()
    assert uploaded.json()['bytes'] == len(payload)
    assert 'storage_path' not in uploaded.json()
    assert client.put(path,content=payload,headers=headers).json() == uploaded.json()
    other = TestClient(app, base_url='https://workbench.example')
    other_csrf = login(other,token='v'*40).json()['csrf']
    assert other.put(path,content=payload,headers={**headers,'X-CSRF-Token':other_csrf}).status_code == 404
    headers['Idempotency-Key']='create'
    body={'project_id':'one','agent':'fixture','environment_version':'v1','goal':'Inspect the supplied bytes','input_ids':[item['id']]}
    created = client.post('/v1/sessions',json=body,headers=headers)
    assert created.status_code == 201
    assert client.post('/v1/sessions',json=body,headers=headers).json() == created.json()
    assert len(store.list_sessions(owner)) == 1


def test_agent_environments_follow_qualified_cli_identity_not_version_names(setup,monkeypatch):
    import cloudworkbench.dashboard as module
    _,_,_,settings,_,client=setup
    settings['projects']['one'].update(allowed_agents=['claude','hermes','codex'],environment_versions=['old','new','arbitrary-name','unqualified'])
    settings['environment_registry']='fixture-registry'
    class Registry:
        def __init__(self,*args,**kwargs):pass
        def active(self,project):return {'manifest':{'version':'new'}}
        def resolve(self,project,version,**kwargs):
            if version=='unqualified':raise ValueError('Not qualified')
            return {'manifest':{'cli_versions':{'hermes' if version=='arbitrary-name' else 'claude':'1'}}}
    monkeypatch.setattr(module,'EnvironmentRegistry',Registry);login(client)
    p=client.get('/auth/config').json()['projects'][0]
    assert p['default_environment_version']=='new'
    assert p['environments_by_agent']=={'claude':{'versions':['old','new'],'default':'new'},
        'hermes':{'versions':['arbitrary-name'],'default':'arbitrary-name'},'codex':{'versions':[],'default':None}}


def test_agent_environments_fail_closed_on_registry_unavailable(setup,monkeypatch):
    import cloudworkbench.dashboard as module
    _,_,_,settings,_,client=setup
    settings['projects']['one']['environment_versions']=['old']
    settings['environment_registry']='missing'
    def missing(*args,**kwargs):raise OSError('missing')
    monkeypatch.setattr(module,'EnvironmentRegistry',missing);login(client)
    p=client.get('/auth/config').json()['projects'][0]
    assert p['environments_by_agent']=={'claude':{'versions':[],'default':None}}


def test_multiple_compatible_environments_without_matching_active_require_choice(setup,monkeypatch):
    import cloudworkbench.dashboard as module
    _,_,_,settings,_,client=setup
    settings['projects']['one']['environment_versions']=['a','b']
    settings['environment_registry']='fixture'
    class Registry:
        def __init__(self,*args,**kwargs):pass
        def active(self,project):return None
        def resolve(self,*args,**kwargs):return {'manifest':{'cli_versions':{'claude':'1'}}}
    monkeypatch.setattr(module,'EnvironmentRegistry',Registry);login(client)
    assert client.get('/auth/config').json()['projects'][0]['environments_by_agent']['claude']=={'versions':['a','b'],'default':None}
