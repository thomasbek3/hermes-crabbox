"""Retention policy storage and bounded logical dry-run manifests; never deletes files."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import PurePosixPath
import re

DEFAULTS = {'keep': False, 'retention_days': 30, 'deadline': None, 'native_hours': None, 'secret_hours': None}
CLOSED = frozenset({'completed', 'failed', 'cancelled', 'interrupted'})
PROTECTED_PATH_PARTS = frozenset({'native', 'credentials', '.credentials', 'credential-state', 'credential_state', 'shared_inputs', 'tokens', 'secrets', 'auth', '.env'})
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')


class RetentionError(ValueError):
    def __init__(self, detail, status_code=422):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def _time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise RetentionError('Invalid retention timestamp') from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RetentionError('Retention timestamps require a timezone')
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise RetentionError('Retention timestamp is out of range') from exc


def _stamp(value=None):
    return _time(value if value is not None else datetime.now(timezone.utc)).isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise RetentionError('Invalid retention identity')
    return value


def ensure_schema(db):
    """Call once in Store initialization; caller owns transaction and migration."""
    db.execute('''CREATE TABLE IF NOT EXISTS session_retention(
        session_id TEXT PRIMARY KEY REFERENCES sessions(id),
        policy TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL)''')


def _validate_policy(policy):
    if set(policy) != set(DEFAULTS) or type(policy['keep']) is not bool:
        raise RetentionError('Invalid retention policy fields')
    for key in ('retention_days', 'native_hours', 'secret_hours'):
        if key != 'retention_days' and policy[key] is None:
            continue
        if type(policy[key]) is not int or policy[key] < 1:
            raise RetentionError('Retention intervals must be positive integers')
    if policy['retention_days'] > 3650:
        raise RetentionError('Maximum session retention is 3650 days')
    native, secret = policy['native_hours'], policy['secret_hours']
    if native is not None and native >= policy['retention_days'] * 24:
        raise RetentionError('Native policy must be shorter than session policy')
    if secret is not None and secret >= (native if native is not None else policy['retention_days'] * 24):
        raise RetentionError('Secret policy must be shorter than native/session policy')
    if policy['deadline'] is not None:
        policy['deadline'] = _stamp(policy['deadline'])
    return policy


def get_policy(db, session_id):
    _id(session_id)
    if not db.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone():
        raise RetentionError('Session not found', 404)
    row = db.execute('SELECT policy,revision,updated_at FROM session_retention WHERE session_id=?', (session_id,)).fetchone()
    if row is None:
        return {**DEFAULTS, 'revision': 0, 'updated_at': None}
    try:
        saved = json.loads(row[0])
    except (ValueError, TypeError) as exc:
        raise RetentionError('Stored retention policy is invalid') from exc
    if not isinstance(saved, dict):
        raise RetentionError('Stored retention policy is invalid')
    policy = _validate_policy({**DEFAULTS, **saved})
    return {**policy, 'revision': row[1], 'updated_at': row[2]}


def set_policy(db, session_id, changes, *, expected_revision, now=None):
    """Authorized caller must use one write transaction and an idempotency key.

    Controls affect planning only. They do not adopt a scheduled cleanup policy.
    """
    if not isinstance(changes, dict) or not changes or set(changes) - set(DEFAULTS):
        raise RetentionError('Unknown or empty retention policy update')
    if type(expected_revision) is not int or expected_revision < 0:
        raise RetentionError('Expected policy revision must be a nonnegative integer')
    current = get_policy(db, session_id)
    if current['revision'] != expected_revision:
        raise RetentionError('Retention policy changed; refresh before updating', 409)
    policy = _validate_policy({**{key: current[key] for key in DEFAULTS}, **changes})
    revision, stamp = expected_revision + 1, _stamp(now)
    if policy['deadline'] is not None:
        delta = _time(policy['deadline']) - _time(stamp)
        if abs(delta) > timedelta(days=3650):
            raise RetentionError('Explicit deadline must be within 3650 days of this control update')
        if policy['native_hours'] is not None and delta <= timedelta(hours=policy['native_hours']):
            raise RetentionError('Explicit session deadline must follow the configured native review deadline')
    db.execute('''INSERT INTO session_retention(session_id,policy,revision,updated_at) VALUES(?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET policy=excluded.policy,revision=excluded.revision,updated_at=excluded.updated_at''',
        (session_id, json.dumps(policy, sort_keys=True), revision, stamp))
    return {**policy, 'revision': revision, 'updated_at': stamp}


def _bounded(db, query, arguments, limit):
    rows = db.execute(query + ' LIMIT ?', (*arguments, limit + 1)).fetchall()
    if len(rows) > limit:
        raise RetentionError('Retention inventory exceeds item budget; no partial manifest produced', 413)
    return [dict(row) for row in rows]


def plan_session(db, session_id, *, as_of=None, max_items=10000, max_manifest_bytes=1048576):
    """Build a non-executable manifest from a consistent caller-owned DB snapshot.

    Caller enforces owner/project authorization before this function. No filesystem
    is read: runtime quiescence, nofollow inode identity/content inventory, shared
    references, and policy adoption are mandatory gates for any future executor.
    """
    _id(session_id)
    if type(max_items) is not int or not 1 <= max_items <= 10000:
        raise RetentionError('Invalid inventory item budget')
    if type(max_manifest_bytes) is not int or not 1024 <= max_manifest_bytes <= 16777216:
        raise RetentionError('Invalid manifest byte budget')
    stamp = _stamp(as_of)
    observed = _time(stamp)
    policy = get_policy(db, session_id)
    row = db.execute('SELECT id,owner_id,project_id,created_at,archived FROM sessions WHERE id=?', (session_id,)).fetchone()
    session = dict(row)
    attempts = _bounded(db, 'SELECT id,generation,state,updated_at,created_at FROM attempts WHERE session_id=? ORDER BY generation,id', (session_id,), max_items)
    artifacts = _bounded(db, 'SELECT id,attempt_id,metadata FROM artifacts WHERE session_id=? ORDER BY id', (session_id,), max_items)
    if len(attempts) + len(artifacts) + 3 > max_items:
        raise RetentionError('Retention inventory exceeds item budget; no partial manifest produced', 413)
    for table in ('events', 'turns'):
        if db.execute(f'SELECT 1 FROM {table} WHERE session_id=? AND julianday(created_at) IS NULL LIMIT 1', (session_id,)).fetchone():
            raise RetentionError('Activity timestamp is unsupported; no expiry inferred')
    event = db.execute('SELECT sequence,created_at FROM events WHERE session_id=? ORDER BY julianday(created_at) DESC,sequence DESC LIMIT 1', (session_id,)).fetchone()
    turn = db.execute('SELECT created_at FROM turns WHERE session_id=? ORDER BY julianday(created_at) DESC,ordinal DESC LIMIT 1', (session_id,)).fetchone()
    sequence = db.execute('SELECT COALESCE(MAX(sequence),0) FROM events WHERE session_id=?', (session_id,)).fetchone()[0]
    dates = [session['created_at'], *[a['updated_at'] for a in attempts], *[a['created_at'] for a in attempts]]
    dates += [value for value in (policy['updated_at'], event[1] if event else None, turn[0] if turn else None) if value]
    latest = max(map(_time, dates))
    deadline = _time(policy['deadline']) if policy['deadline'] is not None else latest + timedelta(days=policy['retention_days'])
    native_deadline = latest + timedelta(hours=policy['native_hours']) if policy['native_hours'] is not None else None
    protected = [{key: a[key] for key in ('id', 'generation', 'state')} for a in attempts if a['state'] not in CLOSED]
    reasons = (['keep'] if policy['keep'] else []) + (['nonterminal_unknown_or_paused_attempt'] if protected else [])
    if native_deadline is not None and native_deadline >= deadline:
        reasons.append('native_deadline_conflict')
    due = observed >= deadline
    native_due = observed >= native_deadline if native_deadline is not None else False
    records = []
    for artifact in artifacts:
        aid = _id(artifact['id'])
        attempt_id = _id(artifact['attempt_id'])
        try:
            data = json.loads(artifact['metadata'])
        except (ValueError, TypeError) as exc:
            raise RetentionError('Artifact integrity metadata invalid; no deletion inventory produced') from exc
        if not isinstance(data, dict):
            raise RetentionError('Artifact integrity metadata invalid; no deletion inventory produced')
        path, size, digest = data.get('path'), data.get('bytes'), data.get('sha256')
        if (not isinstance(path, str) or not path or len(path) > 4096 or '\\' in path or any(ord(c) < 32 for c in path)
                or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or PROTECTED_PATH_PARTS.intersection(PurePosixPath(path).parts)
                or type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise RetentionError('Artifact integrity metadata invalid; no deletion inventory produced')
        records.append({'kind': 'artifact', 'id': aid, 'attempt_id': attempt_id, 'path': path, 'bytes': size, 'sha256': digest})
    records += [{'kind': 'attempt_result_and_logs', 'id': _id(a['id']), 'generation': a['generation'], 'bytes': None} for a in attempts]
    records += [{'kind': 'workspace', 'id': session_id, 'excludes': ['native', 'credentials', 'shared_inputs'], 'bytes': None},
                {'kind': 'repository_baseline', 'id': session_id, 'bytes': None},
                {'kind': 'session_database_records', 'id': session_id, 'bytes': None}]
    snapshot = {'session': session, 'policy': policy, 'attempts': attempts, 'event_sequence': sequence,
                'last_activity': latest.isoformat(), 'records': records}
    result = {'format': 'cloud-workbench-retention-plan-v1', 'session_id': session_id, 'as_of': stamp,
            'dry_run': True, 'executable': False, 'scheduled_cleanup': False, 'policy': policy,
            'last_activity': latest.isoformat(), 'retention_deadline': deadline.isoformat(),
            'expired': due, 'eligible_for_review': due and not reasons,
            'protected_reasons': reasons, 'protected_attempts': protected,
            'snapshot_sha256': _hash(snapshot), 'deletion_manifest': records if due and not reasons else [],
            'inventory': records, 'known_artifact_bytes': sum(r['bytes'] for r in records if r['kind'] == 'artifact'),
            'native_state': {'deadline': native_deadline.isoformat() if native_deadline else None, 'expired': native_due, 'policy_configured': native_deadline is not None,
                             'eligible_for_separate_review': native_due and not reasons, 'identity': session_id,
                             'included_in_session_deletion_manifest': False, 'encrypted_sensitive_state': True},
            'secret_state': {'configured_max_age_hours': policy['secret_hours'], 'policy_adopted': False,
                             'inventory_performed': False, 'included_in_deletion_manifest': False,
                             'reason': 'Provider credentials/cookies require independent credential-owner policy; never infer deletion authority from session expiry'},
            'shared_inputs': {'included_in_deletion_manifest': False, 'reason': 'May be referenced by other sessions or future turns'},
            'required_before_execution': ['explicit policy adoption and purge authority', 'fresh snapshot and policy revision match',
                'runtime quiescence and filesystem nofollow identity/hash inventory', 'recheck shared references and native/secret policy', 'durable deletion tombstone']}
    if len(json.dumps(result, ensure_ascii=False).encode()) > max_manifest_bytes:
        raise RetentionError('Retention manifest exceeds byte budget; no partial manifest produced', 413)
    return result


def require_matching_plan(previous, current):
    """Identity guard only; a match is never permission to delete."""
    if (not isinstance(previous, dict) or not isinstance(current, dict)
            or not isinstance(previous.get('snapshot_sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', previous['snapshot_sha256'])
            or previous.get('format') != 'cloud-workbench-retention-plan-v1'
            or current.get('format') != previous['format']
            or previous.get('session_id') != current.get('session_id')
            or previous.get('snapshot_sha256') != current.get('snapshot_sha256')
            or previous.get('eligible_for_review') != current.get('eligible_for_review')):
        raise RetentionError('Retention snapshot changed; regenerate deletion manifest', 409)
    return True
