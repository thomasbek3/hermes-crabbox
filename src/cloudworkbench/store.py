"""Durable single-worker controller state. No runtime or shell operations."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import os
import re
import stat
import time
import uuid
from .models import LIVE, TERMINAL, TRANSITIONS, SessionRequest
from . import retention, provider_leases


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid.uuid4())


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class StoreError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, db_path: Path, *, shared_group: bool = False, policy: dict | None = None):
        defaults = {
            'max_pending_attempts': 100,
            'input_max_bytes': 100 * 1024**2,
            'input_owner_bytes': 1024**3,
            'input_global_bytes': 4 * 1024**3,
        }
        overrides = policy or {}
        if set(overrides) - defaults.keys():
            raise ValueError('Unknown admission policy setting')
        self.policy = {**defaults, **overrides}
        if any(type(value) is not int or value <= 0 for value in self.policy.values()):
            raise ValueError('Admission policy limits must be positive integers')
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
            if exists:
                versions = [row[0] for row in db.execute('SELECT version FROM schema_version')]
                if versions not in ([1], [2], [3]):
                    raise StoreError(503, 'Unsupported database schema; restore compatible service or run explicit backed-up migration')
            schema = '''
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO schema_version VALUES(1);
            CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,scopes TEXT NOT NULL,projects TEXT NOT NULL,created_at TEXT NOT NULL,revoked_at TEXT);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES clients(id),project_id TEXT NOT NULL,goal TEXT NOT NULL,created_at TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),ordinal INTEGER NOT NULL,request TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(session_id,ordinal));
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),turn_id TEXT NOT NULL REFERENCES turns(id),agent TEXT NOT NULL,generation INTEGER NOT NULL,state TEXT NOT NULL,parent_attempt_id TEXT,resume_mode TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,runtime_id TEXT,reason TEXT,outcome TEXT,exit_code INTEGER,result TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,started_at TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_session ON attempts(session_id) WHERE state IN ('preparing','running','waiting_input','awaiting_approval','checkpointing','held','verifying');
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn ON attempts(turn_id) WHERE state IN ('queued','preparing','running','waiting_input','awaiting_approval','checkpointing','held','verifying');
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_credential ON attempts(agent) WHERE agent!='hermes' AND state IN ('preparing','running','waiting_input','awaiting_approval','checkpointing','held','verifying');
            CREATE TABLE IF NOT EXISTS events(session_id TEXT NOT NULL REFERENCES sessions(id),sequence INTEGER NOT NULL,attempt_id TEXT,type TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(session_id,sequence));
            CREATE TABLE IF NOT EXISTS idempotency(principal_id TEXT NOT NULL REFERENCES clients(id),key TEXT NOT NULL,request_hash TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(principal_id,key));
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),attempt_id TEXT NOT NULL REFERENCES attempts(id),metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inputs(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES clients(id),name TEXT NOT NULL,mime TEXT NOT NULL,state TEXT NOT NULL,storage_path TEXT,sha256 TEXT,bytes INTEGER,created_at TEXT NOT NULL);
            '''
            if exists and versions in ([2], [3]):
                schema = '\n'.join(line for line in schema.splitlines() if 'INSERT OR IGNORE INTO schema_version' not in line and 'CREATE UNIQUE INDEX IF NOT EXISTS one_' not in line)
                from .scheduler import verify_schema
                verify_schema(db)
                if versions == [3]:
                    from .delivery_schema import verify_schema as verify_delivery_schema
                    verify_delivery_schema(db)
            db.executescript(schema)
            retention.ensure_schema(db)
            provider_leases.ensure_schema(db)
        desired_mode = 0o660 if shared_group else 0o600
        if self.path.stat().st_uid == os.geteuid():
            self.path.chmod(desired_mode)
        elif stat.S_IMODE(self.path.stat().st_mode) != desired_mode:
            raise StoreError(503, 'Database permissions do not match configured service boundary')

    def migrate_scheduler(self):
        from .scheduler import migrate
        migrate(self)

    def migrate_delivery(self):
        from .delivery_schema import migrate
        migrate(self)

    def _routed_schema(self, db):
        return db.execute('SELECT version FROM schema_version').fetchone()[0] in (2, 3)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        return db

    @contextmanager
    def _tx(self):
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _scope(self, principal, scope):
        if scope not in principal['scopes']:
            raise StoreError(403, 'Missing required scope: ' + scope)

    def _session(self, db, principal, session_id):
        row = db.execute('SELECT * FROM sessions WHERE id=? AND owner_id=?', (session_id, principal['id'])).fetchone()
        if not row or row['project_id'] not in principal['projects']:
            raise StoreError(404, 'Session not found')
        return dict(row)

    def _idem(self, db, principal, key, request, action):
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise StoreError(422, 'Idempotency-Key must contain 1-200 characters')
        digest = hashlib.sha256(encode(request).encode()).hexdigest()
        row = db.execute('SELECT * FROM idempotency WHERE principal_id=? AND key=?', (principal['id'], key)).fetchone()
        if row:
            if row['request_hash'] != digest:
                raise StoreError(409, 'Idempotency key reused with different request')
            return json.loads(row['response'])
        value = action()
        db.execute('INSERT INTO idempotency VALUES(?,?,?,?)', (principal['id'], key, digest, encode(value)))
        return value

    def add_client(self, name, token, scopes, projects):
        if len(token) < 32:
            raise StoreError(422, 'Client token must be at least 32 characters')
        if not set(scopes) <= {'submit', 'observe', 'retrieve', 'cancel'}:
            raise StoreError(422, 'Unknown or reserved client scope')
        principal = dict(id=uid(), name=name, scopes=list(scopes), projects=list(projects))
        with self._tx() as db:
            db.execute('INSERT INTO clients(id,name,token_hash,scopes,projects,created_at) VALUES(?,?,?,?,?,?)', (principal['id'], name, hashlib.sha256(token.encode()).hexdigest(), encode(scopes), encode(projects), now()))
        return principal

    def authenticate(self, token):
        with self._connect() as db:
            row = db.execute('SELECT id,name,scopes,projects FROM clients WHERE token_hash=? AND revoked_at IS NULL', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row:
            return None
        return {**dict(row), 'scopes': json.loads(row['scopes']), 'projects': json.loads(row['projects'])}

    def _event(self, db, session_id, attempt_id, event_type, payload):
        body = encode(payload)
        if len(body.encode()) > 65536:
            raise StoreError(413, 'Event payload too large')
        sequence = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE session_id=?', (session_id,)).fetchone()[0]
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (session_id, sequence, attempt_id, event_type, body, now()))
        return sequence

    def _attempt(self, db, attempt_id):
        row = db.execute('SELECT a.*,t.request FROM attempts a JOIN turns t ON t.id=a.turn_id WHERE a.id=?', (attempt_id,)).fetchone()
        if not row:
            raise StoreError(404, 'Attempt not found')
        value = dict(row)
        value['request'] = json.loads(value['request'])
        value['result'] = json.loads(value['result']) if value['result'] else None
        value['attempt_id'] = value['id']
        value['cancel_requested'] = bool(value['cancel_requested'])
        return value

    def _new_attempt(self, db, session_id, turn_id, agent, parent=None):
        kind_filter = " AND execution_kind!='hermes_child'" if self._routed_schema(db) else ''
        pending_states = tuple(sorted(LIVE | {'queued'}))
        count = db.execute('SELECT COUNT(*) FROM attempts WHERE state IN (' + ','.join('?' for _ in pending_states) + ')' + kind_filter, pending_states).fetchone()[0]
        if count >= self.policy['max_pending_attempts']:
            raise StoreError(503, 'Pending attempt capacity reached; cancel queued work or retry after completion')
        attempt_id = uid()
        generation = db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM attempts WHERE session_id=?', (session_id,)).fetchone()[0]
        columns, values = '', ()
        if self._routed_schema(db):
            columns = ',root_sequence'
            values = (db.execute('SELECT COALESCE(MAX(root_sequence),0)+1 FROM attempts WHERE session_id=?',(session_id,)).fetchone()[0],)
        db.execute('INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,parent_attempt_id,resume_mode,created_at,updated_at'+columns+') VALUES(?,?,?,?,?,?,?,?,?,?'+(',?' if values else '')+')', (attempt_id, session_id, turn_id, agent, generation, 'queued', parent, 'reconstructed' if parent else None, now(), now(), *values))
        self._event(db, session_id, attempt_id, 'attempt.state', {'state': 'queued', 'generation': generation, 'resume_mode': 'reconstructed' if parent else None})
        return dict(session_id=session_id, turn_id=turn_id, attempt_id=attempt_id, state='queued', generation=generation, resume_mode='reconstructed' if parent else None)

    def create_session(self, principal, request, idempotency_key):
        self._scope(principal, 'submit')
        request = SessionRequest.model_validate(request).model_dump()
        if request['project_id'] not in principal['projects']:
            raise StoreError(403, 'Project is not authorized')
        with self._tx() as db:
            def create():
                for input_id in request['input_ids']:
                    row = db.execute('SELECT state FROM inputs WHERE id=? AND owner_id=?', (input_id, principal['id'])).fetchone()
                    if not row or row['state'] != 'ready':
                        raise StoreError(422, 'Input unavailable')
                session_id, turn_id = uid(), uid()
                db.execute('INSERT INTO sessions(id,owner_id,project_id,goal,created_at) VALUES(?,?,?,?,?)', (session_id, principal['id'], request['project_id'], request['goal'], now()))
                db.execute('INSERT INTO turns VALUES(?,?,?,?,?)', (turn_id, session_id, 1, encode(request), now()))
                return self._new_attempt(db, session_id, turn_id, request['agent'])
            return self._idem(db, principal, idempotency_key, ['create', request], create)

    def get_retention(self, principal, session_id):
        self._scope(principal, 'observe')
        with self._connect() as db:
            db.execute('BEGIN')
            self._session(db, principal, session_id)
            try:
                return retention.plan_session(db, session_id)
            except retention.RetentionError as exc:
                raise StoreError(exc.status_code, exc.detail) from exc

    def set_retention(self, principal, session_id, changes, expected_revision, key):
        self._scope(principal, 'submit')
        with self._tx() as db:
            self._session(db, principal, session_id)
            def update():
                try:
                    result = retention.set_policy(db, session_id, changes, expected_revision=expected_revision)
                except retention.RetentionError as exc:
                    raise StoreError(exc.status_code, exc.detail) from exc
                self._event(db, session_id, None, 'session.retention_changed', result)
                return result
            return self._idem(db, principal, key, ['retention', session_id, changes, expected_revision], update)

    def get_environment_snapshot(self, session_id, before_generation):
        """Trusted runner lookup; never borrow an environment from another session."""
        if type(before_generation) is not int or before_generation < 1:
            raise ValueError('Invalid generation')
        with self._connect() as db:
            row = db.execute("SELECT json_extract(result,'$.environment') AS environment FROM attempts WHERE session_id=? AND generation<? AND json_extract(result,'$.environment') IS NOT NULL ORDER BY generation DESC LIMIT 1",
                             (session_id,before_generation)).fetchone()
        return json.loads(row['environment']) if row else None

    def list_sessions(self, principal, limit=50, offset=0):
        self._scope(principal, 'observe')
        if not 1 <= limit <= 100 or offset < 0:
            raise StoreError(422, 'Invalid pagination')
        projects = principal['projects']
        if not projects:
            return []
        placeholders = ','.join('?' for _ in projects)
        with self._connect() as db:
            rows = db.execute('SELECT * FROM sessions WHERE owner_id=? AND archived=0 AND project_id IN (' + placeholders + ') ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                              (principal['id'], *projects, limit, offset)).fetchall()
            return [dict(row) for row in rows]

    def get_session(self, principal, session_id):
        self._scope(principal, 'observe')
        with self._connect() as db:
            result = self._session(db, principal, session_id)
            result['turns'] = [{**dict(row), 'request': json.loads(row['request'])} for row in db.execute('SELECT * FROM turns WHERE session_id=? ORDER BY ordinal', (session_id,))]
            result['attempts'] = [self._attempt(db, row['id']) for row in db.execute('SELECT id FROM attempts WHERE session_id=? ORDER BY generation', (session_id,))]
            roots = [a for a in result['attempts'] if a.get('execution_kind','legacy')!='hermes_child']
            if self._routed_schema(db): roots.sort(key=lambda a:a['root_sequence'])
            latest = roots[-1] if roots else {}
            result['state'] = latest.get('state')
            result['outcome'] = latest.get('outcome')
            result['active_attempt_id'] = next((a['id'] for a in roots if a['state'] in LIVE), None)
            return result

    def add_message(self, principal, session_id, message, key):
        self._scope(principal, 'submit')
        if not isinstance(message, str) or not message.strip() or len(message.encode()) > 131072:
            raise StoreError(422, 'Invalid message')
        with self._tx() as db:
            self._session(db, principal, session_id)
            def create():
                if self._routed_schema(db) and db.execute("SELECT 1 FROM attempts WHERE session_id=? AND execution_kind!='legacy'",(session_id,)).fetchone():
                    raise StoreError(409,'Routed followup requires typed workflow admission')
                previous = db.execute('SELECT * FROM turns WHERE session_id=? ORDER BY ordinal DESC LIMIT 1', (session_id,)).fetchone()
                request = json.loads(previous['request'])
                request['goal'] = message
                request['previous_turn_id'] = previous['id']
                turn_id = uid()
                db.execute('INSERT INTO turns VALUES(?,?,?,?,?)', (turn_id, session_id, previous['ordinal'] + 1, encode(request), now()))
                result = self._new_attempt(db, session_id, turn_id, request['agent'])
                result['delivery'] = 'queued'
                result['correlation_id'] = turn_id
                return result
            return self._idem(db, principal, key, ['message', session_id, message], create)

    def resume(self, principal, session_id, key):
        self._scope(principal, 'submit')
        with self._tx() as db:
            self._session(db, principal, session_id)
            def create():
                if self._routed_schema(db) and db.execute("SELECT 1 FROM attempts WHERE session_id=? AND execution_kind!='legacy'",(session_id,)).fetchone():
                    raise StoreError(409,'Routed resume requires typed workflow admission')
                latest = db.execute('SELECT * FROM attempts WHERE session_id=? ORDER BY generation DESC LIMIT 1', (session_id,)).fetchone()
                busy = db.execute("SELECT 1 FROM attempts WHERE session_id=? AND state NOT IN ('completed','failed','cancelled','interrupted','paused')", (session_id,)).fetchone()
                if busy:
                    raise StoreError(409, 'Session has pending or live attempts')
                return self._new_attempt(db, session_id, latest['turn_id'], latest['agent'], latest['id'])
            return self._idem(db, principal, key, ['resume', session_id], create)

    def cancel(self, principal, attempt_id, key):
        self._scope(principal, 'cancel')
        with self._tx() as db:
            attempt = self._attempt(db, attempt_id)
            self._session(db, principal, attempt['session_id'])
            def stop():
                if attempt['state'] not in TERMINAL:
                    state = 'cancelled' if attempt['state'] == 'queued' else attempt['state']
                    if state == 'cancelled':
                        db.execute('UPDATE attempts SET state=?,cancel_requested=1,updated_at=? WHERE id=?', (state, now(), attempt_id))
                    else:
                        db.execute('UPDATE attempts SET cancel_requested=1,updated_at=? WHERE id=?', (now(), attempt_id))
                    provider_leases.revoke_attempt(db, attempt_id, 'attempt_cancelled')
                    self._event(db, attempt['session_id'], attempt_id, 'attempt.state' if state == 'cancelled' else 'attempt.cancel_requested', {'state': state, 'cancel_requested': True})
                return self._attempt(db, attempt_id)
            return self._idem(db, principal, key, ['cancel', attempt_id], stop)

    def claim_next(self, capacity=2, blocked_agents=None, external_running=0, hermes_capacity=1):
        blocked_agents = blocked_agents or {}
        if capacity < 0 or external_running < 0:
            raise ValueError('Capacity counts must be nonnegative')
        if type(hermes_capacity) is not int or not 1 <= hermes_capacity <= 8:
            raise ValueError('Hermes capacity must be between one and eight')
        with self._tx() as db:
            migrated = self._routed_schema(db)
            hermes_limit = 1 if migrated else hermes_capacity
            if migrated and db.execute("SELECT 1 FROM workflow_roots WHERE state IN ('held','releasing','quarantined')").fetchone():
                return None
            kind_filter = " AND execution_kind='legacy'" if migrated else ''
            live = db.execute("SELECT session_id,agent FROM attempts WHERE state IN ('preparing','running','waiting_input','awaiting_approval','checkpointing','held','verifying')").fetchall()
            if len(live) + external_running >= capacity:
                return None
            busy_sessions, busy_agents = {r['session_id'] for r in live}, {r['agent'] for r in live}
            hermes_running = sum(r['agent'] == 'hermes' for r in live)
            for row in db.execute("SELECT * FROM attempts WHERE state='queued'" + kind_filter + " ORDER BY created_at,generation,id").fetchall():
                if row['agent'] in blocked_agents:
                    reason = str(blocked_agents[row['agent']]) if isinstance(blocked_agents, dict) else 'adapter_unavailable'
                    if row['reason'] != reason:
                        db.execute('UPDATE attempts SET reason=?,updated_at=? WHERE id=?', (reason, now(), row['id']))
                        self._event(db, row['session_id'], row['id'], 'attempt.blocked', {'reason': reason})
                    continue
                if provider_leases.legacy_blocked(db, row['agent']):
                    reason = 'provider_account_reserved'
                    if row['reason'] != reason:
                        db.execute('UPDATE attempts SET reason=?,updated_at=? WHERE id=?', (reason, now(), row['id']))
                        self._event(db, row['session_id'], row['id'], 'attempt.blocked', {'reason': reason})
                    continue
                agent_busy = (hermes_running >= hermes_limit if row['agent'] == 'hermes'
                              else row['agent'] in busy_agents)
                if row['session_id'] in busy_sessions or agent_busy:
                    continue
                db.execute("UPDATE attempts SET state='preparing',reason=NULL,updated_at=?,started_at=? WHERE id=?", (now(), now(), row['id']))
                self._event(db, row['session_id'], row['id'], 'attempt.state', {'state': 'preparing', 'generation': row['generation']})
                return self._attempt(db, row['id'])
        return None

    def transition(self, attempt_id, state, *, expected_generation, **fields):
        return self._transition(attempt_id,state,expected_generation=expected_generation,**fields)

    def _transition(self, attempt_id, state, *, expected_generation, workflow_authority=None, **fields):
        allowed = {'exit_code', 'reason', 'outcome', 'runtime_id', 'result'}
        if set(fields) - allowed:
            raise ValueError('Unsupported transition fields')
        with self._tx() as db:
            attempt = self._attempt(db, attempt_id)
            if attempt.get('execution_kind','legacy')!='legacy' and (workflow_authority is None or not workflow_authority(db,attempt)):
                raise StoreError(409,'Routed transition requires current workflow ownership')
            if attempt['generation'] != expected_generation:
                raise StoreError(409, 'Stale attempt generation')
            if state != attempt['state'] and state not in TRANSITIONS.get(attempt['state'], set()):
                raise StoreError(409, f"Illegal transition {attempt['state']} -> {state}")
            if attempt['state'] in TERMINAL:
                raise StoreError(409, 'Terminal attempt is immutable')
            if state == 'completed' and fields.get('outcome') not in {'verified', 'needs_review', 'unverified', 'rejected'}:
                raise StoreError(422, 'Completed attempt requires explicit outcome')
            if state in LIVE and attempt['state'] not in LIVE and attempt.get('execution_kind','legacy') == 'legacy' and provider_leases.legacy_blocked(db, attempt['agent']):
                raise StoreError(409, 'Provider account is reserved')
            if state in TERMINAL:
                provider_leases.revoke_attempt(db, attempt_id, 'attempt_terminal')
            changes = {**fields, 'state': state, 'updated_at': now()}
            if 'result' in changes:
                changes['result'] = encode(changes['result'])
            db.execute('UPDATE attempts SET ' + ','.join(k + '=?' for k in changes) + ' WHERE id=?', (*changes.values(), attempt_id))
            self._event(db, attempt['session_id'], attempt_id, 'attempt.state', {'state': state, **{k:v for k,v in fields.items() if k != 'result'}})
            return self._attempt(db, attempt_id)

    def fence_attempt(self, attempt_id, expected_generation):
        with self._tx() as db:
            attempt = self._attempt(db, attempt_id)
            if attempt['generation'] != expected_generation or attempt['state'] not in LIVE:
                raise StoreError(409, 'Stale or inactive attempt')
            if attempt.get('execution_kind','legacy') != 'legacy':
                raise StoreError(409,'Routed generation fencing requires workflow reconciliation')
            generation = db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM attempts WHERE session_id=?', (attempt['session_id'],)).fetchone()[0]
            provider_leases.revoke_attempt(db, attempt_id, 'attempt_fenced')
            db.execute('UPDATE attempts SET generation=?,updated_at=? WHERE id=?', (generation, now(), attempt_id))
            self._event(db, attempt['session_id'], attempt_id, 'attempt.fenced', {'generation': generation})
            return self._attempt(db, attempt_id)

    def get_attempt(self, attempt_id):
        with self._connect() as db:
            return self._attempt(db, attempt_id)

    def get_attempt_inputs(self, attempt_id, expected_generation):
        with self._connect() as db:
            attempt = self._attempt(db, attempt_id)
            if attempt['generation'] != expected_generation:
                raise StoreError(409, 'Stale attempt generation')
            owner = db.execute('SELECT owner_id FROM sessions WHERE id=?', (attempt['session_id'],)).fetchone()['owner_id']
            inputs = []
            for input_id in attempt['request'].get('input_ids', []):
                row = db.execute("SELECT * FROM inputs WHERE id=? AND owner_id=? AND state='ready'", (input_id, owner)).fetchone()
                if not row:
                    raise StoreError(422, 'Attempt input unavailable')
                inputs.append(dict(row))
            return inputs

    def get_native_resume_session(self, attempt_id):
        """Return the immediately preceding successful Hermes native identity.

        This controller-only lookup does not create or repair native state. An
        incomplete prior turn requires explicit recovery, never a fresh session.
        """
        with self._connect() as db:
            db.execute('BEGIN')
            routed = self._routed_schema(db)
            kind = 'execution_kind' if routed else "'legacy' AS execution_kind"
            current = db.execute(f'SELECT id,session_id,generation,agent,{kind} FROM attempts WHERE id=?',
                                 (attempt_id,)).fetchone()
            if (current is None or current['agent'] != 'hermes' or current['execution_kind'] != 'legacy'
                    or type(current['generation']) is not int or current['generation'] < 1):
                raise StoreError(409, 'Native Hermes continuation scope unavailable')
            kind_filter = " AND execution_kind='legacy'" if routed else ''
            previous = db.execute("SELECT id,agent,state,generation,CASE WHEN length(CAST(result AS BLOB))<=1048576 "
                "THEN result ELSE NULL END AS result FROM attempts WHERE session_id=? AND generation<?"
                + kind_filter + ' ORDER BY generation DESC,id DESC LIMIT 1',
                (current['session_id'],current['generation'])).fetchone()
            if previous is None:
                return None
            if previous['agent'] != 'hermes' or previous['state'] != 'completed':
                raise StoreError(409, 'Prior Hermes session requires explicit recovery')
            try:
                result = json.loads(previous['result'])
                provider = result['provider_result']
                native = provider['native_session_id']
                valid = (type(provider) is dict and provider.get('is_error') is False
                    and type(native) is str and re.fullmatch(r'[0-9]{8}_[0-9]{6}_[0-9a-f]{6}',native))
            except (ValueError, TypeError, KeyError, RecursionError):
                valid = False
            if not valid:
                raise StoreError(409, 'Prior Hermes session requires explicit recovery')
            return native

    def get_context(self, attempt_id):
        """Bounded public conversation context for explicit reconstructed continuation."""
        with self._connect() as db:
            attempt = self._attempt(db, attempt_id)
            ordinal = db.execute('SELECT ordinal FROM turns WHERE id=?', (attempt['turn_id'],)).fetchone()[0]
            turns = db.execute('SELECT * FROM turns WHERE session_id=? AND ordinal<=? ORDER BY ordinal DESC LIMIT 20', (attempt['session_id'], ordinal)).fetchall()
            context, remaining = [], 65536
            for turn in turns:
                request = json.loads(turn['request'])
                result = db.execute('SELECT outcome,result FROM attempts WHERE turn_id=? AND generation<? ORDER BY generation DESC LIMIT 1', (turn['id'], attempt['generation'])).fetchone()
                if turn['ordinal'] == ordinal and result is None:
                    continue
                summary = None
                if result and result['result']:
                    value = json.loads(result['result'])
                    if isinstance(value, dict) and isinstance(value.get('summary'), str):
                        summary = value['summary'][:8192]
                entry = {'turn_id':turn['id'], 'goal':request['goal'][:8192], 'outcome':result['outcome'] if result else None, 'summary':summary}
                size = len(encode(entry).encode())
                if size > remaining:
                    break
                remaining -= size
                context.append(entry)
            context.reverse()
            return context

    def active_attempts(self, *, execution_kind="legacy"):
        with self._connect() as db:
            if execution_kind not in {'legacy','hermes_root','hermes_child'}: raise ValueError('Explicit execution kind required')
            kind_filter = ' AND execution_kind=?' if self._routed_schema(db) else ''
            if not kind_filter and execution_kind != 'legacy': return []
            rows = db.execute("SELECT id FROM attempts WHERE state IN ('preparing','running','waiting_input','awaiting_approval','checkpointing','held','verifying')" + kind_filter, (execution_kind,) if kind_filter else ()).fetchall()
            return [self._attempt(db, row['id']) for row in rows]

    def resource_candidates(self):
        with self._connect() as db:
            db.execute('PRAGMA busy_timeout=20')
            started = time.monotonic()
            db.set_progress_handler(lambda:int(time.monotonic()-started >= .05),100)
            rows = db.execute("SELECT id FROM attempts WHERE state IN ('running','verifying') ORDER BY id LIMIT ?", (self.policy['max_pending_attempts'],)).fetchall()
            return [self._attempt(db,row['id']) for row in rows]

    def resource_attempt(self, attempt_id):
        """Optional observation reads must not inherit the 30-second busy wait."""
        with self._connect() as db:
            db.execute('PRAGMA busy_timeout=20')
            started = time.monotonic()
            db.set_progress_handler(lambda:int(time.monotonic()-started >= .05),100)
            return self._attempt(db, attempt_id)

    def append_resource_sample(self, binding, event_type, payload, *, expected_generation, dedupe_key):
        from .resource_monitor import EVENT_TYPE, validated_payload
        if event_type != EVENT_TYPE or type(expected_generation) is not int or expected_generation != binding.generation:
            raise StoreError(422, 'Invalid resource publication identity')
        try:
            clean = validated_payload(binding, payload)
        except (ValueError,TypeError,KeyError):
            raise StoreError(422, 'Invalid resource observation') from None
        if dedupe_key != 'resource:' + clean['sample_id']:
            raise StoreError(422, 'Invalid resource publication key')
        clean['_source_event'] = dedupe_key
        with self._connect() as db:
            db.execute('PRAGMA busy_timeout=20')
            started = time.monotonic()
            db.set_progress_handler(lambda:int(time.monotonic()-started >= .05),100)
            db.execute('BEGIN IMMEDIATE')
            try:
                attempt = self._attempt(db, binding.attempt_id)
                if not binding.matches(attempt):
                    db.rollback()
                    return None
                previous = db.execute("SELECT sequence,payload FROM events WHERE attempt_id=? AND type=? AND json_extract(payload,'$._source_event')=? LIMIT 1",
                                      (binding.attempt_id, EVENT_TYPE, dedupe_key)).fetchone()
                if previous:
                    if previous['payload'] != encode(clean):
                        raise StoreError(409, 'Resource publication conflict')
                    sequence = previous['sequence']
                else:
                    sequence = self._event(db, binding.session_id, binding.attempt_id, EVENT_TYPE, clean)
                db.commit()
                return sequence
            except Exception:
                db.rollback()
                raise

    def append_event(self, attempt_id, type, payload, *, expected_generation, dedupe_key=None):
        if type == 'controller.resource.sample':
            raise StoreError(403, 'Resource observations require the guarded controller publisher')
        if dedupe_key is not None and (not isinstance(dedupe_key, str) or not 1 <= len(dedupe_key) <= 128):
            raise StoreError(422, 'Source event key must contain 1-128 characters')
        with self._tx() as db:
            attempt = self._attempt(db, attempt_id)
            if attempt['generation'] != expected_generation:
                raise StoreError(409, 'Stale attempt generation')
            if dedupe_key is not None:
                payload = {**payload, '_source_event': dedupe_key}
                previous = db.execute("SELECT sequence,payload FROM events WHERE attempt_id=? AND type=? AND json_extract(payload,'$._source_event')=? ORDER BY sequence LIMIT 1", (attempt_id, type, dedupe_key)).fetchone()
                if previous:
                    if previous['payload'] != encode(payload):
                        raise StoreError(409, 'Source event key reused with different payload')
                    return previous['sequence']
            return self._event(db, attempt['session_id'], attempt_id, type, payload)

    def events(self, principal, session_id, after=0, limit=1000):
        self._scope(principal, 'observe')
        if after < 0 or not 1 <= limit <= 1000:
            raise StoreError(422, 'Invalid event cursor')
        with self._connect() as db:
            self._session(db, principal, session_id)
            return [{**dict(r), 'payload': json.loads(r['payload']), 'schema_version': 1} for r in db.execute('SELECT * FROM events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?', (session_id, after, limit))]

    def register_artifact(self, attempt_id, metadata, *, expected_generation):
        with self._tx() as db:
            attempt = self._attempt(db, attempt_id)
            if attempt['generation'] != expected_generation:
                raise StoreError(409, 'Stale attempt generation')
            if attempt.get('execution_kind','legacy')!='legacy':
                if 'generation' in metadata and metadata['generation']!=expected_generation: raise StoreError(409,'Artifact generation mismatch')
                metadata={**metadata,'generation':expected_generation}
            existing = db.execute("SELECT metadata FROM artifacts WHERE attempt_id=? AND json_extract(metadata, '$.path')=?", (attempt_id, metadata['path'])).fetchall()
            if existing:
                saved = json.loads(existing[0]['metadata'])
                if len(existing) != 1 or any(saved.get(key) != value for key, value in metadata.items() if key != 'id'):
                    raise StoreError(409, 'Artifact publication conflict')
                return saved
            value = {**metadata, 'id': metadata.get('id', uid()), 'session_id': attempt['session_id'], 'attempt_id': attempt_id}
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?)', (value['id'], attempt['session_id'], attempt_id, encode(value)))
            self._event(db, attempt['session_id'], attempt_id, 'artifact.created', {k:v for k,v in value.items() if k != 'storage_path'})
            return value

    def list_artifacts(self, principal, session_id):
        self._scope(principal, 'retrieve')
        with self._connect() as db:
            self._session(db, principal, session_id)
            return [json.loads(r['metadata']) for r in db.execute('SELECT metadata FROM artifacts WHERE session_id=?', (session_id,))]

    def get_artifact(self, principal, artifact_id):
        self._scope(principal, 'retrieve')
        with self._connect() as db:
            row = db.execute('SELECT * FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
            if not row:
                raise StoreError(404, 'Artifact not found')
            self._session(db, principal, row['session_id'])
            return json.loads(row['metadata'])

    def archive(self, principal, session_id, key):
        self._scope(principal, 'submit')
        with self._tx() as db:
            self._session(db, principal, session_id)
            def archive():
                db.execute('UPDATE sessions SET archived=1 WHERE id=?', (session_id,))
                self._event(db, session_id, None, 'session.archived', {})
                return {'session_id': session_id, 'archived': True}
            return self._idem(db, principal, key, ['archive', session_id], archive)

    def _input_usage(self, db, owner_id):
        reservation = self.policy['input_max_bytes']
        row = db.execute("""SELECT
            COALESCE(SUM(CASE WHEN state='ready' THEN bytes ELSE ? END),0) AS total,
            COALESCE(SUM(CASE WHEN owner_id=? THEN CASE WHEN state='ready' THEN bytes ELSE ? END ELSE 0 END),0) AS owner
            FROM inputs""", (reservation, owner_id, reservation)).fetchone()
        return row['owner'], row['total']

    def _check_input_budget(self, db, owner_id, additional_bytes):
        owner_bytes, total_bytes = self._input_usage(db, owner_id)
        if owner_bytes + additional_bytes > self.policy['input_owner_bytes']:
            raise StoreError(429, 'Owner input storage budget exhausted; no automatic deletion is performed')
        if total_bytes + additional_bytes > self.policy['input_global_bytes']:
            raise StoreError(503, 'Global input storage budget exhausted; no automatic deletion is performed')

    def reserve_input(self, principal, request, key):
        self._scope(principal, 'submit')
        with self._tx() as db:
            def create():
                self._check_input_budget(db, principal['id'], self.policy['input_max_bytes'])
                input_id = uid()
                db.execute('INSERT INTO inputs(id,owner_id,name,mime,state,created_at) VALUES(?,?,?,?,?,?)', (input_id, principal['id'], request['name'], request['mime'], 'pending', now()))
                return {'id': input_id, 'state': 'pending'}
            return self._idem(db, principal, key, ['input', request], create)

    def get_input(self, principal, input_id):
        self._scope(principal, 'submit')
        with self._connect() as db:
            row = db.execute('SELECT * FROM inputs WHERE id=? AND owner_id=?', (input_id, principal['id'])).fetchone()
            if not row:
                raise StoreError(404, 'Input not found')
            return dict(row)

    def finalize_input(self, principal, input_id, metadata, key=None):
        self._scope(principal, 'submit')
        size = metadata.get('bytes')
        if type(size) is not int or size < 0:
            raise StoreError(422, 'Input size must be a nonnegative integer')
        if size > self.policy['input_max_bytes']:
            raise StoreError(413, 'Input exceeds configured file byte limit')
        with self._tx() as db:
            def finalize():
                row = db.execute('SELECT * FROM inputs WHERE id=? AND owner_id=?', (input_id, principal['id'])).fetchone()
                if not row:
                    raise StoreError(404, 'Input not found')
                if row['state'] == 'ready':
                    if row['sha256'] != metadata['sha256'] or row['bytes'] != metadata['bytes']:
                        raise StoreError(409, 'Input already finalized with different content')
                    return dict(row)
                self._check_input_budget(db, principal['id'], size - self.policy['input_max_bytes'])
                db.execute("UPDATE inputs SET state='ready',storage_path=?,sha256=?,bytes=? WHERE id=?", (metadata['storage_path'], metadata['sha256'], metadata['bytes'], input_id))
                return dict(db.execute('SELECT * FROM inputs WHERE id=?', (input_id,)).fetchone())
            if key is None:
                return finalize()
            return self._idem(db, principal, key, ['upload', input_id, metadata['sha256'], metadata['bytes']], finalize)
