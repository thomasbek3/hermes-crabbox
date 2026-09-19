"""Explicit local schema migration and controller-only routed admission.

No runtime dispatch, filesystem copies, model calls, or implicit live migrations.
All methods require trusted controller data; broker correlation is not authority.
"""
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
import re
import sqlite3
import time

from .models import LIVE, TERMINAL, SessionRequest
from .budget_authority import BudgetAuthority
from .provider_leases import Reservation, LeaseError
from .store import StoreError, encode, now, uid
from .inference_budget import InferenceBudget, RootScope, AttemptScope, ensure_schema

_LIVE = "'preparing','running','waiting_input','awaiting_approval','checkpointing','held','verifying'"
_PENDING = "'queued'," + _LIVE
_ROOTS = "execution_kind IN ('legacy','hermes_root')"
INDEXES = {
    'one_live_session': f'CREATE UNIQUE INDEX one_live_session ON attempts(session_id) WHERE {_ROOTS} AND state IN ({_LIVE})',
    'one_active_turn': f'CREATE UNIQUE INDEX one_active_turn ON attempts(turn_id) WHERE {_ROOTS} AND state IN ({_PENDING})',
    'one_live_credential': f"CREATE UNIQUE INDEX one_live_credential ON attempts(agent) WHERE execution_kind='legacy' AND state IN ({_LIVE})",
    'one_live_child': f"CREATE UNIQUE INDEX one_live_child ON attempts(workflow_root_id) WHERE execution_kind='hermes_child' AND state IN ({_LIVE})",
    'one_active_seat': f"CREATE UNIQUE INDEX one_active_seat ON attempts(role_seat_id) WHERE execution_kind='hermes_child' AND state IN ({_PENDING})",
    'root_sequence': 'CREATE UNIQUE INDEX root_sequence ON attempts(session_id,root_sequence) WHERE root_sequence IS NOT NULL',
}

TABLES = [
    "CREATE TABLE workflow_child_launch(child_id TEXT PRIMARY KEY REFERENCES attempts(id),generation INTEGER NOT NULL,binding TEXT NOT NULL,binding_digest TEXT NOT NULL,controller_instance_id TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('bound','start_intent','started','fenced')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
    """CREATE TABLE workflow_roots(root_id TEXT PRIMARY KEY REFERENCES attempts(id), generation INTEGER NOT NULL,
      owner_id TEXT NOT NULL,project_id TEXT NOT NULL,session_id TEXT NOT NULL,turn_id TEXT NOT NULL,
      frozen TEXT NOT NULL,frozen_digest TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('queued','held','releasing','quarantined','released')),
      child_attempt_id TEXT REFERENCES attempts(id),cancel_requested INTEGER NOT NULL DEFAULT 0,
      admitted_at REAL,deadline_at REAL,version INTEGER NOT NULL DEFAULT 0)""",
    'CREATE TABLE workflow_root_cleanup(root_id TEXT PRIMARY KEY REFERENCES workflow_roots(root_id),generation INTEGER NOT NULL,controller_instance_id TEXT NOT NULL,cleanup_id TEXT UNIQUE NOT NULL,target TEXT NOT NULL,runtime_receipt TEXT,release_receipt TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)',
    'CREATE TABLE workflow_accounts(root_id TEXT NOT NULL REFERENCES workflow_roots(root_id),account_id TEXT NOT NULL,reservation_id TEXT UNIQUE NOT NULL REFERENCES provider_reservations(id),PRIMARY KEY(root_id,account_id))',
    'CREATE TABLE workflow_requests(id TEXT PRIMARY KEY,broker_pending_id TEXT UNIQUE NOT NULL,root_id TEXT NOT NULL REFERENCES workflow_roots(root_id),parent_generation INTEGER NOT NULL,role TEXT NOT NULL,payload TEXT NOT NULL,plan TEXT NOT NULL,ordinal INTEGER NOT NULL,UNIQUE(root_id,ordinal))',
    'CREATE TABLE workflow_seats(id TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES workflow_requests(id),ordinal INTEGER NOT NULL,profile TEXT NOT NULL,UNIQUE(request_id,ordinal))',
    'CREATE TABLE workflow_steps(root_id TEXT NOT NULL REFERENCES workflow_roots(root_id),step_id TEXT NOT NULL,ordinal INTEGER NOT NULL,role TEXT NOT NULL,profile TEXT NOT NULL,request_id TEXT UNIQUE REFERENCES workflow_requests(id),input_revision_sha256 TEXT,PRIMARY KEY(root_id,step_id),UNIQUE(root_id,ordinal))',
    'CREATE TABLE workflow_step_gates(root_id TEXT NOT NULL,step_id TEXT NOT NULL,child_id TEXT NOT NULL REFERENCES attempts(id),generation INTEGER NOT NULL,decision TEXT NOT NULL,revision_sha256 TEXT NOT NULL,artifact_id TEXT NOT NULL REFERENCES artifacts(id),artifact_sha256 TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(root_id,step_id),FOREIGN KEY(root_id,step_id) REFERENCES workflow_steps(root_id,step_id))',
]

_CHILD_CONTROL_SECONDS = .050


def _control_connect(path):
    # No Store initializer, 30-second busy timeout, or missing-file creation.
    db=sqlite3.connect(path.absolute().as_uri()+'?mode=rw',uri=True,timeout=0,isolation_level=None)
    db.row_factory=sqlite3.Row
    return db


@contextmanager
def _child_control_tx(store,*,write=True):
    """Bounded lock acquisition and VM work; kernel I/O is not interruptible.

    A commit exception or late acknowledgement is uncertain, never permission to
    retry start. Durable readback and the existing intent fence govern recovery.
    """
    deadline=time.monotonic()+_CHILD_CONTROL_SECONDS
    db=None;committing=False
    try:
        db=_control_connect(store.path)
        db.set_progress_handler(lambda:int(time.monotonic()>=deadline),100)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=0')
        if time.monotonic()>=deadline:raise StoreError(503,'Child control database deadline')
        while True:
            if time.monotonic() >= deadline:
                raise StoreError(503,'Child control database unavailable')
            try:
                db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                break
            except sqlite3.OperationalError as exc:
                # Retry only a busy BEGIN: no transaction body or start intent
                # has run. Body/COMMIT failures retain the uncertainty fence.
                code = getattr(exc, 'sqlite_errorcode', None)
                if code is None or code & 255 != sqlite3.SQLITE_BUSY or db.in_transaction:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(.001, remaining))
        if time.monotonic() >= deadline:
            raise StoreError(503,'Child control database deadline')
        yield db
        if time.monotonic()>=deadline:raise StoreError(503,'Child control database deadline')
        committing=True
        db.commit()
        if time.monotonic()>=deadline:raise StoreError(503,'Child control commit acknowledgement deadline')
    except BaseException as exc:
        if db is not None:
            with suppress(Exception):db.set_progress_handler(None,0)
            with suppress(Exception):
                if db.in_transaction:db.rollback()
        if committing and write and isinstance(exc,Exception):
            raise StoreError(503,'Child control commit outcome uncertain') from None
        if isinstance(exc,sqlite3.Error):raise StoreError(503,'Child control database unavailable') from None
        raise
    finally:
        if db is not None:
            with suppress(Exception):db.set_progress_handler(None,0)
            with suppress(Exception):db.close()


def enabled(db):
    return db.execute('SELECT version FROM schema_version').fetchone()[0] in (2, 3)


def verify_schema(db):
    for sql in TABLES:
        name=sql.split("(",1)[0].split()[-1]
        row=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()
        if not row or row[0]!=sql:raise StoreError(503,"Scheduler table differs from reviewed migration")
    for name, sql in INDEXES.items():
        row = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if not row or row[0] != sql:
            raise StoreError(503, 'Scheduler schema differs from reviewed migration')
    from .provider_leases import legacy_guard_sql
    for suffix in ("insert","update"):
        row=db.execute("SELECT sql FROM sqlite_master WHERE name=?",("provider_guard_legacy_"+suffix,)).fetchone()
        if not row or row[0] != legacy_guard_sql(suffix,typed=True):
            raise StoreError(503,"Provider legacy guard not migrated")
    for name, sql in TRIGGERS.items():
        row = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if not row or row[0] != sql:
            raise StoreError(503, 'Scheduler guard differs from reviewed migration')


TRIGGERS = {
    'workflow_identity_insert': """CREATE TRIGGER workflow_identity_insert BEFORE INSERT ON attempts
    WHEN COALESCE(((NEW.execution_kind='legacy' AND NEW.workflow_root_id IS NULL AND NEW.workflow_parent_id IS NULL AND NEW.workflow_parent_generation IS NULL AND NEW.role_seat_id IS NULL AND NEW.root_sequence IS NOT NULL)
      OR (NEW.execution_kind='hermes_root' AND NEW.agent='hermes' AND NEW.workflow_root_id=NEW.id AND NEW.workflow_parent_id IS NULL AND NEW.workflow_parent_generation IS NULL AND NEW.role_seat_id IS NULL AND NEW.root_sequence IS NOT NULL AND NEW.generation=1 AND NEW.state='queued')
      OR (NEW.execution_kind='hermes_child' AND NEW.agent='hermes' AND NEW.workflow_root_id IS NOT NULL AND NEW.workflow_parent_id=NEW.workflow_root_id AND NEW.workflow_parent_generation>0 AND NEW.role_seat_id IS NOT NULL AND NEW.root_sequence IS NULL AND NEW.generation=1 AND NEW.state='queued')),0)=0
    BEGIN SELECT RAISE(ABORT,'invalid_workflow_identity'); END""",
    'workflow_identity_immutable': """CREATE TRIGGER workflow_identity_immutable BEFORE UPDATE OF execution_kind,workflow_root_id,workflow_parent_id,workflow_parent_generation,role_seat_id,root_sequence ON attempts
    WHEN NEW.execution_kind IS NOT OLD.execution_kind OR NEW.workflow_root_id IS NOT OLD.workflow_root_id OR NEW.workflow_parent_id IS NOT OLD.workflow_parent_id OR NEW.workflow_parent_generation IS NOT OLD.workflow_parent_generation OR NEW.role_seat_id IS NOT OLD.role_seat_id OR NEW.root_sequence IS NOT OLD.root_sequence
    BEGIN SELECT RAISE(ABORT,'immutable_workflow_identity'); END""",
    'workflow_start_guard': f"""CREATE TRIGGER workflow_start_guard BEFORE UPDATE OF state ON attempts
    WHEN NEW.execution_kind!='legacy' AND NEW.state IN ({_LIVE}) AND NOT EXISTS(
      SELECT 1 FROM workflow_roots w JOIN attempts r ON r.id=w.root_id
      WHERE w.root_id=NEW.workflow_root_id AND w.state='held' AND w.cancel_requested=0
       AND r.generation=w.generation AND r.cancel_requested=0
       AND NOT EXISTS(SELECT 1 FROM workflow_accounts wa JOIN provider_reservations pr ON pr.id=wa.reservation_id JOIN provider_accounts pa ON pa.account_id=pr.account_id WHERE wa.root_id=w.root_id AND (pr.state!='held' OR pa.active_id IS NOT pr.id))
       AND EXISTS(SELECT 1 FROM workflow_accounts WHERE root_id=w.root_id)
       AND (NEW.execution_kind='hermes_root' OR (
         r.state IN ({_LIVE}) AND NEW.workflow_parent_generation=r.generation AND w.child_attempt_id=NEW.id
         AND EXISTS(SELECT 1 FROM workflow_seats s JOIN workflow_requests q ON q.id=s.request_id WHERE s.id=NEW.role_seat_id AND q.root_id=w.root_id AND q.parent_generation=r.generation AND NEW.session_id=r.session_id AND NEW.turn_id=r.turn_id))))
    BEGIN SELECT RAISE(ABORT,'workflow_admission_required'); END""",
    'legacy_capacity_guard': f"""CREATE TRIGGER legacy_capacity_guard BEFORE UPDATE OF state ON attempts
    WHEN NEW.execution_kind='legacy' AND NEW.state IN ({_LIVE}) AND EXISTS(SELECT 1 FROM workflow_roots WHERE state IN ('held','releasing','quarantined'))
    BEGIN SELECT RAISE(ABORT,'workflow_capacity_reserved'); END""",
}

