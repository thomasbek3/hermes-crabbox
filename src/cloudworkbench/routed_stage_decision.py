"""Authenticated stage-contract decisions; never protected task acceptance."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import provider_leases
from .inference_budget import _stamp
from .models import TERMINAL, TRANSITIONS
from .routed_cleanup import RoutedChildCleanup
from .routed_driver import _exclusive_driver
from .routed_observation_store import load_observation, validate_forbidden_values, _location
from .routed_publication import (_read, _directory, result_authority,
    read_cleanup_evidence, validate_cleanup_membership)
from .routed_verification import _immutable
from .scheduler import _child_control_tx
from .stage_contracts import VERSION, ROLES, stage_contract, parse_stage_output
from .store import encode, now
from .workflow_revisions import WorkspaceRevision, verify_revision

CODING_ROLES = frozenset({'feature', 'bug_fix', 'refactoring', 'perf_issue', 'hillclimb'})
SUPPORTED_ROLES = ROLES - {'acceptance_verification'}
_BODY_KEYS = frozenset(('schema_version verification_scope attempt_id generation session_id '
    'root_id root_generation step_id role workflow_sha256 assignment_sha256 launch_binding_sha256 '
    'caller_spec_sha256 runtime_id profile_sha256 stage_contract_version stage_contract_sha256 '
    'stage_output stage_output_sha256 input_revision_sha256 candidate_revision_sha256 '
    'carried_revision_sha256 input_binding candidate_binding readonly observation_artifact_id '
    'observation_metadata_sha256 candidate_artifact_id candidate_metadata_sha256 cleanup_sha256 '
    'outcome gate reservation_version controller_instance_id accounts_retained').split())


class StageDecisionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(ok, code):
    if not ok:
        raise StageDecisionError(code)


def _raw(value):
    return encode(value).encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'stage_decision_json_invalid')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise StageDecisionError('stage_decision_json_invalid') from None


def _scan(raw, policy):
    _require(not any(v in raw for v in policy), 'stage_decision_secret_refused')
    pending = [_json(raw)]
    while pending:
        value = pending.pop()
        if type(value) is str:
            _require(not any(v in value.encode() for v in policy), 'stage_decision_secret_refused')
        elif type(value) is dict:
            pending.extend(value.keys()); pending.extend(value.values())
        elif type(value) is list:
            pending.extend(value)


def _identity(child_id, generation):
    return 'stage-decision-' + _sha(f'{child_id}:{generation}'.encode())


def _artifact(db, artifact_id, child):
    row = db.execute('SELECT metadata FROM artifacts WHERE id=? AND attempt_id=? AND session_id=?',
                     (artifact_id, child['id'], child['session_id'])).fetchone()
    _require(row is not None, 'stage_decision_artifact_missing')
    metadata = _json(row['metadata'])
    _require(type(metadata) is dict and metadata.get('id') == artifact_id
             and metadata.get('attempt_id') == child['id']
             and metadata.get('session_id') == child['session_id']
             and type(metadata.get('generation')) is int and metadata['generation'] == child['generation'],
             'stage_decision_artifact_invalid')
    return metadata


def _event(db, child_id, kind, expected, *, artifact_id=None):
    clause = " AND json_extract(payload,'$.id')=?" if artifact_id else ''
    params = (child_id, kind, artifact_id) if artifact_id else (child_id, kind)
    rows = db.execute('SELECT sequence,payload FROM events WHERE attempt_id=? AND type=?' + clause + ' LIMIT 2', params).fetchall()
    _require(len(rows) == 1 and _raw(_json(rows[0]['payload'])) == _raw(expected), 'stage_decision_event_changed')
    return rows[0]['sequence']


def _contract(root, assignment):
    _require(_sha(root['frozen'].encode()) == assignment['workflow_digest'], 'stage_decision_workflow_changed')
    provenance = _json(root['frozen'])['provenance']
    _require(provenance.get('stage_contract_version') == VERSION, 'stage_decision_version_required')
    role = assignment['role']
    _require(role in SUPPORTED_ROLES, 'stage_decision_role_unsupported')
    steps = provenance['workflow']['steps']
    _require(sum(step['id'] == assignment['step_id'] and step['role'] == role for step in steps) == 1,
             'stage_decision_step_changed')
    try:
        return stage_contract(role, assignment['input_revision_sha256'],
                              _json(assignment['payload'])['arguments']['context_refs'])
    except (ValueError, TypeError, KeyError):
        raise StageDecisionError('stage_decision_contract_invalid') from None


def _outcome(output):
    state = output.recommendation or output.status
    if state in ('complete', 'approve'):
        return 'unverified', 'pass'
    if state == 'revise':
        return 'needs_review', 'reject'
    _require(state == 'needs_review', 'stage_decision_output_invalid')
    return 'needs_review', None


def _parse_output(raw, contract):
    try:
        return parse_stage_output(raw, contract)
    except ValueError:
        raise StageDecisionError('stage_decision_output_invalid') from None


def _gate_event(body, identity, digest):
    return {'step_id': body['step_id'], 'decision': body['gate'],
            'revision_sha256': body['input_revision_sha256'], 'artifact_id': identity,
            'evidence_sha256': digest, 'verification_scope': 'stage_contract'}


def _decision_event(body, identity, digest):
    return {'artifact_id': identity, 'sha256': digest, 'outcome': body['outcome'],
            'gate': body['gate'], 'verification_scope': 'stage_contract'}


def _metadata(body, path, raw):
    return {'id': _identity(body['attempt_id'], body['generation']),
            'session_id': body['session_id'], 'attempt_id': body['attempt_id'],
            'generation': body['generation'], 'path': f"routed/{body['attempt_id']}/{body['generation']}/stage-decision.json",
            'storage_path': str(path), 'bytes': len(raw), 'sha256': _sha(raw),
            'mime': 'application/json', 'provenance': 'controller_stage_contract',
            'verification_scope': 'stage_contract', 'verification_pass': False,
            'input_revision_sha256': body['input_revision_sha256'],
            'output_revision_sha256': body['carried_revision_sha256']}


def _references(db, child, body):
    values = {}
    for prefix in ('candidate', 'observation'):
        value = _artifact(db, body[prefix + '_artifact_id'], child)
        _require(_sha(_raw(value)) == body[prefix + '_metadata_sha256'], 'stage_decision_evidence_changed')
        _event(db, child['id'], 'artifact.created', {k:v for k,v in value.items() if k != 'storage_path'}, artifact_id=value['id'])
        values[prefix] = value
    candidate, observation = values['candidate'], values['observation']
    _require(candidate.get('provenance') == 'controller_captured_candidate'
             and candidate.get('verification_pass') is False
             and candidate.get('sha256') == body['candidate_revision_sha256']
             and candidate.get('output_revision_sha256') == body['candidate_revision_sha256']
             and candidate.get('input_revision_sha256') == body['input_revision_sha256']
             and candidate.get('launch_binding_sha256') == body['launch_binding_sha256']
             and candidate.get('cleanup_sha256') == body['cleanup_sha256']
             and observation.get('kind') == 'worker_observation'
             and observation.get('provenance') == 'worker_reported'
             and observation.get('verification_pass') is False
             and observation.get('binding_digest') == body['launch_binding_sha256'], 'stage_decision_evidence_changed')
    _event(db, child['id'], 'workflow.candidate_published', {'artifact_id':candidate['id'],
        'generation':child['generation'], 'input_revision_sha256':body['input_revision_sha256'],
        'output_revision_sha256':body['candidate_revision_sha256'], 'verification_pass':False})
    _event(db, child['id'], 'workflow.observation_published', {'artifact_id':observation['id'],
        'sha256':observation['sha256'], 'generation':child['generation'],
        'binding_digest':body['launch_binding_sha256'], 'provenance':'worker_reported', 'verification_pass':False})
    return values



def _relation(body, assignment):
    for key in ('generation','root_generation','reservation_version'):
        _require(type(body[key]) is int and body[key] > 0, 'stage_decision_relation_changed')
    for key in ('input_revision_sha256','candidate_revision_sha256','carried_revision_sha256',
                'cleanup_sha256','runtime_id'):
        _require(type(body[key]) is str and re.fullmatch('[0-9a-f]{64}',body[key]) is not None,
                 'stage_decision_relation_changed')
    common = {key:assignment[name] for key,name in (
        ('owner_id','owner_id'),('project_id','project_id'),('session_id','session_id'),
        ('turn_id','turn_id'),('root_attempt_id','root_id'),('root_generation','root_generation'))}
    for key in ('input_binding','candidate_binding'):
        value = body[key]
        _require(type(value) is dict and set(value) == set(common) | {'attempt_id','generation'}
                 and all(_raw(value[k]) == _raw(v) for k,v in common.items())
                 and type(value['attempt_id']) is str and bool(value['attempt_id'])
                 and type(value['generation']) is int and value['generation'] > 0,
                 'stage_decision_relation_changed')
    _require(body['candidate_binding']['attempt_id'] == body['attempt_id']
             and body['candidate_binding']['generation'] == body['generation'],
             'stage_decision_relation_changed')


def load_finalized_stage(db, child_id, *, forbidden_values=()):
    """Authenticate one historical stage record; no live callback or mutation."""
    policy = validate_forbidden_values(forbidden_values)
    _require(db.in_transaction and type(child_id) is str, 'stage_decision_read_context_invalid')
    child = db.execute('SELECT * FROM attempts WHERE id=?', (child_id,)).fetchone()
    _require(child is not None and child['execution_kind'] == 'hermes_child'
             and child['state'] == 'completed', 'stage_decision_not_finalized')
    saved = _json(child['result'] or '{}').get('routed_stage_decision')
    _require(type(saved) is dict and set(saved) == {'artifact_id','sha256'}, 'stage_decision_not_finalized')
    metadata = _artifact(db, saved['artifact_id'], child)
    _require(type(metadata.get('bytes')) is int and 0 < metadata['bytes'] <= 256 * 1024,
             'stage_decision_artifact_invalid')
    raw = _read(Path(metadata['storage_path']), 256 * 1024)
    _scan(raw, policy)
    body = _json(raw)
    _require(type(body) is dict and set(body) == _BODY_KEYS and _raw(body) == raw
             and type(body['schema_version']) is int and body['schema_version'] == 1
             and body['verification_scope'] == 'stage_contract'
             and saved['artifact_id'] == _identity(child_id, child['generation'])
             and saved['sha256'] == _sha(raw)
             and _raw(metadata) == _raw(_metadata(body, metadata['storage_path'], raw))
             and body['attempt_id'] == child_id and body['generation'] == child['generation']
             and body['session_id'] == child['session_id'] and body['outcome'] == child['outcome']
             and body['root_id'] == child['workflow_root_id']
             and body['root_generation'] == child['workflow_parent_generation'], 'stage_decision_record_changed')
    launch = db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?', (child_id,)).fetchone()
    root = db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?', (body['root_id'],)).fetchone()
    _require(launch is not None and root is not None, 'stage_decision_launch_missing')
    binding = _json(launch['binding']); assignment = binding['assignment']
    _require(_sha(launch['binding'].encode()) == launch['binding_digest'] == body['launch_binding_sha256']
             and _sha(_raw(assignment)) == body['assignment_sha256']
             and binding['profile_digest'] == body['profile_sha256']
             and launch['controller_instance_id'] == body['controller_instance_id']
             and launch['state'] == 'fenced' and type(launch['generation']) is int
             and launch['generation'] == body['generation']
             and binding['child_id'] == child_id and binding['generation'] == body['generation']
             and binding['caller_spec_digest'] == body['caller_spec_sha256']
             and binding['input_revision_sha256'] == body['input_revision_sha256']
             and assignment['session_id'] == body['session_id']
             and assignment['turn_id'] == child['turn_id']
             and binding['runtime_id'] == body['runtime_id'] == child['runtime_id']
             and _sha(_raw(binding['caller_spec'])) == body['caller_spec_sha256']
             and all(body[key] == assignment[name] for key,name in (
                 ('root_id','root_id'),('root_generation','root_generation'),('role','role'),
                 ('step_id','step_id'),('workflow_sha256','workflow_digest'),
                 ('input_revision_sha256','input_revision_sha256'))), 'stage_decision_launch_changed')
    _relation(body, assignment)
    contract = _contract(root, assignment)
    output = _parse_output(json.dumps(body['stage_output'], ensure_ascii=False, separators=(',',':')), contract)
    _require(body['stage_contract_version'] == VERSION and body['stage_contract_sha256'] == contract.digest
             and _sha(_raw(output.to_dict())) == body['stage_output_sha256']
             and _outcome(output) == (body['outcome'],body['gate'])
             and type(body['readonly']) is bool and body['readonly'] == (body['role'] not in CODING_ROLES)
             and binding['caller_spec']['workspace_readonly'] is body['readonly']
             and body['carried_revision_sha256'] == (body['input_revision_sha256'] if body['readonly'] else body['candidate_revision_sha256']),
             'stage_decision_output_changed')
    _references(db, child, body)
    _event(db, child_id, 'artifact.created', {k:v for k,v in metadata.items() if k != 'storage_path'}, artifact_id=metadata['id'])
    lifecycle = db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='attempt.state' AND json_extract(payload,'$.state') IN ('verifying','completed') ORDER BY sequence LIMIT 3", (child_id,)).fetchall()
    _require([_json(row[0]) for row in lifecycle] == [{'state':'verifying'}, {'state':'completed','outcome':body['outcome']}],
             'stage_decision_event_changed')
    gate = db.execute('SELECT * FROM workflow_step_gates WHERE root_id=? AND step_id=?', (body['root_id'],body['step_id'])).fetchone()
    if body['gate'] is None:
        _require(gate is None and not db.execute("SELECT 1 FROM events WHERE attempt_id=? AND type='workflow.step_gate' LIMIT 1", (child_id,)).fetchone(), 'stage_decision_gate_changed')
    else:
        expected = (body['root_id'],body['step_id'],child_id,child['generation'],body['gate'],
                    body['input_revision_sha256'],metadata['id'],metadata['sha256'],metadata['sha256'])
        _require(gate is not None and tuple(gate[key] for key in ('root_id','step_id','child_id','generation','decision','revision_sha256','artifact_id','artifact_sha256','evidence_sha256')) == expected, 'stage_decision_gate_changed')
        _event(db,child_id,'workflow.step_gate',_gate_event(body,metadata['id'],metadata['sha256']))
    _event(db,child_id,'workflow.stage_decision',_decision_event(body,metadata['id'],metadata['sha256']))
    return body, metadata


@dataclass(frozen=True)
class CommittedStage:
    artifact_id: str
    attempt_id: str
    generation: int
    sha256: str
    outcome: str
    gate: str | None
    input_revision_sha256: str
    candidate_revision_sha256: str
    carried_revision_sha256: str
    event_sequence: int


def _committed(db, body, metadata):
    sequence = _event(db,body['attempt_id'],'workflow.stage_decision',_decision_event(body,metadata['id'],metadata['sha256']))
    return CommittedStage(metadata['id'],body['attempt_id'],body['generation'],metadata['sha256'],
        body['outcome'],body['gate'],body['input_revision_sha256'],body['candidate_revision_sha256'],
        body['carried_revision_sha256'],sequence)


def _scope(caller, results, input_revision):
    _require(type(input_revision) is WorkspaceRevision and results.candidate is not None,
             'stage_decision_candidate_required')
    return {'attempt_id':caller.attempt_id, 'generation':caller.generation,
            'session_id':caller.session_id, 'caller_spec_sha256':caller.digest,
            'launch_binding_sha256':results.quiesced.binding_digest,
            'runtime_id':results.quiesced.runtime_id, 'input_revision_sha256':input_revision.sha256,
            'candidate_revision_sha256':results.candidate.revision.sha256,
            'candidate_artifact_id':results.candidate.artifact_id,
            'observation_artifact_id':results.observation.artifact_id,
            'input_binding':asdict(input_revision.binding),
            'candidate_binding':asdict(results.candidate.revision.binding)}


def _replay(db, caller, scope, policy):
    child = db.execute('SELECT state,result FROM attempts WHERE id=?', (caller.attempt_id,)).fetchone()
    _require(child is not None, 'stage_decision_child_missing')
    result = _json(child['result'] or '{}')
    if 'routed_stage_decision' not in result:
        _require(child['state'] not in TERMINAL, 'stage_decision_terminal_record_missing')
        return None
    body, metadata = load_finalized_stage(db,caller.attempt_id,forbidden_values=policy)
    _require(all(_raw(body[key]) == _raw(value) for key,value in scope.items()), 'stage_decision_replay_conflict')
    return body, metadata


def _storage(root, caller, input_revision, candidate_revision):
    root = Path(root)
    fd = _directory(root,private=True)
    try:
        _require(stat.S_IMODE(os.fstat(fd).st_mode) in (0o700,0o2700), 'stage_decision_storage_not_private')
    finally:
        os.close(fd)
    for source in (caller.workspace,caller.scratch,caller.task_dir,caller.worker_socket_dir,
                   input_revision.path,candidate_revision.path):
        source = Path(source)
        _require(root != source and root not in source.parents and source not in root.parents,
                 'stage_decision_storage_overlap')
    return root


def complete_stage(scheduler, routed, caller, results, *, input_revision, storage_root,
                   services, forbidden_values):
    policy = validate_forbidden_values(forbidden_values)
    scope = _scope(caller,results,input_revision)
    with _child_control_tx(scheduler.store,write=False) as db:
        replay = _replay(db,caller,scope,policy)
        if replay:
            return _committed(db,*replay)
    observed = load_observation(scheduler,routed.root,caller,results.quiesced,forbidden_values=policy)
    _require(observed.status == 'completed' and observed.exit_code == 0 and observed.failure is None,
             'stage_decision_execution_incomplete')
    original = verify_revision(scheduler.store,input_revision)
    candidate = verify_revision(scheduler.store,results.candidate.revision)
    storage = _storage(storage_root,caller,input_revision,results.candidate.revision)
    with _exclusive_driver(routed,caller):
        collector = RoutedChildCleanup(scheduler,routed,caller,results.quiesced,services=services)
        evidence = collector.collect()
        _require(evidence == results.cleanup, 'stage_decision_cleanup_changed')
        proof = read_cleanup_evidence(collector,evidence)
        with _child_control_tx(scheduler.store,write=False) as db:
            context = result_authority(scheduler,db,caller,results.quiesced)
            assignment = context['assignment']; contract = _contract(context['root'],assignment)
            child = context['child']
            observation_metadata = _artifact(db,results.observation.artifact_id,child)
            candidate_metadata = _artifact(db,results.candidate.artifact_id,child)
        readonly = assignment['role'] not in CODING_ROLES
        _require(caller.workspace_readonly is readonly and assignment['input_revision_sha256'] == input_revision.sha256,
                 'stage_decision_revision_changed')
        for binding in (input_revision.binding, results.candidate.revision.binding):
            _require(all(getattr(binding,key) == assignment[name] for key,name in (
                ('owner_id','owner_id'),('project_id','project_id'),('session_id','session_id'),
                ('turn_id','turn_id'),('root_attempt_id','root_id'),('root_generation','root_generation'))),
                'stage_decision_revision_scope_changed')
        _require((results.candidate.revision.binding.attempt_id,results.candidate.revision.binding.generation)
                 == (caller.attempt_id,caller.generation), 'stage_decision_revision_scope_changed')
        _require(not readonly or all(original[k] == candidate[k] for k in ('files','directories','selected_paths','scope')),
                 'stage_decision_readonly_changed')
        observation_raw = _read(Path(observation_metadata['storage_path']),32 * 1024**2)
        _scan(observation_raw,policy)
        _require(_sha(observation_raw) == observation_metadata['sha256'] == results.observation.sha256
                 and len(observation_raw) == observation_metadata['bytes']
                 and _json(observation_raw)['observation'] == _json(_raw(asdict(observed))),
                 'stage_decision_observation_changed')
        launch_raw = _read(_location(routed.root,caller)/'launch.json',512 * 1024)
        _require(_sha(launch_raw) == dict(caller.task_files)['launch.json'], 'stage_decision_launch_changed')
        receipt_raw = _json(launch_raw)['receipt_json']; receipt = _json(receipt_raw)
        _require(_sha(receipt_raw.encode()) == observed.launch_receipt_sha256
                 and receipt.get('stage_contract_sha256') == contract.digest
                 and _raw(receipt.get('stage_contract')) == _raw(contract.to_dict()), 'stage_decision_launch_changed')
        _require(observed.events and observed.events[-1].type == 'adapter.result', 'stage_decision_output_invalid')
        output = _parse_output(observed.events[-1].payload['summary'],contract)
        outcome, gate = _outcome(output)
        body = {'schema_version':1,'verification_scope':'stage_contract',**scope,
            'root_id':assignment['root_id'],'root_generation':assignment['root_generation'],
            'step_id':assignment['step_id'],'role':assignment['role'],
            'workflow_sha256':assignment['workflow_digest'], 'assignment_sha256':context['binding'].assignment_digest,
            'profile_sha256':context['binding'].profile_digest,
            'stage_contract_version':VERSION,'stage_contract_sha256':contract.digest,
            'stage_output':output.to_dict(),'stage_output_sha256':_sha(_raw(output.to_dict())),
            'carried_revision_sha256':input_revision.sha256 if readonly else results.candidate.revision.sha256,
            'readonly':readonly,'observation_metadata_sha256':_sha(_raw(observation_metadata)),
            'candidate_metadata_sha256':_sha(_raw(candidate_metadata)), 'cleanup_sha256':evidence.evidence_sha256,
            'outcome':outcome,'gate':gate,'reservation_version':context['root']['version'],
            'controller_instance_id':scheduler.leases.instance_id,'accounts_retained':context['accounts']}
        _require(set(body) == _BODY_KEYS,'stage_decision_body_invalid')
        _relation(body,assignment)
        raw = _raw(body); _require(len(raw) <= 256 * 1024,'stage_decision_body_limit'); _scan(raw,policy)
        path = storage/('stage-decision-'+_sha(raw)+'.json')
        _immutable(path,raw,mode=0o600)
        metadata = _metadata(body,path,raw)
        with _child_control_tx(scheduler.store) as db:
            replay = _replay(db,caller,scope,policy)
            if replay:
                _require(replay[1]['sha256'] == metadata['sha256'], 'stage_decision_replay_conflict')
                return _committed(db,*replay)
            current = result_authority(scheduler,db,caller,results.quiesced)
            _require(current['assignment'] == assignment and _contract(current['root'],assignment) == contract,
                     'stage_decision_assignment_changed')
            _references(db,current['child'],body)
            validate_cleanup_membership(db,proof,current['binding'],current['root'],current['accounts'],scheduler)
            _require(not db.execute('SELECT 1 FROM workflow_step_gates WHERE root_id=? AND step_id=?',
                (body['root_id'],body['step_id'])).fetchone(), 'stage_decision_gate_conflict')
            db.execute('INSERT INTO artifacts(id,session_id,attempt_id,metadata) VALUES(?,?,?,?)',
                (metadata['id'],caller.session_id,caller.attempt_id,encode(metadata)))
            scheduler.store._event(db,caller.session_id,caller.attempt_id,'artifact.created',
                {k:v for k,v in metadata.items() if k != 'storage_path'})
            state = current['child']['state']
            for next_state in ('verifying','completed'):
                _require(next_state in TRANSITIONS.get(state,set()), 'stage_decision_lifecycle_unsupported')
                scheduler.store._event(db,caller.session_id,caller.attempt_id,'attempt.state',
                    {'state':next_state,**({'outcome':outcome} if next_state == 'completed' else {})})
                state = next_state
            result = dict(current['child'].get('result') or {})
            _require(not any(key in result for key in ('routed_decision','routed_stage_decision')), 'stage_decision_record_conflict')
            result['routed_stage_decision'] = {'artifact_id':metadata['id'],'sha256':metadata['sha256']}
            provider_leases.revoke_attempt(db,caller.attempt_id,'attempt_terminal')
            db.execute("UPDATE attempts SET state='completed',outcome=?,result=?,updated_at=? WHERE id=? AND generation=?",
                (outcome,encode(result),now(),caller.attempt_id,caller.generation))
            if gate is not None:
                db.execute('INSERT INTO workflow_step_gates VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (body['root_id'],body['step_id'],caller.attempt_id,caller.generation,gate,
                     input_revision.sha256,metadata['id'],metadata['sha256'],metadata['sha256'],now()))
                scheduler.store._event(db,caller.session_id,caller.attempt_id,'workflow.step_gate',
                    _gate_event(body,metadata['id'],metadata['sha256']))
            scheduler.store._event(db,caller.session_id,caller.attempt_id,'workflow.stage_decision',
                _decision_event(body,metadata['id'],metadata['sha256']))
            db.execute('UPDATE inference_budget_roots SET high_water_us=max(high_water_us,?) WHERE root_id=?',
                (_stamp(current['observed_at']),body['root_id']))
            return _committed(db,body,metadata)


def _release_valid(payload,body,metadata):
    expected = {'schema_version','decision_sha256','target','outcome','caller_provider_cleanup_sha256',
                'accounts_retained','controller_instance_id','receipt_sha256'}
    _require(type(payload) is dict and set(payload) == expected and type(payload['schema_version']) is int
             and payload['schema_version'] == 1 and payload['decision_sha256'] == metadata['sha256']
             and payload['outcome'] == 'confirmed_stopped'
             and _raw(payload['target']) == _raw({'root_id':body['root_id'],'root_generation':body['root_generation'],
                 'child_id':body['attempt_id'],'child_generation':body['generation'],
                 'reservation_version':body['reservation_version'],'runtime_id':body['runtime_id']})
             and _raw(payload['accounts_retained']) == _raw(body['accounts_retained'])
             and payload['controller_instance_id'] == body['controller_instance_id']
             and type(payload['caller_provider_cleanup_sha256']) is str
             and re.fullmatch('[0-9a-f]{64}',payload['caller_provider_cleanup_sha256']) is not None
             and payload['receipt_sha256'] == _sha(_raw({k:v for k,v in payload.items() if k != 'receipt_sha256'})),
             'stage_decision_release_changed')


def load_stage_release(db,child_id,*,forbidden_values=()):
    body, metadata = load_finalized_stage(db,child_id,forbidden_values=forbidden_values)
    rows = db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='workflow.stage_child_released' LIMIT 2",(child_id,)).fetchall()
    if not rows:
        return None
    _require(len(rows) == 1,'stage_decision_release_changed')
    payload = _json(rows[0]['payload']); _release_valid(payload,body,metadata)
    _scan(_raw(payload),validate_forbidden_values(forbidden_values))
    return payload


def release_stage_child(scheduler,routed,caller,results,*,input_revision,decision_sha256,
                        services,forbidden_values):
    policy = validate_forbidden_values(forbidden_values); scope = _scope(caller,results,input_revision)
    def read(db):
        pair = _replay(db,caller,scope,policy)
        _require(pair is not None and pair[1]['sha256'] == decision_sha256,'stage_decision_release_changed')
        return pair, load_stage_release(db,caller.attempt_id,forbidden_values=policy)
    with _child_control_tx(scheduler.store,write=False) as db:
        pair, replay = read(db)
    if replay is not None:
        return replay
    with _exclusive_driver(routed,caller):
        collector = RoutedChildCleanup(scheduler,routed,caller,results.quiesced,services=services)
        target,bound,accounts,_,_ = collector._snapshot()
        evidence = collector.collect(target=target); proof = read_cleanup_evidence(collector,evidence)
        payload = {'schema_version':1,'decision_sha256':decision_sha256,'target':asdict(target),
            'outcome':'confirmed_stopped','caller_provider_cleanup_sha256':evidence.evidence_sha256,
            'accounts_retained':accounts,'controller_instance_id':scheduler.leases.instance_id}
        payload['receipt_sha256'] = _sha(_raw(payload)); _release_valid(payload,*pair); _scan(_raw(payload),policy)
        with _child_control_tx(scheduler.store) as db:
            _, replay = read(db)
            if replay is not None:
                return replay
            child = scheduler.store._attempt(db,caller.attempt_id)
            root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(target.root_id,)).fetchone()
            _require(root is not None and scheduler._owner_current(db,target.root_id)
                     and child['generation'] == target.child_generation and child['runtime_id'] == target.runtime_id
                     and child['state'] == 'completed' and root['generation'] == target.root_generation,
                     'stage_decision_release_authority_changed')
            validate_cleanup_membership(db,proof,bound,dict(root),accounts,scheduler)
            changed = db.execute("UPDATE workflow_roots SET child_attempt_id=NULL,version=version+1 WHERE root_id=? AND generation=? AND child_attempt_id=? AND version=? AND state='held'",
                (target.root_id,target.root_generation,caller.attempt_id,target.reservation_version)).rowcount
            _require(changed == 1,'stage_decision_release_target_changed')
            scheduler.store._event(db,caller.session_id,caller.attempt_id,'workflow.stage_child_released',payload)
        return payload
