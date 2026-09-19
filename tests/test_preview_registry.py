from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import os
import sqlite3
import threading
import uuid

import pytest

from cloudworkbench.preview_registry import (
    COOKIE_NAME, PreviewBinding, PreviewError, PreviewPrincipal,
    PreviewRegistry, WorkerBackend, exact_origin,
)

DASH = 'https://control.example.test'
ORIGIN = 'https://s1.preview.example.test'
OTHER = 'https://s2.preview.example.test'


class Fixture:
    def __init__(self, tmp_path):
        self.now = 1000
        self.binding = PreviewBinding(str(uuid.uuid4()), 'project', str(uuid.uuid4()), str(uuid.uuid4()), 1, 'web')
        self.principal = PreviewPrincipal(self.binding.owner_id, 'a' * 64)
        self.backend = WorkerBackend('worker', 'b' * 64, 'c' * 64, '172.20.0.2', 3000)
        self.active = True
        self.path = tmp_path / 'private' / 'preview.sqlite'
        self.registry = self.open()
        self.rid = self.register()

    def authority(self, binding, principal):
        return self.active and binding == self.binding and principal == self.principal

    def open(self):
        return PreviewRegistry(self.path, dashboard_origin=DASH, configured_origins=(ORIGIN, OTHER),
            services={'web': ('http', 3000)}, authority=self.authority, clock=lambda: self.now)

    def register(self, **kwargs):
        return self.registry.register_worker_backend(self.binding, self.backend,
            origin=kwargs.pop('origin', ORIGIN), readiness_proven=True, **kwargs)

    def grant(self, **kwargs):
        return self.registry.issue_grant(self.rid, self.principal,
            dashboard_request_origin=DASH, preview_origin=ORIGIN, **kwargs)

    def exchange(self, token, **kwargs):
        return self.registry.exchange(token, preview_origin=ORIGIN, request_origin=DASH, **kwargs)

    def cookie(self):
        return self.exchange(self.grant().token)

    def lookup(self, cookie, **kwargs):
        return self.registry.lookup_cookie(cookie.token, preview_origin=ORIGIN, **kwargs)


@pytest.fixture
def f(tmp_path):
    return Fixture(tmp_path)


def test_roundtrip_returns_identity_not_transport_credentials(f):
    grant = f.grant()
    cookie = f.exchange(grant.token)
    result = f.lookup(cookie)
    assert result.binding == f.binding and result.backend == f.backend
    assert result.origin == ORIGIN
    assert 'token' not in asdict(result)
    assert grant.token not in repr(grant) and cookie.token not in repr(cookie)
    assert cookie.cookie_options() == dict(key=COOKIE_NAME, value=cookie.token, path='/', secure=True,
        httponly=True, samesite='strict', max_age=900)
    for p in f.path.parent.iterdir():
        assert grant.token.encode() not in p.read_bytes()
        assert cookie.token.encode() not in p.read_bytes()
    with sqlite3.connect(f.path) as db:
        assert db.execute('SELECT length(hash) FROM cookies').fetchone()[0] == 64


@pytest.mark.parametrize('origin', ['http://x.example', 'https://x.example/', 'https://X.example',
    'https://x.example:443', 'https://x.example:8443', 'https://x.example?x', 'https://x.example#x',
    'https://user@x.example', 'https://*.example', 'https://x.example.', 'https://127.0.0.1',
    'https://[::1]', 'https://localhost', 'https://x..example', 'https://é.example', None,
    'https://x.example\n', 'https://x.example/../', 'https://x.example\\@evil.example'])
def test_exact_origin_rejects_ambiguous_or_shared_cookie_hosts(origin):
    with pytest.raises(PreviewError):
        exact_origin(origin)


def test_disabled_until_all_dependencies_configured(tmp_path):
    registry = PreviewRegistry(tmp_path / 'registry', dashboard_origin=DASH)
    with pytest.raises(PreviewError, match='preview_unconfigured'):
        registry.issue_grant(str(uuid.uuid4()), None, dashboard_request_origin=DASH, preview_origin=ORIGIN)


