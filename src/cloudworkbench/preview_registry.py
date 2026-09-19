"""Private preview capabilities only: no proxy, DNS, Docker or live endpoint setup."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

COOKIE_NAME = '__Host-cloud_workbench_preview'
_APP_ID = 0x43575052
_SAFE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z')
_HEX = re.compile(r'[a-f0-9]{64}\Z')
_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')


class PreviewError(ValueError):
    """Fixed error codes only; never includes a grant, cookie or request URL."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def exact_origin(value: str) -> str:
    if not isinstance(value, str) or len(value) > 253 or not value.isascii():
        raise PreviewError('invalid_origin')
    try:
        p = urlsplit(value)
        host = p.hostname or ''
        if (p.scheme != 'https' or value != 'https://' + host or p.username or p.password
                or p.port is not None or p.path or p.query or p.fragment or '.' not in host
                or not re.search(r'[a-z]', host.split('.')[-1]) or re.fullmatch(r'0x[0-9a-f]+', host.split('.')[-1])
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.'))):
            raise PreviewError('invalid_origin')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return value
        raise PreviewError('invalid_origin')
    except (TypeError, ValueError) as exc:
        if isinstance(exc, PreviewError):
            raise
        raise PreviewError('invalid_origin') from None


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise PreviewError('invalid_identity') from None


def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise PreviewError('invalid_limit')
    return value


def _hash(kind, value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise PreviewError('invalid_capability')
    return hashlib.sha256((kind + '\0' + value).encode()).hexdigest()


@dataclass(frozen=True)
class PreviewBinding:
    owner_id: str
    project_id: str
    session_id: str
    attempt_id: str
    generation: int
    service: str

    def __post_init__(self):
        for value in (self.owner_id, self.session_id, self.attempt_id):
            _uuid(value)
        if any(not isinstance(v, str) or not _SAFE.fullmatch(v) for v in (self.project_id, self.service)):
            raise PreviewError('invalid_identity')
        _limit(self.generation, 2**31-1)


@dataclass(frozen=True)
class PreviewPrincipal:
    owner_id: str
    credential_revision: str

    def __post_init__(self):
        _uuid(self.owner_id)
        if not isinstance(self.credential_revision, str) or not _HEX.fullmatch(self.credential_revision):
            raise PreviewError('invalid_principal')


@dataclass(frozen=True)
class WorkerBackend:
    worker_id: str
    container_id: str
    network_id: str
    address: str
    port: int
    protocol: str = 'http'

    def __post_init__(self):
        if not isinstance(self.worker_id, str) or not _SAFE.fullmatch(self.worker_id):
            raise PreviewError('invalid_backend')
        if any(not isinstance(v, str) or not _HEX.fullmatch(v) for v in (self.container_id, self.network_id)):
            raise PreviewError('invalid_backend')
        try:
            ip = ipaddress.ip_address(self.address)
            if str(ip) != self.address or not any(ip in net for net in (
                    ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
                    ipaddress.ip_network('192.168.0.0/16'), ipaddress.ip_network('fc00::/7')) if ip.version == net.version) or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
                raise ValueError
        except (ValueError, TypeError):
            raise PreviewError('invalid_backend') from None
        _limit(self.port, 65535)
        if self.protocol not in ('http', 'https'):
            raise PreviewError('invalid_backend')


@dataclass(frozen=True)
class IssuedGrant:
    token: str = field(repr=False)
    registration_id: str
    origin: str
    expires_at: float


@dataclass(frozen=True)
class IssuedCookie:
    token: str = field(repr=False)
    registration_id: str
    origin: str
    expires_at: float
    max_age: int

    def cookie_options(self):
        # No Domain attribute: __Host- requires HTTPS and Path=/.
        return {'key': COOKIE_NAME, 'value': self.token, 'path': '/', 'secure': True,
                'httponly': True, 'samesite': 'strict', 'max_age': self.max_age}


@dataclass(frozen=True)
class AuthorizedPreview:
    registration_id: str
    origin: str
    binding: PreviewBinding
    backend: WorkerBackend
    expires_at: float


Authority = Callable[[PreviewBinding, PreviewPrincipal], bool]


class _Connection(sqlite3.Connection):
    observed_now: float


class PreviewRegistry:
    """Dedicated controller-owned DB; trusted worker registration is an internal API."""
    def __init__(self, path: Path, *, dashboard_origin: str,
                 configured_origins=(), services: dict | None = None,
                 authority: Authority | None = None, clock=time.time):
        self.dashboard_origin = exact_origin(dashboard_origin)
        self.origins = frozenset(exact_origin(v) for v in configured_origins)
        if self.dashboard_origin in self.origins or len(self.origins) > 1024:
            raise PreviewError('invalid_preview_origins')
        self.services = dict(services or {})
        if len(self.services) > 64:
            raise PreviewError('invalid_service_policy')
        for name, policy in self.services.items():
            if not isinstance(name, str) or not _SAFE.fullmatch(name) or not isinstance(policy, tuple) or len(policy) != 2 or policy[0] not in ('http','https'):
                raise PreviewError('invalid_service_policy')
            _limit(policy[1], 65535)
        self.authority, self.clock = authority, clock
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path != self.path.resolve() or self.path.parent.stat().st_uid != os.geteuid() or stat.S_IMODE(self.path.parent.stat().st_mode) & 0o777 != 0o700:
            raise PreviewError('unsafe_registry_path')
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PreviewError('unsafe_registry_file')
        with self._db(initializing=True) as db:
            app = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app not in (0, _APP_ID) or (app == 0 and tables):
                raise PreviewError('foreign_database')
            if app == _APP_ID and db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise PreviewError('unsupported_schema')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS origin_owners(host TEXT PRIMARY KEY, owner_id TEXT NOT NULL, project_id TEXT NOT NULL, session_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS registrations(id TEXT PRIMARY KEY, origin TEXT NOT NULL, binding TEXT NOT NULL, backend TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS grants(hash TEXT PRIMARY KEY, registration_id TEXT NOT NULL REFERENCES registrations(id), principal TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS cookies(hash TEXT PRIMARY KEY, registration_id TEXT NOT NULL REFERENCES registrations(id), principal TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS clock_fence(id INTEGER PRIMARY KEY CHECK(id=1), seen REAL NOT NULL);
                INSERT OR IGNORE INTO clock_fence VALUES(1,0);
            ''')
            db.execute(f'PRAGMA application_id={_APP_ID}')
            db.execute('PRAGMA user_version=1')

    @contextmanager
    def _db(self, *, initializing=False, readonly=False):
        try:
            db = sqlite3.connect(self.path, timeout=3, factory=_Connection)
        except sqlite3.Error:
            raise PreviewError('registry_unavailable') from None
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN' if readonly else 'BEGIN IMMEDIATE')
            if not initializing:
                try:
                    now = self.clock()
                except Exception:
                    raise PreviewError('clock_unavailable') from None
                previous = db.execute('SELECT seen FROM clock_fence WHERE id=1').fetchone()[0]
                if type(now) not in (int, float) or not math.isfinite(now) or now < previous:
                    raise PreviewError('clock_unavailable')
                db.observed_now = now
                if not readonly:
                    db.execute('UPDATE clock_fence SET seen=? WHERE id=1', (now,))
                db.execute('SAVEPOINT operation')
            try:
                yield db
            except (PreviewError, TypeError, ValueError, KeyError) as exc:
                if not initializing:
                    # Rejected requests still fence time: expiry must never reverse.
                    if readonly:
                        db.rollback()
                        db.execute('BEGIN IMMEDIATE')
                        db.execute('UPDATE clock_fence SET seen=MAX(seen,?) WHERE id=1', (db.observed_now,))
                    else:
                        db.execute('ROLLBACK TO operation')
                    db.commit()
                if isinstance(exc, PreviewError):
                    raise
                raise PreviewError('registry_corrupt') from None
            db.commit()
        except sqlite3.Error:
            db.rollback()
            raise PreviewError('registry_unavailable') from None
        finally:
            db.close()

    def _now(self, db):
        return db.observed_now

    def _configured(self):
        if not self.origins or not self.services or not callable(self.authority):
            raise PreviewError('preview_unconfigured')

    def _authorize(self, binding, principal):
        if principal.owner_id != binding.owner_id:
            raise PreviewError('preview_denied')
        try:
            allowed = self.authority(binding, principal)
        except Exception:
            allowed = False
        if allowed is not True:
            raise PreviewError('preview_denied')

    def _registration(self, db, rid, origin, now):
        _uuid(rid)
        row = db.execute('SELECT * FROM registrations WHERE id=?', (rid,)).fetchone()
        if row is None or row['revoked'] or not row['created'] <= now < row['expires'] or row['origin'] != origin or origin not in self.origins:
            raise PreviewError('preview_unavailable')
        return row, PreviewBinding(**json.loads(row['binding']))

    @staticmethod
    def _prune(db, now):
        db.execute('DELETE FROM grants WHERE expires<=? OR used=1 OR registration_id IN (SELECT id FROM registrations WHERE revoked=1)', (now,))
        db.execute('DELETE FROM cookies WHERE expires<=? OR registration_id IN (SELECT id FROM registrations WHERE revoked=1 OR expires<=?)', (now,now))
        db.execute('DELETE FROM grants WHERE registration_id IN (SELECT id FROM registrations WHERE revoked=1 OR expires<=?)', (now,))
        db.execute('DELETE FROM registrations WHERE revoked=1 OR expires<=?', (now,))

    @staticmethod
    def _owner_count(db, table, owner_id):
        return sum(json.loads(row['principal'])['owner_id'] == owner_id
                   for row in db.execute(f'SELECT principal FROM {table}'))

    def register_worker_backend(self, binding: PreviewBinding, backend: WorkerBackend, *, origin: str,
                                readiness_proven: bool, ttl_seconds=900) -> str:
        self._configured()
        if type(binding) is not PreviewBinding or type(backend) is not WorkerBackend or readiness_proven is not True:
            raise PreviewError('backend_unready')
        if exact_origin(origin) not in self.origins or self.services.get(binding.service) != (backend.protocol, backend.port):
            raise PreviewError('backend_policy_mismatch')
        ttl = _limit(ttl_seconds, 3600)
        with self._db() as db:
            now = self._now(db)
            self._prune(db, now)
            if db.execute('SELECT COUNT(*) FROM registrations WHERE origin!=?', (origin,)).fetchone()[0] >= 1024:
                raise PreviewError('registration_limit')
            host = urlsplit(origin).hostname
            reserved = db.execute('SELECT * FROM origin_owners WHERE host=?', (host,)).fetchone()
            if reserved and (reserved['owner_id'], reserved['project_id'], reserved['session_id']) != (binding.owner_id,binding.project_id,binding.session_id):
                raise PreviewError('origin_already_assigned')
            db.execute('INSERT OR IGNORE INTO origin_owners VALUES(?,?,?,?)', (host,binding.owner_id,binding.project_id,binding.session_id))
            # Same-session replacement creates a new immutable identity and revokes old cookies.
            db.execute('UPDATE registrations SET revoked=1 WHERE origin=?', (origin,))
            rid = str(uuid.uuid4())
            db.execute('INSERT INTO registrations VALUES(?,?,?,?,?,?,0)', (rid,origin,json.dumps(asdict(binding)),json.dumps(asdict(backend)),now,now+ttl))
            self._prune(db, now)
            return rid

    def issue_grant(self, registration_id: str, principal: PreviewPrincipal, *, dashboard_request_origin: str,
                    preview_origin: str, ttl_seconds=30) -> IssuedGrant:
        self._configured()
        if dashboard_request_origin != self.dashboard_origin or type(principal) is not PreviewPrincipal:
            raise PreviewError('preview_denied')
        ttl = _limit(ttl_seconds, 60)
        with self._db(readonly=True) as db:
            _, checked = self._registration(db,registration_id,exact_origin(preview_origin),self._now(db))
        self._authorize(checked,principal)
        with self._db() as db:
            now = self._now(db)
            row,binding = self._registration(db,registration_id,exact_origin(preview_origin),now)
            self._prune(db,now)
            if db.execute('SELECT COUNT(*) FROM grants').fetchone()[0] >= 4096 or self._owner_count(db,'grants',principal.owner_id) >= 64:
                raise PreviewError('grant_limit')
            token = secrets.token_urlsafe(32); expires = min(now+ttl,row['expires'])
            db.execute('INSERT INTO grants VALUES(?,?,?,?,?,0)', (_hash('grant',token),registration_id,json.dumps(asdict(principal)),now,expires))
            return IssuedGrant(token,registration_id,row['origin'],expires)

    def exchange(self, grant: str, *, preview_origin: str, request_origin: str,
                 method='POST', cookie_seconds=900) -> IssuedCookie:
        self._configured()
        if method != 'POST' or request_origin != self.dashboard_origin:
            raise PreviewError('preview_denied')
        origin = exact_origin(preview_origin); hashed = _hash('grant',grant)
        ttl = _limit(cookie_seconds,900)
        with self._db(readonly=True) as db:
            saved = db.execute('SELECT * FROM grants WHERE hash=?', (hashed,)).fetchone()
            now = self._now(db)
            if saved is None or saved['used'] or not saved['created'] <= now < saved['expires']:
                raise PreviewError('invalid_capability')
            _, checked = self._registration(db,saved['registration_id'],origin,now)
            principal = PreviewPrincipal(**json.loads(saved['principal']))
        self._authorize(checked,principal)
        with self._db() as db:
            now = self._now(db)
            saved = db.execute('SELECT * FROM grants WHERE hash=?', (hashed,)).fetchone()
            if saved is None or saved['used'] or not saved['created'] <= now < saved['expires']:
                raise PreviewError('invalid_capability')
            row,binding = self._registration(db,saved['registration_id'],origin,now)
            principal = PreviewPrincipal(**json.loads(saved['principal']))
            self._prune(db,now)
            if db.execute('SELECT COUNT(*) FROM cookies').fetchone()[0] >= 4096 or self._owner_count(db,'cookies',principal.owner_id) >= 128:
                raise PreviewError('cookie_limit')
            changed = db.execute('UPDATE grants SET used=1 WHERE hash=? AND used=0', (hashed,)).rowcount
            if changed != 1:
                raise PreviewError('invalid_capability')
            token = secrets.token_urlsafe(32); expires = min(now+ttl,row['expires'])
            db.execute('INSERT INTO cookies VALUES(?,?,?,?,?)', (_hash('cookie',token),row['id'],saved['principal'],now,expires))
            return IssuedCookie(token,row['id'],origin,expires,max(0,math.floor(expires-now)))

    def lookup_cookie(self, cookie: str, *, preview_origin: str, request_origin: str | None = None,
                      method='GET', websocket=False) -> AuthorizedPreview:
        self._configured()
        origin = exact_origin(preview_origin)
        if (type(websocket) is not bool or method not in ('GET','HEAD','OPTIONS','POST','PUT','PATCH','DELETE')
                or (request_origin is not None and request_origin != origin)
                or ((websocket or method not in ('GET','HEAD','OPTIONS')) and request_origin != origin)):
            raise PreviewError('preview_denied')
        with self._db(readonly=True) as db:
            now = self._now(db)
            saved = db.execute('SELECT * FROM cookies WHERE hash=?', (_hash('cookie',cookie),)).fetchone()
            if saved is None or not saved['created'] <= now < saved['expires']:
                # Final write transaction below records expiry even on denied access.
                checked = None
            else:
                _, checked = self._registration(db,saved['registration_id'],origin,now)
                principal = PreviewPrincipal(**json.loads(saved['principal']))
        if checked is not None:
            self._authorize(checked,principal)
        with self._db() as db:
            now = self._now(db)
            saved = db.execute('SELECT * FROM cookies WHERE hash=?', (_hash('cookie',cookie),)).fetchone()
            if saved is None or not saved['created'] <= now < saved['expires']:
                raise PreviewError('invalid_capability')
            row,binding = self._registration(db,saved['registration_id'],origin,now)
            if checked is None:
                raise PreviewError('invalid_capability')
            return AuthorizedPreview(row['id'],origin,binding,WorkerBackend(**json.loads(row['backend'])),min(saved['expires'],row['expires']))

    def revoke_cookie(self, cookie: str, *, preview_origin: str):
        with self._db() as db:
            db.execute('DELETE FROM cookies WHERE hash=? AND registration_id IN (SELECT id FROM registrations WHERE origin=?)', (_hash('cookie',cookie),exact_origin(preview_origin)))

    def revoke_session(self, *, owner_id: str, project_id: str, session_id: str):
        """Trusted lifecycle hook; transport must authorize its caller separately."""
        _uuid(owner_id); _uuid(session_id)
        if not isinstance(project_id,str) or not _SAFE.fullmatch(project_id):
            raise PreviewError('invalid_identity')
        with self._db() as db:
            now = self._now(db)
            for row in db.execute('SELECT id,binding FROM registrations').fetchall():
                binding = PreviewBinding(**json.loads(row['binding']))
                if (binding.owner_id,binding.project_id,binding.session_id) == (owner_id,project_id,session_id):
                    db.execute('UPDATE registrations SET revoked=1 WHERE id=?', (row['id'],))
            self._prune(db,now)

    def revoke_registration(self, registration_id: str, *, binding: PreviewBinding):
        _uuid(registration_id)
        if type(binding) is not PreviewBinding:
            raise PreviewError('invalid_identity')
        with self._db() as db:
            row = db.execute('SELECT binding FROM registrations WHERE id=?', (registration_id,)).fetchone()
            if row is None or json.loads(row['binding']) != asdict(binding):
                raise PreviewError('preview_denied')
            db.execute('UPDATE registrations SET revoked=1 WHERE id=?', (registration_id,))
            self._prune(db,self._now(db))

    def reset_clock(self, *, operator_confirmed: bool):
        """Trusted operator repair: invalidate all capabilities, retain host reservations."""
        if operator_confirmed is not True:
            raise PreviewError('operator_confirmation_required')
        try:
            now = self.clock()
        except Exception:
            raise PreviewError('clock_unavailable') from None
        if type(now) not in (int,float) or not math.isfinite(now) or now < 0:
            raise PreviewError('clock_unavailable')
        with self._db(initializing=True) as db:
            db.execute('DELETE FROM grants')
            db.execute('DELETE FROM cookies')
            db.execute('DELETE FROM registrations')
            db.execute('UPDATE clock_fence SET seen=? WHERE id=1', (now,))
