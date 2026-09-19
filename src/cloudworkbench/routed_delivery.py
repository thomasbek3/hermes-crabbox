"""Controller root delivery: immutable preparation and atomic final publication."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import delivery_schema, provider_leases
from .budget_authority import BudgetAuthority
from .inference_budget import AttemptScope, RootScope, _stamp
from .routed_delivery_policy import RootDeliveryProof, collect_root_delivery_proof, validate_db
from .routed_observation_store import validate_forbidden_values
from .routed_publication import _directory, _read
from .routed_verification import _immutable
from .scheduler import _child_control_tx, _cleanup_target, _runtime_proof
from .store import StoreError, encode, now
from .workflow_revisions import RevisionBinding, WorkspaceRevision


class DeliveryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value:
        raise DeliveryError(code)


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value, 'delivery_json_invalid')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DeliveryError('delivery_json_invalid') from None


def _scan(raw, policy):
    _require(not any(secret in raw for secret in policy), 'delivery_secret_refused')
    pending = [_json(raw)] if policy else []
    while pending:
        value = pending.pop()
        if type(value) is str:
            _require(not any(secret in value.encode() for secret in policy), 'delivery_secret_refused')
        elif type(value) is dict:
            pending.extend(value.keys())
            pending.extend(value.values())
        elif type(value) is list:
            pending.extend(value)


@dataclass(frozen=True)
class PreparedDelivery:
    root_id: str
    generation: int
    intent_sha256: str


@dataclass(frozen=True)
class DeliveryReceipt:
    root_id: str
    generation: int
    session_id: str
    artifact_id: str
    sha256: str
    kind: str
    outcome: str
    selected_revision_sha256: str | None
    delivery_version: int
    intent_sha256: str
    cleanup_sha256: str
    event_sequence: int


_ENVELOPE_KEYS = {'schema_version', 'root_id', 'generation', 'expected_delivery_version',
    'base_revision', 'base_artifact', 'base_last_artifact', 'selected_revision_sha256',
    'kind', 'workflow_proof', 'artifact'}
_ARTIFACT_KEYS = {'id', 'session_id', 'attempt_id', 'generation', 'path', 'storage_path',
    'sha256', 'bytes', 'mime', 'provenance', 'kind', 'outcome', 'selected_revision_sha256',
    'workflow_sha256', 'delivery_version'}


def _identity(root_id, generation):
    return 'delivery-' + _sha(f'{root_id}:{generation}'.encode())


def _document(envelope, proof):
    return {'schema_version': 1, 'root_id': envelope['root_id'], 'generation': envelope['generation'],
        'delivery_version': envelope['expected_delivery_version'] + 1,
        'base_delivery_version': envelope['expected_delivery_version'],
        'base_revision_sha256': envelope['base_revision'], 'base_artifact_id': envelope['base_artifact'],
        'kind': proof.kind, 'outcome': proof.outcome, 'workflow': proof.public_record()}


def _envelope(db, intent, policy):
    value = _json(intent['proof_json'])
    _require(type(value) is dict and set(value) == _ENVELOPE_KEYS
        and type(value['schema_version']) is int and value['schema_version'] == 1
        and _raw(value).decode() == intent['proof_json'], 'delivery_intent_invalid')
    _scan(_raw(value), policy)
    for key in ('root_id', 'generation', 'expected_delivery_version', 'base_revision',
                'base_artifact', 'selected_revision_sha256', 'kind'):
        _require(_raw(value[key]) == _raw(intent[key]), 'delivery_intent_conflict')
    base = db.execute('SELECT * FROM workflow_delivery_bases WHERE root_id=?', (intent['root_id'],)).fetchone()
    _require(base is not None and base['generation'] == intent['generation']
        and value['base_last_artifact'] == base['last_artifact_id'], 'delivery_base_changed')
    proof = RootDeliveryProof.from_json(_raw(value['workflow_proof']).decode())
    public = proof.public_record()
    _require((public['root_id'], public['generation'], proof.kind, public['selected_revision_sha256']) ==
        (intent['root_id'], intent['generation'], intent['kind'], intent['selected_revision_sha256']),
        'delivery_intent_conflict')
    metadata = value['artifact']
    _require(type(metadata) is dict and set(metadata) == _ARTIFACT_KEYS, 'delivery_artifact_invalid')
    raw = _raw(_document(value, proof))
    expected = {'id': _identity(intent['root_id'], intent['generation']),
        'session_id': public['session_id'], 'attempt_id': intent['root_id'], 'generation': intent['generation'],
        'path': f"routed/{intent['root_id']}/{intent['generation']}/delivery.json",
        'sha256': _sha(raw), 'bytes': len(raw), 'mime': 'application/json',
        'provenance': 'controller_root_delivery', 'kind': proof.kind, 'outcome': proof.outcome,
        'selected_revision_sha256': public['selected_revision_sha256'],
        'workflow_sha256': public['workflow_sha256'], 'delivery_version': intent['expected_delivery_version'] + 1}
    _require(all(_raw(metadata[k]) == _raw(v) for k, v in expected.items()), 'delivery_artifact_changed')
    path = Path(metadata['storage_path'])
    _require(path.is_absolute() and '..' not in path.parts
        and path.name == 'root-delivery-' + metadata['sha256'] + '.json', 'delivery_storage_changed')
    return value, proof, metadata, raw


def _intent(db, prepared, policy):
    _require(type(prepared) is PreparedDelivery, 'delivery_prepared_required')
    intent = delivery_schema.read_intent(db, prepared.root_id, prepared.generation)
    _require(intent is not None and intent['proof_sha256'] == prepared.intent_sha256, 'delivery_intent_changed')
    return (intent, *_envelope(db, intent, policy))


def _base_current(db, root, intent):
    session = db.execute('SELECT * FROM sessions WHERE id=?', (root['session_id'],)).fetchone()
    base = db.execute('SELECT * FROM workflow_delivery_bases WHERE root_id=?', (root['root_id'],)).fetchone()
    _require(session is not None and base is not None and
        (session['delivery_version'], session['delivered_revision_sha256'], session['delivered_artifact_id'],
         session['last_delivery_artifact_id']) ==
        (intent['expected_delivery_version'], intent['base_revision'], intent['base_artifact'], base['last_artifact_id']),
        'delivery_base_changed')
    _require(session['delivery_version'] < delivery_schema.MAX_VERSION, 'delivery_version_exhausted')
    return session


def _authority(scheduler, db, root_id, generation, *, states):
    root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?', (root_id,)).fetchone()
    attempt = scheduler.store._attempt(db, root_id)
    _require(root is not None and attempt['execution_kind'] == 'hermes_root'
        and type(generation) is int and root['generation'] == attempt['generation'] == generation
        and attempt['state'] in states and not attempt['cancel_requested'] and not root['cancel_requested']
        and root['state'] in ('held', 'releasing') and root['child_attempt_id'] is None
        and attempt['session_id'] == root['session_id'] and attempt['turn_id'] == root['turn_id'],
        'delivery_authority_unavailable')
    client = db.execute('SELECT * FROM clients WHERE id=?', (root['owner_id'],)).fetchone()
    session = db.execute('SELECT * FROM sessions WHERE id=?', (root['session_id'],)).fetchone()
    _require(client is not None and client['revoked_at'] is None
        and 'submit' in _json(client['scopes']) and root['project_id'] in _json(client['projects'])
        and session is not None and not session['archived']
        and (session['owner_id'], session['project_id']) == (root['owner_id'], root['project_id']),
        'delivery_authority_unavailable')
    accounts = db.execute('''SELECT w.account_id,p.id,p.state,p.controller_instance_id,a.active_id,a.persistent_owner_id
        FROM workflow_accounts w JOIN provider_reservations p ON p.id=w.reservation_id
        JOIN provider_accounts a ON a.account_id=w.account_id WHERE w.root_id=?''', (root_id,)).fetchall()
    _require(os.getpid() == scheduler.leases.pid and accounts
        and {r['account_id']: r['persistent_owner_id'] for r in accounts} == _json(root['frozen'])['accounts']
        and all(r['controller_instance_id'] == scheduler.leases.instance_id and r['id'] == r['active_id']
            and r['state'] in ('held', 'cleaning') for r in accounts), 'delivery_owner_changed')
    _require(not db.execute("SELECT 1 FROM role_broker_pending WHERE root_id=? AND controller_request_id IS NULL AND terminal_reason IS NULL LIMIT 1", (root_id,)).fetchone(),
        'delivery_pending_work')
    _require(not db.execute("SELECT 1 FROM attempts WHERE workflow_root_id=? AND id!=? AND state!='completed' LIMIT 1", (root_id, root_id)).fetchone(),
        'delivery_pending_work')
    _require(not db.execute('''SELECT 1 FROM provider_request_leases q JOIN workflow_accounts w ON w.reservation_id=q.reservation_id
        WHERE w.root_id=? AND q.state!='released' LIMIT 1''', (root_id,)).fetchone()
        and not db.execute('''SELECT 1 FROM provider_dispatch d JOIN attempts a ON a.id=d.attempt_id
        WHERE a.workflow_root_id=? AND (d.state NOT IN ('cleaned','delivered') OR d.uncertain!=0 OR d.operation IS NOT NULL) LIMIT 1''', (root_id,)).fetchone(),
        'delivery_cleanup_unconfirmed')
    observed = scheduler.clock()
    scope = RootScope(root['owner_id'], root['project_id'], root['session_id'], root['turn_id'], root_id, generation)
    _require(root['deadline_at'] > observed and BudgetAuthority(AttemptScope(scope, root_id, generation)).check(db, now=observed).allowed,
        'delivery_budget_unavailable')
    return root, attempt, observed


def _event(db, root_id, kind, expected, *, artifact_id=None):
    clause = '' if artifact_id is None else " AND json_extract(payload,'$.id')=?"
    args = (root_id, kind) if artifact_id is None else (root_id, kind, artifact_id)
    rows = db.execute('SELECT sequence,payload FROM events WHERE attempt_id=? AND type=?' + clause + ' LIMIT 2', args).fetchall()
    _require(len(rows) == 1 and _raw(_json(rows[0]['payload'])) == _raw(expected), 'delivery_event_changed')
    return rows[0]['sequence']


def _historical_cleanup(scheduler, db, root_id, generation, intent_sha256, purpose):
    root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?', (root_id,)).fetchone()
    saved = db.execute('SELECT * FROM workflow_root_cleanup WHERE root_id=?', (root_id,)).fetchone()
    _require(root is not None and root['state'] == 'released' and root['child_attempt_id'] is None
        and saved is not None and saved['release_receipt'] is not None and saved['runtime_receipt'] is not None,
        'delivery_cleanup_changed')
    target = _cleanup_target(saved['target'], delivery_root_id=root_id)
    runtime = _json(saved['runtime_receipt'])
    release = _json(saved['release_receipt'])
    _require(encode(asdict(target)) == saved['target'] and _raw(runtime).decode() == saved['runtime_receipt']
        and type(runtime) is dict and set(runtime) == {'target', 'outcome', 'observed_at', 'evidence_sha256'}
        and type(release) is dict and set(release) == {'root_id', 'generation', 'cleanup_id', 'intent_sha256',
            'target_sha256', 'runtime_receipt_sha256', 'provider_receipt_sha256', 'fences_retained',
            'release_purpose', 'released_at'}
        and _raw(release).decode() == saved['release_receipt'], 'delivery_cleanup_changed')
    _runtime_proof(saved['runtime_receipt'], target)
    _require(root['generation'] == saved['generation'] == target.generation == generation
        and saved['controller_instance_id'] == target.controller_instance_id
        and saved['cleanup_id'] == target.cleanup_id and target.root_id == root_id
        and root['version'] == target.reservation_version + 1, 'delivery_cleanup_changed')
    terminal = 'completed' if purpose == 'delivery' else 'cancelled'
    frozen_root = next((a for a in target.attempts if a.attempt_id == root_id), None)
    _require(frozen_root is not None and frozen_root.state in
        (('verifying',) if purpose == 'delivery' else ('verifying', 'cancelled')), 'delivery_cleanup_changed')
    rows = db.execute('SELECT * FROM attempts WHERE workflow_root_id=? ORDER BY id', (root_id,)).fetchall()
    actual = []
    for row in rows:
        state = row['state']
        if row['id'] == root_id:
            _require(state == terminal and row['generation'] == generation
                and row['execution_kind'] == 'hermes_root', 'delivery_cleanup_changed')
            state = frozen_root.state
        actual.append({'attempt_id': row['id'], 'generation': row['generation'],
            'execution_kind': row['execution_kind'], 'state': state, 'runtime_id': row['runtime_id']})
    _require(any(row['id'] == root_id for row in rows)
        and _raw(actual) == _raw([asdict(a) for a in target.attempts]), 'delivery_cleanup_changed')
    accounts = db.execute('SELECT account_id,reservation_id FROM workflow_accounts WHERE root_id=? ORDER BY account_id',
        (root_id,)).fetchall()
    _require([(r['account_id'], r['reservation_id']) for r in accounts] ==
        [(r.account_id, r.reservation_id) for r in target.reservations], 'delivery_cleanup_changed')
    providers = {}
    for reservation in target.reservations:
        row = db.execute('SELECT * FROM provider_reservations WHERE id=?', (reservation.reservation_id,)).fetchone()
        _require(row is not None and row['state'] == 'released'
            and (row['account_id'], row['epoch'], row['controller_instance_id']) ==
                (reservation.account_id, reservation.epoch, reservation.controller_instance_id), 'delivery_cleanup_changed')
        providers[reservation.reservation_id] = scheduler._released_provider_proof(row, reservation, target.inspector_id)
    expected = {'root_id': root_id, 'generation': generation, 'cleanup_id': target.cleanup_id,
        'intent_sha256': intent_sha256, 'target_sha256': _sha(saved['target'].encode()),
        'runtime_receipt_sha256': _sha(saved['runtime_receipt'].encode()),
        'provider_receipt_sha256': providers, 'fences_retained': False, 'release_purpose': purpose,
        'released_at': release['released_at']}
    _require(type(release['released_at']) is str and bool(release['released_at'])
        and _raw(release) == _raw(expected), 'delivery_cleanup_changed')
    _event(db, root_id, 'workflow.root_released', release)
    return release


def _published_db(scheduler, db, root_id, generation, policy):
    attempt = scheduler.store._attempt(db, root_id)
    _require(attempt['state'] == 'completed' and attempt['generation'] == generation, 'delivery_not_published')
    pointer = (attempt['result'] or {}).get('routed_delivery')
    _require(type(pointer) is dict and set(pointer) == {'artifact_id', 'sha256', 'intent_sha256'}, 'delivery_record_missing')
    prepared = PreparedDelivery(root_id, generation, pointer['intent_sha256'])
    intent, envelope, proof, metadata, raw = _intent(db, prepared, policy)
    validate_db(db, proof)
    root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?', (root_id,)).fetchone()
    saved = db.execute('SELECT metadata FROM artifacts WHERE id=? AND attempt_id=? AND session_id=?',
        (metadata['id'], root_id, metadata['session_id'])).fetchone()
    cleanup = db.execute('SELECT * FROM workflow_root_cleanup WHERE root_id=?', (root_id,)).fetchone()
    _require(root['state'] == 'released' and saved is not None and _json(saved[0]) == metadata
        and pointer['artifact_id'] == metadata['id'] and pointer['sha256'] == metadata['sha256']
        and attempt['outcome'] == proof.outcome and cleanup is not None and cleanup['release_receipt'] is not None,
        'delivery_record_changed')
    release = _historical_cleanup(scheduler, db, root_id, generation, intent['proof_sha256'], 'delivery')
    _event(db, root_id, 'artifact.created', {k: v for k, v in metadata.items() if k != 'storage_path'}, artifact_id=metadata['id'])
    body = {'artifact_id': metadata['id'], 'sha256': metadata['sha256'], 'intent_sha256': intent['proof_sha256'],
        'kind': proof.kind, 'outcome': proof.outcome, 'selected_revision_sha256': metadata['selected_revision_sha256'],
        'delivery_version': metadata['delivery_version'], 'cleanup_sha256': _sha(_raw(release))}
    sequence = _event(db, root_id, 'workflow.delivery', body)
    terminal = db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='attempt.state' AND json_extract(payload,'$.state')='completed' LIMIT 2", (root_id,)).fetchall()
    _require(len(terminal) == 1 and _json(terminal[0][0]) == {'state': 'completed', 'outcome': proof.outcome}, 'delivery_event_changed')
    return DeliveryReceipt(root_id, generation, metadata['session_id'], metadata['id'], metadata['sha256'],
        proof.kind, proof.outcome, metadata['selected_revision_sha256'], metadata['delivery_version'],
        intent['proof_sha256'], body['cleanup_sha256'], sequence), metadata, raw


def load_root_delivery(scheduler, root_id, *, expected_generation, forbidden_values=()):
    """Read the exact historical commit; never consult or touch a newer account owner."""
    policy = validate_forbidden_values(forbidden_values)
    with _child_control_tx(scheduler.store, write=False) as db:
        before = _published_db(scheduler, db, root_id, expected_generation, policy)
    receipt, metadata, raw = before
    _require(_read(Path(metadata['storage_path']), len(raw)) == raw, 'delivery_artifact_changed')
    _scan(raw, policy)
    with _child_control_tx(scheduler.store, write=False) as db:
        _require(_published_db(scheduler, db, root_id, expected_generation, policy) == before, 'delivery_record_changed')
    return receipt


def prepare_root_delivery(scheduler, root_id, *, expected_generation, expected_delivery_version,
                          expected_base_revision_sha256, selected_revision, storage_root, forbidden_values=()):
    policy = validate_forbidden_values(forbidden_values)
    _require(type(expected_delivery_version) is int and 0 <= expected_delivery_version < delivery_schema.MAX_VERSION
        and (expected_base_revision_sha256 is None or type(expected_base_revision_sha256) is str
            and re.fullmatch('[0-9a-f]{64}', expected_base_revision_sha256))
        and type(selected_revision) is WorkspaceRevision, 'delivery_scope_invalid')
    storage = Path(storage_root)
    fd = _directory(storage, private=True)
    try:
        _require(stat.S_IMODE(os.fstat(fd).st_mode) in (0o700, 0o2700), 'delivery_storage_not_private')
    finally:
        os.close(fd)
    _require(storage != selected_revision.path and storage not in selected_revision.path.parents
        and selected_revision.path not in storage.parents, 'delivery_storage_overlap')
    with _child_control_tx(scheduler.store, write=False) as db:
        prior = delivery_schema.read_intent(db, root_id, expected_generation)
        if prior is not None:
            envelope, proof, metadata, raw = _envelope(db, prior, policy)
            private = proof.to_dict()['private']
            _require((prior['expected_delivery_version'], prior['base_revision']) ==
                (expected_delivery_version, expected_base_revision_sha256)
                and private['revision_sha256'] == selected_revision.sha256
                and private['revision_path'] == str(selected_revision.path)
                and private['revision_binding'] == asdict(selected_revision.binding)
                and Path(metadata['storage_path']).parent == storage, 'delivery_prepare_conflict')
            return PreparedDelivery(root_id, expected_generation, prior['proof_sha256'])
        root, _, _ = _authority(scheduler, db, root_id, expected_generation, states=('running',))
        base = db.execute('SELECT * FROM workflow_delivery_bases WHERE root_id=?', (root_id,)).fetchone()
        _require(base is not None and (base['delivery_version'], base['revision_sha256']) ==
            (expected_delivery_version, expected_base_revision_sha256), 'delivery_base_changed')
        base = dict(base)
    proof = collect_root_delivery_proof(scheduler, root_id, expected_generation=expected_generation,
        selected_revision=selected_revision, forbidden_values=policy)
    envelope = {'schema_version': 1, 'root_id': root_id, 'generation': expected_generation,
        'expected_delivery_version': expected_delivery_version, 'base_revision': base['revision_sha256'],
        'base_artifact': base['artifact_id'], 'base_last_artifact': base['last_artifact_id'],
        'selected_revision_sha256': proof.public_record()['selected_revision_sha256'],
        'kind': proof.kind, 'workflow_proof': proof.to_dict()}
    raw = _raw(_document(envelope, proof)); _scan(raw, policy)
    _require(len(raw) <= 256 * 1024, 'delivery_document_limit')
    path = storage / ('root-delivery-' + _sha(raw) + '.json')
    _immutable(path, raw, mode=0o600)
    metadata = {'id': _identity(root_id, expected_generation), 'session_id': proof.public_record()['session_id'],
        'attempt_id': root_id, 'generation': expected_generation, 'path': f'routed/{root_id}/{expected_generation}/delivery.json',
        'storage_path': str(path), 'sha256': _sha(raw), 'bytes': len(raw), 'mime': 'application/json',
        'provenance': 'controller_root_delivery', 'kind': proof.kind, 'outcome': proof.outcome,
        'selected_revision_sha256': envelope['selected_revision_sha256'],
        'workflow_sha256': proof.public_record()['workflow_sha256'], 'delivery_version': expected_delivery_version + 1}
    envelope['artifact'] = metadata; _scan(_raw(envelope), policy)
    with _child_control_tx(scheduler.store) as db:
        prior = delivery_schema.read_intent(db, root_id, expected_generation)
        if prior is not None:
            _require(_json(prior['proof_json']) == envelope, 'delivery_prepare_conflict')
            return PreparedDelivery(root_id, expected_generation, prior['proof_sha256'])
        root, _, observed = _authority(scheduler, db, root_id, expected_generation, states=('running',))
        validate_db(db, proof)
        _base_current(db, root, envelope)
        intent = delivery_schema.record_intent(db, root_id=root_id, expected_generation=expected_generation,
            expected_delivery_version=expected_delivery_version, base_revision=base['revision_sha256'],
            base_artifact=base['artifact_id'], selected_revision_sha256=envelope['selected_revision_sha256'],
            kind=proof.kind, proof=envelope, created_at=now())
        _envelope(db, intent, policy)
        db.execute('UPDATE role_broker_grants SET revoked=1 WHERE parent_id=? AND generation=?', (root_id, expected_generation))
        db.execute("UPDATE attempts SET state='verifying',updated_at=? WHERE id=?", (now(), root_id))
        db.execute('UPDATE inference_budget_roots SET high_water_us=max(high_water_us,?) WHERE root_id=?', (_stamp(observed), root_id))
        scheduler.store._event(db, root['session_id'], root_id, 'attempt.state', {'state': 'verifying'})
        scheduler.store._event(db, root['session_id'], root_id, 'workflow.delivery_prepared',
            {'generation': expected_generation, 'intent_sha256': intent['proof_sha256'], 'kind': proof.kind,
             'artifact_id': metadata['id'], 'sha256': metadata['sha256']})
    return PreparedDelivery(root_id, expected_generation, intent['proof_sha256'])


def _validate_prepared_files(scheduler, prepared, policy):
    with _child_control_tx(scheduler.store, write=False) as db:
        intent, envelope, proof, metadata, raw = _intent(db, prepared, policy)
    _require(_read(Path(metadata['storage_path']), len(raw)) == raw, 'delivery_artifact_changed')
    private = proof.to_dict()['private']
    revision = WorkspaceRevision(Path(private['revision_path']), private['revision_sha256'], RevisionBinding(**private['revision_binding']))
    current = collect_root_delivery_proof(scheduler, prepared.root_id, expected_generation=prepared.generation,
        selected_revision=revision, forbidden_values=policy)
    _require(current.canonical_json == proof.canonical_json, 'delivery_evidence_changed')
    return intent, envelope, proof, metadata, raw


def finalize_root_delivery(scheduler, prepared, *, cleanup_verifier, forbidden_values=()):
    policy = validate_forbidden_values(forbidden_values)
    _require(type(prepared) is PreparedDelivery, 'delivery_prepared_required')
    with _child_control_tx(scheduler.store, write=False) as db:
        _intent(db, prepared, policy)
        completed = scheduler.store._attempt(db, prepared.root_id)['state'] == 'completed'
    if completed:
        return load_root_delivery(scheduler, prepared.root_id, expected_generation=prepared.generation, forbidden_values=policy)
    _validate_prepared_files(scheduler, prepared, policy)
    try:
        scheduler.prepare_delivery_cleanup(prepared.root_id, expected_generation=prepared.generation,
            intent_sha256=prepared.intent_sha256, verifier=cleanup_verifier)
    except StoreError:
        with _child_control_tx(scheduler.store, write=False) as db:
            completed = scheduler.store._attempt(db, prepared.root_id)['state'] == 'completed'
        if completed:
            return load_root_delivery(scheduler, prepared.root_id, expected_generation=prepared.generation, forbidden_values=policy)
        raise
    _, _, proof, metadata, _ = _validate_prepared_files(scheduler, prepared, policy)
    with _child_control_tx(scheduler.store) as db:
        intent, envelope, saved_proof, saved_metadata, _ = _intent(db, prepared, policy)
        if scheduler.store._attempt(db, prepared.root_id)['state'] == 'completed':
            return _published_db(scheduler, db, prepared.root_id, prepared.generation, policy)[0]
        _require((saved_proof, saved_metadata) == (proof, metadata), 'delivery_intent_changed')
        root, attempt, observed = _authority(scheduler, db, prepared.root_id, prepared.generation, states=('verifying',))
        _base_current(db, root, intent); validate_db(db, proof)
        release = scheduler.commit_delivery_cleanup(db, prepared.root_id, expected_generation=prepared.generation,
            intent_sha256=prepared.intent_sha256)
        db.execute('INSERT INTO artifacts(id,session_id,attempt_id,metadata) VALUES(?,?,?,?)',
            (metadata['id'], root['session_id'], root['root_id'], encode(metadata)))
        session = _base_current(db, root, intent)
        revision = metadata['selected_revision_sha256'] if proof.kind == 'workspace' else session['delivered_revision_sha256']
        artifact = metadata['id'] if proof.kind == 'workspace' else session['delivered_artifact_id']
        changed = db.execute('''UPDATE sessions SET delivery_version=delivery_version+1,delivered_revision_sha256=?,
            delivered_artifact_id=?,last_delivery_artifact_id=? WHERE id=? AND delivery_version=?
            AND delivered_revision_sha256 IS ? AND delivered_artifact_id IS ? AND last_delivery_artifact_id IS ?''',
            (revision, artifact, metadata['id'], root['session_id'], intent['expected_delivery_version'],
             intent['base_revision'], intent['base_artifact'], envelope['base_last_artifact'])).rowcount
        _require(changed == 1, 'delivery_base_changed')
        result = dict(attempt['result'] or {})
        _require('routed_delivery' not in result, 'delivery_record_conflict')
        result['routed_delivery'] = {'artifact_id': metadata['id'], 'sha256': metadata['sha256'], 'intent_sha256': prepared.intent_sha256}
        provider_leases.revoke_attempt(db, root['root_id'], 'attempt_terminal')
        db.execute("UPDATE attempts SET state='completed',outcome=?,result=?,updated_at=? WHERE id=? AND generation=? AND state='verifying'",
            (proof.outcome, encode(result), now(), root['root_id'], prepared.generation))
        db.execute('UPDATE inference_budget_roots SET high_water_us=max(high_water_us,?) WHERE root_id=?', (_stamp(observed), root['root_id']))
        scheduler.store._event(db, root['session_id'], root['root_id'], 'artifact.created', {k: v for k, v in metadata.items() if k != 'storage_path'})
        scheduler.store._event(db, root['session_id'], root['root_id'], 'attempt.state', {'state': 'completed', 'outcome': proof.outcome})
        payload = {**result['routed_delivery'], 'kind': proof.kind, 'outcome': proof.outcome,
            'selected_revision_sha256': metadata['selected_revision_sha256'], 'delivery_version': metadata['delivery_version'],
            'cleanup_sha256': _sha(_raw(release))}
        scheduler.store._event(db, root['session_id'], root['root_id'], 'workflow.delivery', payload)
    return load_root_delivery(scheduler, prepared.root_id, expected_generation=prepared.generation, forbidden_values=policy)


def cleanup_cancelled_root_delivery(scheduler, prepared, *, cleanup_verifier, forbidden_values=()):
    """Release a known-clean cancelled delivery without publishing or changing the session."""
    policy = validate_forbidden_values(forbidden_values)
    with _child_control_tx(scheduler.store, write=False) as db:
        _intent(db, prepared, policy)
        attempt = scheduler.store._attempt(db, prepared.root_id)
        _require(attempt['state'] in ('verifying', 'cancelled') and attempt['cancel_requested'], 'delivery_not_cancelled')
        events = db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='workflow.delivery_cancelled_cleanup' LIMIT 2", (prepared.root_id,)).fetchall()
        if events:
            _require(len(events) == 1, 'delivery_event_changed')
            body = _json(events[0][0])
            release = _historical_cleanup(scheduler, db, prepared.root_id, prepared.generation,
                prepared.intent_sha256, 'cancelled_delivery')
            _require(_raw(body) == _raw(release), 'delivery_cleanup_changed')
            return body
    scheduler.prepare_delivery_cleanup(prepared.root_id, expected_generation=prepared.generation,
        intent_sha256=prepared.intent_sha256, verifier=cleanup_verifier)
    with _child_control_tx(scheduler.store) as db:
        _intent(db, prepared, policy)
        current = scheduler.store._attempt(db, prepared.root_id)
        _require(current['state'] in ('verifying', 'cancelled') and current['cancel_requested'], 'delivery_not_cancelled')
        if current['state'] == 'verifying':
            db.execute("UPDATE attempts SET state='cancelled',updated_at=? WHERE id=?", (now(), prepared.root_id))
            scheduler.store._event(db, current['session_id'], prepared.root_id, 'attempt.state', {'state': 'cancelled'})
        db.execute('UPDATE workflow_roots SET cancel_requested=1 WHERE root_id=?', (prepared.root_id,))
        release = scheduler.commit_cancelled_delivery_cleanup(db, prepared.root_id,
            expected_generation=prepared.generation, intent_sha256=prepared.intent_sha256)
        scheduler.store._event(db, attempt['session_id'], prepared.root_id, 'workflow.delivery_cancelled_cleanup', release)
        return release