@pytest.mark.parametrize('changes', [dict(address='127.0.0.1'), dict(address='169.254.169.254'),
    dict(address='100.83.74.92'), dict(address='1.1.1.1'), dict(address='host.example'),
    dict(address='0.0.0.0'), dict(address='224.0.0.1'), dict(container_id='short'),
    dict(network_id='short'), dict(port=0), dict(port=True), dict(protocol='file')])
def test_backend_validation(f, changes):
    with pytest.raises(PreviewError):
        replace(f.backend, **changes)


def test_service_policy_readiness_and_origin_required(f):
    for args in [dict(backend=replace(f.backend, port=4000)), dict(origin=OTHER + '.evil'),
                 dict(readiness_proven=False)]:
        kwargs = dict(binding=f.binding, backend=f.backend, origin=ORIGIN, readiness_proven=True)
        kwargs.update(args)
        with pytest.raises(PreviewError):
            f.registry.register_worker_backend(**kwargs)


def test_atomic_single_use_across_independent_connections(f):
    grant = f.grant()
    second = f.open()
    barrier = threading.Barrier(2)
    def redeem(registry):
        barrier.wait()
        try:
            return registry.exchange(grant.token, preview_origin=ORIGIN, request_origin=DASH)
        except PreviewError as e:
            return e.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, (f.registry, second)))
    assert sum(not isinstance(r, str) for r in results) == 1
    assert results.count('invalid_capability') == 1
    with sqlite3.connect(f.path) as db:
        assert db.execute('SELECT count(*) FROM cookies').fetchone()[0] == 1


def test_wrong_audience_does_not_consume_grant(f):
    grant = f.grant()
    with pytest.raises(PreviewError):
        f.registry.exchange(grant.token, preview_origin=OTHER, request_origin=DASH)
    cookie = f.exchange(grant.token)
    with pytest.raises(PreviewError):
        f.registry.lookup_cookie(cookie.token, preview_origin=OTHER)
    assert f.lookup(cookie)


@pytest.mark.parametrize('field,value', [('generation', 2), ('project_id', 'different'),
    ('attempt_id', str(uuid.uuid4())), ('session_id', str(uuid.uuid4())),
    ('owner_id', str(uuid.uuid4()))])
def test_current_authority_binding_change_denies_grant_and_cookie(f, field, value):
    cookie = f.cookie()
    grant = f.grant()
    f.binding = replace(f.binding, **{field: value})
    with pytest.raises(PreviewError, match='preview_denied'):
        f.exchange(grant.token)
    with pytest.raises(PreviewError, match='preview_denied'):
        f.lookup(cookie)
    with pytest.raises(PreviewError, match='preview_denied'):
        f.grant()


def test_client_revocation_and_rotation_checked_every_request(f):
    cookie = f.cookie()
    grant = f.grant()
    f.principal = replace(f.principal, credential_revision='d' * 64)
    with pytest.raises(PreviewError, match='preview_denied'):
        f.lookup(cookie)
    with pytest.raises(PreviewError, match='preview_denied'):
        f.exchange(grant.token)
    assert f.cookie()
    f.active = False
    with pytest.raises(PreviewError):
        f.grant()


def test_callback_exception_and_truthy_nonbool_deny(f):
    cookie = f.cookie()
    f.registry.authority = lambda *_: 1
    with pytest.raises(PreviewError, match='preview_denied'):
        f.lookup(cookie)
    def broken(*args):
        raise RuntimeError('not exposed')
    f.registry.authority = broken
    with pytest.raises(PreviewError, match='^preview_denied$'):
        f.lookup(cookie)


@pytest.mark.parametrize('kind', ['grant', 'cookie', 'registration'])
def test_expiry_cannot_reverse_after_rejected_request_and_restart(f, kind):
    grant = f.grant(ttl_seconds=5)
    cookie = f.exchange(grant.token, cookie_seconds=10) if kind == 'cookie' else None
    f.now += {'grant': 5, 'cookie': 10, 'registration': 900}[kind]
    with pytest.raises(PreviewError):
        f.lookup(cookie) if cookie else f.exchange(grant.token)
    f.now = 1001
    f.registry = f.open()
    with pytest.raises(PreviewError, match='clock_unavailable'):
        f.lookup(cookie) if cookie else f.exchange(grant.token)


