"""Explicit, quiescent delivery migration and immutable admission snapshots.

These controller-only helpers require a caller transaction and never commit it.
They validate storage identity, not the semantic authority of delivery proofs.
"""
import hashlib
import json
import re

from .store import StoreError, encode

MAX_PROOF_BYTES = 256 * 1024
MAX_VERSION = 2**63 - 1
_SESSION_V2 = 'CREATE TABLE sessions(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES clients(id),project_id TEXT NOT NULL,goal TEXT NOT NULL,created_at TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0)'
COLUMNS = (
    "delivery_version INTEGER NOT NULL DEFAULT 0 CHECK(typeof(delivery_version)='integer' AND delivery_version>=0)",
    'delivered_revision_sha256 TEXT',
    'delivered_artifact_id TEXT REFERENCES artifacts(id)',
    'last_delivery_artifact_id TEXT REFERENCES artifacts(id)',
)
SESSION_SQL = _SESSION_V2[:-1] + ', ' + ', '.join(COLUMNS) + ')'


def _hash_sql(name):
    return f"(typeof({name})='text' AND length({name})=64 AND {name} NOT GLOB '*[^0-9a-f]*')"


def _pair_sql(revision, artifact):
    return f"(({revision} IS NULL AND {artifact} IS NULL) OR ({_hash_sql(revision)} AND typeof({artifact})='text' AND length({artifact}) BETWEEN 1 AND 128))"


TABLES = {
    'workflow_delivery_bases': f"""CREATE TABLE workflow_delivery_bases(root_id TEXT PRIMARY KEY NOT NULL REFERENCES workflow_roots(root_id),generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation BETWEEN 1 AND 2147483648),delivery_version INTEGER NOT NULL CHECK(typeof(delivery_version)='integer' AND delivery_version>=0),revision_sha256 TEXT,artifact_id TEXT REFERENCES artifacts(id),last_artifact_id TEXT REFERENCES artifacts(id),CHECK({_pair_sql('revision_sha256','artifact_id')}),CHECK((delivery_version=0 AND revision_sha256 IS NULL AND artifact_id IS NULL AND last_artifact_id IS NULL) OR (delivery_version>0 AND typeof(last_artifact_id)='text' AND length(last_artifact_id) BETWEEN 1 AND 128)))""",
    'workflow_delivery_intents': f"""CREATE TABLE workflow_delivery_intents(root_id TEXT PRIMARY KEY NOT NULL REFERENCES workflow_roots(root_id),generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation BETWEEN 1 AND 2147483648),expected_delivery_version INTEGER NOT NULL CHECK(typeof(expected_delivery_version)='integer' AND expected_delivery_version>=0),base_revision TEXT,base_artifact TEXT REFERENCES artifacts(id),selected_revision_sha256 TEXT,kind TEXT NOT NULL CHECK(kind IN ('workspace','report')),proof_json TEXT NOT NULL CHECK(typeof(proof_json)='text' AND length(CAST(proof_json AS BLOB)) BETWEEN 2 AND {MAX_PROOF_BYTES} AND json_valid(proof_json)),proof_sha256 TEXT NOT NULL CHECK({_hash_sql('proof_sha256')}),created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at) BETWEEN 1 AND 64),CHECK({_pair_sql('base_revision','base_artifact')}),CHECK((kind='workspace' AND {_hash_sql('selected_revision_sha256')}) OR (kind='report' AND selected_revision_sha256 IS NULL)))""",
}
_SESSION_VALID = f"""typeof(NEW.delivery_version)='integer' AND NEW.delivery_version>=0 AND {_pair_sql('NEW.delivered_revision_sha256','NEW.delivered_artifact_id')} AND ((NEW.delivery_version=0 AND NEW.delivered_revision_sha256 IS NULL AND NEW.delivered_artifact_id IS NULL AND NEW.last_delivery_artifact_id IS NULL) OR (NEW.delivery_version>0 AND typeof(NEW.last_delivery_artifact_id)='text' AND length(NEW.last_delivery_artifact_id) BETWEEN 1 AND 128)) AND (NEW.delivered_artifact_id IS NULL OR EXISTS(SELECT 1 FROM artifacts WHERE id=NEW.delivered_artifact_id AND session_id=NEW.id)) AND (NEW.last_delivery_artifact_id IS NULL OR EXISTS(SELECT 1 FROM artifacts WHERE id=NEW.last_delivery_artifact_id AND session_id=NEW.id))"""
TRIGGERS = {}
for operation in ('INSERT', 'UPDATE'):
    name = 'session_delivery_' + operation.lower()
    TRIGGERS[name] = f"CREATE TRIGGER {name} BEFORE {operation} ON sessions WHEN COALESCE(({_SESSION_VALID}),0)=0 BEGIN SELECT RAISE(ABORT,'invalid_session_delivery'); END"
