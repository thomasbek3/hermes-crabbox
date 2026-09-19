"""Controller-only publication of worker observations, never protected outcomes."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .budget_authority import BudgetAuthority
from .inference_budget import AttemptScope, RootScope, _stamp
from .routed_cleanup import RoutedChildCleanup, ChildCleanupEvidence, _publish_exclusive
from .routed_collection import ExecutionObservation, collect_execution
from .routed_driver import QuiescedStage, _validate_prepared
from .routed_runtime import CallerSpec
from .scheduler import RoleScheduler, _child_control_tx
from .store import encode


class PublicationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value:
        raise PublicationError(code)


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class PublishedObservation:
    artifact_id: str
    attempt_id: str
    generation: int
    sha256: str
    event_sequence: int


def _authority(db, scheduler, spec, quiesced):
    _require(type(scheduler) is RoleScheduler and type(spec) is CallerSpec
             and type(quiesced) is QuiescedStage and type(spec.generation) is int
             and spec.generation > 0 and type(quiesced.generation) is int
             and (quiesced.attempt_id, quiesced.generation) == (spec.attempt_id, spec.generation),
             'publication_context_invalid')
    child = scheduler.store._attempt(db, spec.attempt_id)
    root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?',
                      (child.get('workflow_root_id'),)).fetchone()
    launch = db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',
                        (spec.attempt_id,)).fetchone()
    _require(root is not None and launch is not None and child['execution_kind'] == 'hermes_child'
             and child['generation'] == spec.generation and child['state'] in ('preparing', 'running')
             and not child['cancel_requested'] and root['state'] == 'held'
             and not root['cancel_requested'] and root['child_attempt_id'] == spec.attempt_id
             and scheduler._owner_current(db, root['root_id'])
             and launch['state'] == 'fenced' and launch['generation'] == spec.generation
             and launch['controller_instance_id'] == scheduler.leases.instance_id,
             'publication_authority_unavailable')
    bound = scheduler._child_binding_record(launch)
    body = json.loads(launch['binding'])
    parent = scheduler.store._attempt(db, root['root_id'])
    client = db.execute('SELECT * FROM clients WHERE id=?', (root['owner_id'],)).fetchone()
    session = db.execute('SELECT * FROM sessions WHERE id=?', (spec.session_id,)).fetchone()
    _require(parent['execution_kind'] == 'hermes_root' and parent['state'] == 'running'
             and not parent['cancel_requested'] and parent['generation'] == root['generation']
             == child['workflow_parent_generation'] and child['workflow_parent_id'] == parent['id']
             and (child['session_id'], child['turn_id']) == (root['session_id'], root['turn_id'])
             and child['session_id'] == spec.session_id
             and client is not None and client['revoked_at'] is None
             and 'submit' in json.loads(client['scopes']) and root['project_id'] in json.loads(client['projects'])
             and session is not None and (session['owner_id'], session['project_id'])
             == (root['owner_id'], root['project_id']), 'publication_authority_unavailable')
    accounts = [dict(r) for r in db.execute('''SELECT w.account_id,w.reservation_id,p.epoch,
        p.controller_instance_id,a.persistent_owner_id FROM workflow_accounts w
        JOIN provider_reservations p ON p.id=w.reservation_id
        JOIN provider_accounts a ON a.account_id=w.account_id WHERE w.root_id=? ORDER BY w.account_id''',
                                           (root['root_id'],))]
    _require({r['account_id']: r['persistent_owner_id'] for r in accounts}
             == json.loads(root['frozen'])['accounts'], 'publication_account_changed')
    row = db.execute('''SELECT s.id AS seat_id,s.ordinal,s.profile,q.id AS request_id,q.role,q.payload,
        q.parent_generation,step.step_id,step.ordinal AS step_ordinal,step.input_revision_sha256
        FROM workflow_seats s JOIN workflow_requests q ON q.id=s.request_id
        JOIN workflow_steps step ON step.request_id=q.id AND step.root_id=q.root_id
        WHERE s.id=? AND q.root_id=?''', (child['role_seat_id'], root['root_id'])).fetchone()
    _require(row is not None, 'publication_assignment_changed')
    assignment = {'root_id': root['root_id'], 'root_generation': root['generation'],
        'owner_id': root['owner_id'], 'project_id': root['project_id'], 'session_id': root['session_id'],
        'turn_id': root['turn_id'], 'workflow_digest': root['frozen_digest'], **dict(row)}
    _require(body['assignment'] == assignment and bound.assignment_digest == _hash(encode(assignment).encode())
             and bound.binding_digest == quiesced.binding_digest
             and bound.caller_spec_digest == spec.digest and body['caller_spec'] == json.loads(encode(asdict(spec)))
             and bound.runtime_id == child['runtime_id'] == quiesced.runtime_id
             and bound.input_revision_sha256 == assignment['input_revision_sha256'],
             'publication_binding_changed')
    scope = RootScope(root['owner_id'], root['project_id'], root['session_id'], root['turn_id'],
                      root['root_id'], root['generation'])
    observed = scheduler.clock()
    _require(root['deadline_at'] > observed and
             BudgetAuthority(AttemptScope(scope, spec.attempt_id, spec.generation)).check(db, now=observed).allowed,
             'publication_budget_unavailable')
    return bound, dict(root), accounts, observed


def result_authority(scheduler, db, spec, quiesced):
    """Read-only predicate inside a caller-owned bounded SQLite transaction."""
    _require(db.in_transaction and Path(db.execute('PRAGMA database_list').fetchone()[2]).resolve()
             == scheduler.store.path.resolve(), 'publication_transaction_required')
    bound, root, accounts, observed = _authority(db, scheduler, spec, quiesced)
    body = json.loads(db.execute('SELECT binding FROM workflow_child_launch WHERE child_id=?',
                                (spec.attempt_id,)).fetchone()[0])
    return {'child': scheduler.store._attempt(db, spec.attempt_id), 'root': root,
            'assignment': body['assignment'], 'binding': bound, 'accounts': accounts,
            'observed_at': observed}


def authorize_result(scheduler, spec, quiesced):
    """Read-only identity/lifecycle authority; never authorizes cleanup or execution."""
    with _child_control_tx(scheduler.store, write=False) as db:
        return _authority(db, scheduler, spec, quiesced)[0]


def _directory(path, *, private):
    path = Path(path)
    _require(path.is_absolute() and '..' not in path.parts, 'publication_path_invalid')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if private:
            _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) in (0o700, 0o2700, 0o750, 0o2750),
                     'publication_directory_untrusted')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(path, maximum, *, private=True):
    path = Path(path)
    directory = _directory(path.parent, private=private)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.geteuid()
                 and not before.st_mode & 0o022 and before.st_size <= maximum, 'publication_file_untrusted')
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        _require(len(raw) == before.st_size and
                 (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                 'publication_file_changed')
        return bytes(raw)
    finally:
        os.close(fd)


def _stage(root, raw):
    root = Path(root)
    directory = _directory(root, private=True)
    os.close(directory)
    path = root / ('worker-observation-' + _hash(raw) + '.json')
    fd, name = tempfile.mkstemp(prefix='.observation-', suffix='.pending', dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), 0o440)
            os.fsync(stream.fileno())
        try:
            _publish_exclusive(temporary, path)
        except FileExistsError:
            _require(_read(path, len(raw)) == raw, 'publication_file_conflict')
        directory = _directory(root, private=True)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _validate_raw(observation, expected, result_bytes, events_bytes):
    _require(type(observation) is ExecutionObservation, 'publication_observation_invalid')
    result = observation.result_json.encode() if result_bytes is None else result_bytes
    events = observation.events_jsonl.encode() if events_bytes is None else events_bytes
    _require(type(result) is bytes and len(result) <= 65536 and type(events) is bytes
             and len(events) <= 16 * 1024**2, 'publication_observation_limit')
    def reader(*, name, offset, max_bytes):
        raw, evidence = ((result, observation.result_evidence) if name == 'result'
                         else (events, observation.events_evidence))
        data = raw[offset:offset + max_bytes]
        return {'present': True, 'offset': offset, 'next_offset': offset + len(data),
            'inode': evidence.inode, 'size': len(raw), 'mtime_ns': evidence.mtime_ns,
            'data': data, 'provenance': 'worker_reported'}
    parsed = collect_execution(reader, launch_receipt_sha256=expected)
    _require(_bytes(asdict(parsed)) == _bytes(asdict(observation)), 'publication_observation_changed')
    return result, events


def validate_cleanup_membership(db, proof, bound, root, accounts, scheduler):
    """Recheck exact durable membership inside the final publication transaction."""
    _require(db.in_transaction and Path(db.execute('PRAGMA database_list').fetchone()[2]).resolve()
             == scheduler.store.path.resolve(), 'publication_transaction_required')
    target = proof['target']
    _require(target == {'root_id': root['root_id'], 'root_generation': root['generation'],
        'child_id': bound.child_id, 'child_generation': bound.generation,
        'reservation_version': root['version'], 'runtime_id': bound.runtime_id}
        and proof['launch_binding'] == asdict(bound) and proof['accounts_retained'] == accounts,
        'publication_cleanup_changed')
    grants = [dict(r) for r in db.execute('SELECT * FROM provider_execution_grants WHERE attempt_id=? ORDER BY id LIMIT 33',
                                        (bound.child_id,))]
    workers = proof['workers']
    _require(len(grants) <= 32 and len(workers) == len(grants)
             and {w['executor']['grant_id'] for w in workers} == {g['id'] for g in grants}
             and all(g['generation'] == bound.generation and g['revoked_at'] is not None
                     and g['reservation_id'] in {a['reservation_id'] for a in accounts} for g in grants),
             'publication_cleanup_changed')
    leases = [dict(r) for r in db.execute('''SELECT l.* FROM provider_request_leases l
        JOIN provider_execution_grants g ON g.id=l.grant_id WHERE g.attempt_id=? ORDER BY l.id LIMIT 1025''',
                                         (bound.child_id,))]
    dispatch_ids = [r[0] for r in db.execute('''SELECT DISTINCT d.request_id FROM provider_dispatch d
        LEFT JOIN provider_request_leases l ON l.id=d.request_id
        LEFT JOIN provider_execution_grants g ON g.id=l.grant_id
        WHERE d.attempt_id=? OR g.attempt_id=? ORDER BY d.request_id LIMIT 1025''',
                                           (bound.child_id, bound.child_id))]
    grants_by_id = {g['id']: g for g in grants}
    _require(len(leases) <= 1024 and dispatch_ids == [r['id'] for r in leases]
             and all(r['purpose'] == 'inference' and r['reservation_id'] ==
                     grants_by_id[r['grant_id']]['reservation_id'] for r in leases),
             'publication_cleanup_membership_changed')
    rows = [dict(r) for r in db.execute('''SELECT l.*,d.state AS dispatch_state,d.uncertain,d.operation,
        d.attempt_id,d.generation,d.profile_digest FROM provider_request_leases l
        JOIN provider_execution_grants g ON g.id=l.grant_id
        JOIN provider_dispatch d ON d.request_id=l.id WHERE g.attempt_id=? OR d.attempt_id=? ORDER BY l.id LIMIT 1025''',
                                       (bound.child_id, bound.child_id))]
    _require(len(rows) <= 1024 and [r['id'] for r in rows] == [p['request_id'] for p in proof['providers']],
             'publication_cleanup_changed')
    for row, provider in zip(rows, proof['providers']):
        _require(row['state'] == 'released' and row['dispatch_state'] in ('cleaned', 'delivered')
                 and row['uncertain'] == 0 and row['operation'] is None
                 and row['attempt_id'] == bound.child_id and row['generation'] == bound.generation
                 and row['profile_digest'] == bound.profile_digest and row['cleanup_receipt']
                 and provider == {'request_id': row['id'], 'grant_id': row['grant_id'],
                    'physical_receipt_sha256': _hash(row['cleanup_receipt'].encode()),
                    'material_cleanup_confirmed': True}, 'publication_cleanup_changed')


def read_cleanup_evidence(collector, cleanup):
    """Replay trusted cleanup, then read its exact private hash-bound receipt."""
    _require(type(collector) is RoutedChildCleanup and type(cleanup) is ChildCleanupEvidence,
             'publication_cleanup_context_invalid')
    _require(collector.collect() == cleanup, 'publication_cleanup_changed')
    expected_path = collector.runtime.root / ('child-cleanup-' + cleanup.evidence_sha256 + '.json')
    _require(cleanup.evidence_path == expected_path, 'publication_cleanup_path_changed')
    raw = _read(expected_path, 2 * 1024**2)
    _require(_hash(raw) == cleanup.evidence_sha256, 'publication_cleanup_changed')
    return json.loads(raw)


def publish_observation(scheduler, *, prepared, spec, quiesced, execution_profile,
                        collector, cleanup, publication_root, forbidden_values=(),
                        result_bytes=None, events_bytes=None):
    """Publish after exact cleanup; inputs and collector are controller-owned only."""
    from .routed_observation_store import (validate_forbidden_values, scan_output_secrets,
                                           ObservationStoreError)
    try:
        forbidden_values = validate_forbidden_values(forbidden_values)
    except ObservationStoreError:
        raise PublicationError('publication_secret_policy_invalid') from None
    _validate_prepared(prepared, spec, execution_profile)
    with _child_control_tx(scheduler.store, write=False) as db:
        context = result_authority(scheduler, db, spec, quiesced)
    bound, root = context['binding'], context['root']
    _require(asdict(prepared.materialization.consumer) == {
        'owner_id': root['owner_id'], 'project_id': root['project_id'],
        'session_id': root['session_id'], 'turn_id': root['turn_id'],
        'root_attempt_id': root['root_id'], 'root_generation': root['generation'],
        'attempt_id': spec.attempt_id, 'generation': spec.generation}
        and prepared.workflow_digest == root['frozen_digest']
        and prepared.profile_digest == bound.profile_digest
        and prepared.materialization.revision_sha256 == bound.input_revision_sha256,
        'publication_prepared_scope_changed')
    _require(type(collector) is RoutedChildCleanup and type(cleanup) is ChildCleanupEvidence
             and collector.scheduler is scheduler and collector.spec == spec and collector.quiesced == quiesced
             and quiesced.verification_pass is False and quiesced.provider_cleanup_qualified is False
             and quiesced.scheduler_seat_released is False, 'publication_cleanup_context_invalid')
    expected = _hash(prepared.launch.receipt_json.encode())
    result, events = _validate_raw(quiesced.observation, expected, result_bytes, events_bytes)
    launch_raw = _read(Path(spec.task_dir) / 'launch.json', 512 * 1024, private=False)
    _require(_hash(launch_raw) == dict(spec.task_files)['launch.json']
             and json.loads(launch_raw) == json.loads(_bytes(asdict(prepared.launch))),
             'publication_launch_changed')
    try:
        scan_output_secrets(result, events, forbidden_values=forbidden_values)
    except ObservationStoreError:
        raise PublicationError('publication_secret_rejected') from None
    proof = read_cleanup_evidence(collector, cleanup)
    document = {'version': 1, 'kind': 'worker_observation', 'provenance': 'worker_reported',
        'verification_pass': False, 'gate_authorized': False, 'revision_promotion_authorized': False,
        'scope': asdict(prepared.materialization.consumer),
        'binding': asdict(bound), 'cleanup_evidence_sha256': cleanup.evidence_sha256,
        'observation': asdict(quiesced.observation)}
    artifact_raw = _bytes(document)
    _require(len(artifact_raw) <= 32 * 1024**2, 'publication_observation_limit')
    _require(not any(secret in artifact_raw for secret in forbidden_values), 'publication_secret_rejected')
    path = _stage(publication_root, artifact_raw)
    _require(_read(path, len(artifact_raw)) == artifact_raw, 'publication_file_changed')
    identity = 'observation-' + _hash((spec.attempt_id + ':' + str(spec.generation)).encode())
    metadata = {'id': identity, 'session_id': spec.session_id, 'attempt_id': spec.attempt_id,
        'generation': spec.generation, 'path': 'observations/' + str(spec.generation) + '/worker.json',
        'storage_path': str(path), 'bytes': len(artifact_raw), 'sha256': _hash(artifact_raw),
        'mime': 'application/json', 'kind': 'worker_observation', 'provenance': 'worker_reported',
        'verification_pass': False, 'binding_digest': bound.binding_digest}
    event_payload = {'artifact_id': identity, 'sha256': metadata['sha256'],
        'generation': spec.generation, 'binding_digest': bound.binding_digest,
        'provenance': 'worker_reported', 'verification_pass': False}
    with _child_control_tx(scheduler.store) as db:
        current, root, accounts, observed = _authority(db, scheduler, spec, quiesced)
        _require(current == bound, 'publication_binding_changed')
        validate_cleanup_membership(db, proof, current, root, accounts, scheduler)
        prior = db.execute('SELECT metadata FROM artifacts WHERE id=? OR (attempt_id=? AND json_extract(metadata,\'$.path\')=?)',
                           (identity, spec.attempt_id, metadata['path'])).fetchall()
        if prior:
            _require(len(prior) == 1 and json.loads(prior[0][0]) == metadata, 'publication_conflict')
            rows = db.execute("SELECT sequence,payload FROM events WHERE attempt_id=? AND type='workflow.observation_published' AND json_extract(payload,'$.artifact_id')=?",
                              (spec.attempt_id, identity)).fetchall()
            _require(len(rows) == 1 and json.loads(rows[0]['payload']) == event_payload,
                     'publication_record_incomplete')
            sequence = rows[0]['sequence']
        else:
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?)',
                       (identity, spec.session_id, spec.attempt_id, encode(metadata)))
            scheduler.store._event(db, spec.session_id, spec.attempt_id, 'artifact.created',
                                   {k: v for k, v in metadata.items() if k != 'storage_path'})
            sequence = scheduler.store._event(db, spec.session_id, spec.attempt_id,
                'workflow.observation_published', event_payload)
        db.execute('UPDATE inference_budget_roots SET high_water_us=? WHERE root_id=? AND high_water_us<?',
                   (_stamp(observed), root['root_id'], _stamp(observed)))
    return PublishedObservation(identity, spec.attempt_id, spec.generation, metadata['sha256'], sequence)
