"""Preserve an authenticated stopped workflow, then release its exact resources."""
from dataclasses import dataclass
import hashlib

from . import provider_leases
from .routed_delivery import _authority, _event
from .routed_observation_store import validate_forbidden_values
from .routed_stop_policy import collect_stopped_workflow_proof, validate_db
from .root_cleanup_history import load_root_cleanup
from .scheduler import _child_control_tx
from .store import encode, now


class WorkflowStopError(ValueError):
    pass


def _require(value, reason):
    if not value:
        raise WorkflowStopError(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class StoppedWorkflowReceipt:
    root_id: str
    generation: int
    outcome: str
    proof_sha256: str
    cleanup_sha256: str
    event_sequence: int


def _record(proof):
    return {'schema_version': 1, 'proof_sha256': proof.sha256,
            'proof': proof.public_record()}


def _saved(db, scheduler, root_id, generation, proof):
    validate_db(db, proof)
    attempt = scheduler.store._attempt(db, root_id)
    public = proof.public_record()
    expected = _record(proof)
    _require(attempt['execution_kind'] == 'hermes_root' and attempt['generation'] == generation
        and attempt['state'] == 'completed' and attempt['outcome'] == public['outcome']
        and encode((attempt['result'] or {}).get('routed_stopped')) == encode(expected),
        'stopped_workflow_record_changed')
    event = {'proof_sha256': proof.sha256, **public}
    sequence = _event(db, root_id, 'workflow.stopped', event)
    terminal = db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='attempt.state' "
        "AND json_extract(payload,'$.state')='completed' LIMIT 2", (root_id,)).fetchall()
    _require(len(terminal) == 1 and terminal[0][0] == encode({'state': 'completed', 'outcome': public['outcome']}),
        'stopped_workflow_event_changed')
    return sequence


def load_stopped_workflow(scheduler, root_id, *, expected_generation, forbidden_values=()):
    """Authenticate historical stop and cleanup without touching a newer owner."""
    policy = validate_forbidden_values(forbidden_values)
    proof = collect_stopped_workflow_proof(scheduler, root_id,
        expected_generation=expected_generation, forbidden_values=policy)
    with _child_control_tx(scheduler.store, write=False) as db:
        sequence = _saved(db, scheduler, root_id, expected_generation, proof)
        release = load_root_cleanup(db, root_id, expected_generation)
    return StoppedWorkflowReceipt(root_id, expected_generation, proof.public_record()['outcome'],
        proof.sha256, _sha(encode(release).encode()), sequence)


def close_stopped_workflow(scheduler, root_id, *, expected_generation, cleanup_verifier,
                          forbidden_values=()):
    """Controller-only closure; never submits a replacement job or publishes a workspace.

    The terminal outcome can be durable while physical cleanup is unknown. Such a
    failure retains capacity; an explicit retry reconciles the saved target.
    """
    policy = validate_forbidden_values(forbidden_values)
    proof = collect_stopped_workflow_proof(scheduler, root_id,
        expected_generation=expected_generation, forbidden_values=policy)
    public = proof.public_record()
    _require(public['outcome'] in ('needs_review', 'rejected'), 'stopped_workflow_outcome_invalid')
    _require(len(encode(_record(proof)).encode()) <= 65536, 'stopped_workflow_record_limit')
    with _child_control_tx(scheduler.store) as db:
        attempt = scheduler.store._attempt(db, root_id)
        if attempt['state'] == 'completed':
            _saved(db, scheduler, root_id, expected_generation, proof)
        else:
            _require(db.execute('SELECT version FROM schema_version').fetchone()[0] == 3,
                'stopped_workflow_schema_required')
            _require(not db.execute('SELECT 1 FROM workflow_delivery_intents WHERE root_id=?', (root_id,)).fetchone(),
                'stopped_workflow_delivery_conflict')
            root, attempt, _ = _authority(scheduler, db, root_id, expected_generation, states=('running', 'verifying'))
            _require(root['state'] == 'held', 'stopped_workflow_cleanup_conflict')
            validate_db(db, proof)
            result = dict(attempt['result'] or {})
            _require('routed_stopped' not in result and 'routed_delivery' not in result,
                'stopped_workflow_record_conflict')
            result['routed_stopped'] = _record(proof)
            db.execute('UPDATE role_broker_grants SET revoked=1 WHERE parent_id=? AND generation=?',
                (root_id, expected_generation))
            provider_leases.revoke_attempt(db, root_id, 'attempt_terminal')
            if attempt['state'] == 'running':
                db.execute("UPDATE attempts SET state='verifying',updated_at=? WHERE id=?", (now(), root_id))
                scheduler.store._event(db, root['session_id'], root_id, 'attempt.state', {'state': 'verifying'})
            db.execute("UPDATE attempts SET state='completed',outcome=?,result=?,updated_at=? WHERE id=?",
                (public['outcome'], encode(result), now(), root_id))
            scheduler.store._event(db, root['session_id'], root_id, 'attempt.state',
                {'state': 'completed', 'outcome': public['outcome']})
            scheduler.store._event(db, root['session_id'], root_id, 'workflow.stopped',
                {'proof_sha256': proof.sha256, **public})
        released = db.execute('SELECT state FROM workflow_roots WHERE root_id=?', (root_id,)).fetchone()[0] == 'released'
    if not released:
        scheduler.release_root(root_id, expected_generation=expected_generation, verifier=cleanup_verifier)
    return load_stopped_workflow(scheduler, root_id, expected_generation=expected_generation,
        forbidden_values=policy)