for table in TABLES:
    for operation in ('UPDATE', 'DELETE'):
        name = table + '_no_' + operation.lower()
        TRIGGERS[name] = f"CREATE TRIGGER {name} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_delivery_record'); END"
TRIGGERS['workflow_delivery_base_insert'] = """CREATE TRIGGER workflow_delivery_base_insert BEFORE INSERT ON workflow_delivery_bases WHEN NOT EXISTS(SELECT 1 FROM workflow_roots r JOIN sessions s ON s.id=r.session_id WHERE r.root_id=NEW.root_id AND r.generation=NEW.generation AND s.delivery_version=NEW.delivery_version AND s.delivered_revision_sha256 IS NEW.revision_sha256 AND s.delivered_artifact_id IS NEW.artifact_id AND s.last_delivery_artifact_id IS NEW.last_artifact_id) BEGIN SELECT RAISE(ABORT,'delivery_base_mismatch'); END"""
TRIGGERS['workflow_delivery_intent_insert'] = """CREATE TRIGGER workflow_delivery_intent_insert BEFORE INSERT ON workflow_delivery_intents WHEN NOT EXISTS(SELECT 1 FROM workflow_delivery_bases b JOIN workflow_roots r ON r.root_id=b.root_id WHERE b.root_id=NEW.root_id AND b.generation=NEW.generation AND r.generation=NEW.generation AND b.delivery_version=NEW.expected_delivery_version AND b.revision_sha256 IS NEW.base_revision AND b.artifact_id IS NEW.base_artifact) BEGIN SELECT RAISE(ABORT,'delivery_intent_base_mismatch'); END"""


def _require(ok, detail, status=409):
    if not ok:
        raise StoreError(status, detail)


def enabled(db):
    return [row[0] for row in db.execute('SELECT version FROM schema_version')] == [3]


def verify_schema(db):
    """Verify only v3 additions; caller also verifies the original scheduler schema."""
    _require(enabled(db), 'Explicit delivery migration required', 503)
    expected = {'sessions': SESSION_SQL, **TABLES, **TRIGGERS}
    for name, sql in expected.items():
        row = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        _require(row is not None and row[0] == sql, 'Delivery schema differs from reviewed migration', 503)
    known = set(TRIGGERS)
    extra = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (tbl_name IN ('workflow_delivery_bases','workflow_delivery_intents') OR name LIKE 'session_delivery_%')").fetchall()
    _require({row[0] for row in extra} == known, 'Delivery schema differs from reviewed migration', 503)