@pytest.mark.parametrize('now', [float('nan'), float('inf'), -1, True, '1001'])
def test_invalid_clock_fails_closed(f, now):
    cookie = f.cookie()
    f.now = now
    with pytest.raises(PreviewError, match='clock_unavailable'):
        f.lookup(cookie)


@pytest.mark.parametrize('kwargs', [dict(method='POST'), dict(method='DELETE'), dict(websocket=True),
    dict(method='CONNECT'), dict(request_origin=DASH), dict(request_origin='null'),
    dict(request_origin=ORIGIN + '.evil'), dict(websocket='yes')])
def test_mutating_and_websocket_requests_require_exact_preview_origin(f, kwargs):
    with pytest.raises(PreviewError, match='preview_denied'):
        f.lookup(f.cookie(), **kwargs)


def test_preview_same_origin_mutations_and_websocket_allowed(f):
    cookie = f.cookie()
    assert f.lookup(cookie, method='POST', request_origin=ORIGIN)
    assert f.lookup(cookie, websocket=True, request_origin=ORIGIN)


@pytest.mark.parametrize('method,origin', [('GET', DASH), ('POST', ORIGIN), ('POST', 'null')])
def test_exchange_requires_dashboard_post(f, method, origin):
    with pytest.raises(PreviewError):
        f.registry.exchange(f.grant().token, preview_origin=ORIGIN, request_origin=origin, method=method)


def test_owner_and_dashboard_origin_required_to_issue(f):
    for principal, origin in [(replace(f.principal, owner_id=str(uuid.uuid4())), DASH),
                              (f.principal, ORIGIN)]:
        with pytest.raises(PreviewError):
            f.registry.issue_grant(f.rid, principal, dashboard_request_origin=origin, preview_origin=ORIGIN)


def test_new_registration_revokes_old_capabilities_and_freezes_backend(f):
    old = f.cookie()
    grant = f.grant()
    f.binding = replace(f.binding, generation=2)
    f.backend = replace(f.backend, container_id='e' * 64)
    f.rid = f.register()
    with pytest.raises(PreviewError):
        f.lookup(old)
    with pytest.raises(PreviewError):
        f.exchange(grant.token)
    assert f.lookup(f.cookie()).backend == f.backend


def test_origin_never_reused_for_different_session_even_after_revocation(f):
    f.registry.revoke_registration(f.rid, binding=f.binding)
    f.binding = replace(f.binding, session_id=str(uuid.uuid4()))
    with pytest.raises(PreviewError, match='origin_already_assigned'):
        f.register()
    assert f.register(origin=OTHER)


@pytest.mark.parametrize('kind', ['cookie', 'session', 'registration'])
def test_revocation_survives_restart(f, kind):
    cookie = f.cookie()
    if kind == 'cookie':
        f.registry.revoke_cookie(cookie.token, preview_origin=ORIGIN)
    elif kind == 'session':
        f.registry.revoke_session(owner_id=f.binding.owner_id, project_id=f.binding.project_id,
                                  session_id=f.binding.session_id)
    else:
        f.registry.revoke_registration(f.rid, binding=f.binding)
    f.registry = f.open()
    with pytest.raises(PreviewError):
        f.lookup(cookie)


def test_fenced_revocation_preserves_current_registration(f):
    cookie = f.cookie()
    with pytest.raises(PreviewError):
        f.registry.revoke_registration(f.rid, binding=replace(f.binding, generation=2))
    assert f.lookup(cookie)


def test_reopen_preserves_valid_cookie(f):
    cookie = f.cookie()
    f.registry = f.open()
    assert f.lookup(cookie)


