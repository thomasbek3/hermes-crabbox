"""Controller-only account reservations and fenced inference/refresh leases.

IDs are database references, not bearer credentials. No provider/token/runtime IO.
The required trusted verifier must inspect actual cleanup; model receipts are invalid.
"""
from dataclasses import asdict, dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import threading
import json
import math
import re
import time
import uuid

from .models import LIVE

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_ACTIVE = ('held', 'cleaning', 'quarantined')


class LeaseError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Reservation:
    account_id: str
    persistent_owner_id: str
    reservation_id: str
    epoch: int
    controller_instance_id: str


@dataclass(frozen=True)
class RequestLease:
    reservation: Reservation
    request_id: str
    grant_id: str
    purpose: str
    expires_at: float


@dataclass(frozen=True)
class CleanupTarget:
    scope: str
    reservation: Reservation
    request_id: str | None
    grant_id: str | None
    attempt_id: str | None
    generation: int | None
    frozen_at: float
    cleanup_id: str
    dispatches: tuple = ()


@dataclass(frozen=True)
class CleanupReceipt:
    target: CleanupTarget
    inspector_id: str
    outcome: str
    observed_at: float
    evidence_sha256: str


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise LeaseError('invalid_identity')
    return value


def _time(value):
    if type(value) not in (int, float) or not 0 <= value <= 10**11 or not math.isfinite(value):
        raise LeaseError('invalid_time')
    return float(value)


def ensure_schema(db):
    """Additive schema; no existing attempt data/index/version is rewritten."""
    expected={'provider_schema_version','provider_accounts','provider_reservations','provider_execution_grants','provider_request_leases','provider_dispatch'}
    present={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} & expected
    if present and present!=expected:raise LeaseError('provider_schema_incompatible')
    if 'provider_dispatch' in present and not {'internal_network_id','external_network_id','internal_network_name','external_network_name','deadline_at'} <= {row[1] for row in db.execute('PRAGMA table_info(provider_dispatch)')}:
        raise LeaseError('provider_schema_incompatible')
    has_tables = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_reservations'").fetchone()
    has_version = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_schema_version'").fetchone()
    if has_version:
        versions = [row[0] for row in db.execute('SELECT version FROM provider_schema_version')]
        if versions != [2]:raise LeaseError('provider_schema_incompatible')
    if has_tables and (not has_version or 'runtime_id' in {row[1] for row in db.execute('PRAGMA table_info(provider_reservations)')}):
        raise LeaseError('provider_schema_incompatible')
    statements = [
        'CREATE TABLE IF NOT EXISTS provider_schema_version(version INTEGER PRIMARY KEY)',
        'INSERT OR IGNORE INTO provider_schema_version VALUES(2)', 
        '''CREATE TABLE IF NOT EXISTS provider_accounts(
            account_id TEXT PRIMARY KEY, legacy_agent TEXT UNIQUE NOT NULL,
            persistent_owner_id TEXT UNIQUE NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
            active_id TEXT, registered_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS provider_reservations(
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES provider_accounts(account_id),
            epoch INTEGER NOT NULL, controller_instance_id TEXT NOT NULL, state TEXT NOT NULL,
            created_at REAL NOT NULL, reason TEXT, frozen_at REAL, cleanup_id TEXT, cleanup_receipt TEXT,
            UNIQUE(account_id,epoch))''',
        "CREATE UNIQUE INDEX IF NOT EXISTS provider_one_owner ON provider_reservations(account_id) WHERE state IN ('held','cleaning','quarantined')",
        '''CREATE TABLE IF NOT EXISTS provider_execution_grants(
            id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL REFERENCES provider_reservations(id),
            attempt_id TEXT NOT NULL REFERENCES attempts(id), generation INTEGER NOT NULL,
            expires_at REAL NOT NULL, created_at REAL NOT NULL, revoked_at REAL, reason TEXT)''',
        '''CREATE TABLE IF NOT EXISTS provider_request_leases(
            id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL REFERENCES provider_reservations(id),
            grant_id TEXT NOT NULL REFERENCES provider_execution_grants(id), purpose TEXT NOT NULL,
            state TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL,
            frozen_at REAL, cleanup_id TEXT, reason TEXT, cleanup_receipt TEXT)''',
        "CREATE UNIQUE INDEX IF NOT EXISTS provider_one_request ON provider_request_leases(reservation_id) WHERE state IN ('held','cleaning','quarantined')",
        '''CREATE TABLE IF NOT EXISTS provider_dispatch(
            request_id TEXT PRIMARY KEY REFERENCES provider_request_leases(id),
            reservation_id TEXT NOT NULL REFERENCES provider_reservations(id),
            attempt_id TEXT NOT NULL REFERENCES attempts(id), generation INTEGER NOT NULL,
            request_nonce TEXT NOT NULL, payload_digest TEXT NOT NULL, profile_digest TEXT NOT NULL,
            launch_nonce TEXT NOT NULL UNIQUE, provider_name TEXT NOT NULL UNIQUE, gateway_name TEXT NOT NULL UNIQUE,
            internal_network_name TEXT NOT NULL UNIQUE, external_network_name TEXT NOT NULL UNIQUE,
            provider_id TEXT, gateway_id TEXT, internal_network_id TEXT, external_network_id TEXT, state TEXT NOT NULL, version INTEGER NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0, uncertain INTEGER NOT NULL DEFAULT 0,
            operation TEXT, operation_receipt TEXT, failure_code TEXT, response BLOB, response_digest TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, deadline_at REAL NOT NULL, UNIQUE(attempt_id,generation,request_nonce))''',
    ]
    for statement in statements:
        db.execute(statement)
    typed = any(r[1] == 'execution_kind' for r in db.execute('PRAGMA table_info(attempts)'))
    for suffix in ('insert','update'):
        sql=legacy_guard_sql(suffix,typed=typed)
        db.execute(sql.replace('CREATE TRIGGER ','CREATE TRIGGER IF NOT EXISTS ',1))