TRIGGERS['workflow_snapshot_immutable'] = '''CREATE TRIGGER workflow_snapshot_immutable BEFORE UPDATE OF owner_id,project_id,session_id,turn_id,frozen,frozen_digest ON workflow_roots
 WHEN NEW.owner_id IS NOT OLD.owner_id OR NEW.project_id IS NOT OLD.project_id OR NEW.session_id IS NOT OLD.session_id OR NEW.turn_id IS NOT OLD.turn_id OR NEW.frozen IS NOT OLD.frozen OR NEW.frozen_digest IS NOT OLD.frozen_digest
 BEGIN SELECT RAISE(ABORT,'immutable_workflow_snapshot'); END'''
TRIGGERS['workflow_request_immutable'] = '''CREATE TRIGGER workflow_request_immutable BEFORE UPDATE ON workflow_requests BEGIN SELECT RAISE(ABORT,'immutable_workflow_request'); END'''
TRIGGERS['workflow_seat_immutable'] = '''CREATE TRIGGER workflow_seat_immutable BEFORE UPDATE ON workflow_seats BEGIN SELECT RAISE(ABORT,'immutable_workflow_seat'); END'''
TRIGGERS['workflow_step_immutable'] = '''CREATE TRIGGER workflow_step_immutable BEFORE UPDATE ON workflow_steps
 WHEN NEW.root_id IS NOT OLD.root_id OR NEW.step_id IS NOT OLD.step_id OR NEW.ordinal IS NOT OLD.ordinal OR NEW.role IS NOT OLD.role OR NEW.profile IS NOT OLD.profile OR OLD.request_id IS NOT NULL
 BEGIN SELECT RAISE(ABORT,'immutable_workflow_step'); END'''
TRIGGERS['workflow_gate_immutable'] = '''CREATE TRIGGER workflow_gate_immutable BEFORE UPDATE ON workflow_step_gates BEGIN SELECT RAISE(ABORT,'immutable_workflow_gate'); END'''
TRIGGERS['workflow_runtime_immutable'] = '''CREATE TRIGGER workflow_runtime_immutable BEFORE UPDATE OF runtime_id ON attempts
 WHEN OLD.execution_kind!='legacy' AND NEW.runtime_id IS NOT OLD.runtime_id AND (OLD.runtime_id IS NOT NULL OR NOT EXISTS(SELECT 1 FROM workflow_roots WHERE root_id=OLD.workflow_root_id AND state='held'))
 BEGIN SELECT RAISE(ABORT,'immutable_workflow_runtime'); END'''
TRIGGERS['workflow_cleanup_immutable'] = '''CREATE TRIGGER workflow_cleanup_immutable BEFORE UPDATE ON workflow_root_cleanup
 WHEN NEW.root_id IS NOT OLD.root_id OR NEW.generation IS NOT OLD.generation OR NEW.controller_instance_id IS NOT OLD.controller_instance_id OR NEW.cleanup_id IS NOT OLD.cleanup_id OR NEW.target IS NOT OLD.target
 OR (OLD.runtime_receipt IS NOT NULL AND NEW.runtime_receipt IS NOT OLD.runtime_receipt) OR (OLD.release_receipt IS NOT NULL AND NEW.release_receipt IS NOT OLD.release_receipt)
 BEGIN SELECT RAISE(ABORT,'immutable_root_cleanup'); END'''
TRIGGERS['workflow_child_launch_immutable'] = """CREATE TRIGGER workflow_child_launch_immutable BEFORE UPDATE ON workflow_child_launch
 WHEN NEW.child_id IS NOT OLD.child_id OR NEW.generation IS NOT OLD.generation OR NEW.binding IS NOT OLD.binding OR NEW.binding_digest IS NOT OLD.binding_digest OR NEW.controller_instance_id IS NOT OLD.controller_instance_id
 OR (NEW.state IS NOT OLD.state AND NOT ((OLD.state='bound' AND NEW.state='start_intent') OR (OLD.state='start_intent' AND NEW.state='started') OR (OLD.state!='fenced' AND NEW.state='fenced')))
 BEGIN SELECT RAISE(ABORT,'immutable_child_launch'); END"""

@dataclass(frozen=True)
class ChildCallerBinding:
    child_id: str
    generation: int
    runtime_id: str
    caller_spec_digest: str
    profile_digest: str
    input_revision_sha256: str
    assignment_digest: str
    binding_digest: str
    state: str
    may_start: bool = False

TRIGGERS['workflow_child_grant_fence'] = """CREATE TRIGGER workflow_child_grant_fence BEFORE INSERT ON provider_execution_grants
 WHEN EXISTS(SELECT 1 FROM workflow_child_launch WHERE child_id=NEW.attempt_id AND state='fenced')
 BEGIN SELECT RAISE(ABORT,'child_execution_fenced'); END"""

TRIGGERS['workflow_child_launch_no_delete'] = """CREATE TRIGGER workflow_child_launch_no_delete BEFORE DELETE ON workflow_child_launch
 BEGIN SELECT RAISE(ABORT,'immutable_child_launch'); END"""

TRIGGERS['legacy_capacity_insert'] = TRIGGERS['legacy_capacity_guard'].replace('legacy_capacity_guard BEFORE UPDATE OF state','legacy_capacity_insert BEFORE INSERT')

@dataclass(frozen=True)
class ChildCleanupTarget:
    root_id: str
    root_generation: int
    child_id: str
    child_generation: int
    reservation_version: int
    runtime_id: str | None

@dataclass(frozen=True)
class ChildCleanupReceipt:
    target: ChildCleanupTarget
    outcome: str
    evidence_sha256: str


@dataclass(frozen=True)
class AttemptCleanupBinding:
    attempt_id: str
    generation: int
    execution_kind: str
    state: str
    runtime_id: str | None


@dataclass(frozen=True)
class RootCleanupTarget:
    root_id: str
    generation: int
    reservation_version: int
    controller_instance_id: str
    cleanup_id: str
    frozen_at: float
    attempts: tuple[AttemptCleanupBinding, ...]
    reservations: tuple[Reservation, ...]
    inspector_id: str


@dataclass(frozen=True)
class RootCleanupReceipt:
    target: RootCleanupTarget
    outcome: str
    observed_at: float
    evidence_sha256: str