def test_unsafe_db_or_parent_rejected(tmp_path):
    path = tmp_path / 'unsafe'
    path.write_bytes(b'')
    path.chmod(0o644)
    with pytest.raises(PreviewError, match='unsafe_registry_file'):
        PreviewRegistry(path, dashboard_origin=DASH)
    path.chmod(0o600)
    with sqlite3.connect(path) as db:
        db.execute('create table unrelated (x)')
    with pytest.raises(PreviewError, match='foreign_database'):
        PreviewRegistry(path, dashboard_origin=DASH)
    link = tmp_path / 'link'
    link.symlink_to(path)
    with pytest.raises(PreviewError, match='unsafe_registry_path'):
        PreviewRegistry(link, dashboard_origin=DASH)
    tmp_path.chmod(0o777)
    with pytest.raises(PreviewError, match='unsafe_registry_path'):
        PreviewRegistry(path, dashboard_origin=DASH)


def test_schema_version_and_shared_dashboard_origin_rejected(f):
    with sqlite3.connect(f.path) as db:
        db.execute('pragma user_version=2')
    with pytest.raises(PreviewError, match='unsupported_schema'):
        f.open()
    with pytest.raises(PreviewError, match='invalid_preview_origins'):
        PreviewRegistry(f.path, dashboard_origin=DASH, configured_origins=[DASH])


@pytest.mark.parametrize('value', [0, -1, True, 61, 1.5])
def test_grant_ttl_bounded(f, value):
    with pytest.raises(PreviewError):
        f.grant(ttl_seconds=value)


def test_transaction_failure_does_not_consume_grant(f, monkeypatch):
    grant = f.grant()
    monkeypatch.setattr('cloudworkbench.preview_registry.secrets.token_urlsafe', lambda _: 'bad')
    with pytest.raises(PreviewError):
        f.exchange(grant.token)
    monkeypatch.undo()
    assert f.exchange(grant.token)


@pytest.mark.parametrize('mode,allowed', [(0o700, True), (0o2700, True), (0o2750, False), (0o750, False), (0o770, False)])
def test_private_parent_accepts_inherited_setgid_without_group_access(tmp_path, mode, allowed):
    parent = tmp_path / 'private'
    parent.mkdir(mode=mode)
    parent.chmod(mode)
    if allowed:
        PreviewRegistry(parent / 'registry', dashboard_origin=DASH)
    else:
        with pytest.raises(PreviewError, match='unsafe_registry_path'):
            PreviewRegistry(parent / 'registry', dashboard_origin=DASH)



def test_registration_expiry_caps_cookie_and_grant(f):
    f.rid = f.register(ttl_seconds=3)
    grant = f.grant()
    cookie = f.exchange(grant.token)
    assert grant.expires_at == cookie.expires_at == 1003
    assert cookie.max_age == 3
    f.now = 1003
    with pytest.raises(PreviewError):
        f.lookup(cookie)
    with pytest.raises(PreviewError):
        f.grant()


def test_grant_and_cookie_are_not_interchangeable(f):
    grant = f.grant()
    cookie = f.exchange(grant.token)
    with pytest.raises(PreviewError):
        f.registry.lookup_cookie(grant.token, preview_origin=ORIGIN)
    with pytest.raises(PreviewError):
        f.exchange(cookie.token)


def test_registration_limit_and_expired_grant_cleanup(f):
    for _ in range(10):
        f.grant(ttl_seconds=1)
    f.now += 1
    f.grant()
    with sqlite3.connect(f.path) as db:
        assert db.execute('SELECT count(*) FROM grants').fetchone()[0] == 1
        row = db.execute('SELECT origin,binding,backend,created,expires,revoked FROM registrations').fetchone()
        db.executemany('INSERT INTO registrations VALUES(?,?,?,?,?,?,?)', [(str(uuid.uuid4()), 'https://x' + str(i) + '.example.test', *row[1:]) for i in range(1024)])
    with pytest.raises(PreviewError, match='registration_limit'):
        f.register()