def legacy_guard_sql(suffix, *, typed):
    if suffix not in {'insert','update'}: raise ValueError('Invalid guard')
    event='INSERT' if suffix=='insert' else 'UPDATE OF state,agent'
    kind_guard="AND NEW.execution_kind='legacy'" if typed else ''
    return f"""CREATE TRIGGER provider_guard_legacy_{suffix}
            BEFORE {event} ON attempts
            WHEN NEW.state IN ('preparing','running','waiting_input','awaiting_approval','checkpointing','held','verifying')
             {kind_guard}
             AND EXISTS(SELECT 1 FROM provider_accounts
                        WHERE legacy_agent=NEW.agent AND active_id IS NOT NULL)
            BEGIN SELECT RAISE(ABORT,'provider_account_reserved'); END"""


def legacy_blocked(db, agent):
    return bool(db.execute('SELECT 1 FROM provider_accounts WHERE legacy_agent=? AND active_id IS NOT NULL', (agent,)).fetchone())


def revoke_attempt(db, attempt_id, reason, stamp=None):
    """Called inside Store's cancellation/fencing/terminal transaction."""
    stamp = _time(time.time() if stamp is None else stamp)
    db.execute('UPDATE provider_execution_grants SET revoked_at=?,reason=? WHERE attempt_id=? AND revoked_at IS NULL', (stamp, reason, attempt_id))
    db.execute("UPDATE provider_dispatch SET cancel_requested=1,version=version+1,updated_at=? WHERE attempt_id=? AND cancel_requested=0 AND state NOT IN ('cleaned','delivered','failed')", (stamp, attempt_id))
    db.execute("""UPDATE provider_request_leases SET state='quarantined',reason=?
        WHERE grant_id IN (SELECT id FROM provider_execution_grants WHERE attempt_id=?) AND state='held'""", (reason, attempt_id))


