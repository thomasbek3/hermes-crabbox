"""Controller-only D9 accounting inside a caller-owned SQLite write transaction.

This module grants no execution authority. Current generation/cancellation/profile
checks and dispatch creation must share this transaction with admission.
"""
from dataclasses import asdict, dataclass
import hashlib
from functools import wraps
import json
import math
import re
import sqlite3
import time

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_UNIT = 60_000_000  # One request's credit; refill = elapsed microseconds * rate/min.


class BudgetError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BudgetPolicy:
    version: str = "d9-v1"
    root_requests: int = 128
    attempt_requests: int = 32
    requests_per_minute: int = 60
    burst: int = 4
    root_wall_seconds: int = 14400
    cleanup_reserve_seconds: int = 160


@dataclass(frozen=True)
class RootScope:
    owner_id: str
    project_id: str
    session_id: str
    turn_id: str
    root_attempt_id: str
    root_generation: int


@dataclass(frozen=True)
class AttemptScope:
    root: RootScope
    attempt_id: str
    generation: int


@dataclass(frozen=True)
class Admission:
    decision: str  # charged / replay / refused; refusal observations must be committed.
    reason: str | None
    root_requests_used: int
    attempt_requests_used: int
    root_deadline_at: float
    request_deadline_at: float | None
    remaining_seconds: float
    retry_after_seconds: float | None
    policy_digest: str