def test_slow_authority_does_not_hold_database_lock(f):
    import time
    cookie = f.cookie()
    original = f.registry.authority
    barrier = threading.Barrier(64)
    def authority(binding, principal):
        barrier.wait(timeout=2)
        time.sleep(0.1)
        return original(binding, principal)
    f.registry.authority = authority
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=64) as pool:
        results = list(pool.map(lambda _: f.lookup(cookie), range(64)))
    assert len(results) == 64
    assert time.monotonic() - start < 3


def test_revoke_during_authority_callback_is_rechecked(f):
    cookie = f.cookie()
    def authority(*_):
        f.registry.revoke_registration(f.rid, binding=f.binding)
        return True
    f.registry.authority = authority
    with pytest.raises(PreviewError):
        f.lookup(cookie)


def test_expired_registrations_are_reclaimed_without_reassigning_host(f):
    for _ in range(1025):
        f.rid = f.register(ttl_seconds=1)
        f.now += 1
    f.rid = f.register()
    with sqlite3.connect(f.path) as db:
        assert db.execute('SELECT count(*) FROM registrations').fetchone()[0] == 1
    f.binding = replace(f.binding, session_id=str(uuid.uuid4()))
    with pytest.raises(PreviewError, match='origin_already_assigned'):
        f.register()


def test_project_scope_cannot_change_on_reserved_host(f):
    f.registry.revoke_session(owner_id=f.binding.owner_id, project_id=f.binding.project_id,
                              session_id=f.binding.session_id)
    f.binding = replace(f.binding, project_id='other')
    with pytest.raises(PreviewError, match='origin_already_assigned'):
        f.register()


def test_corrupt_binding_and_throwing_clock_have_fixed_error_codes(f):
    cookie = f.cookie()
    with sqlite3.connect(f.path) as db:
        bad = asdict(f.binding)
        del bad['generation']
        db.execute('UPDATE registrations SET binding=?', (json.dumps(bad),))
    with pytest.raises(PreviewError, match='^registry_corrupt$'):
        f.lookup(cookie)
    def broken():
        raise RuntimeError('untrusted details')
    f.registry.clock = broken
    with pytest.raises(PreviewError, match='^clock_unavailable$'):
        f.lookup(cookie)


@pytest.mark.parametrize('host', ['1.2.3', '0x7f.0.0.1', '0177.0.0.1', 'a.123', '1.0x7f'])
def test_browser_numeric_host_aliases_rejected(host):
    with pytest.raises(PreviewError):
        exact_origin('https://' + host)


@pytest.mark.parametrize('address', ['0.1.2.3', '192.0.2.1', '198.18.0.1'])
def test_only_rfc1918_or_unique_local_backend_addresses(f, address):
    with pytest.raises(PreviewError):
        replace(f.backend, address=address)


def test_one_owner_cannot_exhaust_global_grants(f):
    for _ in range(64):
        f.grant()
    with pytest.raises(PreviewError, match='grant_limit'):
        f.grant()
    other = replace(f.binding, owner_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()))
    principal = PreviewPrincipal(other.owner_id, 'f' * 64)
    rid = f.registry.register_worker_backend(other, f.backend, origin=OTHER, readiness_proven=True)
    f.registry.authority = lambda b,p: b == other and p == principal
    assert f.registry.issue_grant(rid, principal, dashboard_request_origin=DASH, preview_origin=OTHER)


def test_clock_repair_invalidates_caps_but_preserves_host_assignments(f):
    cookie = f.cookie()
    f.now += 10 * 365 * 86400
    with pytest.raises(PreviewError):
        f.lookup(cookie)
    f.now = 1001
    with pytest.raises(PreviewError, match='clock_unavailable'):
        f.lookup(cookie)
    with pytest.raises(PreviewError, match='operator_confirmation_required'):
        f.registry.reset_clock(operator_confirmed=False)
    f.registry.reset_clock(operator_confirmed=True)
    with pytest.raises(PreviewError):
        f.lookup(cookie)
    f.rid = f.register()
    assert f.cookie()
    f.binding = replace(f.binding, session_id=str(uuid.uuid4()))
    with pytest.raises(PreviewError, match='origin_already_assigned'):
        f.register()