def _cleanup_target(body, *, delivery_root_id=None):
    try:
        value=json.loads(body)
        value['attempts']=tuple(AttemptCleanupBinding(**item) for item in value['attempts'])
        value['reservations']=tuple(Reservation(**item) for item in value['reservations'])
        target=RootCleanupTarget(**value)
        ids=(target.root_id,target.controller_instance_id,target.cleanup_id,target.inspector_id)
        if any(type(v) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',v) for v in ids):raise ValueError
        if any(type(v) is not int or v<1 for v in (target.generation,target.reservation_version)):raise ValueError
        if type(target.frozen_at) not in (int,float) or not math.isfinite(target.frozen_at):raise ValueError
        if not 1<=len(target.attempts)<=33 or not 1<=len(target.reservations)<=8:raise ValueError
        if len({a.attempt_id for a in target.attempts})!=len(target.attempts) or len({r.account_id for r in target.reservations})!=len(target.reservations):raise ValueError
        for a in target.attempts:
            if type(a.attempt_id) is not str or type(a.generation) is not int or a.generation<1 or a.execution_kind not in {'hermes_root','hermes_child'} or (a.state not in TERMINAL and not (delivery_root_id == a.attempt_id == target.root_id and a.execution_kind == 'hermes_root' and a.state == 'verifying')):raise ValueError
            if a.runtime_id is not None and (type(a.runtime_id) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}',a.runtime_id)):raise ValueError
        for r in target.reservations:
            if type(r.epoch) is not int or r.epoch<1:raise ValueError
            if any(type(v) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',v) for v in (r.account_id,r.persistent_owner_id,r.reservation_id,r.controller_instance_id)):raise ValueError
        return target
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError):
        raise StoreError(409,'Invalid persisted root cleanup target') from None


def _runtime_proof(body,target):
    try:
        value=json.loads(body)
        if value['target']!=asdict(target) and value['target']!=json.loads(encode(asdict(target))):raise ValueError
        if value['outcome'] not in {'terminated','fenced'} or type(value['observed_at']) not in (int,float) or not math.isfinite(value['observed_at']) or value['observed_at']<target.frozen_at or not re.fullmatch('[0-9a-f]{64}',value['evidence_sha256']):raise ValueError
    except (ValueError,TypeError,KeyError,AttributeError):raise StoreError(409,'Invalid persisted root runtime proof') from None


def migrate(store):
    """Operator-only explicit migration. Caller must back up and stop old writers first.

    Refuses nonquiescent data; transaction rollback leaves schema and history intact.
    Opening Store alone never performs this migration.
    """
    with store._tx() as db:
        if enabled(db):
            verify_schema(db)
            if db.execute('SELECT version FROM schema_version').fetchone()[0] == 3:
                from .delivery_schema import verify_schema as verify_delivery_schema
                verify_delivery_schema(db)
            return
        if db.execute(f'SELECT 1 FROM attempts WHERE state IN ({_LIVE})').fetchone() or db.execute('SELECT 1 FROM provider_accounts WHERE active_id IS NOT NULL').fetchone():
            raise StoreError(409, 'Migration requires no live attempts or provider reservations')
        conflicts=db.execute('SELECT session_id,generation FROM attempts GROUP BY session_id,generation HAVING COUNT(*)>1 LIMIT 20').fetchall()
        if conflicts:
            raise StoreError(409, 'Ambiguous legacy chronology; preserve DB and inspect sessions: '+','.join(str(r[0])+':'+str(r[1]) for r in conflicts))
        for definition in (
            "execution_kind TEXT NOT NULL DEFAULT 'legacy' CHECK(execution_kind IN ('legacy','hermes_root','hermes_child'))",
            'workflow_root_id TEXT REFERENCES attempts(id)', 'workflow_parent_id TEXT REFERENCES attempts(id)',
            'workflow_parent_generation INTEGER', 'role_seat_id TEXT', 'root_sequence INTEGER',
        ):
            db.execute('ALTER TABLE attempts ADD COLUMN ' + definition)
        db.execute('UPDATE attempts SET root_sequence=(SELECT COUNT(*) FROM attempts b WHERE b.session_id=attempts.session_id AND b.generation<=attempts.generation)')
        for sql in TABLES: db.execute(sql)
        for name in ('one_live_session','one_active_turn','one_live_credential'):
            db.execute('DROP INDEX ' + name)
        for sql in INDEXES.values(): db.execute(sql)
        for sql in TRIGGERS.values(): db.execute(sql)
        for name in ('provider_guard_legacy_insert','provider_guard_legacy_update'):
            db.execute('DROP TRIGGER ' + name)
        db.execute('UPDATE schema_version SET version=2')
        from .provider_leases import ensure_schema as ensure_provider_schema
        ensure_provider_schema(db)
        ensure_schema(db)
        verify_schema(db)


class RoleScheduler:
    """SQL admission foundation, not a runtime executor or model-facing API."""
    def __init__(self, store, leases, *, clock=time.time):
        self.store, self.leases, self.clock = store, leases, clock
        self.budget = InferenceBudget(clock=clock)
        with store._connect() as db:
            if not enabled(db): raise StoreError(503, 'Explicit scheduler migration required')
            verify_schema(db)

    def enqueue_root(self, principal, request, key, *, frozen):
        request = SessionRequest.model_validate(request).model_dump()
        self.store._scope(principal, 'submit')
        if request['agent'] != 'hermes' or request['project_id'] not in principal['projects']:
            raise StoreError(403, 'Routed root requires authorized Hermes project')
        body = encode(frozen)
        if not isinstance(frozen, dict) or set(frozen) != {'accounts','role_plans','provenance'} or not isinstance(frozen['accounts'],dict) or not 1<=len(frozen['accounts'])<=8 or not isinstance(frozen['role_plans'],dict) or not frozen['provenance'] or len(body.encode()) > 65536:
            raise StoreError(422, 'Bounded immutable workflow snapshot required')
        workflow=frozen['provenance'].get('workflow') if isinstance(frozen['provenance'],dict) else None
        steps=workflow.get('steps') if isinstance(workflow,dict) else []
        if workflow is not None:
            if workflow.get('ready') is not True or not isinstance(steps,list) or not 1<=len(steps)<=32:
                raise StoreError(409,'Complete ready workflow required')
            ids=[]
            for step in steps:
                if not isinstance(step,dict) or not isinstance(step.get('id'),str) or not re.fullmatch('[A-Za-z0-9_.-]{1,128}',step['id']) or step.get('ready') is not True or frozen['role_plans'].get(step.get('role')) != [{'ready':True,'profile':step.get('profile')}]:
                    raise StoreError(409,'Frozen workflow step mismatch')
                ids.append(step['id'])
            if len(set(ids))!=len(ids):raise StoreError(409,'Duplicate workflow step identity')
        with self.store._tx() as db:
            def create():
                if db.execute(f'SELECT COUNT(*) FROM attempts WHERE {_ROOTS} AND state IN ({_PENDING})').fetchone()[0] >= self.store.policy['max_pending_attempts']:
                    raise StoreError(503, 'Pending root capacity reached')
                for identity in request['input_ids']:
                    if not db.execute("SELECT 1 FROM inputs WHERE id=? AND owner_id=? AND state='ready'", (identity,principal['id'])).fetchone():
                        raise StoreError(422, 'Input unavailable')
                session, turn, attempt = uid(), uid(), uid()
                stamp = now()
                db.execute('INSERT INTO sessions(id,owner_id,project_id,goal,created_at) VALUES(?,?,?,?,?)',(session,principal['id'],request['project_id'],request['goal'],stamp))
                db.execute('INSERT INTO turns VALUES(?,?,?,?,?)',(turn,session,1,encode(request),stamp))
                db.execute("INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at,execution_kind,workflow_root_id,root_sequence) VALUES(?,?,?,'hermes',1,'queued',?,?,'hermes_root',?,1)",(attempt,session,turn,stamp,stamp,attempt))
                db.execute("INSERT INTO workflow_roots(root_id,generation,owner_id,project_id,session_id,turn_id,frozen,frozen_digest,state) VALUES(?,1,?,?,?,?,?,?,'queued')",(attempt,principal['id'],request['project_id'],session,turn,body,hashlib.sha256(body.encode()).hexdigest()))
                if db.execute('SELECT version FROM schema_version').fetchone()[0] == 3:
                    from .delivery_schema import freeze_base
                    freeze_base(db,attempt,1)
                for ordinal,step in enumerate(steps):
                    db.execute('INSERT INTO workflow_steps(root_id,step_id,ordinal,role,profile) VALUES(?,?,?,?,?)',(attempt,step['id'],ordinal,step['role'],encode(step['profile'])))
                self.store._event(db,session,attempt,'attempt.state',{'state':'queued','execution_kind':'hermes_root','generation':1})
                return {'session_id':session,'turn_id':turn,'attempt_id':attempt,'generation':1,'state':'queued'}
            return self.store._idem(db,principal,key,['hermes-root',request,frozen],create)

    def admit_root(self, attempt_id, *, accounts, external_running=0):
        """Accounts maps registered account IDs to persistent owner IDs (trusted config).

        Locks precede SQLite. Reservations, budget registration, and root claim commit
        together. Restart or unknown cleanup retains capacity; never timeout-release.
        """
        if not isinstance(accounts, dict) or not 1 <= len(accounts) <= 8 or type(external_running) is not int or external_running < 0:
            raise StoreError(422,'Invalid root admission configuration')
        with ExitStack() as stack:
            deadline = time.monotonic() + 5
            for identity in sorted(accounts):
                stack.enter_context(self.leases.account_lock(identity, timeout=max(0,deadline-time.monotonic())))
            with self.store._tx() as db:
                a = self.store._attempt(db,attempt_id)
                if a['execution_kind']!='hermes_root' or a['state']!='queued' or a['cancel_requested']:
                    raise StoreError(409,'Root is not admissible')
                if external_running or db.execute(f'SELECT 1 FROM attempts WHERE state IN ({_LIVE})').fetchone() or db.execute("SELECT 1 FROM workflow_roots WHERE state IN ('held','releasing','quarantined')").fetchone():
                    return None
                root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(attempt_id,)).fetchone()
                if accounts != json.loads(root['frozen'])['accounts']: raise StoreError(409,'Frozen account mapping conflict')
                client = db.execute('SELECT * FROM clients WHERE id=? AND revoked_at IS NULL',(root['owner_id'],)).fetchone()
                session = db.execute('SELECT * FROM sessions WHERE id=?',(root['session_id'],)).fetchone()
                if (not client or root['project_id'] not in json.loads(client['projects'])
                        or 'submit' not in json.loads(client['scopes']) or not session
                        or session['archived'] or session['owner_id']!=root['owner_id']
                        or session['project_id']!=root['project_id']):
                    raise StoreError(403,'Root authority revoked')
                for identity in sorted(accounts):
                    reservation = self.leases.reserve_in_transaction(db,identity,persistent_owner_id=accounts[identity])
                    db.execute('INSERT INTO workflow_accounts VALUES(?,?,?)',(attempt_id,identity,reservation.reservation_id))
                stamp = self.clock()
                from .routed_remediation import admission_budget
                policy, budget_admitted_at, deadline_at = admission_budget(db, root, stamp)
                scope = RootScope(root['owner_id'],root['project_id'],root['session_id'],root['turn_id'],attempt_id,a['generation'])
                self.budget.register_root(db,scope,policy,admitted_at=budget_admitted_at)
                self.budget.register_attempt(db,AttemptScope(scope,attempt_id,a['generation']))
                db.execute("UPDATE workflow_roots SET state='held',admitted_at=?,deadline_at=?,version=version+1 WHERE root_id=?",(stamp,deadline_at,attempt_id))
                db.execute("UPDATE attempts SET state='preparing',started_at=?,updated_at=? WHERE id=?",(now(),now(),attempt_id))
                self.store._event(db,a['session_id'],attempt_id,'attempt.state',{'state':'preparing','execution_kind':'hermes_root'})
                return self.store._attempt(db,attempt_id)

    def _owner_current(self, db, root_id):
        if os.getpid()!=self.leases.pid: return False
        rows=db.execute("""SELECT p.state,p.controller_instance_id,p.id,a.active_id FROM workflow_accounts w
         JOIN provider_reservations p ON p.id=w.reservation_id JOIN provider_accounts a ON a.account_id=w.account_id WHERE w.root_id=?""",(root_id,)).fetchall()
        return bool(rows) and all(r['state']=='held' and r['controller_instance_id']==self.leases.instance_id and r['id']==r['active_id'] for r in rows)

    def transition(self, attempt_id, state, *, expected_generation, **fields):
        def owned(db,attempt):
            if 'runtime_id' in fields:
                root=db.execute('SELECT state FROM workflow_roots WHERE root_id=?',(attempt.get('workflow_root_id'),)).fetchone()
                if not root or root['state']!='held':raise StoreError(409,'Routed runtime binding frozen')
            if state=='completed' and attempt.get('execution_kind')=='hermes_root':
                incomplete=db.execute("SELECT 1 FROM workflow_steps s LEFT JOIN workflow_step_gates g ON g.root_id=s.root_id AND g.step_id=s.step_id WHERE s.root_id=? AND (g.decision IS NULL OR g.decision!='pass') LIMIT 1",(attempt_id,)).fetchone()
                occupied=db.execute('SELECT child_attempt_id FROM workflow_roots WHERE root_id=?',(attempt_id,)).fetchone()
                pending=db.execute(f"SELECT 1 FROM attempts WHERE workflow_root_id=? AND execution_kind='hermes_child' AND state IN ({_PENDING}) LIMIT 1",(attempt_id,)).fetchone()
                if incomplete or pending or not occupied or occupied['child_attempt_id'] is not None:
                    raise StoreError(409,'Root completion requires all passing step gates and confirmed child cleanup')
            return attempt.get('execution_kind') in {'hermes_root','hermes_child'} and self._owner_current(db,attempt['workflow_root_id'])
        return self.store._transition(attempt_id,state,expected_generation=expected_generation,workflow_authority=owned,**fields)

    def authorize_parent(self, db, scope):
        """Use as RoleBroker authorization callback. Initial children are leaves."""
        if (db.execute('SELECT version FROM schema_version').fetchone()[0]==3
            and db.execute('SELECT 1 FROM workflow_delivery_intents WHERE root_id=?',(scope.root_attempt_id,)).fetchone()):
            return False
        row=db.execute(f"""SELECT 1 FROM workflow_roots w JOIN attempts a ON a.id=w.root_id JOIN clients c ON c.id=w.owner_id
         WHERE w.root_id=? AND a.id=? AND a.generation=? AND w.generation=a.generation
          AND w.owner_id=? AND w.project_id=? AND a.execution_kind='hermes_root' AND a.state IN ({_LIVE})
          AND a.cancel_requested=0 AND w.cancel_requested=0 AND w.state='held' AND w.deadline_at>?
          AND c.revoked_at IS NULL AND EXISTS(SELECT 1 FROM json_each(c.projects) WHERE value=w.project_id)""",
          (scope.root_attempt_id,scope.parent_attempt_id,scope.generation,scope.owner_id,scope.project_id,self.clock())).fetchone()
        if not row or not self._owner_current(db,scope.root_attempt_id): return False
        root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(scope.root_attempt_id,)).fetchone()
        budget_scope=RootScope(root['owner_id'],root['project_id'],root['session_id'],root['turn_id'],root['root_id'],root['generation'])
        return BudgetAuthority(AttemptScope(budget_scope,root['root_id'],root['generation'])).check(db,now=self.clock()).allowed

    def admit_request(self, broker, scope, pending_id, *, plan, step_id=None, input_revision_sha256=None):
        """Plan is a trusted frozen full ordered plan (never model arguments).

        No model readiness proof is inferred: all seats must have ready=True; caller
        binds profiles/qualification to the immutable root policy before calling.
        """
        body=encode(plan)
        if not isinstance(plan,list) or not 1<=len(plan)<=8 or len(body.encode())>65536 or any(not isinstance(s,dict) or s.get('ready') is not True or not isinstance(s.get('profile'),dict) for s in plan):
            raise StoreError(409,'Complete ready ordered plan required')
        with self.store._tx() as db:
            steps=db.execute('SELECT * FROM workflow_steps WHERE root_id=? ORDER BY ordinal',(scope.root_attempt_id,)).fetchall()
            step=None
            if steps:
                if not isinstance(input_revision_sha256,str) or not re.fullmatch('[0-9a-f]{64}',input_revision_sha256):raise StoreError(422,'Assigned input revision required')
                step=next((row for row in steps if row['step_id']==step_id),None)
                if step is None:raise StoreError(409,'Explicit frozen workflow step required')
                if step['request_id'] is not None and step['input_revision_sha256']!=input_revision_sha256:raise StoreError(409,'Assigned input revision is immutable')
                prior=db.execute('SELECT COUNT(*) FROM workflow_steps s LEFT JOIN workflow_step_gates g ON g.root_id=s.root_id AND g.step_id=s.step_id WHERE s.root_id=? AND s.ordinal<? AND (g.decision IS NULL OR g.decision!=?)',(scope.root_attempt_id,step['ordinal'],'pass')).fetchone()[0]
                if prior:raise StoreError(409,'Prior workflow step requires a passing gate')
                if len(plan)!=1 or encode(plan[0]['profile'])!=step['profile']:raise StoreError(409,'Workflow step profile mismatch')
                pending=db.execute('SELECT controller_request_id,payload FROM role_broker_pending WHERE id=?',(pending_id,)).fetchone()
                if not pending or json.loads(pending['payload'])['arguments']['role']!=step['role']:raise StoreError(409,'Workflow step role mismatch')
                if step['request_id'] is not None and step['request_id']!=pending['controller_request_id']:raise StoreError(409,'Workflow step already requested')
                if pending['controller_request_id'] is not None and step['request_id']!=pending['controller_request_id']:raise StoreError(409,'Native call already linked to another step')
                from .routed_progression import validate_stage_admission
                validate_stage_admission(db,scope,step,input_revision_sha256,json.loads(pending['payload']))
            elif step_id is not None or input_revision_sha256 is not None:raise StoreError(409,'No frozen workflow step')
            def create(db,pending):
                if not self.authorize_parent(db,scope): raise StoreError(403,'Parent authority denied')
                used=db.execute('SELECT COUNT(*) FROM workflow_seats s JOIN workflow_requests q ON q.id=s.request_id WHERE q.root_id=?',(scope.root_attempt_id,)).fetchone()[0]
                if used+len(plan)>32: raise StoreError(429,'Root seat capacity reached')
                request_id=uid()
                ordinal=db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM workflow_requests WHERE root_id=?',(scope.root_attempt_id,)).fetchone()[0]
                raw=db.execute('SELECT payload FROM role_broker_pending WHERE id=?',(pending_id,)).fetchone()[0]
                role=json.loads(raw)['arguments']['role']
                frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?',(scope.root_attempt_id,)).fetchone()[0])
                if frozen['role_plans'].get(role)!=plan: raise StoreError(409,'Frozen role plan conflict')
                db.execute('INSERT INTO workflow_requests VALUES(?,?,?,?,?,?,?,?)',(request_id,pending_id,scope.root_attempt_id,scope.generation,role,raw,body,ordinal))
                if step is not None:
                    db.execute('UPDATE workflow_steps SET request_id=?,input_revision_sha256=? WHERE root_id=? AND step_id=? AND request_id IS NULL',(request_id,input_revision_sha256,scope.root_attempt_id,step_id))
                root=self.store._attempt(db,scope.root_attempt_id)
                for index,seat in enumerate(plan):
                    sid,aid=uid(),uid()
                    db.execute('INSERT INTO workflow_seats VALUES(?,?,?,?)',(sid,request_id,index,encode(seat['profile'])))
                    stamp=now()
                    db.execute("INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at,execution_kind,workflow_root_id,workflow_parent_id,workflow_parent_generation,role_seat_id) VALUES(?,?,?,'hermes',1,'queued',?,?,'hermes_child',?,?,?,?)",(aid,root['session_id'],root['turn_id'],stamp,stamp,root['id'],root['id'],scope.generation,sid))
                    self.store._event(db,root['session_id'],aid,'attempt.state',{'state':'queued','execution_kind':'hermes_child','role_request_id':request_id,'seat_ordinal':index})
                return request_id
            return broker.admit_in_transaction(db,scope,pending_id,create)

    def claim_child(self, root_id):
        with self.store._tx() as db:
            root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
            if not root or not self._owner_current(db,root_id) or root['state']!='held' or root['child_attempt_id'] or root['cancel_requested'] or root['deadline_at']<=self.clock(): return None
            parent=self.store._attempt(db,root_id)
            if parent['state'] not in LIVE or parent['cancel_requested'] or parent['generation']!=root['generation']: return None
            client=db.execute('SELECT projects FROM clients WHERE id=? AND revoked_at IS NULL',(root['owner_id'],)).fetchone()
            if not client or root['project_id'] not in json.loads(client['projects']): return None
            child=db.execute("""SELECT a.id FROM attempts a JOIN workflow_seats s ON s.id=a.role_seat_id JOIN workflow_requests q ON q.id=s.request_id
             WHERE a.workflow_root_id=? AND a.execution_kind='hermes_child' AND a.state='queued' AND a.cancel_requested=0 AND a.workflow_parent_generation=? ORDER BY q.ordinal,s.ordinal LIMIT 1""",(root_id,root['generation'])).fetchone()
            if not child:return None
            scope=RootScope(root['owner_id'],root['project_id'],root['session_id'],root['turn_id'],root_id,root['generation'])
            if not BudgetAuthority(AttemptScope(scope,root_id,root['generation'])).check(db,now=self.clock()).allowed: return None
            self.budget.register_attempt(db,AttemptScope(scope,child['id'],1),parent=AttemptScope(scope,root_id,root['generation']),deadline_at=min(root['deadline_at'],self.clock()+4200))
            db.execute('UPDATE workflow_roots SET child_attempt_id=?,version=version+1 WHERE root_id=? AND child_attempt_id IS NULL',(child['id'],root_id))
            db.execute("UPDATE attempts SET state='preparing',started_at=?,updated_at=? WHERE id=?",(now(),now(),child['id']))
            self.store._event(db,root['session_id'],child['id'],'attempt.state',{'state':'preparing','execution_kind':'hermes_child'})
            return self.store._attempt(db,child['id'])

    def release_child(self, child_id, *, expected_generation, verifier):
        """Trusted reconciliation only; verifier runs without SQLite lock.

        This does NOT inspect Docker. Production integration must supply an exact
        ownership/absence inspector. No timer or terminal state alone frees a slot.
        """
        if not callable(verifier): raise StoreError(503,'Trusted cleanup verifier required')
        with self.store._connect() as db:
            child=self.store._attempt(db,child_id)
            if {'routed_decision','routed_stage_decision'} & set(child.get('result') or {}):
                raise StoreError(409,'Decision-managed child requires decision cleanup release')
            root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(child.get('workflow_root_id'),)).fetchone()
            if child.get('execution_kind')!='hermes_child' or child['generation']!=expected_generation or child['state'] not in TERMINAL or not root or root['state']!='held' or not self._owner_current(db,root['root_id']) or root['child_attempt_id']!=child_id:
                raise StoreError(409,'Child cleanup target changed')
            target=ChildCleanupTarget(root['root_id'],root['generation'],child_id,child['generation'],root['version'],child['runtime_id'])
        receipt=verifier(target)
        import re
        if type(receipt) is not ChildCleanupReceipt or receipt.target!=target or receipt.outcome!='confirmed_stopped' or not re.fullmatch('[0-9a-f]{64}',receipt.evidence_sha256):
            raise StoreError(409,'Child cleanup unconfirmed')
        with self.store._tx() as db:
            current=self.store._attempt(db,child_id)
            if not self._owner_current(db,target.root_id): raise StoreError(409,'Child cleanup owner changed')
            if current['generation']!=target.child_generation or current['state'] not in TERMINAL or current['runtime_id']!=target.runtime_id: raise StoreError(409,'Child cleanup target changed')
            changed=db.execute('UPDATE workflow_roots SET child_attempt_id=NULL,version=version+1 WHERE root_id=? AND generation=? AND child_attempt_id=? AND version=? AND state="held"',(target.root_id,target.root_generation,child_id,target.reservation_version)).rowcount
            if changed!=1: raise StoreError(409,'Child cleanup reservation changed')
            self.store._event(db,current['session_id'],child_id,'workflow.child_released',{'evidence_sha256':receipt.evidence_sha256})

    def child_assignment(self, child_id, *, expected_generation):
        """Controller projection; caller must still authorize a fresh runtime launch.

        Child task comes from the native role request, never the root's turn goal.
        Profile data is frozen provenance, not a claim of provider qualification.
        """
        with self.store._connect() as db:
            db.execute('BEGIN')
            child=self.store._attempt(db,child_id)
            if child.get('execution_kind')!='hermes_child' or child['generation']!=expected_generation:
                raise StoreError(409,'Child assignment fence mismatch')
            row=db.execute("""SELECT s.profile,s.ordinal,q.id AS request_id,q.role,q.payload,w.frozen,w.frozen_digest FROM workflow_seats s
             JOIN workflow_requests q ON q.id=s.request_id JOIN workflow_roots w ON w.root_id=q.root_id WHERE s.id=? AND q.root_id=?""",(child['role_seat_id'],child['workflow_root_id'])).fetchone()
            if not row:raise StoreError(409,'Child assignment missing')
            arguments=json.loads(row['payload'])['arguments']
            step=db.execute('SELECT step_id,ordinal,input_revision_sha256 FROM workflow_steps WHERE root_id=? AND request_id=?',(child['workflow_root_id'],row['request_id'])).fetchone()
            return {'attempt':child,'role_request_id':row['request_id'],'role':row['role'],'seat_ordinal':row['ordinal'],'workflow_step_id':step['step_id'] if step else None,'workflow_step_ordinal':step['ordinal'] if step else None,'input_revision_sha256':step['input_revision_sha256'] if step else None,
                    'task':arguments['task'],'context_refs':arguments['context_refs'],'profile':json.loads(row['profile']),
                    'workflow_snapshot':json.loads(row['frozen']),'workflow_digest':row['frozen_digest']}

    def record_step_gate(self, child_id, *, expected_generation, decision, revision_sha256,
                         reviewed_artifact_id, reviewed_artifact_sha256, evidence_sha256):
        """CONTROLLER ONLY verified-artifact gate; never consume model prose as authority.

        The artifact must be published by this exact child/generation with trusted
        reviewed_revision_sha256 metadata. Recording PASS additionally requires
        completed/verified lifecycle. Rejection is immutable and stops advancement.
        """
        if decision not in {'pass','reject'} or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in (revision_sha256,reviewed_artifact_sha256,evidence_sha256)):
            raise StoreError(422,'Invalid workflow gate receipt')
        with self.store._tx() as db:
            child=self.store._attempt(db,child_id)
            if child.get('execution_kind')!='hermes_child' or child['generation']!=expected_generation or child['state'] not in TERMINAL or not self._owner_current(db,child['workflow_root_id']):
                raise StoreError(409,'Workflow gate child fence mismatch')
            if decision=='pass' and (child['state']!='completed' or child['outcome']!='verified'):
                raise StoreError(409,'Passing gate requires verified child completion')
            step=db.execute('SELECT w.* FROM workflow_steps w JOIN workflow_seats s ON s.request_id=w.request_id WHERE s.id=? AND w.root_id=?',(child['role_seat_id'],child['workflow_root_id'])).fetchone()
            if not step:raise StoreError(409,'Child is not bound to a workflow step')
            if revision_sha256!=step['input_revision_sha256']:raise StoreError(409,'Workflow gate assigned revision mismatch')
            artifact=db.execute('SELECT * FROM artifacts WHERE id=? AND attempt_id=? AND session_id=?',(reviewed_artifact_id,child_id,child['session_id'])).fetchone()
            metadata=json.loads(artifact['metadata']) if artifact else {}
            if metadata.get('generation')!=expected_generation or metadata.get('sha256')!=reviewed_artifact_sha256 or metadata.get('reviewed_revision_sha256')!=revision_sha256:
                raise StoreError(409,'Workflow gate artifact binding mismatch')
            fields=(child['workflow_root_id'],step['step_id'],child_id,expected_generation,decision,revision_sha256,reviewed_artifact_id,reviewed_artifact_sha256,evidence_sha256)
            existing=db.execute('SELECT * FROM workflow_step_gates WHERE root_id=? AND step_id=?',fields[:2]).fetchone()
            if existing:
                if tuple(existing[name] for name in ('root_id','step_id','child_id','generation','decision','revision_sha256','artifact_id','artifact_sha256','evidence_sha256'))!=fields:raise StoreError(409,'Workflow gate is immutable')
                return dict(existing)
            db.execute('INSERT INTO workflow_step_gates VALUES(?,?,?,?,?,?,?,?,?,?)',(*fields,now()))
            self.store._event(db,child['session_id'],child_id,'workflow.step_gate',{'step_id':step['step_id'],'decision':decision,'revision_sha256':revision_sha256,'artifact_id':reviewed_artifact_id,'evidence_sha256':evidence_sha256})
            return dict(db.execute('SELECT * FROM workflow_step_gates WHERE root_id=? AND step_id=?',fields[:2]).fetchone())

    def bind_runtime(self, attempt_id, *, expected_generation, runtime_id):
        """Bind the routed job runtime once; verifier runtimes need separate ownership.

        A missing ID is not absence proof. Cleanup must also reconcile attempt/gen
        labeled creation operations, including a create whose response was lost.
        """
        if not isinstance(runtime_id,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}',runtime_id):
            raise StoreError(422,'Invalid routed runtime identity')
        current=self.store.get_attempt(attempt_id)
        if current.get('execution_kind') not in {'hermes_root','hermes_child'}:
            raise StoreError(409,'Routed runtime required')
        return self.transition(attempt_id,current['state'],expected_generation=expected_generation,runtime_id=runtime_id)

    def _child_launch_authority(self,db,child_id,generation):
        if type(generation) is not int or generation<1:raise StoreError(422,'Invalid child launch generation')
        child=self.store._attempt(db,child_id)
        root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(child.get('workflow_root_id'),)).fetchone()
        if (child.get('execution_kind')!='hermes_child' or child['generation']!=generation
            or child['state'] not in ('preparing','running') or child['cancel_requested'] or not root
            or root['state']!='held' or root['cancel_requested'] or root['child_attempt_id']!=child_id
            or not self._owner_current(db,root['root_id'])):raise StoreError(409,'Child launch authority unavailable')
        parent=self.store._attempt(db,child['workflow_parent_id'])
        client=db.execute('SELECT * FROM clients WHERE id=? AND revoked_at IS NULL',(root['owner_id'],)).fetchone()
        session=db.execute('SELECT owner_id,project_id FROM sessions WHERE id=?',(child['session_id'],)).fetchone()
        reservations=db.execute('SELECT w.account_id,a.persistent_owner_id FROM workflow_accounts w JOIN provider_accounts a ON a.account_id=w.account_id WHERE w.root_id=?',(root['root_id'],)).fetchall()
        if (parent['id']!=root['root_id'] or parent['execution_kind']!='hermes_root' or parent['state']!='running'
            or parent['cancel_requested'] or parent['generation']!=root['generation'] or parent['generation']!=child['workflow_parent_generation']
            or child['session_id']!=root['session_id'] or child['turn_id']!=root['turn_id']
            or not session or (session['owner_id'],session['project_id'])!=(root['owner_id'],root['project_id'])
            or not client or root['project_id'] not in json.loads(client['projects']) or 'submit' not in json.loads(client['scopes'])
            or {r['account_id']:r['persistent_owner_id'] for r in reservations}!=json.loads(root['frozen'])['accounts']):
            raise StoreError(409,'Child launch authority unavailable')
        scope=RootScope(root['owner_id'],root['project_id'],root['session_id'],root['turn_id'],root['root_id'],root['generation'])
        observed=self.clock()
        decision=BudgetAuthority(AttemptScope(scope,child_id,generation)).check(db,now=observed)
        if not decision.allowed or root['deadline_at']<=observed:raise StoreError(409,'Child launch budget authority unavailable')
        # Keep successful launch/collection authority observations durable without charging requests.
        from .inference_budget import _stamp
        db.execute('UPDATE inference_budget_roots SET high_water_us=? WHERE root_id=? AND high_water_us<?',(_stamp(observed),root['root_id'],_stamp(observed)))
        row=db.execute("""SELECT s.id AS seat_id,s.ordinal,s.profile,q.id AS request_id,q.role,q.payload,q.parent_generation,
          step.step_id,step.ordinal AS step_ordinal,step.input_revision_sha256
          FROM workflow_seats s JOIN workflow_requests q ON q.id=s.request_id
          JOIN workflow_steps step ON step.request_id=q.id AND step.root_id=q.root_id
          WHERE s.id=? AND q.root_id=?""",(child['role_seat_id'],root['root_id'])).fetchone()
        if not row or row['parent_generation']!=root['generation'] or not re.fullmatch('[0-9a-f]{64}',row['input_revision_sha256'] or ''):
            raise StoreError(409,'Frozen child launch assignment unavailable')
        assignment={'root_id':root['root_id'],'root_generation':root['generation'],'owner_id':root['owner_id'],
            'project_id':root['project_id'],'session_id':root['session_id'],'turn_id':root['turn_id'],
            'workflow_digest':root['frozen_digest'],**dict(row)}
        return child,root,assignment

    @staticmethod
    def _child_binding_record(row,*,may_start=False):
        body=json.loads(row['binding'])
        if hashlib.sha256(encode(body).encode()).hexdigest()!=row['binding_digest']:
            raise StoreError(409,'Child caller binding corrupted')
        return ChildCallerBinding(body['child_id'],body['generation'],body['runtime_id'],body['caller_spec_digest'],
            body['profile_digest'],body['input_revision_sha256'],body['assignment_digest'],row['binding_digest'],row['state'],may_start)

    def bind_child_caller(self,child_id,*,expected_generation,runtime_id,caller_spec,execution_profile,input_revision_sha256):
        """Controller only: persist binding after stopped inspection, before any start.

        The supplied spec/profile are trusted controller values, not Docker proof.
        Exact replay still requires current authority. No provider/account grant is issued.
        """
        from .routed_runtime import CallerSpec
        from .native_responses import NativeProfile
        from .inference_transport import PinnedCLI
        from .provider_protocol import provider_profile_digest
        if (type(caller_spec) is not CallerSpec or type(expected_generation) is not int or type(caller_spec.generation) is not int
            or not isinstance(runtime_id,str) or not re.fullmatch('[0-9a-f]{64}',runtime_id)
            or caller_spec.attempt_id!=child_id or caller_spec.generation!=expected_generation
            or not isinstance(input_revision_sha256,str) or not re.fullmatch('[0-9a-f]{64}',input_revision_sha256)):
            raise StoreError(422,'Invalid child caller binding')
        if type(execution_profile) is NativeProfile:
            effective=(execution_profile.provider,execution_profile.model,execution_profile.effort,'codex_responses')
        elif type(execution_profile) is PinnedCLI:
            effective=('claude-code',execution_profile.native_model,execution_profile.effort,'chat_completions')
        else:raise StoreError(422,'Unsupported child execution profile')
        profile_record=asdict(execution_profile)
        if type(execution_profile) is PinnedCLI:
            profile_record['path']=str(execution_profile.path)
        with _child_control_tx(self.store) as db:
            child,root,assignment=self._child_launch_authority(db,child_id,expected_generation)
            profile=json.loads(assignment['profile'])
            if (caller_spec.session_id!=child['session_id'] or assignment['input_revision_sha256']!=input_revision_sha256
                or tuple(profile.get(k) for k in ('provider','model','effort','transport'))!=effective):
                raise StoreError(409,'Child caller assignment differs')
            body={'child_id':child_id,'generation':expected_generation,'runtime_id':runtime_id,
                'caller_spec_digest':caller_spec.digest,'caller_spec':asdict(caller_spec),
                'profile_digest':provider_profile_digest(execution_profile),'execution_profile':profile_record,
                'input_revision_sha256':input_revision_sha256,'assignment':assignment,
                'assignment_digest':hashlib.sha256(encode(assignment).encode()).hexdigest()}
            raw=encode(body)
            if len(raw.encode())>65536:raise StoreError(422,'Child binding too large')
            digest=hashlib.sha256(raw.encode()).hexdigest()
            row=db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',(child_id,)).fetchone()
            if row:
                if row['state']=='fenced':raise StoreError(409,'Child caller execution fenced')
                if row['binding']!=raw or row['binding_digest']!=digest or row['controller_instance_id']!=self.leases.instance_id:
                    raise StoreError(409,'Child caller binding is immutable')
                if child['runtime_id']!=runtime_id:raise StoreError(409,'Child runtime identity differs')
                return self._child_binding_record(row)
            if child['state']!='preparing' or child['runtime_id'] not in (None,runtime_id):raise StoreError(409,'Child runtime already assigned')
            db.execute('UPDATE attempts SET runtime_id=?,updated_at=? WHERE id=?',(runtime_id,now(),child_id))
            db.execute("INSERT INTO workflow_child_launch VALUES(?,?,?,?,?,'bound',?,?)",(child_id,expected_generation,raw,digest,self.leases.instance_id,now(),now()))
            self.store._event(db,child['session_id'],child_id,'workflow.caller_bound',{'generation':expected_generation,'binding_digest':digest,'runtime_id':runtime_id})
            return self._child_binding_record(db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',(child_id,)).fetchone())

    def _checked_child_binding(self,db,child_id,generation,binding_digest):
        child,root,assignment=self._child_launch_authority(db,child_id,generation)
        row=db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',(child_id,)).fetchone()
        if (not row or row['generation']!=generation or row['binding_digest']!=binding_digest
            or row['controller_instance_id']!=self.leases.instance_id):raise StoreError(409,'Child caller fence differs')
        if row['state']=='fenced':raise StoreError(409,'Child caller execution fenced')
        result=self._child_binding_record(row)
        if result.runtime_id!=child['runtime_id'] or result.assignment_digest!=hashlib.sha256(encode(assignment).encode()).hexdigest():
            raise StoreError(409,'Child caller assignment changed')
        return child,row

    def begin_child_start(self,child_id,*,expected_generation,binding_digest):
        """Only bound->intent returns may_start=True. Intent replay never grants an RPC."""
        with _child_control_tx(self.store) as db:
            child,row=self._checked_child_binding(db,child_id,expected_generation,binding_digest)
            first=row['state']=='bound'
            if first:
                if child['state']!='preparing':raise StoreError(409,'Child is not preparing')
                db.execute("UPDATE workflow_child_launch SET state='start_intent',updated_at=? WHERE child_id=?",(now(),child_id))
                self.store._event(db,child['session_id'],child_id,'workflow.caller_start_intent',{'binding_digest':binding_digest})
            return self._child_binding_record(db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',(child_id,)).fetchone(),may_start=first)

    def check_child_start_authority(self,child_id,*,expected_generation,binding_digest):
        """Fresh authority predicate, never permission to retry an unknown launch."""
        with _child_control_tx(self.store) as db:
            _,row=self._checked_child_binding(db,child_id,expected_generation,binding_digest)
            if row['state'] not in ('start_intent','started'):raise StoreError(409,'Child start intent missing')
            return self._child_binding_record(row)

    def confirm_child_started(self,child_id,*,expected_generation,binding_digest):
        """Driver must have observed exact runtime running; this records that assertion."""
        with _child_control_tx(self.store) as db:
            child,row=self._checked_child_binding(db,child_id,expected_generation,binding_digest)
            if row['state'] not in ('start_intent','started'):raise StoreError(409,'Child start intent missing')
            if row['state']=='start_intent':
                db.execute("UPDATE workflow_child_launch SET state='started',updated_at=? WHERE child_id=?",(now(),child_id))
                db.execute("UPDATE attempts SET state='running',updated_at=? WHERE id=?",(now(),child_id))
                self.store._event(db,child['session_id'],child_id,'workflow.caller_started',{'binding_digest':binding_digest})
            return self._child_binding_record(db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',(child_id,)).fetchone())

    def read_child_launch(self,child_id,*,expected_generation):
        """Durable observation only; never adopts ownership or grants launch authority."""
        if type(expected_generation) is not int or expected_generation<1:raise StoreError(422,'Invalid child launch generation')
        with _child_control_tx(self.store,write=False) as db:
            child=self.store._attempt(db,child_id)
            if child.get('execution_kind')!='hermes_child' or child['generation']!=expected_generation:
                raise StoreError(409,'Child caller generation changed')
            row=db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',(child_id,)).fetchone()
            if not row:return None
            if row['generation']!=expected_generation:raise StoreError(409,'Child caller generation changed')
            return self._child_binding_record(row)

    def fence_child_execution(self,child_id,*,expected_generation,binding_digest):
        """Revoke exact owned child execution even after cancellation/deadline denial.

        Does not stop Docker or release any occupancy/account reservation.
        Replacement generations and account owners are never modified.
        """
        if type(expected_generation) is not int or expected_generation<1:raise StoreError(422,'Invalid child launch generation')
        with _child_control_tx(self.store) as db:
            child=self.store._attempt(db,child_id)
            row=db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',(child_id,)).fetchone()
            root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(child.get('workflow_root_id'),)).fetchone()
            if (child.get('execution_kind')!='hermes_child' or child['generation']!=expected_generation
                or not row or row['generation']!=expected_generation or row['binding_digest']!=binding_digest
                or row['controller_instance_id']!=self.leases.instance_id or not root
                or not self._owner_current(db,root['root_id'])):raise StoreError(409,'Child teardown ownership differs')
            accounts=db.execute('SELECT w.account_id,a.persistent_owner_id FROM workflow_accounts w JOIN provider_accounts a ON a.account_id=w.account_id WHERE w.root_id=?',(root['root_id'],)).fetchall()
            if {r['account_id']:r['persistent_owner_id'] for r in accounts}!=json.loads(root['frozen'])['accounts']:
                raise StoreError(409,'Child teardown account owner differs')
            value=self._child_binding_record(row);body=json.loads(row['binding'])
            parent=self.store._attempt(db,root['root_id'])
            if (value.runtime_id!=child['runtime_id'] or root['child_attempt_id']!=child_id
                or root['generation']!=body['assignment']['root_generation'] or parent['generation']!=root['generation']):
                raise StoreError(409,'Child teardown generation differs')
            if row['state']!='fenced':
                from .provider_leases import revoke_attempt
                revoke_attempt(db,child_id,'routed_caller_fenced')
                db.execute("UPDATE workflow_child_launch SET state='fenced',updated_at=? WHERE child_id=?",(now(),child_id))
                self.store._event(db,child['session_id'],child_id,'workflow.caller_fenced',{'binding_digest':binding_digest})
            return self._child_binding_record(db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',(child_id,)).fetchone())

    def _delivery_intent(self, db, root_id, expected_generation, intent_sha256):
        from .delivery_schema import read_intent
        from .routed_delivery_policy import RootDeliveryProof, validate_db
        if type(intent_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',intent_sha256):
            raise StoreError(422,'Invalid delivery intent digest')
        intent=read_intent(db,root_id,expected_generation)
        if intent is None or intent['proof_sha256']!=intent_sha256:
            raise StoreError(409,'Delivery intent binding changed')
        try:
            envelope=json.loads(intent['proof_json'])
            base=db.execute('SELECT * FROM workflow_delivery_bases WHERE root_id=?',(root_id,)).fetchone()
            root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
            echoes={key:intent[key] for key in ('root_id','generation','expected_delivery_version',
                'base_revision','base_artifact','selected_revision_sha256','kind')}
            if (set(envelope)!={'schema_version',*echoes,'base_last_artifact','workflow_proof','artifact'}
                or type(envelope['schema_version']) is not int or envelope['schema_version']!=1
                or encode({key:envelope[key] for key in echoes})!=encode(echoes)
                or envelope['base_last_artifact']!=base['last_artifact_id']):raise ValueError
            proof=RootDeliveryProof.from_json(encode(envelope['workflow_proof']))
            public=proof.public_record()
            expected={key:root[key] for key in ('root_id','generation','session_id','owner_id','project_id','turn_id')}
            expected.update(workflow_sha256=root['frozen_digest'],kind=intent['kind'],
                selected_revision_sha256=intent['selected_revision_sha256'])
            if encode({key:public[key] for key in expected})!=encode(expected):raise ValueError
            artifact=envelope['artifact']
            artifact_keys={'id','session_id','attempt_id','generation','path','storage_path','sha256','bytes',
                'mime','provenance','kind','outcome','selected_revision_sha256','workflow_sha256','delivery_version'}
            artifact_expected={'id':'delivery-'+hashlib.sha256((root_id+':'+str(expected_generation)).encode()).hexdigest(),
                'session_id':root['session_id'],'attempt_id':root_id,'generation':expected_generation,
                'path':f'routed/{root_id}/{expected_generation}/delivery.json','mime':'application/json',
                'provenance':'controller_root_delivery','kind':intent['kind'],'outcome':public['outcome'],
                'selected_revision_sha256':intent['selected_revision_sha256'],
                'workflow_sha256':root['frozen_digest'],'delivery_version':intent['expected_delivery_version']+1}
            if (type(artifact) is not dict or set(artifact)!=artifact_keys
                or encode({k:artifact[k] for k in artifact_expected})!=encode(artifact_expected)
                or type(artifact['bytes']) is not int or not 0<artifact['bytes']<=256*1024
                or type(artifact['sha256']) is not str or re.fullmatch('[0-9a-f]{64}',artifact['sha256']) is None
                or type(artifact['storage_path']) is not str or not os.path.isabs(artifact['storage_path'])
                or len(artifact['storage_path'].encode())>4096 or any(ord(c)<32 for c in artifact['storage_path'])):raise ValueError
            validate_db(db,proof)
        except (ValueError,TypeError,KeyError,AttributeError,RecursionError):
            raise StoreError(409,'Delivery intent semantic binding changed') from None
        return intent,public

    def _delivery_cleanup_view(self, db, root_id, expected_generation, intent_sha256):
        _,public=self._delivery_intent(db,root_id,expected_generation,intent_sha256)
        root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
        attempt=self.store._attempt(db,root_id)
        cancelled=attempt['state']=='cancelled' and attempt['cancel_requested']
        if (attempt.get('execution_kind')!='hermes_root' or type(expected_generation) is not int
            or root['generation']!=expected_generation or attempt['generation']!=expected_generation
            or (attempt['state']!='verifying' and not cancelled) or root['child_attempt_id'] is not None):
            raise StoreError(409,'Delivery cleanup requires verifying root and cleared child occupancy')
        rows=db.execute('SELECT * FROM attempts WHERE workflow_root_id=? ORDER BY id LIMIT 34',(root_id,)).fetchall()
        if not 1<=len(rows)<=33 or any(r['id']!=root_id and r['state'] not in TERMINAL for r in rows):
            raise StoreError(409,'Delivery cleanup requires terminal children')
        references=public['decision_refs']
        steps=db.execute('SELECT * FROM workflow_steps WHERE root_id=? ORDER BY ordinal LIMIT 33',(root_id,)).fetchall()
        children={row['id']:row for row in rows if row['id']!=root_id}
        if (not 1<=len(steps)<=32 or len(references)!=len(steps) or len(children)!=len(steps)
            or {ref['child_id'] for ref in references}!=set(children)):
            raise StoreError(409,'Delivery child membership changed')
        for step,ref in zip(steps,references):
            child=children[ref['child_id']]
            task=step['role']=='acceptance_verification'
            event_type='workflow.decided_child_released' if task else 'workflow.stage_child_released'
            events=db.execute("SELECT type,payload FROM events WHERE attempt_id=? AND type IN ('workflow.stage_child_released','workflow.decided_child_released') LIMIT 2",(child['id'],)).fetchall()
            if len(events)!=1:raise StoreError(409,'Delivery child release proof missing')
            try:
                payload=json.loads(events[0]['payload']);target=payload['target']
                pointer=json.loads(child['result']).get('routed_decision' if task else 'routed_stage_decision')
                keys={'schema_version','decision_sha256','target','outcome','caller_provider_cleanup_sha256',
                    'accounts_retained','controller_instance_id','receipt_sha256'}
                if task:keys|={'scope','verifier_cleanup'}
                seat=db.execute('SELECT request_id FROM workflow_seats WHERE id=?',(child['role_seat_id'],)).fetchone()
                if (type(pointer) is not dict or pointer!={'artifact_id':ref['artifact_id'],'sha256':ref['sha256']}
                    or child['state']!='completed' or child['generation']!=ref['generation']
                    or (ref['step_id'],ref['role'],ref['input_revision_sha256'])!=(step['step_id'],step['role'],step['input_revision_sha256'])
                    or seat is None or seat['request_id']!=step['request_id']
                    or events[0]['type']!=event_type or set(payload)!=keys
                    or type(payload['schema_version']) is not int or payload['schema_version']!=1
                    or payload['decision_sha256']!=pointer['sha256']
                    or payload['outcome']!='confirmed_stopped'
                    or set(target)!={'root_id','root_generation','child_id','child_generation','reservation_version','runtime_id'}
                    or encode({k:v for k,v in target.items() if k!='reservation_version'})!=encode({
                        'root_id':root_id,'root_generation':expected_generation,'child_id':child['id'],
                        'child_generation':child['generation'],'runtime_id':child['runtime_id']})
                    or type(target['reservation_version']) is not int or target['reservation_version']<1
                    or type(payload['caller_provider_cleanup_sha256']) is not str
                    or re.fullmatch('[0-9a-f]{64}',payload['caller_provider_cleanup_sha256']) is None
                    or payload['controller_instance_id']!=self.leases.instance_id
                    or payload['receipt_sha256']!=ref['release_sha256']
                    or payload['receipt_sha256']!=hashlib.sha256(encode({k:v for k,v in payload.items() if k!='receipt_sha256'}).encode()).hexdigest()):raise ValueError
            except (ValueError,KeyError,TypeError):raise StoreError(409,'Delivery child release proof changed') from None
        bindings=tuple(AttemptCleanupBinding(r['id'],r['generation'],r['execution_kind'],r['state'],r['runtime_id']) for r in rows)
        reservations=db.execute('''SELECT p.*,a.persistent_owner_id,a.active_id FROM workflow_accounts w
          JOIN provider_reservations p ON p.id=w.reservation_id JOIN provider_accounts a ON a.account_id=w.account_id
          WHERE w.root_id=? ORDER BY p.account_id LIMIT 9''',(root_id,)).fetchall()
        if not 1<=len(reservations)<=8 or {r['account_id']:r['persistent_owner_id'] for r in reservations}!=json.loads(root['frozen'])['accounts']:
            raise StoreError(409,'Delivery account binding changed')
        values=tuple(Reservation(r['account_id'],r['persistent_owner_id'],r['id'],r['epoch'],r['controller_instance_id']) for r in reservations)
        for ref in references:
            release=db.execute("SELECT payload FROM events WHERE attempt_id=? AND type IN ('workflow.stage_child_released','workflow.decided_child_released')",(ref['child_id'],)).fetchone()
            if encode(json.loads(release['payload'])['accounts_retained'])!=encode([asdict(value) for value in values]):
                raise StoreError(409,'Delivery child release account binding changed')
        return root,bindings,values,reservations

    def _check_delivery_cleanup_target(self, db, target, intent_sha256):
        root,bindings,current,rows=self._delivery_cleanup_view(db,target.root_id,target.generation,intent_sha256)
        saved=db.execute('SELECT * FROM workflow_root_cleanup WHERE root_id=?',(target.root_id,)).fetchone()
        # Cancellation can alter the root's logical state after this immutable
        # physical target froze. It cannot alter any runtime or child identity.
        original={v.attempt_id:v for v in target.attempts}
        comparable=tuple(AttemptCleanupBinding(v.attempt_id,v.generation,v.execution_kind,
            'verifying' if (v.attempt_id==target.root_id and v.state=='cancelled'
                and original.get(v.attempt_id) and original[v.attempt_id].state=='verifying') else v.state,v.runtime_id)
            for v in bindings)
        if (os.getpid()!=self.leases.pid or root['state']!='releasing' or root['version']!=target.reservation_version
            or comparable!=target.attempts or current!=target.reservations or not saved
            or saved['target']!=encode(asdict(target)) or target.controller_instance_id!=self.leases.instance_id
            or any(r.controller_instance_id!=self.leases.instance_id or row['active_id']!=r.reservation_id
                or row['state'] not in {'held','cleaning','quarantined'} for row,r in zip(rows,current))):
            raise StoreError(409,'Delivery cleanup target changed')
        return root,bindings,current,rows,saved

    def _delivery_cleanup_proof(self, db, target, intent_sha256):
        root,bindings,reservations,rows,saved=self._check_delivery_cleanup_target(db,target,intent_sha256)
        if saved['runtime_receipt'] is None:raise StoreError(409,'Delivery runtime cleanup unconfirmed')
        _runtime_proof(saved['runtime_receipt'],target)
        providers={r.reservation_id:self._released_provider_proof(row,r,target.inspector_id)
                   for row,r in zip(rows,reservations)}
        return {'root_id':target.root_id,'generation':target.generation,'cleanup_id':target.cleanup_id,
            'intent_sha256':intent_sha256,'target_sha256':hashlib.sha256(encode(asdict(target)).encode()).hexdigest(),
            'runtime_receipt_sha256':hashlib.sha256(saved['runtime_receipt'].encode()).hexdigest(),
            'provider_receipt_sha256':providers,'fences_retained':True}

    def prepare_delivery_cleanup(self, root_id, *, expected_generation, intent_sha256, verifier):
        """Prove physical cleanup while retaining accounts and root capacity.

        The trusted verifier must inspect/reconcile this exact immutable target;
        it must not create or restart jobs. An exception or lost acknowledgment
        retains the target and ownership. A later call may invoke the verifier
        again only for that same target until its proof is durably recorded.
        Once recorded, retries validate the proof without invoking the verifier.
        Cancellation permits cleanup but never commit_delivery_cleanup.
        """
        if not callable(verifier):raise StoreError(503,'Trusted root cleanup verifier required')
        if os.getpid()!=self.leases.pid:raise StoreError(409,'Delivery cleanup controller changed')
        with self.store._tx() as db:
            _,_,reservations,_=self._delivery_cleanup_view(db,root_id,expected_generation,intent_sha256)
        with ExitStack() as locks:
            deadline=time.monotonic()+5
            for reservation in reservations:
                locks.enter_context(self.leases.account_lock(reservation.account_id,timeout=max(.001,deadline-time.monotonic())))
            with self.store._tx() as db:
                root,bindings,current,rows=self._delivery_cleanup_view(db,root_id,expected_generation,intent_sha256)
                if current!=reservations or any(r.controller_instance_id!=self.leases.instance_id
                    or row['active_id']!=r.reservation_id or row['state'] not in {'held','cleaning','quarantined'}
                    for row,r in zip(rows,current)):
                    raise StoreError(409,'Delivery cleanup requires current account owner')
                prior=db.execute('SELECT * FROM workflow_root_cleanup WHERE root_id=?',(root_id,)).fetchone()
                if prior:
                    target=_cleanup_target(prior['target'],delivery_root_id=root_id)
                    self._check_delivery_cleanup_target(db,target,intent_sha256)
                    if prior['release_receipt'] is not None:raise StoreError(409,'Delivery cleanup already committed')
                    runtime_receipt=prior['runtime_receipt']
                else:
                    if root['state']!='held':raise StoreError(409,'Delivery cleanup state changed')
                    target=RootCleanupTarget(root_id,expected_generation,root['version']+1,self.leases.instance_id,
                        uid(),self.leases._now(),bindings,current,self.leases.inspector_id)
                    changed=db.execute("UPDATE workflow_roots SET state='releasing',version=version+1 WHERE root_id=? AND version=? AND state='held'",
                        (root_id,root['version'])).rowcount
                    if changed!=1:raise StoreError(409,'Delivery cleanup reservation changed')
                    db.execute('INSERT INTO workflow_root_cleanup(root_id,generation,controller_instance_id,cleanup_id,target,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (root_id,expected_generation,self.leases.instance_id,target.cleanup_id,encode(asdict(target)),now(),now()))
                    self.store._event(db,root['session_id'],root_id,'workflow.root_cleanup_started',
                        {'cleanup_id':target.cleanup_id,'generation':expected_generation,'delivery_intent_sha256':intent_sha256})
                    runtime_receipt=None
            if runtime_receipt is None:
                try:receipt=verifier(target)
                except Exception:raise StoreError(409,'Delivery runtime cleanup unconfirmed') from None
                if (type(receipt) is not RootCleanupReceipt or receipt.target!=target or receipt.outcome not in {'terminated','fenced'}
                    or type(receipt.observed_at) not in (int,float) or not target.frozen_at<=receipt.observed_at<=self.leases._now()
                    or type(receipt.evidence_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',receipt.evidence_sha256)):
                    raise StoreError(409,'Delivery runtime cleanup unconfirmed')
                runtime_receipt=encode(asdict(receipt))
                with self.store._tx() as db:
                    self._check_delivery_cleanup_target(db,target,intent_sha256)
                    db.execute('UPDATE workflow_root_cleanup SET runtime_receipt=?,updated_at=? WHERE root_id=?',
                        (runtime_receipt,now(),root_id))
            else:
                _runtime_proof(runtime_receipt,target)
            for reservation in target.reservations:
                with self.store._tx() as db:
                    _,_,_,rows,_=self._check_delivery_cleanup_target(db,target,intent_sha256)
                    row=next(r for r in rows if r['id']==reservation.reservation_id)
                    proven=row['state']=='cleaning' and row['reason']=='root_cleanup_verified'
                    if proven:self._released_provider_proof(row,reservation,target.inspector_id)
                if not proven:self.leases.cleanup_owner(reservation,retain_fence=True)
            with self.store._tx() as db:
                return self._delivery_cleanup_proof(db,target,intent_sha256)

    def commit_delivery_cleanup(self, db, root_id, *, expected_generation, intent_sha256):
        """Release only within the caller's delivery/root-completion transaction.

        No locks, inspection callbacks, file reads, or provider operations occur.
        Rolling back this transaction retains all prepared physical-cleanup fences.
        """
        return self._commit_delivery_cleanup(db,root_id,expected_generation,intent_sha256,cancelled=False)

    def commit_cancelled_delivery_cleanup(self, db, root_id, *, expected_generation, intent_sha256):
        """Release proven cancelled work; never changes session delivery or outcome."""
        return self._commit_delivery_cleanup(db,root_id,expected_generation,intent_sha256,cancelled=True)

    def _commit_delivery_cleanup(self, db, root_id, expected_generation, intent_sha256, *, cancelled):
        if not db.in_transaction or os.path.realpath(db.execute('PRAGMA database_list').fetchone()[2])!=str(self.store.path.resolve()):
            raise StoreError(409,'Delivery cleanup transaction required')
        self._delivery_intent(db,root_id,expected_generation,intent_sha256)
        attempt=self.store._attempt(db,root_id)
        root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
        allowed=(attempt['state']=='cancelled' and attempt['cancel_requested']) if cancelled else (
            attempt['state']=='verifying' and not attempt['cancel_requested'] and not root['cancel_requested'])
        if not allowed:
            raise StoreError(409,'Delivery promotion authority unavailable')
        saved=db.execute('SELECT * FROM workflow_root_cleanup WHERE root_id=?',(root_id,)).fetchone()
        if not saved or saved['release_receipt'] is not None:raise StoreError(409,'Delivery cleanup not prepared')
        target=_cleanup_target(saved['target'],delivery_root_id=root_id)
        proof=self._delivery_cleanup_proof(db,target,intent_sha256)
        for reservation in target.reservations:
            changed=db.execute('UPDATE provider_accounts SET active_id=NULL WHERE account_id=? AND active_id=?',
                (reservation.account_id,reservation.reservation_id)).rowcount
            if changed!=1:raise StoreError(409,'Delivery provider ownership changed')
            changed=db.execute("UPDATE provider_reservations SET state='released',reason=NULL WHERE id=? AND state='cleaning' AND reason='root_cleanup_verified'",
                (reservation.reservation_id,)).rowcount
            if changed!=1:raise StoreError(409,'Delivery provider cleanup changed')
        final={**proof,'fences_retained':False,'release_purpose':'cancelled_delivery' if cancelled else 'delivery','released_at':now()}
        db.execute('UPDATE workflow_root_cleanup SET release_receipt=?,updated_at=? WHERE root_id=?',
            (encode(final),now(),root_id))
        changed=db.execute("UPDATE workflow_roots SET state='released',version=version+1 WHERE root_id=? AND generation=? AND version=? AND state='releasing'",
            (root_id,expected_generation,target.reservation_version)).rowcount
        if changed!=1:raise StoreError(409,'Delivery cleanup reservation changed')
        self.store._event(db,root['session_id'],root_id,'workflow.root_released',final)
        return final

    def _root_cleanup_view(self, db, root_id, expected_generation):
        root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
        attempt=self.store._attempt(db,root_id)
        if not root or attempt.get('execution_kind')!='hermes_root' or type(expected_generation) is not int or attempt['generation']!=expected_generation or root['generation']!=expected_generation or attempt['state'] not in TERMINAL or root['child_attempt_id'] is not None:
            raise StoreError(409,'Root cleanup requires terminal root and cleared child occupancy')
        rows=db.execute("SELECT * FROM attempts WHERE workflow_root_id=? ORDER BY id",(root_id,)).fetchall()
        if any(r['state'] not in TERMINAL for r in rows):raise StoreError(409,'Root cleanup requires terminal children')
        bindings=tuple(AttemptCleanupBinding(r['id'],r['generation'],r['execution_kind'],r['state'],r['runtime_id']) for r in rows)
        reservations=db.execute("""SELECT p.*,a.persistent_owner_id,a.active_id FROM workflow_accounts w
         JOIN provider_reservations p ON p.id=w.reservation_id JOIN provider_accounts a ON a.account_id=w.account_id
         WHERE w.root_id=? ORDER BY p.account_id""",(root_id,)).fetchall()
        expected=json.loads(root['frozen'])['accounts']
        if {r['account_id']:r['persistent_owner_id'] for r in reservations}!=expected:
            raise StoreError(409,'Root account binding changed')
        values=tuple(Reservation(r['account_id'],r['persistent_owner_id'],r['id'],r['epoch'],r['controller_instance_id']) for r in reservations)
        return root,bindings,values,reservations

    def _released_provider_proof(self, row, reservation, inspector_id):
        if row['state'] not in {'released','cleaning'} or not row['cleanup_receipt'] or (row['state']=='cleaning' and row['reason']!='root_cleanup_verified'):
            raise StoreError(409,'Provider cleanup is unconfirmed')
        try:
            proof=json.loads(row['cleanup_receipt'])
            if proof['target']['cleanup_id']!=row['cleanup_id'] or proof['target']['frozen_at']!=row['frozen_at'] or type(proof['observed_at']) not in (int,float) or not math.isfinite(proof['observed_at']) or proof['observed_at']<row['frozen_at'] or proof['target']['scope']!='owner' or proof['target']['reservation']!=asdict(reservation) or proof['outcome'] not in {'terminated','fenced'} or proof['inspector_id']!=inspector_id or not re.fullmatch('[0-9a-f]{64}',proof['evidence_sha256']):
                raise ValueError
        except (ValueError,KeyError,TypeError):raise StoreError(409,'Provider cleanup proof does not match frozen owner') from None
        return hashlib.sha256(row['cleanup_receipt'].encode()).hexdigest()

    def release_root(self, root_id, *, expected_generation, verifier):
        """Release root capacity only after caller and exact provider cleanup proof.

        Uses existing ProviderLeases cleanup_owner/dispatcher proof. Callbacks and
        provider cleanup run outside DB transactions, under ordered account locks.
        Current owner performs cleanup. A successor may finalize only durable
        all-proven cleanup, with no callbacks or adoption of unproven reservations.
        """
        if not callable(verifier):raise StoreError(503,'Trusted root cleanup verifier required')
        with self.store._connect() as db:
            root,bindings,reservations,rows=self._root_cleanup_view(db,root_id,expected_generation)
            prior=db.execute('SELECT * FROM workflow_root_cleanup WHERE root_id=?',(root_id,)).fetchone()
            if root['state']=='released':
                if not prior or not prior['release_receipt']:raise StoreError(409,'Released root proof missing')
                target=_cleanup_target(prior['target'])
                if target.generation!=expected_generation or target.attempts!=bindings or target.reservations!=reservations:raise StoreError(409,'Released root target changed')
                return json.loads(prior['release_receipt'])
        if os.getpid()!=self.leases.pid:raise StoreError(409,'Root cleanup controller changed')
        with ExitStack() as locks:
            deadline=time.monotonic()+5
            for reservation in reservations:
                locks.enter_context(self.leases.account_lock(reservation.account_id,timeout=max(.001,deadline-time.monotonic())))
            with self.store._tx() as db:
                root,bindings,current,rows=self._root_cleanup_view(db,root_id,expected_generation)
                if current!=reservations:raise StoreError(409,'Root account binding changed')
                prior=db.execute('SELECT * FROM workflow_root_cleanup WHERE root_id=?',(root_id,)).fetchone()
                if root['state']=='released':
                    if not prior or not prior['release_receipt']:raise StoreError(409,'Released root proof missing')
                    target=_cleanup_target(prior['target'])
                    if target.attempts!=bindings or target.reservations!=current:raise StoreError(409,'Released root target changed')
                    return json.loads(prior['release_receipt'])
                foreign=any(r.controller_instance_id!=self.leases.instance_id for r in current)
                if foreign and (not prior or not prior['runtime_receipt']):raise StoreError(409,'Root cleanup requires current owner')
                for row,reservation in zip(rows,current):
                    if row['state']=='released':
                        if not prior:raise StoreError(409,'Provider cleanup target missing')
                        self._released_provider_proof(row,reservation,_cleanup_target(prior['target']).inspector_id)
                    elif row['active_id']!=reservation.reservation_id or row['state'] not in {'held','cleaning','quarantined'}:
                        raise StoreError(409,'Root provider ownership changed')
                if prior:
                    target=_cleanup_target(prior['target'])
                    if root['state']!='releasing' or root['version']!=target.reservation_version or target.attempts!=bindings or target.reservations!=current:
                        raise StoreError(409,'Root cleanup target changed')
                else:
                    if root['state']!='held':raise StoreError(409,'Root cleanup state changed')
                    target=RootCleanupTarget(root_id,expected_generation,root['version']+1,self.leases.instance_id,uid(),self.leases._now(),bindings,current,self.leases.inspector_id)
                    db.execute("UPDATE workflow_roots SET state='releasing',version=version+1 WHERE root_id=? AND version=?",(root_id,root['version']))
                    db.execute('INSERT INTO workflow_root_cleanup(root_id,generation,controller_instance_id,cleanup_id,target,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(root_id,expected_generation,self.leases.instance_id,target.cleanup_id,encode(asdict(target)),now(),now()))
                    self.store._event(db,root['session_id'],root_id,'workflow.root_cleanup_started',{'cleanup_id':target.cleanup_id,'generation':expected_generation})
                runtime_receipt=prior['runtime_receipt'] if prior else None
                if runtime_receipt is not None:_runtime_proof(runtime_receipt,target)
                if foreign:
                    for row,reservation in zip(rows,current):self._released_provider_proof(row,reservation,target.inspector_id)
            if runtime_receipt is None:
                try:receipt=verifier(target)
                except Exception:raise StoreError(409,'Root runtime cleanup unconfirmed') from None
                if type(receipt) is not RootCleanupReceipt or receipt.target!=target or receipt.outcome not in {'terminated','fenced'} or type(receipt.observed_at) not in (int,float) or not target.frozen_at<=receipt.observed_at<=self.leases._now() or not isinstance(receipt.evidence_sha256,str) or not re.fullmatch('[0-9a-f]{64}',receipt.evidence_sha256):
                    raise StoreError(409,'Root runtime cleanup unconfirmed')
                runtime_receipt=encode(asdict(receipt))
                with self.store._tx() as db:
                    self._check_root_cleanup_target(db,target)
                    db.execute('UPDATE workflow_root_cleanup SET runtime_receipt=?,updated_at=? WHERE root_id=?',(runtime_receipt,now(),root_id))
            for reservation in target.reservations:
                with self.store._connect() as db:
                    self._check_root_cleanup_target(db,target)
                    row=db.execute('SELECT * FROM provider_reservations WHERE id=?',(reservation.reservation_id,)).fetchone()
                    already_released=row and (row['state']=='released' or (row['state']=='cleaning' and row['reason']=='root_cleanup_verified'))
                    if already_released:self._released_provider_proof(row,reservation,target.inspector_id)
                if not already_released:
                    self.leases.cleanup_owner(reservation,retain_fence=True)
            with self.store._tx() as db:
                root,bindings,current,rows=self._check_root_cleanup_target(db,target)
                providers={r.reservation_id:self._released_provider_proof(row,r,target.inspector_id) for row,r in zip(rows,current)}
                for row,reservation in zip(rows,current):
                    if row['state']=='released':continue
                    changed=db.execute('UPDATE provider_accounts SET active_id=NULL WHERE account_id=? AND active_id=?',(reservation.account_id,reservation.reservation_id)).rowcount
                    if changed!=1:raise StoreError(409,'Root provider ownership changed')
                    db.execute("UPDATE provider_reservations SET state='released',reason=NULL WHERE id=?",(reservation.reservation_id,))
                final={'root_id':root_id,'generation':expected_generation,'cleanup_id':target.cleanup_id,
                       'runtime_receipt_sha256':hashlib.sha256(runtime_receipt.encode()).hexdigest(),
                       'provider_receipt_sha256':providers,'released_at':now()}
                db.execute('UPDATE workflow_root_cleanup SET release_receipt=?,updated_at=? WHERE root_id=?',(encode(final),now(),root_id))
                changed=db.execute("UPDATE workflow_roots SET state='released',version=version+1 WHERE root_id=? AND version=? AND state='releasing'",(root_id,target.reservation_version)).rowcount
                if changed!=1:raise StoreError(409,'Root cleanup reservation changed')
                self.store._event(db,root['session_id'],root_id,'workflow.root_released',final)
                return final

    def _check_root_cleanup_target(self, db, target):
        root,bindings,current,rows=self._root_cleanup_view(db,target.root_id,target.generation)
        saved=db.execute('SELECT target FROM workflow_root_cleanup WHERE root_id=?',(target.root_id,)).fetchone()
        if os.getpid()!=self.leases.pid or root['state']!='releasing' or root['version']!=target.reservation_version or bindings!=target.attempts or current!=target.reservations or not saved or saved['target']!=encode(asdict(target)):
            raise StoreError(409,'Root cleanup target changed')
        if target.controller_instance_id!=self.leases.instance_id:
            receipt=db.execute('SELECT runtime_receipt FROM workflow_root_cleanup WHERE root_id=?',(target.root_id,)).fetchone()[0]
            _runtime_proof(receipt,target)
            for row,reservation in zip(rows,current):self._released_provider_proof(row,reservation,target.inspector_id)
        return root,bindings,current,rows