def _integer(value, minimum=1, maximum=1_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BudgetError("invalid_budget_value")
    return value


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise BudgetError("invalid_budget_identity")
    return value


def _stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**11:
        raise BudgetError("invalid_budget_time")
    return round(value * 1_000_000)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _root(scope):
    if type(scope) is not RootScope:
        raise BudgetError("invalid_root_scope")
    values = asdict(scope)
    for name, value in values.items():
        _integer(value) if name == "root_generation" else _identity(value)
    return _canonical(values)


def _attempt(scope):
    if type(scope) is not AttemptScope:
        raise BudgetError("invalid_attempt_scope")
    _root(scope.root)
    _identity(scope.attempt_id)
    _integer(scope.generation)


def _policy(policy):
    if type(policy) is not BudgetPolicy:
        raise BudgetError("invalid_budget_policy")
    _identity(policy.version)
    for name in ("root_requests", "attempt_requests"):
        _integer(getattr(policy, name))
    _integer(policy.requests_per_minute, maximum=6000)
    _integer(policy.burst, maximum=1000)
    _integer(policy.root_wall_seconds, minimum=160, maximum=86400)
    _integer(policy.cleanup_reserve_seconds, minimum=160, maximum=policy.root_wall_seconds)
    body = _canonical(asdict(policy))
    return body, hashlib.sha256(body.encode()).hexdigest()


def _transaction(db):
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise BudgetError("budget_transaction_required")


def _atomic(method):
    @wraps(method)
    def call(self, db, *args, **kwargs):
        _transaction(db)
        db.execute("SAVEPOINT inference_budget_operation")
        try:
            # Acquire the write lock before reading counters, including deferred callers.
            db.execute("UPDATE inference_budget_version SET version=version WHERE 0")
            result = method(self, db, *args, **kwargs)
            db.execute("RELEASE inference_budget_operation")
            return result
        except BaseException:
            # RAISE(ROLLBACK), FULL and some IO errors may abort the outer transaction.
            if db.in_transaction:
                db.execute("ROLLBACK TO inference_budget_operation")
                db.execute("RELEASE inference_budget_operation")
            raise
    return call


def _one(db, sql, args=()):
    cursor = db.execute(sql, args)
    row = cursor.fetchone()
    return dict(zip((col[0] for col in cursor.description), row)) if row is not None else None


_SCHEMA = (
    "CREATE TABLE inference_budget_version(version INTEGER PRIMARY KEY)",
    "INSERT INTO inference_budget_version VALUES(1)",
    """CREATE TABLE inference_budget_roots(
        root_id TEXT PRIMARY KEY, scope_json TEXT NOT NULL, policy_json TEXT NOT NULL,
        policy_digest TEXT NOT NULL, admitted_us INTEGER NOT NULL, deadline_us INTEGER NOT NULL,
        high_water_us INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0 CHECK(used>=0))""",
    """CREATE TABLE inference_budget_attempts(
        attempt_id TEXT PRIMARY KEY, root_id TEXT NOT NULL REFERENCES inference_budget_roots(root_id),
        parent_id TEXT, parent_generation INTEGER, latest_generation INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0 CHECK(used>=0), credits INTEGER NOT NULL,
        refill_us INTEGER NOT NULL, deadline_us INTEGER NOT NULL)""",
    """CREATE TABLE inference_budget_generations(
        attempt_id TEXT NOT NULL REFERENCES inference_budget_attempts(attempt_id),
        generation INTEGER NOT NULL, PRIMARY KEY(attempt_id,generation))""",
    """CREATE TABLE inference_budget_requests(
        attempt_id TEXT NOT NULL REFERENCES inference_budget_attempts(attempt_id),
        generation INTEGER NOT NULL, nonce TEXT NOT NULL, payload_digest TEXT NOT NULL,
        profile_digest TEXT NOT NULL, charged_us INTEGER NOT NULL, deadline_us INTEGER NOT NULL,
        PRIMARY KEY(attempt_id,generation,nonce))""",
)
_TABLES = {"inference_budget_version", "inference_budget_roots", "inference_budget_attempts",
           "inference_budget_generations", "inference_budget_requests"}


def ensure_schema(db):
    """Explicit, atomic initializer. No Store hook, migration, or implicit adoption."""
    _transaction(db)
    existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} & _TABLES
    if existing:
        if existing != _TABLES or [row[0] for row in db.execute("SELECT version FROM inference_budget_version")] != [1]:
            raise BudgetError("budget_schema_incompatible")
        # Refuse stale/partial schemas even if their version was incorrectly stamped.
        columns = (
            ("inference_budget_roots", "root_id scope_json policy_json policy_digest admitted_us deadline_us high_water_us used"),
            ("inference_budget_attempts", "attempt_id root_id parent_id parent_generation latest_generation used credits refill_us deadline_us"),
            ("inference_budget_generations", "attempt_id generation"),
            ("inference_budget_requests", "attempt_id generation nonce payload_digest profile_digest charged_us deadline_us"),
        )
        for table, expected in columns:
            if [row[1] for row in db.execute(f"PRAGMA table_info({table})")] != expected.split():
                raise BudgetError("budget_schema_incompatible")
        return
    db.execute("SAVEPOINT inference_budget_schema")
    try:
        for statement in _SCHEMA:
            db.execute(statement)
        db.execute("RELEASE inference_budget_schema")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK TO inference_budget_schema")
            db.execute("RELEASE inference_budget_schema")
        raise