def migrate(store):
    """Operator must stop writers and take an offline backup before invoking."""
    from .scheduler import verify_schema as verify_scheduler
    with store._tx() as db:
        versions = [row[0] for row in db.execute('SELECT version FROM schema_version')]
        _require(versions in ([2], [3]), 'Explicit scheduler migration required before delivery migration', 503)
        verify_scheduler(db)
        if versions == [3]:
            verify_schema(db)
            return
        # Queued work is preserved but cannot be actively admitted during migration.
        checks = (
            "SELECT 1 FROM attempts WHERE state NOT IN ('queued','completed','failed','cancelled','interrupted','paused') LIMIT 1",
            "SELECT 1 FROM workflow_roots WHERE state!='released' LIMIT 1",
            "SELECT 1 FROM provider_accounts WHERE active_id IS NOT NULL LIMIT 1",
            "SELECT 1 FROM provider_reservations WHERE state!='released' LIMIT 1",
            "SELECT 1 FROM provider_request_leases WHERE state!='released' LIMIT 1",
            "SELECT 1 FROM provider_dispatch WHERE state NOT IN ('cleaned','delivered') OR uncertain!=0 OR operation IS NOT NULL LIMIT 1",
        )
        _require(not any(db.execute(sql).fetchone() for sql in checks), 'Delivery migration requires quiescent attempts and reservations')
        before = db.execute("SELECT sql FROM sqlite_master WHERE name='sessions'").fetchone()
        _require(before is not None and before[0] == _SESSION_V2, 'Delivery sessions schema differs before migration', 503)
        _require(not db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'workflow_delivery_%' OR name LIKE 'session_delivery_%'").fetchone(), 'Unexpected partial delivery schema', 503)
        for column in COLUMNS:
            db.execute('ALTER TABLE sessions ADD COLUMN ' + column)
        for sql in TABLES.values():
            db.execute(sql)
        for sql in TRIGGERS.values():
            db.execute(sql)
        db.execute('UPDATE schema_version SET version=3')
        verify_schema(db)
        _require(not db.execute('PRAGMA foreign_key_check').fetchone(), 'Delivery migration foreign key violation', 503)


def _context(db, root_id, generation):
    _require(db.in_transaction and enabled(db), 'Delivery transaction required', 503)
    _require(type(root_id) is str and 1 <= len(root_id) <= 128 and type(generation) is int and 1 <= generation <= 2**31, 'Invalid delivery root identity', 422)
    root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?', (root_id,)).fetchone()
    _require(root is not None and root['generation'] == generation, 'Delivery root generation changed')
    return root


def freeze_base(db, root_id, expected_generation):
    """Freeze the session view once; exact replay never refreshes it."""
    root = _context(db, root_id, expected_generation)
    existing = db.execute('SELECT * FROM workflow_delivery_bases WHERE root_id=?', (root_id,)).fetchone()
    if existing is not None:
        _require(existing['generation'] == expected_generation, 'Delivery base generation changed')
        return dict(existing)
    attempt = db.execute('SELECT state,generation FROM attempts WHERE id=?', (root_id,)).fetchone()
    _require(root['state'] == 'queued' and attempt is not None and attempt['state'] == 'queued' and attempt['generation'] == expected_generation, 'Delivery base must be frozen before admission')
    session = db.execute('SELECT * FROM sessions WHERE id=?', (root['session_id'],)).fetchone()
    _require(session is not None, 'Delivery session missing')
    db.execute('INSERT INTO workflow_delivery_bases(root_id,generation,delivery_version,revision_sha256,artifact_id,last_artifact_id) VALUES(?,?,?,?,?,?)',
        (root_id, expected_generation, session['delivery_version'], session['delivered_revision_sha256'], session['delivered_artifact_id'], session['last_delivery_artifact_id']))
    return dict(db.execute('SELECT * FROM workflow_delivery_bases WHERE root_id=?', (root_id,)).fetchone())


def _proof(proof):
    _require(type(proof) is dict, 'Invalid delivery proof', 422)
    try:
        raw = encode(proof)
        size = len(raw.encode('utf-8'))
        # Reject non-JSON finite values accepted by the default Python encoder.
        json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError(422, 'Invalid delivery proof') from None
    _require(2 <= size <= MAX_PROOF_BYTES, 'Delivery proof exceeds bound', 413)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def read_intent(db, root_id, expected_generation):
    _context(db, root_id, expected_generation)
    row = db.execute('SELECT * FROM workflow_delivery_intents WHERE root_id=?', (root_id,)).fetchone()
    if row is None:
        return None
    value = dict(row)
    _require(value['generation'] == expected_generation, 'Delivery intent generation changed')
    try:
        proof = json.loads(value['proof_json'])
        canonical, digest = _proof(proof)
    except (ValueError, TypeError):
        raise StoreError(409, 'Invalid persisted delivery proof') from None
    _require(canonical == value['proof_json'] and digest == value['proof_sha256'], 'Delivery intent proof changed')
    base = db.execute('SELECT * FROM workflow_delivery_bases WHERE root_id=?', (root_id,)).fetchone()
    _require(base is not None and (base['generation'], base['delivery_version'], base['revision_sha256'], base['artifact_id']) ==
        (expected_generation, value['expected_delivery_version'], value['base_revision'], value['base_artifact']), 'Delivery intent base changed')
    return value


def record_intent(db, *, root_id, expected_generation, expected_delivery_version, base_revision,
                  base_artifact, selected_revision_sha256, kind, proof, created_at):
    _context(db, root_id, expected_generation)
    _require(type(expected_delivery_version) is int and 0 <= expected_delivery_version <= MAX_VERSION, 'Invalid delivery version', 422)
    hash_ok = lambda value: type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None
    _require((base_revision is None and base_artifact is None) or (hash_ok(base_revision) and type(base_artifact) is str and 1 <= len(base_artifact) <= 128), 'Invalid delivery base', 422)
    _require(type(kind) is str and ((kind == 'workspace' and hash_ok(selected_revision_sha256)) or (kind == 'report' and selected_revision_sha256 is None)), 'Invalid delivery kind or revision', 422)
    _require(type(created_at) is str and 1 <= len(created_at) <= 64, 'Invalid delivery timestamp', 422)
    raw, digest = _proof(proof)
    value = dict(root_id=root_id,generation=expected_generation,expected_delivery_version=expected_delivery_version,
        base_revision=base_revision,base_artifact=base_artifact,selected_revision_sha256=selected_revision_sha256,
        kind=kind,proof_json=raw,proof_sha256=digest,created_at=created_at)
    existing = read_intent(db, root_id, expected_generation)
    if existing is not None:
        _require(existing == value, 'Delivery intent replay conflict')
        return existing
    columns = tuple(value)
    db.execute('INSERT INTO workflow_delivery_intents(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(value.values()))
    return read_intent(db, root_id, expected_generation)
