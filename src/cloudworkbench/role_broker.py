"""Local durable role-call authorization/pending records; no sockets, scheduling or providers."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import uuid

ACTIONS = frozenset({'cloud_request_roles', 'cloud_get_role_results'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_ROLE = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
REJECT_REASONS = frozenset({'capability_expired','capability_revoked','parent_cancelled','policy_unavailable','operator_rejected'})


class BrokerError(ValueError):
    def __init__(self, status, code):
        self.status_code, self.code = status, code
        super().__init__(code)


def _identifier(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _native_id(value):
    return type(value) is str and 1 <= len(value) <= 256 and all(33 <= ord(c) <= 126 for c in value)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _storage_errors(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except sqlite3.Error as error:
            self._diagnose('storage_unavailable', error)
            raise BrokerError(503, 'storage_unavailable') from None
    return guarded


@dataclass(frozen=True)
class ParentScope:
    owner_id: str
    project_id: str
    root_attempt_id: str
    parent_attempt_id: str
    generation: int
    native_session_id: str

    def __post_init__(self):
        if not all(_identifier(v) for v in (self.owner_id, self.project_id, self.root_attempt_id, self.parent_attempt_id)):
            raise BrokerError(400, 'invalid_parent_scope')
        if type(self.generation) is not int or not 1 <= self.generation <= 2**63-1 or not _native_id(self.native_session_id):
            raise BrokerError(400, 'invalid_parent_scope')


class RoleBroker:
    """Trusted controller methods; exposing this object directly to a job is forbidden.

    authorize_parent(db, scope) is mandatory, read-only, and runs in the SAME SQLite
    transaction as mutation. Integration must check owner/project/root ancestry, current
    generation, cancellation and lifecycle in the authoritative controller tables.
    """
    def __init__(self, db_path: Path, *, authorize_parent, clock=time.time, max_root_requests=32,
                 shared_group=False, diagnostic_hook=None, busy_timeout_ms=1000):
        if not callable(authorize_parent) or not callable(clock) or type(max_root_requests) is not int or not 1 <= max_root_requests <= 10000:
            raise BrokerError(500, 'invalid_broker_configuration')
        if diagnostic_hook is not None and not callable(diagnostic_hook) or type(shared_group) is not bool or type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 10000:
            raise BrokerError(500, 'invalid_broker_configuration')
        self.diagnostic_hook, self.busy_timeout_ms = diagnostic_hook, busy_timeout_ms
        self.path = Path(db_path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise BrokerError(500, 'unsafe_broker_database')
            if stat.S_IMODE(info.st_mode) not in ({0o600,0o660} if shared_group else {0o600}):
                raise BrokerError(500, 'unsafe_broker_permissions')
        finally:
            os.close(fd)
        self.authorize_parent, self.clock, self.max_root_requests = authorize_parent, clock, max_root_requests
        with self._connect() as db:
            if db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise BrokerError(503, 'wal_unavailable')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS role_broker_grants(
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    parent_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    scope TEXT NOT NULL, roles TEXT NOT NULL, actions TEXT NOT NULL,
                    expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS role_broker_pending(
                    id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
                    grant_id TEXT NOT NULL REFERENCES role_broker_grants(id),
                    scope TEXT NOT NULL, owner_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    root_id TEXT NOT NULL, native_call_id TEXT NOT NULL,
                    payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    created_at REAL NOT NULL, controller_request_id TEXT, terminal_reason TEXT);
                CREATE INDEX IF NOT EXISTS role_broker_root_count
                    ON role_broker_pending(owner_id, project_id, root_id);
            ''')

    def _diagnose(self, code, error):
        if self.diagnostic_hook is None:
            return
        diagnostic = {'code': code, 'exception_type': type(error).__name__[:64]}
        number = getattr(error, 'sqlite_errorcode', None)
        if type(number) is int:
            diagnostic['sqlite_errorcode'] = number
        try:
            self.diagnostic_hook(diagnostic)
        except Exception:
            pass

    @contextmanager
    def _connect(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            yield db
        except sqlite3.Error as error:
            self._diagnose('storage_unavailable', error)
            raise BrokerError(503, 'storage_unavailable') from None
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def _snapshot(self):
        with self._connect() as db:
            db.execute('BEGIN')
            try:
                yield db
            finally:
                db.rollback()

    @contextmanager
    def transaction(self):
        """Controller seam: pending admission and future child SQL can share one commit."""
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise BrokerError(503, 'clock_unavailable')
        return value

    def _authorize(self, db, scope):
        if not isinstance(scope, ParentScope):
            raise BrokerError(403, 'parent_scope_denied')
        try:
            permitted = self.authorize_parent(db, scope)
        except Exception as error:
            self._diagnose('parent_authorization_unavailable', error)
            raise BrokerError(503, 'parent_authorization_unavailable') from None
        if permitted is not True:
            raise BrokerError(403, 'parent_scope_denied')

    def issue(self, scope: ParentScope, *, roles: tuple[str, ...], expires_at: float,
              actions: tuple[str, ...] = ('cloud_request_roles', 'cloud_get_role_results')):
        """CONTROLLER ONLY. Return (grant_id, raw 256-bit capability) once; store hash only."""
        if not isinstance(roles, tuple) or not 1 <= len(roles) <= 64 or any(type(r) is not str or not _ROLE.fullmatch(r) for r in roles) or len(set(roles)) != len(roles):
            raise BrokerError(400, 'invalid_role_scope')
        if not isinstance(actions, tuple) or not actions or any(type(a) is not str or a not in ACTIONS for a in actions) or len(set(actions)) != len(actions):
            raise BrokerError(400, 'invalid_action_scope')
        issued_at = self._now()
        if type(expires_at) not in (int, float) or not math.isfinite(expires_at) or not issued_at < expires_at <= issued_at + 86400:
            raise BrokerError(400, 'invalid_capability_expiry')
        with self.transaction() as db:
            self._authorize(db, scope)
            existing = db.execute('SELECT scope FROM role_broker_grants WHERE parent_id=? AND generation=?', (scope.parent_attempt_id, scope.generation)).fetchall()
            encoded = _json(asdict(scope))
            if any(row['scope'] != encoded for row in existing):
                raise BrokerError(409, 'parent_identity_already_bound')
            identity, token = str(uuid.uuid4()), secrets.token_hex(32)
            db.execute('INSERT INTO role_broker_grants VALUES(?,?,?,?,?,?,?,?,0)',
                       (identity, _digest(token), scope.parent_attempt_id, scope.generation, encoded, _json(roles), _json(actions), expires_at))
        return identity, token

    def revoke(self, grant_id):
        """CONTROLLER ONLY; idempotent, serialized against request admission."""
        with self.transaction() as db:
            db.execute('UPDATE role_broker_grants SET revoked=1 WHERE id=?', (grant_id,))

    def _grant(self, db, token, action):
        if type(token) is not str or not _HASH.fullmatch(token):
            raise BrokerError(401, 'capability_invalid')
        row = db.execute('SELECT * FROM role_broker_grants WHERE token_hash=?', (_digest(token),)).fetchone()
        if not row or row['revoked'] or row['expires_at'] <= self._now():
            raise BrokerError(401, 'capability_invalid')
        if action not in json.loads(row['actions']):
            raise BrokerError(403, 'capability_action_denied')
        scope = ParentScope(**json.loads(row['scope']))
        self._authorize(db, scope)
        return row, scope

    @staticmethod
    def _payload(role, task, context_refs):
        if type(role) is not str or not _ROLE.fullmatch(role) or type(task) is not str:
            raise BrokerError(400, 'invalid_role_request')
        if len(task) > 32768:
            raise BrokerError(400, 'role_request_exceeds_bound')
        try:
            size = len(task.encode('utf-8'))
        except UnicodeError:
            raise BrokerError(400, 'invalid_role_request') from None
        if not 1 <= size <= 32768 or not isinstance(context_refs, tuple) or len(context_refs) > 32:
            raise BrokerError(400, 'role_request_exceeds_bound')
        refs = []
        for value in context_refs:
            if type(value) is not dict or set(value) != {'artifact_id', 'sha256'} or not _identifier(value['artifact_id']) or type(value['sha256']) is not str or not _HASH.fullmatch(value['sha256']):
                raise BrokerError(400, 'invalid_context_reference')
            refs.append(dict(value))
        return _json({'tool': 'cloud_request_roles', 'arguments': {'role': role, 'task': task, 'context_refs': refs}})

    @staticmethod
    def _public(row):
        return {'id': row['id'], 'request_key': row['request_key'], 'payload_hash': row['payload_hash'],
                'payload': json.loads(row['payload']), 'created_at': row['created_at'],
                'state': 'linked' if row['controller_request_id'] else 'rejected' if row['terminal_reason'] else 'pending',
                'reason': row['terminal_reason'],
                'controller_request_id': row['controller_request_id']}

    def _view(self, db, row):
        result = self._public(row)
        if result['state'] == 'pending':
            grant = db.execute('SELECT revoked,expires_at FROM role_broker_grants WHERE id=?', (row['grant_id'],)).fetchone()
            if grant['revoked'] or grant['expires_at'] <= self._now():
                result.update(state='blocked', reason='capability_revoked' if grant['revoked'] else 'capability_expired')
        return result

    def reject(self, scope: ParentScope, request_id, reason):
        """CONTROLLER ONLY: terminalize an unlinked record, including cancelled parents.

        Exact stored scope must match. No liveness authorization is required to refuse
        work; this method never creates work, reauthorizes a grant or refunds quota.
        """
        if not isinstance(scope, ParentScope) or type(reason) is not str or reason not in REJECT_REASONS:
            raise BrokerError(400, 'invalid_rejection')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM role_broker_pending WHERE id=? AND scope=?', (request_id, _json(asdict(scope)))).fetchone()
            if not row:
                raise BrokerError(404, 'role_request_not_found')
            if row['controller_request_id'] or row['terminal_reason'] and row['terminal_reason'] != reason:
                raise BrokerError(409, 'role_request_already_resolved')
            db.execute('UPDATE role_broker_pending SET terminal_reason=? WHERE id=?', (reason, request_id))
            return self._public(db.execute('SELECT * FROM role_broker_pending WHERE id=?', (request_id,)).fetchone())

    def prepare(self, token, *, native_session_id, native_call_id, role, task, context_refs=()):
        """Capability-facing. Return only AFTER durable pending commit; never launches work."""
        if not _native_id(native_session_id) or not _native_id(native_call_id):
            raise BrokerError(400, 'native_identity_unavailable')
        payload = self._payload(role, task, context_refs)
        with self.transaction() as db:
            grant, scope = self._grant(db, token, 'cloud_request_roles')
            if native_session_id != scope.native_session_id:
                raise BrokerError(403, 'native_session_mismatch')
            if role not in json.loads(grant['roles']):
                raise BrokerError(403, 'capability_role_denied')
            key = _digest(_json([scope.parent_attempt_id, scope.generation, scope.native_session_id, native_call_id]))
            existing = db.execute('SELECT * FROM role_broker_pending WHERE request_key=?', (key,)).fetchone()
            if existing:
                if existing['scope'] != grant['scope'] or existing['payload_hash'] != _digest(payload):
                    raise BrokerError(409, 'native_call_payload_conflict')
                return self._view(db, existing)
            count = db.execute('SELECT COUNT(*) FROM role_broker_pending WHERE owner_id=? AND project_id=? AND root_id=?',
                               (scope.owner_id, scope.project_id, scope.root_attempt_id)).fetchone()[0]
            if count >= self.max_root_requests:
                raise BrokerError(429, 'root_request_limit')
            identity = str(uuid.uuid4())
            db.execute('INSERT INTO role_broker_pending VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                (identity, key, grant['id'], grant['scope'], scope.owner_id, scope.project_id, scope.root_attempt_id,
                 native_call_id, payload, _digest(payload), self._now()))
            row = db.execute('SELECT * FROM role_broker_pending WHERE id=?', (identity,)).fetchone()
            return self._public(row)

    def read(self, token, request_id):
        if not _identifier(request_id):
            raise BrokerError(400, 'invalid_request_id')
        with self._snapshot() as db:
            grant, _ = self._grant(db, token, 'cloud_get_role_results')
            row = db.execute('SELECT * FROM role_broker_pending WHERE id=? AND scope=?', (request_id, grant['scope'])).fetchone()
            if not row:
                raise BrokerError(404, 'role_request_not_found')
            if json.loads(row['payload'])['arguments']['role'] not in json.loads(grant['roles']):
                raise BrokerError(403, 'capability_role_denied')
            return self._view(db, row)

    @_storage_errors
    def admit_in_transaction(self, db, scope: ParentScope, request_id, create_mapping):
        """CONTROLLER ONLY. Callback creates child/request SQL in this SAME transaction.

        Callback MUST NOT perform network/runtime side effects or commit. Only this seam
        can make future child creation and linkage atomic; calling an external scheduler
        then linking is unsafe and intentionally unsupported.
        """
        if not db.in_transaction or not callable(create_mapping):
            raise BrokerError(500, 'controller_transaction_required')
        filename = db.execute('PRAGMA database_list').fetchone()[2]
        if Path(filename).resolve() != self.path.resolve():
            raise BrokerError(500, 'controller_database_mismatch')
        self._authorize(db, scope)
        row = db.execute('SELECT * FROM role_broker_pending WHERE id=? AND scope=?', (request_id, _json(asdict(scope)))).fetchone()
        if not row:
            raise BrokerError(404, 'role_request_not_found')
        if row['terminal_reason']:
            return self._public(row)
        grant = db.execute('SELECT * FROM role_broker_grants WHERE id=?', (row['grant_id'],)).fetchone()
        if not grant or grant['revoked'] or grant['expires_at'] <= self._now():
            raise BrokerError(401, 'capability_invalid')
        if row['controller_request_id']:
            return self._public(row)
        savepoint = 'role_admission_' + uuid.uuid4().hex
        db.execute('SAVEPOINT ' + savepoint)
        try:
            mapping = create_mapping(db, self._public(row))
            if not db.in_transaction:
                raise BrokerError(500, 'controller_callback_ended_transaction')
            if not _identifier(mapping):
                raise BrokerError(500, 'invalid_controller_mapping')
            db.execute('UPDATE role_broker_pending SET controller_request_id=? WHERE id=?', (mapping, request_id))
            result = self._public(db.execute('SELECT * FROM role_broker_pending WHERE id=?', (request_id,)).fetchone())
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK TO ' + savepoint)
                db.execute('RELEASE ' + savepoint)
            raise
        db.execute('RELEASE ' + savepoint)
        return result