class InferenceBudget:
    def __init__(self, *, clock=time.time):
        if not callable(clock):
            raise BudgetError("invalid_budget_clock")
        self.clock = clock

    @_atomic
    def register_root(self, db, scope, policy=BudgetPolicy(), *, admitted_at):
        """Controller-only registration at compute admission, not queue submission."""
        _transaction(db)
        body = _root(scope)
        policy_body, digest = _policy(policy)
        admitted = _stamp(admitted_at)
        now = _stamp(self.clock())
        if admitted > now:
            raise BudgetError("budget_admission_in_future")
        existing = _one(db, "SELECT * FROM inference_budget_roots WHERE root_id=?", (scope.root_attempt_id,))
        if existing:
            if (existing["scope_json"], existing["policy_json"], existing["admitted_us"]) != (body, policy_body, admitted):
                raise BudgetError("root_budget_conflict")
            return digest
        db.execute("""INSERT INTO inference_budget_roots
            (root_id,scope_json,policy_json,policy_digest,admitted_us,deadline_us,high_water_us)
            VALUES(?,?,?,?,?,?,?)""", (scope.root_attempt_id, body, policy_body, digest, admitted,
                                     admitted + policy.root_wall_seconds * 1_000_000, now))
        return digest

    def _load_root(self, db, scope):
        body = _root(scope)
        root = _one(db, "SELECT * FROM inference_budget_roots WHERE root_id=?", (scope.root_attempt_id,))
        if not root or root["scope_json"] != body:
            raise BudgetError("root_budget_scope_mismatch")
        return root, BudgetPolicy(**json.loads(root["policy_json"]))

    def _check_ancestry(self, db, attempt):
        # Defensive bounded walk; actual role policy allows fewer nesting levels.
        seen = set()
        while attempt["parent_id"] is not None:
            if attempt["attempt_id"] in seen or len(seen) >= 64:
                raise BudgetError("budget_ancestry_invalid")
            seen.add(attempt["attempt_id"])
            parent = _one(db, "SELECT * FROM inference_budget_attempts WHERE attempt_id=?", (attempt["parent_id"],))
            if not parent or parent["root_id"] != attempt["root_id"] or parent["latest_generation"] != attempt["parent_generation"]:
                raise BudgetError("budget_ancestor_generation_conflict")
            attempt = parent
        if attempt["attempt_id"] != attempt["root_id"]:
            raise BudgetError("budget_ancestry_invalid")

    @_atomic
    def register_attempt(self, db, scope, *, parent=None, previous_generation=None, deadline_at=None):
        """Freeze ancestry/deadline; generation advances retain lifetime count/rate state.

        parent is an explicit registered AttemptScope, absent only for the root.
        previous_generation is mandatory for an advance, never for first registration.
        """
        _transaction(db)
        _attempt(scope)
        if previous_generation is not None:
            _integer(previous_generation)
        root, policy = self._load_root(db, scope.root)
        deadline = root["deadline_us"] if deadline_at is None else _stamp(deadline_at)
        if not root["admitted_us"] < deadline <= root["deadline_us"]:
            raise BudgetError("invalid_attempt_deadline")
        if parent is None:
            if scope.attempt_id != scope.root.root_attempt_id:
                raise BudgetError("budget_parent_required")
            parent_id = parent_generation = None
        else:
            _attempt(parent)
            if parent.root != scope.root or parent.attempt_id == scope.attempt_id or scope.attempt_id == scope.root.root_attempt_id:
                raise BudgetError("budget_parent_mismatch")
            parent_row = _one(db, "SELECT * FROM inference_budget_attempts WHERE attempt_id=?", (parent.attempt_id,))
            if not parent_row or parent_row["root_id"] != root["root_id"] or parent_row["latest_generation"] != parent.generation:
                raise BudgetError("budget_parent_mismatch")
            self._check_ancestry(db, parent_row)
            if deadline > parent_row["deadline_us"]:
                raise BudgetError("invalid_attempt_deadline")
            parent_id, parent_generation = parent.attempt_id, parent.generation
        existing = _one(db, "SELECT * FROM inference_budget_attempts WHERE attempt_id=?", (scope.attempt_id,))
        if existing:
            if (existing["root_id"], existing["parent_id"], existing["parent_generation"], existing["deadline_us"]) != (root["root_id"], parent_id, parent_generation, deadline):
                raise BudgetError("attempt_budget_conflict")
            if scope.generation == existing["latest_generation"]:
                return
            if previous_generation != existing["latest_generation"] or scope.generation != previous_generation + 1:
                raise BudgetError("budget_generation_conflict")
            db.execute("UPDATE inference_budget_attempts SET latest_generation=? WHERE attempt_id=?", (scope.generation, scope.attempt_id))
        else:
            if previous_generation is not None or (parent is None and scope.generation != scope.root.root_generation):
                raise BudgetError("budget_generation_conflict")
            db.execute("""INSERT INTO inference_budget_attempts
                (attempt_id,root_id,parent_id,parent_generation,latest_generation,credits,refill_us,deadline_us)
                VALUES(?,?,?,?,?,?,?,?)""", (scope.attempt_id, root["root_id"], parent_id, parent_generation,
                                          scope.generation, policy.burst * _UNIT, root["high_water_us"], deadline))
        db.execute("INSERT INTO inference_budget_generations VALUES(?,?)", (scope.attempt_id, scope.generation))

    @_atomic
    def admit(self, db, scope, *, nonce, payload_digest, profile_digest):
        """Charge together with dispatch INSERT. Commit refusals to persist clock fencing.

        Exact retry identity is attempt + generation + nonce + payload/profile digests.
        No capability ID participates: rotating grants cannot refill any budget.
        """
        _transaction(db)
        _attempt(scope)
        for value in (nonce, payload_digest, profile_digest):
            if not isinstance(value, str) or not _HEX.fullmatch(value):
                raise BudgetError("invalid_budget_digest")
        root, policy = self._load_root(db, scope.root)
        attempt = _one(db, "SELECT * FROM inference_budget_attempts WHERE attempt_id=?", (scope.attempt_id,))
        if not attempt or attempt["root_id"] != root["root_id"]:
            raise BudgetError("attempt_budget_scope_mismatch")
        if attempt["latest_generation"] != scope.generation:
            raise BudgetError("budget_generation_conflict")
        self._check_ancestry(db, attempt)
        prior = _one(db, """SELECT * FROM inference_budget_requests
            WHERE attempt_id=? AND generation=? AND nonce=?""", (scope.attempt_id, scope.generation, nonce))
        if prior and (prior["payload_digest"], prior["profile_digest"]) != (payload_digest, profile_digest):
            raise BudgetError("budget_nonce_conflict")
        now = _stamp(self.clock())
        effective = max(now, root["high_water_us"])
        db.execute("UPDATE inference_budget_roots SET high_water_us=? WHERE root_id=?", (effective, root["root_id"]))
        remaining = max(0, min(root["deadline_us"], attempt["deadline_us"]) - effective) / 1_000_000

        def result(decision, reason=None, retry=None, request_deadline=None):
            return Admission(decision, reason, root["used"], attempt["used"], root["deadline_us"] / 1_000_000,
                             request_deadline, remaining, retry, root["policy_digest"])

        if now < root["high_water_us"]:
            return result("refused", "budget_clock_rollback")
        if now >= root["deadline_us"]:
            return result("refused", "root_budget_exceeded")
        if now >= attempt["deadline_us"]:
            return result("refused", "attempt_budget_exceeded")
        if prior:
            # This is accounting replay, not permission to relaunch after a request deadline.
            return result("replay", request_deadline=prior["deadline_us"] / 1_000_000)
        if remaining < policy.cleanup_reserve_seconds:
            return result("refused", "insufficient_cleanup_reserve")
        if root["used"] >= policy.root_requests:
            return result("refused", "root_request_limit")
        if attempt["used"] >= policy.attempt_requests:
            return result("refused", "attempt_request_limit")
        credits = min(policy.burst * _UNIT, attempt["credits"] + (now - attempt["refill_us"]) * policy.requests_per_minute)
        if credits < _UNIT:
            return result("refused", "attempt_rate_limit", (_UNIT - credits) / policy.requests_per_minute / 1_000_000)
        request_deadline = min(root["deadline_us"], attempt["deadline_us"], now + policy.cleanup_reserve_seconds * 1_000_000)
        db.execute("UPDATE inference_budget_roots SET used=used+1 WHERE root_id=?", (root["root_id"],))
        db.execute("""UPDATE inference_budget_attempts SET used=used+1,credits=?,refill_us=?
            WHERE attempt_id=?""", (credits - _UNIT, now, scope.attempt_id))
        db.execute("INSERT INTO inference_budget_requests VALUES(?,?,?,?,?,?,?)",
                   (scope.attempt_id, scope.generation, nonce, payload_digest, profile_digest, now, request_deadline))
        root["used"] += 1
        attempt["used"] += 1
        return result("charged", request_deadline=request_deadline / 1_000_000)