class ProviderLeases:
    """Trusted controller facade; never expose these methods/IDs as client authority."""
    def __init__(self, store, *, cleanup_verifier, inspector_id, clock=time.time, lock_dir=None):
        if not callable(cleanup_verifier) or not callable(clock):
            raise LeaseError('trusted_verifier_required')
        self.store = store
        self.verifier = cleanup_verifier
        self.inspector_id = _id(inspector_id)
        self.clock = clock
        self.instance_id = str(uuid.uuid4())
        self.pid = os.getpid()
        self.lock_dir = Path(lock_dir or store.path.parent / 'provider-locks').absolute()
        self.lock_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = self.lock_dir.lstat()
        if self.lock_dir.resolve() != self.lock_dir or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode)&0o777 != 0o700:
            raise LeaseError('unsafe_account_lock_directory')
        self._lock_local = threading.local()

    @contextmanager
    def account_lock(self, account_id, timeout=5):
        _id(account_id)
        if os.getpid() != self.pid:raise LeaseError('controller_process_changed')
        self._ttl(timeout, 30)
        held = getattr(self._lock_local, 'held', {})
        if account_id in held:
            yield
            return
        path = self.lock_dir / (hashlib.sha256(account_id.encode()).hexdigest()+'.lock')
        fd = os.open(path, os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise LeaseError('unsafe_account_lock_file')
            deadline = time.monotonic()+timeout
            while True:
                try:fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic() >= deadline:raise LeaseError('account_lock_busy') from None
                    time.sleep(.01)
            self._lock_local.held = {**held, account_id:fd}
            try:yield
            finally:self._lock_local.held = held
        finally:
            os.close(fd)

    def _now(self):
        return _time(self.clock())

    def register_account(self, account_id, *, legacy_agent, persistent_owner_id):
        account_id, legacy_agent, persistent_owner_id = map(_id, (account_id, legacy_agent, persistent_owner_id))
        with self.store._tx() as db:
            row = db.execute('SELECT * FROM provider_accounts WHERE account_id=?', (account_id,)).fetchone()
            if row:
                if row['legacy_agent'] != legacy_agent or row['persistent_owner_id'] != persistent_owner_id:
                    raise LeaseError('immutable_account_binding')
                return
            if db.execute('SELECT 1 FROM provider_accounts WHERE legacy_agent=? OR persistent_owner_id=?', (legacy_agent, persistent_owner_id)).fetchone():
                raise LeaseError('account_binding_conflict')
            db.execute('INSERT INTO provider_accounts(account_id,legacy_agent,persistent_owner_id,registered_at) VALUES(?,?,?,?)', (account_id, legacy_agent, persistent_owner_id, self._now()))

    def _account(self, db, account_id):
        row = db.execute('SELECT * FROM provider_accounts WHERE account_id=?', (_id(account_id),)).fetchone()
        if not row:
            raise LeaseError('account_not_registered')
        return row

    def _reservation(self, db, expected, *, held=False):
        if (type(expected) is not Reservation or type(expected.epoch) is not int or expected.epoch < 1):
            raise LeaseError('invalid_reservation')
        for value in (expected.account_id, expected.persistent_owner_id, expected.reservation_id):_id(value)
        _id(expected.controller_instance_id)
        row = db.execute('''SELECT r.*,a.persistent_owner_id,a.active_id FROM provider_reservations r
            JOIN provider_accounts a ON a.account_id=r.account_id WHERE r.id=?''', (expected.reservation_id,)).fetchone()
        if (not row or row['active_id'] != row['id'] or row['account_id'] != expected.account_id
                or row['persistent_owner_id'] != expected.persistent_owner_id
                or row['epoch'] != expected.epoch or row['controller_instance_id'] != expected.controller_instance_id
                or row['state'] not in _ACTIVE):
            raise LeaseError('stale_reservation')
        if held and (row['state'] != 'held' or row['controller_instance_id'] != self.instance_id):
            raise LeaseError('owner_requires_reconciliation')
        return row

    def reserve(self, account_id, *, persistent_owner_id):
        with self.account_lock(account_id):
            return self._reserve(account_id, persistent_owner_id=persistent_owner_id)

    def _reserve(self, account_id, *, persistent_owner_id):
        with self.store._tx() as db:
            return self.reserve_in_transaction(db,account_id,persistent_owner_id=persistent_owner_id)

    def reserve_in_transaction(self, db, account_id, *, persistent_owner_id):
        """Controller SQL seam. Caller holds account flock BEFORE opening same DB tx."""
        _id(persistent_owner_id)
        if os.getpid()!=self.pid or account_id not in getattr(self._lock_local,'held',{}) or not db.in_transaction or Path(db.execute('PRAGMA database_list').fetchone()[2]).resolve()!=self.store.path.resolve():
            raise LeaseError('locked_account_transaction_required')
        account = self._account(db, account_id)
        if account['persistent_owner_id'] != persistent_owner_id:
            raise LeaseError('immutable_account_binding')
        if account['active_id'] is not None:
            raise LeaseError('account_reserved')
        placeholders = ','.join('?' for _ in LIVE)
        kind_filter = "AND execution_kind='legacy'" if any(r[1]=='execution_kind' for r in db.execute('PRAGMA table_info(attempts)')) else ''
        if db.execute(f'SELECT 1 FROM attempts WHERE agent=? {kind_filter} AND state IN ({placeholders})', (account['legacy_agent'], *sorted(LIVE))).fetchone():
            raise LeaseError('legacy_account_busy')
        identity = str(uuid.uuid4()); epoch = account['epoch'] + 1
        db.execute("INSERT INTO provider_reservations(id,account_id,epoch,controller_instance_id,state,created_at) VALUES(?,?,?,?, 'held',?)", (identity, account_id, epoch, self.instance_id, self._now()))
        db.execute('UPDATE provider_accounts SET active_id=?,epoch=? WHERE account_id=?', (identity, epoch, account_id))
        return Reservation(account_id, persistent_owner_id, identity, epoch, self.instance_id)

    def current(self, account_id):
        with self.store._connect() as db:
            account = self._account(db, account_id)
            if account['active_id'] is None:
                return None
            row = db.execute('SELECT * FROM provider_reservations WHERE id=?', (account['active_id'],)).fetchone()
            if not row:
                raise LeaseError('invalid_persisted_reservation')
            return {'reservation': Reservation(account_id, account['persistent_owner_id'], row['id'], row['epoch'], row['controller_instance_id']), 'state': row['state'], 'reason': row['reason'], 'requires_reconciliation': row['controller_instance_id'] != self.instance_id or row['state'] != 'held'}

    def _attempt_valid(self, db, attempt_id, generation):
        row = db.execute('''SELECT a.*,c.revoked_at FROM attempts a JOIN sessions s ON s.id=a.session_id
            JOIN clients c ON c.id=s.owner_id WHERE a.id=?''', (attempt_id,)).fetchone()
        return bool(row and row['generation'] == generation and row['state'] in LIVE
                    and not row['cancel_requested'] and row['revoked_at'] is None)

    def _workflow_grant_valid(self, db, reservation_id, attempt_id):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_accounts'").fetchone():return True
        binding=db.execute('SELECT root_id FROM workflow_accounts WHERE reservation_id=?',(reservation_id,)).fetchone()
        attempt=db.execute('SELECT execution_kind,workflow_root_id FROM attempts WHERE id=?',(attempt_id,)).fetchone()
        if not binding:return bool(attempt and attempt['execution_kind']=='legacy')
        return bool(attempt and attempt['execution_kind'] in {'hermes_root','hermes_child'} and attempt['workflow_root_id']==binding['root_id'])

    def issue_grant(self, reservation, *, attempt_id, generation, ttl_seconds=3600):
        _id(attempt_id)
        if type(generation) is not int or generation < 1:
            raise LeaseError('invalid_generation')
        ttl_seconds = self._ttl(ttl_seconds, 14400)
        with self.store._tx() as db:
            self._reservation(db, reservation, held=True)
            if not self._attempt_valid(db, attempt_id, generation) or not self._workflow_grant_valid(db,reservation.reservation_id,attempt_id):
                raise LeaseError('execution_not_authorized')
            count = db.execute('SELECT COUNT(*) FROM provider_execution_grants WHERE reservation_id=? AND revoked_at IS NULL', (reservation.reservation_id,)).fetchone()[0]
            if count >= 128:
                raise LeaseError('grant_capacity')
            identity = str(uuid.uuid4()); stamp = self._now()
            db.execute('INSERT INTO provider_execution_grants(id,reservation_id,attempt_id,generation,expires_at,created_at) VALUES(?,?,?,?,?,?)', (identity, reservation.reservation_id, attempt_id, generation, stamp + ttl_seconds, stamp))
            return identity

    @staticmethod
    def _ttl(value, maximum):
        if type(value) not in (int, float) or not 0 < value <= maximum or not math.isfinite(value):
            raise LeaseError('invalid_ttl')
        return float(value)

    def _grant_error(self, db, grant_id, reservation_id, stamp):
        grant = db.execute('SELECT * FROM provider_execution_grants WHERE id=? AND reservation_id=?', (grant_id, reservation_id)).fetchone()
        if not grant:
            return 'grant_not_found'
        if grant['revoked_at'] is not None:
            return 'grant_revoked'
        reason = None
        if stamp >= grant['expires_at']:
            reason = 'grant_expired'
        elif not self._attempt_valid(db, grant['attempt_id'], grant['generation']) or not self._workflow_grant_valid(db,reservation_id,grant['attempt_id']):
            reason = 'execution_not_authorized'
        if reason:
            db.execute('UPDATE provider_execution_grants SET revoked_at=?,reason=? WHERE id=?', (stamp, reason, grant_id))
            db.execute("UPDATE provider_request_leases SET state='quarantined',reason=? WHERE grant_id=? AND state='held'", (reason, grant_id))
        return reason

    def acquire_request(self, reservation, grant_id, *, purpose='inference', ttl_seconds=120):
        _id(grant_id)
        if purpose not in ('inference', 'refresh'):
            raise LeaseError('invalid_request_purpose')
        ttl_seconds = self._ttl(ttl_seconds, 3600)
        with self.store._tx() as db:
            self._reservation(db, reservation, held=True)
            stamp = self._now()
            error = self._grant_error(db, grant_id, reservation.reservation_id, stamp)
            if error is None:
                if db.execute("SELECT 1 FROM provider_request_leases WHERE reservation_id=? AND state IN ('held','cleaning','quarantined')", (reservation.reservation_id,)).fetchone():
                    error = 'request_lease_busy'
                else:
                    grant = db.execute('SELECT expires_at FROM provider_execution_grants WHERE id=?', (grant_id,)).fetchone()
                    expiry = min(stamp + ttl_seconds, grant['expires_at'])
                    identity = str(uuid.uuid4())
                    db.execute("INSERT INTO provider_request_leases(id,reservation_id,grant_id,purpose,state,expires_at,created_at) VALUES(?,?,?,?,'held',?,?)", (identity, reservation.reservation_id, grant_id, purpose, expiry, stamp))
                    lease = RequestLease(reservation, identity, grant_id, purpose, expiry)
        if error:
            raise LeaseError(error)
        return lease

    def _request(self, db, lease):
        if type(lease) is not RequestLease:
            raise LeaseError('invalid_request_lease')
        self._reservation(db, lease.reservation)
        _id(lease.request_id); _id(lease.grant_id)
        row = db.execute('SELECT * FROM provider_request_leases WHERE id=?', (lease.request_id,)).fetchone()
        if (not row or row['reservation_id'] != lease.reservation.reservation_id or row['grant_id'] != lease.grant_id
                or row['purpose'] != lease.purpose or row['expires_at'] != lease.expires_at or row['state'] not in _ACTIVE):
            raise LeaseError('stale_request_lease')
        return row

    def active_request(self, reservation):
        """Trusted recovery lookup only; a returned ID never grants request access."""
        with self.store._tx() as db:
            self._reservation(db, reservation)
            row = db.execute("SELECT * FROM provider_request_leases WHERE reservation_id=? AND state IN ('held','cleaning','quarantined')", (reservation.reservation_id,)).fetchone()
            if row is None:
                return None
            lease = RequestLease(reservation, row['id'], row['grant_id'], row['purpose'], row['expires_at'])
            self._request(db, lease)
            return lease

    def authorize_request(self, lease):
        with self.store._tx() as db:
            request = self._request(db, lease)
            owner = self._reservation(db, lease.reservation)
            stamp = self._now()
            error = self._grant_error(db, lease.grant_id, lease.reservation.reservation_id, stamp)
            if error is None and (request['state'] != 'held' or owner['state'] != 'held' or owner['controller_instance_id'] != self.instance_id):
                error = 'request_requires_reconciliation'
            if error is None and stamp >= request['expires_at']:
                error = 'request_expired'
                db.execute("UPDATE provider_request_leases SET state='quarantined',reason=? WHERE id=?", (error, lease.request_id))
        if error:
            raise LeaseError(error)
        return True

    def revoke_grant(self, grant_id):
        with self.store._tx() as db:
            if not db.execute('SELECT 1 FROM provider_execution_grants WHERE id=?', (_id(grant_id),)).fetchone():
                raise LeaseError('grant_not_found')
            db.execute("UPDATE provider_execution_grants SET revoked_at=COALESCE(revoked_at,?),reason=COALESCE(reason,'grant_revoked') WHERE id=?", (self._now(), grant_id))
            db.execute("UPDATE provider_request_leases SET state='quarantined',reason='grant_revoked' WHERE grant_id=? AND state='held'", (grant_id,))

    def quarantine(self, reservation):
        with self.store._tx() as db:
            self._reservation(db, reservation)
            db.execute("UPDATE provider_reservations SET state='quarantined',reason='reconciliation_required' WHERE id=?", (reservation.reservation_id,))
            db.execute("UPDATE provider_request_leases SET state='quarantined',reason='reconciliation_required' WHERE reservation_id=? AND state IN ('held','cleaning')", (reservation.reservation_id,))
            db.execute("UPDATE provider_execution_grants SET revoked_at=COALESCE(revoked_at,?),reason=COALESCE(reason,'owner_quarantined') WHERE reservation_id=?", (self._now(), reservation.reservation_id))

    def _verify(self, target):
        try:
            receipt = self.verifier(target)
        except Exception:
            raise LeaseError('cleanup_unconfirmed') from None
        if (type(receipt) is not CleanupReceipt or receipt.target != target or receipt.inspector_id != self.inspector_id
                or receipt.outcome not in ('terminated', 'fenced')
                or not isinstance(receipt.evidence_sha256, str) or not _HEX.fullmatch(receipt.evidence_sha256)):
            raise LeaseError('cleanup_unconfirmed')
        try:
            observed = _time(receipt.observed_at)
        except LeaseError:
            raise LeaseError('cleanup_unconfirmed') from None
        if not target.frozen_at <= observed <= self._now():
            raise LeaseError('cleanup_unconfirmed')
        return json.dumps(asdict(receipt), sort_keys=True, separators=(',', ':'))

    def cleanup_request(self, lease):
        if type(lease) is not RequestLease:raise LeaseError('invalid_request_lease')
        with self.account_lock(lease.reservation.account_id):
            return self._cleanup_request(lease)

    def _dispatch_bindings(self, db, reservation_id, request_id=None, *, cleanup_intent=False):
        query = "SELECT * FROM provider_dispatch WHERE reservation_id=? AND state NOT IN ('cleaned','delivered','failed')"
        args = [reservation_id]
        if request_id is not None:query += ' AND request_id=?';args.append(request_id)
        rows = db.execute(query, args).fetchall()
        if any((row['uncertain'] or row['state'] in ('create_intent','start_intent','cleaning')) and not (cleanup_intent and row['state']=='cleaning' and row['operation']=='cleanup' and row['uncertain']==1) for row in rows):raise LeaseError('dispatch_outcome_unknown')
        return tuple(tuple((key,row[key]) for key in ('request_id','attempt_id','generation','launch_nonce','profile_digest','payload_digest','provider_name','gateway_name','internal_network_name','external_network_name','provider_id','gateway_id','internal_network_id','external_network_id','state','version','operation','uncertain','deadline_at','operation_receipt')) for row in rows)

    def _begin_cleanup_dispatches(self,db,reservation_id,request_id=None):
        bindings=self._dispatch_bindings(db,reservation_id,request_id)
        for binding in bindings:
            row=dict(binding)
            db.execute("UPDATE provider_dispatch SET state='cleaning',operation='cleanup',uncertain=1,version=version+1,updated_at=? WHERE request_id=? AND version=?",(self._now(),row['request_id'],row['version']))
        return self._dispatch_bindings(db,reservation_id,request_id,cleanup_intent=True)

    def _cleanup_request(self, lease):
        with self.store._tx() as db:
            request = self._request(db, lease)
            self._reservation(db, lease.reservation, held=True)
            grant = db.execute('SELECT attempt_id,generation FROM provider_execution_grants WHERE id=?', (lease.grant_id,)).fetchone()
            bindings = self._begin_cleanup_dispatches(db, lease.reservation.reservation_id, lease.request_id)
            stamp = self._now(); cleanup_id = str(uuid.uuid4())
            db.execute("UPDATE provider_request_leases SET state='cleaning',frozen_at=?,cleanup_id=?,reason='cleanup_pending' WHERE id=?", (stamp, cleanup_id, lease.request_id))
            target = CleanupTarget('request', lease.reservation, lease.request_id, lease.grant_id, grant['attempt_id'], grant['generation'], stamp, cleanup_id, bindings)
        try:receipt = self._verify(target)
        except LeaseError:
            if target.dispatches:
                with self.store._tx() as db:
                    for binding in target.dispatches:
                        identity = dict(binding)['request_id']
                        db.execute("UPDATE provider_dispatch SET state='quarantined',uncertain=1,operation='cleanup',version=version+1,failure_code='cleanup_outcome_unknown',updated_at=? WHERE request_id=? AND reservation_id=?", (self._now(), identity, target.reservation.reservation_id))
            raise
        with self.store._tx() as db:
            request = self._request(db, lease)
            self._reservation(db, lease.reservation, held=True)
            if request['state'] != 'cleaning' or request['frozen_at'] != target.frozen_at or request['cleanup_id'] != target.cleanup_id:
                raise LeaseError('cleanup_identity_changed')
            if self._dispatch_bindings(db, lease.reservation.reservation_id, lease.request_id,cleanup_intent=True) != target.dispatches:raise LeaseError('cleanup_identity_changed')
            db.execute("UPDATE provider_dispatch SET state='cleaned',operation=NULL,uncertain=0,version=version+1,updated_at=? WHERE request_id=?", (self._now(), lease.request_id))
            db.execute("UPDATE provider_request_leases SET state='released',cleanup_receipt=?,reason=NULL WHERE id=?", (receipt, lease.request_id))

    def cleanup_owner(self, reservation, *, retain_fence=False):
        if type(retain_fence) is not bool:raise LeaseError('invalid_cleanup_policy')
        if type(reservation) is not Reservation:raise LeaseError('invalid_reservation')
        with self.account_lock(reservation.account_id):
            return self._cleanup_owner(reservation,retain_fence=retain_fence)

    def _cleanup_owner(self, reservation, *, retain_fence=False):
        with self.store._tx() as db:
            self._reservation(db, reservation)
            if not retain_fence and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_accounts'").fetchone() and db.execute('SELECT 1 FROM workflow_accounts WHERE reservation_id=?',(reservation.reservation_id,)).fetchone():raise LeaseError('workflow_cleanup_requires_retained_fence')
            bindings = self._begin_cleanup_dispatches(db, reservation.reservation_id)
            stamp = self._now(); cleanup_id = str(uuid.uuid4())
            db.execute("UPDATE provider_reservations SET state='cleaning',frozen_at=?,cleanup_id=?,reason='cleanup_pending' WHERE id=?", (stamp, cleanup_id, reservation.reservation_id))
            db.execute("UPDATE provider_execution_grants SET revoked_at=COALESCE(revoked_at,?),reason=COALESCE(reason,'owner_cleanup') WHERE reservation_id=?", (stamp, reservation.reservation_id))
            request = db.execute("SELECT r.*,g.attempt_id,g.generation FROM provider_request_leases r JOIN provider_execution_grants g ON g.id=r.grant_id WHERE r.reservation_id=? AND r.state IN ('held','cleaning','quarantined')", (reservation.reservation_id,)).fetchone()
            db.execute("UPDATE provider_request_leases SET state='quarantined',reason='owner_cleanup' WHERE reservation_id=? AND state IN ('held','cleaning')", (reservation.reservation_id,))
            target = CleanupTarget('owner', reservation, request['id'] if request else None, request['grant_id'] if request else None, request['attempt_id'] if request else None, request['generation'] if request else None, stamp, cleanup_id, bindings)
        try:receipt = self._verify(target)
        except LeaseError:
            if target.dispatches:
                with self.store._tx() as db:
                    for binding in target.dispatches:
                        identity = dict(binding)['request_id']
                        db.execute("UPDATE provider_dispatch SET state='quarantined',uncertain=1,operation='cleanup',version=version+1,failure_code='cleanup_outcome_unknown',updated_at=? WHERE request_id=? AND reservation_id=?", (self._now(), identity, target.reservation.reservation_id))
            raise
        with self.store._tx() as db:
            owner = self._reservation(db, reservation)
            if owner['state'] != 'cleaning' or owner['frozen_at'] != target.frozen_at or owner['cleanup_id'] != target.cleanup_id:
                raise LeaseError('cleanup_identity_changed')
            if self._dispatch_bindings(db, reservation.reservation_id,cleanup_intent=True) != target.dispatches:raise LeaseError('cleanup_identity_changed')
            db.execute("UPDATE provider_dispatch SET state='cleaned',operation=NULL,uncertain=0,cancel_requested=1,version=version+1,updated_at=? WHERE reservation_id=? AND state NOT IN ('cleaned','delivered','failed')", (self._now(), reservation.reservation_id))
            db.execute("UPDATE provider_request_leases SET state='released',cleanup_receipt=?,reason='owner_cleanup' WHERE reservation_id=? AND state IN ('held','cleaning','quarantined')", (receipt, reservation.reservation_id))
            if retain_fence:
                db.execute("UPDATE provider_reservations SET cleanup_receipt=?,reason='root_cleanup_verified' WHERE id=?",(receipt,reservation.reservation_id))
            else:
                db.execute("UPDATE provider_reservations SET state='released',cleanup_receipt=?,reason=NULL WHERE id=?", (receipt, reservation.reservation_id))
                db.execute('UPDATE provider_accounts SET active_id=NULL WHERE account_id=? AND active_id=?', (reservation.account_id, reservation.reservation_id))
