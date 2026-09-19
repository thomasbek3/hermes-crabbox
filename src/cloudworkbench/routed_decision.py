"""Controller task-acceptance decisions. No semantic review or root promotion."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from pathlib import Path

from . import provider_leases
from .inference_budget import _stamp
from .models import TERMINAL, TRANSITIONS
from .routed_cleanup import RoutedChildCleanup
from .routed_driver import _exclusive_driver
from .routed_observation_store import validate_forbidden_values, load_observation
from .routed_observation_store import _location as _observation_location
from .routed_publication import _read, result_authority, read_cleanup_evidence, validate_cleanup_membership
from .routed_verification import (load_published_verification, _reconcile_checks,
    _runtime_specs, _immutable, _scan)
from .scheduler import _child_control_tx
from .store import encode, now
from .workflow_revisions import verify_revision, WorkspaceRevision
from .stage_contracts import VERSION as STAGE_VERSION, stage_contract, parse_stage_output


class DecisionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value:
        raise DecisionError(code)


def _raw(value):
    return encode(value).encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


_BODY_KEYS = frozenset(('schema_version verification_scope attempt_id generation session_id caller_spec_sha256 '
    'launch_binding_sha256 runtime_id plan_sha256 input_revision_sha256 tested_revision_sha256 '
    'carried_revision_sha256 candidate_artifact_id input_binding root_id root_generation reservation_version '
    'controller_instance_id step_id role assignment_sha256 workflow_sha256 profile_sha256 accounts_retained '
    'candidate_metadata_sha256 observation_artifact_id observation_metadata_sha256 verification_metadata_sha256 '
    'cleanup_sha256 verification_artifact_id verification_receipt_sha256 outcome gate verifier_checks remaining_criteria').split())
_QUALIFIED_KEYS = frozenset(('stage_contract_version','stage_contract_sha256','stage_output_sha256'))


def _json(raw):
    def pairs(items):
        result = {}
        for key,value in items:
            _require(key not in result,'decision_json_invalid')
            result[key]=value
        return result
    try:
        return json.loads(raw,object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise DecisionError('decision_json_invalid') from None


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None


def _body_schema(body,qualified):
    _require(type(body) is dict and set(body)==_BODY_KEYS | (_QUALIFIED_KEYS if qualified else set()),'decision_body_invalid')
    _require(type(body['schema_version']) is int and body['schema_version']==1,'decision_body_invalid')
    for key in ('generation','root_generation','reservation_version'):
        _require(type(body[key]) is int and 1<=body[key]<=2**63-1,'decision_body_invalid')
    for key,value in body.items():
        if key.endswith('_sha256') or key=='runtime_id':
            _require(_digest(value),'decision_body_invalid')
    for key in ('attempt_id','session_id','root_id','candidate_artifact_id','observation_artifact_id',
                'verification_artifact_id','controller_instance_id','step_id','role'):
        _require(type(body[key]) is str and 1<=len(body[key])<=128,'decision_body_invalid')
    _require(body['verification_scope']=='task_acceptance' and body['role']=='acceptance_verification'
        and body['step_id']=='verify' and type(body['outcome']) is str
        and body['outcome'] in ('verified','rejected','needs_review')
        and body['gate']=={'verified':'pass','rejected':'reject','needs_review':None}[body['outcome']]
        and body['carried_revision_sha256']==body['input_revision_sha256'],'decision_body_invalid')
    _require(type(body['remaining_criteria']) is list and len(body['remaining_criteria'])<=100
        and all(type(v) is str and 1<=len(v)<=128 for v in body['remaining_criteria'])
        and len(set(body['remaining_criteria']))==len(body['remaining_criteria']),'decision_body_invalid')
    _require(type(body['verifier_checks']) is list and len(body['verifier_checks'])<=32
        and all(type(v) is dict and set(v)=={'spec_sha256','runtime_id'}
            and _digest(v['spec_sha256']) and _digest(v['runtime_id']) for v in body['verifier_checks']),
        'decision_body_invalid')
    _require(type(body['accounts_retained']) is list and 1<=len(body['accounts_retained'])<=8,'decision_body_invalid')
    seen=set()
    for account in body['accounts_retained']:
        _require(type(account) is dict and set(account)=={'account_id','reservation_id','epoch','controller_instance_id','persistent_owner_id'}
            and type(account['epoch']) is int and account['epoch']>0
            and all(type(account[k]) is str and 1<=len(account[k])<=128 for k in account if k!='epoch')
            and account['account_id'] not in seen,'decision_body_invalid')
        seen.add(account['account_id'])


def _artifact_event(db,caller,metadata):
    rows=db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='artifact.created' AND json_extract(payload,'$.id')=? LIMIT 2",
        (caller.attempt_id,metadata['id'])).fetchall()
    _require(len(rows)==1 and _raw(_json(rows[0]['payload']))==_raw({k:v for k,v in metadata.items() if k!='storage_path'}),
        'decision_event_conflict')


def _stored_evidence(db,caller,body,assigned,contract,secrets):
    """Authenticate persisted bytes and their recorded semantics, never rerun checks."""
    values={};documents={}
    for key,maximum in (('candidate',2*1024**2),('verification',256*1024),('observation',32*1024**2)):
        metadata=_artifact(db,body[key+'_artifact_id'],caller)
        _require(_sha(_raw(metadata))==body[key+'_metadata_sha256'],'decision_evidence_changed')
        _require(type(metadata.get('bytes')) is int and 0<metadata['bytes']<=maximum,'decision_evidence_changed')
        raw=_read(Path(metadata['storage_path']),maximum,private=key!='candidate')
        _scan(raw,secrets)
        _require(len(raw)==metadata['bytes'] and _sha(raw)==metadata['sha256'],'decision_evidence_changed')
        documents[key]=_json(raw);values[key]=metadata
        _require(json.dumps(documents[key],sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()==raw, 'decision_evidence_changed')
        _artifact_event(db,caller,metadata)
    candidate=documents['candidate']; cm=values['candidate']
    common={key:assigned[name] for key,name in (('owner_id','owner_id'),('project_id','project_id'),
        ('session_id','session_id'),('turn_id','turn_id'),('root_attempt_id','root_id'),('root_generation','root_generation'))}
    scope={**common,'attempt_id':caller.attempt_id,'generation':caller.generation}
    _require(type(candidate) is dict and set(candidate)=={'schema_version','binding','selected_paths','directories','files','scope'}
        and type(candidate['schema_version']) is int and candidate['schema_version']==1
        and _raw(candidate['binding'])==_raw(scope) and candidate['scope']=='controller_selected_deliverables'
        and all(type(candidate[k]) is list for k in ('selected_paths','directories','files'))
        and cm.get('provenance')=='controller_captured_candidate' and cm.get('verification_pass') is False
        and cm['sha256']==cm.get('output_revision_sha256')==body['tested_revision_sha256']
        and cm.get('input_revision_sha256')==body['input_revision_sha256']
        and cm.get('launch_binding_sha256')==body['launch_binding_sha256']
        and cm.get('cleanup_sha256')==body['cleanup_sha256'],'decision_candidate_changed')
    _one_event(db,caller,'workflow.candidate_published',{'artifact_id':cm['id'],'generation':caller.generation,
        'input_revision_sha256':body['input_revision_sha256'],'output_revision_sha256':body['tested_revision_sha256'],'verification_pass':False})
    observation=documents['observation'];om=values['observation']
    _require(type(observation) is dict and set(observation)=={'version','kind','provenance','verification_pass','gate_authorized','revision_promotion_authorized','scope','binding','cleanup_evidence_sha256','observation'}
        and type(observation['version']) is int and observation['version']==1
        and observation['kind']=='worker_observation' and observation['provenance']=='worker_reported'
        and all(observation[k] is False for k in ('verification_pass','gate_authorized','revision_promotion_authorized'))
        and _raw(observation['scope'])==_raw(scope) and observation['cleanup_evidence_sha256']==body['cleanup_sha256']
        and om.get('kind')=='worker_observation' and om.get('provenance')=='worker_reported'
        and om.get('verification_pass') is False and om.get('binding_digest')==body['launch_binding_sha256'],
        'decision_observation_changed')
    expected_binding={'child_id':caller.attempt_id,'generation':caller.generation,'runtime_id':body['runtime_id'],
        'caller_spec_digest':body['caller_spec_sha256'],'profile_digest':body['profile_sha256'],
        'input_revision_sha256':body['input_revision_sha256'],'assignment_digest':body['assignment_sha256'],
        'binding_digest':body['launch_binding_sha256'],'state':'fenced','may_start':False}
    _require(_raw(observation['binding'])==_raw(expected_binding),'decision_observation_changed')
    from .routed_observation_store import _parse
    from .routed_collection import FileEvidence
    saved=observation['observation']
    try:
        parsed=_parse(saved['result_json'].encode(),saved['events_jsonl'].encode(),
            FileEvidence(**saved['result_evidence']),FileEvidence(**saved['events_evidence']),saved['launch_receipt_sha256'])
    except (ValueError,KeyError,TypeError,UnicodeError):
        raise DecisionError('decision_observation_changed') from None
    _require(_raw(asdict(parsed))==_raw(saved) and parsed.status=='completed' and parsed.exit_code==0
        and parsed.failure is None,'decision_observation_changed')
    if contract is not None:
        try: output=parse_stage_output(parsed.events[-1].payload['summary'],contract)
        except (ValueError,IndexError,KeyError,TypeError):raise DecisionError('decision_stage_answer_invalid') from None
        _require(output.status=='complete' and _sha(_raw(output.to_dict()))==body['stage_output_sha256'],
            'decision_stage_answer_invalid')
    _one_event(db,caller,'workflow.observation_published',{'artifact_id':om['id'],'sha256':om['sha256'],
        'generation':caller.generation,'binding_digest':body['launch_binding_sha256'],'provenance':'worker_reported','verification_pass':False})
    verification=documents['verification'];vm=values['verification']
    _require(type(verification) is dict and set(verification)=={'schema_version','plan','assessment'}
        and type(verification['schema_version']) is int and verification['schema_version']==1,'decision_verification_changed')
    plan=verification['plan'];assessment=verification['assessment']
    _require(type(plan) is dict and set(plan)=={'schema_version','binding','input_revision_sha256','candidate_revision_sha256',
        'launch_binding_sha256','cleanup_sha256','environment_sha256','image','checks','acceptance','limits'}
        and type(plan['schema_version']) is int and plan['schema_version']==1 and _raw(plan['binding'])==_raw(scope)
        and _sha(_raw(plan))==body['plan_sha256'] and plan['input_revision_sha256']==body['input_revision_sha256']
        and plan['candidate_revision_sha256']==body['tested_revision_sha256']
        and plan['launch_binding_sha256']==body['launch_binding_sha256'] and plan['cleanup_sha256']==body['cleanup_sha256'],
        'decision_verification_changed')
    _require(type(assessment) is dict and set(assessment)=={'schema_version','plan_sha256','records','outcome','remaining_criteria'}
        and type(assessment['schema_version']) is int and assessment['schema_version']==1
        and assessment['plan_sha256']==body['plan_sha256'] and type(assessment['records']) is list
        and type(plan['checks']) is list and len(plan['checks'])<=32
        and len(assessment['records'])==len(plan['checks'])==len(body['verifier_checks']), 'decision_verification_changed')
    from .routed_verification_policy import CheckExecution,Criterion,VerificationLimits
    from .environments import Check
    try:
        VerificationLimits(**plan['limits'])
        checks=[Check.model_validate(c).model_dump() for c in plan['checks']]
        criteria=[Criterion(**c) for c in plan['acceptance']]
        records=[CheckExecution(**r) for r in assessment['records']]
    except (ValueError,TypeError,KeyError):raise DecisionError('decision_verification_changed') from None
    _require(_raw(checks)==_raw(plan['checks']) and len({c['id'] for c in checks})==len(checks)
        and len(criteria)<=100 and len({c.id for c in criteria})==len(criteria)
        and [r.check_id for r in records]==[c['id'] for c in checks]
        and len({r.runtime_id for r in records})==len(records)
        and [r.runtime_id for r in records]==[c['runtime_id'] for c in body['verifier_checks']]
        and all(r.cleanup_confirmed and not r.oom for r in records),'decision_verification_changed')
    passed={r.check_id for r in records if r.exit_code==0 and not r.timed_out}
    definitions={c['id']:c['description'] for c in checks}
    remaining=[c.id for c in criteria if c.mandatory and (c.id not in passed or definitions.get(c.id)!=c.description)]
    outcome='rejected' if len(passed)!=len(records) else 'needs_review' if not records or remaining else 'passed'
    _require(assessment['outcome']==outcome and _raw(assessment['remaining_criteria'])==_raw(remaining)
        and body['outcome']=={'passed':'verified','rejected':'rejected','needs_review':'needs_review'}[outcome]
        and body['remaining_criteria']==remaining and vm.get('provenance')=='controller_protected_checks'
        and vm['sha256']==body['verification_receipt_sha256'] and vm.get('plan_sha256')==body['plan_sha256']
        and vm.get('input_revision_sha256')==body['input_revision_sha256']
        and vm.get('verified_revision_sha256')==body['tested_revision_sha256']
        and vm.get('launch_binding_sha256')==body['launch_binding_sha256']
        and vm.get('cleanup_sha256')==body['cleanup_sha256'] and vm.get('outcome')==outcome
        and vm.get('protected_checks_passed') is (outcome=='passed'),'decision_verification_changed')
    _one_event(db,caller,'workflow.protected_verification',{'artifact_id':vm['id'],'plan_sha256':body['plan_sha256'],
        'receipt_sha256':vm['sha256'],'outcome':outcome,'remaining_criteria':remaining})


@dataclass(frozen=True)
class CommittedDecision:
    artifact_id: str
    attempt_id: str
    generation: int
    sha256: str
    outcome: str
    gate: str | None
    input_revision_sha256: str
    tested_revision_sha256: str
    carried_revision_sha256: str
    event_sequence: int


def _id(caller):
    return 'decision-' + _sha(f'{caller.attempt_id}:{caller.generation}'.encode())


def _scope(caller, results, prepared, input_revision):
    return {'attempt_id': caller.attempt_id, 'generation': caller.generation,
        'session_id': caller.session_id, 'caller_spec_sha256': caller.digest,
        'launch_binding_sha256': results.quiesced.binding_digest,
        'runtime_id': results.quiesced.runtime_id, 'plan_sha256': prepared.plan.digest,
        'input_revision_sha256': input_revision.sha256,
        'tested_revision_sha256': results.candidate.revision.sha256,
        'carried_revision_sha256': input_revision.sha256,
        'candidate_artifact_id': results.candidate.artifact_id,
        'input_binding': asdict(input_revision.binding)}


def _artifact(db, artifact_id, caller):
    row = db.execute('SELECT metadata FROM artifacts WHERE id=? AND attempt_id=? AND session_id=?',
        (artifact_id, caller.attempt_id, caller.session_id)).fetchone()
    _require(row is not None, 'decision_artifact_missing')
    metadata = _json(row['metadata'])
    _require(type(metadata) is dict and metadata.get('id') == artifact_id
        and metadata.get('attempt_id') == caller.attempt_id and metadata.get('session_id') == caller.session_id
        and type(metadata.get('generation')) is int and metadata['generation'] == caller.generation,
        'decision_artifact_changed')
    return metadata


def _gate_event(body, artifact_id, digest):
    return {'step_id': body['step_id'], 'decision': body['gate'],
        'revision_sha256': body['input_revision_sha256'], 'artifact_id': artifact_id,
        'evidence_sha256': digest, 'verification_scope': 'task_acceptance'}


def _decision_event(body, artifact_id, digest):
    return {'artifact_id': artifact_id, 'sha256': digest, 'outcome': body['outcome'],
        'verification_scope': 'task_acceptance', 'gate': body['gate']}


def _one_event(db, caller, kind, expected):
    rows = db.execute('SELECT sequence,payload FROM events WHERE attempt_id=? AND type=? LIMIT 2',
        (caller.attempt_id, kind)).fetchall()
    _require(len(rows) == 1 and _json(rows[0]['payload']) == expected, 'decision_event_conflict')
    return rows[0]['sequence']


def _expected_contract(root, assignment):
    _require(_sha(root['frozen'].encode()) == assignment['workflow_digest'],
             'decision_stage_contract_binding_changed')
    frozen = _json(root['frozen'])
    provenance = frozen['provenance']
    if 'stage_contract_version' not in provenance:
        return None
    _require(provenance['stage_contract_version'] == STAGE_VERSION,
             'decision_stage_contract_version_unsupported')
    try:
        refs = _json(assignment['payload'])['arguments']['context_refs']
        return stage_contract(assignment['role'], assignment['input_revision_sha256'], refs)
    except (KeyError, TypeError, ValueError):
        raise DecisionError('decision_stage_contract_invalid') from None


def _qualified_answer(contract, observed, routed, caller):
    if contract is None:
        return {}
    # This is the saved launch authenticated by load_observation, not a newly
    # supplied plan or mutable worker output selected by the caller.
    raw = _read(_observation_location(routed.root, caller) / 'launch.json', 512 * 1024)
    _require(_sha(raw) == dict(caller.task_files).get('launch.json'),
             'decision_stage_launch_changed')
    try:
        launch = _json(raw)
        receipt_raw = launch['receipt_json']
        _require(type(receipt_raw) is str and _sha(receipt_raw.encode()) == observed.launch_receipt_sha256,
                 'decision_stage_launch_changed')
        receipt = _json(receipt_raw)
        _require(receipt.get('stage_contract_sha256') == contract.digest
                 and _raw(receipt.get('stage_contract')) == _raw(contract.to_dict()), 'decision_stage_launch_changed')
        terminal = observed.events[-1]
        _require(terminal.type == 'adapter.result', 'decision_stage_answer_invalid')
        output = parse_stage_output(terminal.payload['summary'], contract)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        if isinstance(exc, DecisionError):
            raise
        raise DecisionError('decision_stage_answer_invalid') from None
    _require(output.status == 'complete', 'decision_stage_answer_incomplete')
    return {'stage_contract_version': STAGE_VERSION, 'stage_contract_sha256': contract.digest,
            'stage_output_sha256': _sha(_raw(output.to_dict()))}


def _record(db, caller, scope, *, secrets):
    """Read a complete historical commit; no fresh execution authority is granted."""
    row = db.execute('SELECT * FROM attempts WHERE id=?',
                     (caller.attempt_id,)).fetchone()
    _require(row is not None and type(caller.generation) is int and row['generation'] == caller.generation
        and row['execution_kind']=='hermes_child' and row['session_id']==caller.session_id,'decision_generation_changed')
    result = _json(row['result']) if row['result'] else {}
    saved = result.get('routed_decision')
    if saved is None:
        _require(row['state'] not in TERMINAL, 'decision_terminal_record_missing')
        return None
    _require(type(saved) is dict and set(saved) == {'artifact_id', 'sha256'}, 'decision_record_invalid')
    metadata = _artifact(db, saved['artifact_id'], caller)
    raw = _read(Path(metadata['storage_path']), 256 * 1024)
    _scan(raw, secrets)
    _require(_sha(raw) == saved['sha256'] == metadata.get('sha256') and len(raw) == metadata.get('bytes'),
             'decision_artifact_changed')
    body = _json(raw)
    _require(type(body) is dict,'decision_body_invalid')
    _body_schema(body,'stage_contract_version' in body)
    _require(_raw(body) == raw and body.get('schema_version') == 1
        and body.get('verification_scope') == 'task_acceptance'
        and all(_raw(body.get(k)) == _raw(v) for k, v in scope.items())
        and saved['artifact_id'] == _id(caller)
        and metadata.get('generation') == caller.generation
        and metadata.get('provenance') == 'controller_task_acceptance_decision'
        and row['state'] == 'completed' and row['outcome'] == body.get('outcome'), 'decision_replay_conflict')
    launch = db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',
        (caller.attempt_id,)).fetchone()
    _require(launch is not None and launch['state']=='fenced' and launch['generation']==caller.generation, 'decision_launch_missing')
    launch_body = _json(launch['binding']); assigned = launch_body['assignment']
    _require(launch['binding_digest'] == body['launch_binding_sha256']
        and _sha(launch['binding'].encode()) == launch['binding_digest']
        and launch_body['profile_digest'] == body['profile_sha256']
        and _sha(encode(assigned).encode()) == body['assignment_sha256']
        and assigned['role'] == body['role'] == 'acceptance_verification'
        and assigned['step_id'] == body['step_id'] == 'verify'
        and assigned['workflow_digest'] == body['workflow_sha256']
        and assigned['root_id'] == body['root_id']
        and assigned['root_generation'] == body['root_generation']
        and assigned['input_revision_sha256'] == body['input_revision_sha256'], 'decision_replay_conflict')
    root = db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?', (body['root_id'],)).fetchone()
    _require(root is not None, 'decision_replay_conflict')
    contract = _expected_contract(root, assigned)
    _body_schema(body,contract is not None)
    common = {key:assigned[name] for key,name in (('owner_id','owner_id'),('project_id','project_id'),('session_id','session_id'),('turn_id','turn_id'),('root_attempt_id','root_id'),('root_generation','root_generation'))}
    input_binding=body['input_binding']
    _require(type(input_binding) is dict and set(input_binding)==set(common)|{'attempt_id','generation'}
        and all(_raw(input_binding[k])==_raw(v) for k,v in common.items())
        and type(input_binding['attempt_id']) is str and 1<=len(input_binding['attempt_id'])<=128
        and type(input_binding['generation']) is int and 1<=input_binding['generation']<=2**31
        and launch_body['child_id']==caller.attempt_id and launch_body['generation']==caller.generation
        and launch_body['caller_spec_digest']==_sha(_raw(launch_body['caller_spec']))==body['caller_spec_sha256']
        and launch_body['runtime_id']==body['runtime_id']
        and launch_body['caller_spec']['workspace_readonly'] is True
        and launch['controller_instance_id']==body['controller_instance_id']
        and assigned['session_id']==row['session_id'] and assigned['turn_id']==row['turn_id']
        and assigned['root_id']==row['workflow_root_id'] and assigned['root_generation']==row['workflow_parent_generation']
        and row['runtime_id']==body['runtime_id'], 'decision_replay_conflict')
    frozen_accounts=_json(root['frozen'])['accounts']
    _require({a['account_id']:a['persistent_owner_id'] for a in body['accounts_retained']}==frozen_accounts
        and all(a['controller_instance_id']==body['controller_instance_id'] for a in body['accounts_retained']), 'decision_replay_conflict')
    if contract is not None:
        _require(body.get('stage_contract_version') == STAGE_VERSION
                 and body.get('stage_contract_sha256') == contract.digest
                 and type(body.get('stage_output_sha256')) is str
                 and re.fullmatch('[0-9a-f]{64}', body['stage_output_sha256']) is not None,
                 'decision_replay_conflict')
    try:
        _stored_evidence(db,caller,body,assigned,contract,secrets)
    except (ValueError,OSError,KeyError,TypeError,UnicodeError,RecursionError) as exc:
        if isinstance(exc,DecisionError):raise
        raise DecisionError('decision_evidence_invalid') from None
    artifact_events = db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='artifact.created' AND json_extract(payload,'$.id')=? LIMIT 2",
        (caller.attempt_id,saved['artifact_id'])).fetchall()
    _require(len(artifact_events) == 1 and _json(artifact_events[0]['payload']) ==
        {k:v for k,v in metadata.items() if k != 'storage_path'}, 'decision_event_conflict')
    lifecycle = db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='attempt.state' AND json_extract(payload,'$.state') IN ('verifying','completed') ORDER BY sequence LIMIT 3",
        (caller.attempt_id,)).fetchall()
    _require([_json(r['payload']) for r in lifecycle] ==
        [{'state':'verifying'},{'state':'completed','outcome':body['outcome']}], 'decision_event_conflict')
    gate = db.execute('SELECT * FROM workflow_step_gates WHERE root_id=? AND step_id=?',
        (body['root_id'], body['step_id'])).fetchone()
    expected = (body['root_id'], body['step_id'], caller.attempt_id, caller.generation,
        body['gate'], body['input_revision_sha256'], saved['artifact_id'], saved['sha256'], saved['sha256'])
    if body['gate'] is None:
        _require(gate is None and body['outcome'] == 'needs_review', 'decision_gate_conflict')
        _require(not db.execute("SELECT 1 FROM events WHERE attempt_id=? AND type='workflow.step_gate' LIMIT 1",
            (caller.attempt_id,)).fetchone(), 'decision_event_conflict')
    else:
        _require(gate is not None and tuple(gate[k] for k in ('root_id','step_id','child_id','generation',
            'decision','revision_sha256','artifact_id','artifact_sha256','evidence_sha256')) == expected,
            'decision_gate_conflict')
        _one_event(db, caller, 'workflow.step_gate', _gate_event(body, saved['artifact_id'], saved['sha256']))
    seq = _one_event(db, caller, 'workflow.task_decision', _decision_event(body, saved['artifact_id'], saved['sha256']))
    receipt = CommittedDecision(saved['artifact_id'], caller.attempt_id, caller.generation,
        saved['sha256'], body['outcome'], body['gate'], body['input_revision_sha256'],
        body['tested_revision_sha256'], body['carried_revision_sha256'], seq)
    return receipt, body


def complete_task_verification(scheduler, routed, caller, results, prepared, verifier, *,
                               input_revision, services, forbidden_values):
    """Consume already-published protected evidence for the assigned verify step only."""
    secrets = validate_forbidden_values(forbidden_values)
    _require(type(input_revision) is WorkspaceRevision and results.candidate is not None,
             'decision_revision_required')
    scope = _scope(caller, results, prepared, input_revision)
    with _child_control_tx(scheduler.store, write=False) as db:
        replay = _record(db, caller, scope, secrets=secrets)
    if replay:
        return replay[0]
    # The public loader revalidates controller-observed execution and durable publication.
    published = load_published_verification(scheduler, routed, caller, results, prepared, verifier,
        services=services, forbidden_values=secrets)
    observed = load_observation(scheduler,routed.root,caller,results.quiesced,forbidden_values=secrets)
    _require(observed.status == 'completed' and observed.exit_code == 0 and observed.failure is None,
             'decision_role_execution_incomplete')
    original = verify_revision(scheduler.store, input_revision)
    candidate = verify_revision(scheduler.store, results.candidate.revision)
    _require(all(original[k] == candidate[k] for k in ('files','directories','selected_paths','scope')),
             'decision_readonly_candidate_changed')
    with _exclusive_driver(routed, caller):
        collector = RoutedChildCleanup(scheduler, routed, caller, results.quiesced, services=services)
        evidence = collector.collect()
        _require(evidence == results.cleanup, 'decision_cleanup_changed')
        proof = read_cleanup_evidence(collector, evidence)
        with _child_control_tx(scheduler.store, write=False) as db:
            context = result_authority(scheduler, db, caller, results.quiesced)
            assignment = context['assignment']
            _require(assignment['role'] == 'acceptance_verification' and assignment['step_id'] == 'verify',
                     'decision_stage_unsupported')
            _require(assignment['input_revision_sha256'] == input_revision.sha256
                and _json(db.execute('SELECT binding FROM workflow_child_launch WHERE child_id=?',
                    (caller.attempt_id,)).fetchone()[0])['caller_spec']['workspace_readonly'] is True,
                'decision_assigned_revision_changed')
            binding = input_revision.binding
            _require((binding.owner_id,binding.project_id,binding.session_id,binding.turn_id,
                binding.root_attempt_id,binding.root_generation) == tuple(context['root'][k]
                for k in ('owner_id','project_id','session_id','turn_id','root_id','generation')),
                'decision_revision_scope_changed')
            verification_metadata = _artifact(db, published.artifact_id, caller)
            candidate_metadata = _artifact(db, results.candidate.artifact_id, caller)
            observation_metadata = _artifact(db,results.observation.artifact_id,caller)
            contract = _expected_contract(context['root'], assignment)
        observation_raw = _read(Path(observation_metadata['storage_path']),32 * 1024**2)
        _require(_sha(observation_raw) == observation_metadata['sha256'] == results.observation.sha256
            and _json(observation_raw)['observation'] == _json(_raw(asdict(observed))),
            'decision_observation_changed')
        contract_receipt = _qualified_answer(contract, observed, routed, caller)
        receipt_raw = _read(Path(verification_metadata['storage_path']), 256 * 1024)
        _require(_sha(receipt_raw) == published.receipt_sha256, 'decision_verification_changed')
        observed_checks = _json(receipt_raw)['assessment']['records']
        verifier_checks = [{'spec_sha256':spec.digest,'runtime_id':record['runtime_id']}
            for spec,record in zip(_runtime_specs(routed,prepared),observed_checks)]
        _require(len(verifier_checks) == len(prepared.plan.checks), 'decision_verification_changed')
        outcome = {'passed':'verified','rejected':'rejected','needs_review':'needs_review'}[published.outcome]
        body = {'schema_version':1, 'verification_scope':'task_acceptance', **scope,
            'root_id':context['root']['root_id'], 'root_generation':context['root']['generation'],
            'reservation_version':context['root']['version'],
            'controller_instance_id':scheduler.leases.instance_id,
            'step_id':assignment['step_id'], 'role':assignment['role'],
            'assignment_sha256':context['binding'].assignment_digest,
            'workflow_sha256':assignment['workflow_digest'], 'profile_sha256':context['binding'].profile_digest,
            'accounts_retained':context['accounts'],
            'candidate_metadata_sha256':_sha(_raw(candidate_metadata)),
            'observation_artifact_id':results.observation.artifact_id,
            'observation_metadata_sha256':_sha(_raw(observation_metadata)),
            'verification_metadata_sha256':_sha(_raw(verification_metadata)),
            'cleanup_sha256':evidence.evidence_sha256, 'verification_artifact_id':published.artifact_id,
            'verification_receipt_sha256':published.receipt_sha256,
            'outcome':outcome, 'gate':{'verified':'pass','rejected':'reject','needs_review':None}[outcome],
            'verifier_checks':verifier_checks, 'remaining_criteria':list(published.remaining_criteria),
            **contract_receipt}
        raw = _raw(body); _scan(raw, secrets); digest = _sha(raw)
        destination = prepared.storage / ('decision-' + digest + '.json')
        _immutable(destination, raw)
        artifact_id = _id(caller)
        metadata = {'id':artifact_id,'session_id':caller.session_id,'attempt_id':caller.attempt_id,
            'generation':caller.generation,'path':f'routed/{caller.attempt_id}/{caller.generation}/decision.json',
            'storage_path':str(destination),'bytes':len(raw),'sha256':digest,'mime':'application/json',
            'provenance':'controller_task_acceptance_decision','verification_scope':'task_acceptance',
            'input_revision_sha256':input_revision.sha256,'verified_revision_sha256':scope['tested_revision_sha256'],
            'carried_revision_sha256':input_revision.sha256}
        with _child_control_tx(scheduler.store) as db:
            replay = _record(db, caller, scope, secrets=secrets)
            if replay:
                _require(replay[0].sha256 == digest, 'decision_replay_conflict')
                return replay[0]
            current = result_authority(scheduler, db, caller, results.quiesced)
            _require(current['assignment'] == assignment, 'decision_assignment_changed')
            _require(_expected_contract(current['root'], current['assignment']) == contract,
                     'decision_stage_contract_binding_changed')
            _require(current['child']['request'].get('acceptance',[]) == [asdict(c) for c in prepared.plan.acceptance],
                     'decision_acceptance_changed')
            _require(_artifact(db,published.artifact_id,caller) == verification_metadata
                and _artifact(db,results.candidate.artifact_id,caller) == candidate_metadata
                and _artifact(db,results.observation.artifact_id,caller) == observation_metadata,
                'decision_evidence_changed')
            _one_event(db,caller,'workflow.protected_verification', {
                'artifact_id':published.artifact_id,'plan_sha256':published.plan_sha256,
                'receipt_sha256':published.receipt_sha256,'outcome':published.outcome,
                'remaining_criteria':list(published.remaining_criteria)})
            validate_cleanup_membership(db,proof,current['binding'],current['root'],current['accounts'],scheduler)
            _require(not db.execute('SELECT 1 FROM workflow_step_gates WHERE root_id=? AND step_id=?',
                (body['root_id'],body['step_id'])).fetchone(), 'decision_gate_conflict')
            db.execute('INSERT INTO artifacts(id,session_id,attempt_id,metadata) VALUES(?,?,?,?)',
                (artifact_id,caller.session_id,caller.attempt_id,encode(metadata)))
            scheduler.store._event(db,caller.session_id,caller.attempt_id,'artifact.created',
                {k:v for k,v in metadata.items() if k != 'storage_path'})
            state = current['child']['state']
            for next_state in ('verifying','completed'):
                _require(next_state in TRANSITIONS.get(state,set()), 'decision_lifecycle_unsupported')
                scheduler.store._event(db,caller.session_id,caller.attempt_id,'attempt.state',
                    {'state':next_state,**({'outcome':outcome} if next_state == 'completed' else {})})
                state = next_state
            result = dict(current['child'].get('result') or {})
            _require('routed_decision' not in result, 'decision_record_conflict')
            result['routed_decision'] = {'artifact_id':artifact_id,'sha256':digest}
            provider_leases.revoke_attempt(db,caller.attempt_id,'attempt_terminal')
            db.execute("UPDATE attempts SET state='completed',outcome=?,result=?,updated_at=? WHERE id=? AND generation=?",
                (outcome,encode(result),now(),caller.attempt_id,caller.generation))
            if body['gate'] is not None:
                db.execute('INSERT INTO workflow_step_gates(root_id,step_id,child_id,generation,decision,revision_sha256,artifact_id,artifact_sha256,evidence_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (body['root_id'],body['step_id'],caller.attempt_id,caller.generation,body['gate'],
                     input_revision.sha256,artifact_id,digest,digest,now()))
                scheduler.store._event(db,caller.session_id,caller.attempt_id,'workflow.step_gate',
                    _gate_event(body,artifact_id,digest))
            sequence = scheduler.store._event(db,caller.session_id,caller.attempt_id,'workflow.task_decision',
                _decision_event(body,artifact_id,digest))
            db.execute('UPDATE inference_budget_roots SET high_water_us=max(high_water_us,?) WHERE root_id=?',
                (_stamp(current['observed_at']),body['root_id']))
        return CommittedDecision(artifact_id,caller.attempt_id,caller.generation,digest,outcome,body['gate'],
            input_revision.sha256,scope['tested_revision_sha256'],input_revision.sha256,sequence)


def release_decided_child(scheduler, routed, caller, results, prepared, verifier, *,
                          input_revision, decision_sha256, services, forbidden_values):
    """Release occupancy only after fresh exact cleanup; replay is historical and inert."""
    secrets = validate_forbidden_values(forbidden_values)
    scope = _scope(caller,results,prepared,input_revision)
    def existing(db):
        saved = _record(db,caller,scope,secrets=secrets)
        _require(saved is not None and saved[0].sha256 == decision_sha256,'decision_release_conflict')
        rows = db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='workflow.decided_child_released' LIMIT 2",
                          (caller.attempt_id,)).fetchall()
        if rows:
            _require(len(rows) == 1,'decision_release_conflict')
            body = _json(rows[0]['payload'])
            _require(body.get('decision_sha256') == decision_sha256 and body.get('scope') == scope,
                     'decision_release_conflict')
            _validate_release(body,saved[1],scope,decision_sha256)
            return body
        return None
    with _child_control_tx(scheduler.store,write=False) as db:
        replay = existing(db)
    if replay:
        return replay
    with _exclusive_driver(routed,caller):
        # These cleanup APIs remain usable after cancellation; none can create/start.
        checks = _reconcile_checks(scheduler,routed,caller,results,prepared,verifier,services,secrets)
        specs = _runtime_specs(routed,prepared)
        _require(len(checks) == len(specs) and all(type(v) is dict and v.get('spec_sha256') == s.digest
            and v.get('cleanup_confirmed') is True for v,s in zip(checks,specs)), 'decision_verifier_cleanup_unknown')
        collector = RoutedChildCleanup(scheduler,routed,caller,results.quiesced,services=services)
        target,bound,accounts,_,_ = collector._snapshot()
        evidence = collector.collect(target=target)
        proof = read_cleanup_evidence(collector,evidence)
        payload = {'schema_version':1,'scope':scope,'decision_sha256':decision_sha256,
            'target':asdict(target),'outcome':'confirmed_stopped',
            'caller_provider_cleanup_sha256':evidence.evidence_sha256,'verifier_cleanup':list(checks),
            'accounts_retained':accounts,'controller_instance_id':scheduler.leases.instance_id}
        payload['receipt_sha256'] = _sha(_raw(payload))
        with _child_control_tx(scheduler.store,write=False) as db:
            committed = _record(db,caller,scope,secrets=secrets)
        _validate_release(payload,committed[1],scope,decision_sha256)
        _scan(_raw(payload),secrets)
        with _child_control_tx(scheduler.store) as db:
            replay = existing(db)
            if replay:
                return replay
            child = scheduler.store._attempt(db,caller.attempt_id)
            root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(target.root_id,)).fetchone()
            _require(scheduler._owner_current(db,target.root_id) and child['generation'] == target.child_generation
                and child['runtime_id'] == target.runtime_id and child['state'] == 'completed'
                and root is not None and root['generation'] == target.root_generation,
                'decision_release_authority_changed')
            validate_cleanup_membership(db,proof,bound,dict(root),accounts,scheduler)
            changed = db.execute("UPDATE workflow_roots SET child_attempt_id=NULL,version=version+1 WHERE root_id=? AND generation=? AND child_attempt_id=? AND version=? AND state='held'",
                (target.root_id,target.root_generation,caller.attempt_id,target.reservation_version)).rowcount
            _require(changed == 1,'decision_release_target_changed')
            scheduler.store._event(db,caller.session_id,caller.attempt_id,'workflow.decided_child_released',payload)
        return payload


def _validate_release(payload, decision, scope, digest):
    expected_keys = {'schema_version','scope','decision_sha256','target','outcome',
        'caller_provider_cleanup_sha256','verifier_cleanup','accounts_retained',
        'controller_instance_id','receipt_sha256'}
    _require(set(payload) == expected_keys and payload['schema_version'] == 1
        and payload['outcome'] == 'confirmed_stopped'
        and payload['scope'] == scope and payload['decision_sha256'] == digest
        and payload['accounts_retained'] == decision['accounts_retained']
        and payload['receipt_sha256'] == _sha(_raw({k:v for k,v in payload.items() if k != 'receipt_sha256'})),
        'decision_release_conflict')
    target = payload['target']
    _require(type(target) is dict and set(target) == {'root_id','root_generation','child_id',
        'child_generation','reservation_version','runtime_id'}
        and target['root_id'] == decision['root_id']
        and type(target['root_generation']) is int and target['root_generation'] == decision['root_generation']
        and target['child_id'] == scope['attempt_id']
        and type(target['child_generation']) is int and target['child_generation'] == scope['generation']
        and target['runtime_id'] == scope['runtime_id']
        and type(target['reservation_version']) is int
        and target['reservation_version'] == decision['reservation_version']
        and payload['controller_instance_id'] == decision['controller_instance_id']
        and type(payload['caller_provider_cleanup_sha256']) is str
        and re.fullmatch('[0-9a-f]{64}',payload['caller_provider_cleanup_sha256']) is not None,
        'decision_release_conflict')
    checks = payload['verifier_cleanup']
    _require(type(checks) is list and len(checks) == len(decision['verifier_checks'])
        and all(type(row) is dict and row.get('spec_sha256') == expected['spec_sha256']
            and row.get('runtime_id') == expected['runtime_id'] and row.get('cleanup_confirmed') is True
            for row,expected in zip(checks,decision['verifier_checks'])), 'decision_release_conflict')


def load_finalized_task(db, child_id, *, forbidden_values=()):
    """Authenticate a stored task decision without current execution authority."""
    from types import SimpleNamespace
    policy = validate_forbidden_values(forbidden_values)
    _require(db.in_transaction and type(child_id) is str, 'decision_read_context_invalid')
    child = db.execute('SELECT * FROM attempts WHERE id=?', (child_id,)).fetchone()
    launch = db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?', (child_id,)).fetchone()
    _require(child is not None and child['execution_kind'] == 'hermes_child'
             and launch is not None and launch['state'] == 'fenced'
             and launch['generation'] == child['generation'], 'decision_launch_missing')
    binding = _json(launch['binding'])
    caller = SimpleNamespace(attempt_id=child_id, generation=child['generation'], session_id=child['session_id'])
    identity = _id(caller)
    metadata = _artifact(db, identity, caller)
    raw = _read(Path(metadata['storage_path']), 256 * 1024)
    _scan(raw, policy)
    body = _json(raw)
    scope_keys = ('attempt_id','generation','session_id','caller_spec_sha256','launch_binding_sha256',
        'runtime_id','plan_sha256','input_revision_sha256','tested_revision_sha256',
        'carried_revision_sha256','candidate_artifact_id','input_binding')
    _require(type(body) is dict and all(key in body for key in scope_keys), 'decision_replay_conflict')
    scope = {key:body[key] for key in scope_keys}
    assigned = binding['assignment']
    expected = {'attempt_id':child_id,'generation':child['generation'],'session_id':child['session_id'],
        'runtime_id':child['runtime_id'],'caller_spec_sha256':_sha(_raw(binding['caller_spec'])),
        'launch_binding_sha256':launch['binding_digest'],
        'input_revision_sha256':assigned['input_revision_sha256'],
        'carried_revision_sha256':assigned['input_revision_sha256']}
    _require(all(_raw(scope[k]) == _raw(v) for k,v in expected.items())
        and binding['runtime_id'] == child['runtime_id'] and binding['child_id'] == child_id
        and binding['generation'] == child['generation']
        and assigned['root_id'] == child['workflow_root_id']
        and assigned['root_generation'] == child['workflow_parent_generation']
        and assigned['session_id'] == child['session_id'] and assigned['turn_id'] == child['turn_id'],
        'decision_replay_conflict')
    common = {key:assigned[name] for key,name in (
        ('owner_id','owner_id'),('project_id','project_id'),('session_id','session_id'),
        ('turn_id','turn_id'),('root_attempt_id','root_id'),('root_generation','root_generation'))}
    input_binding = body['input_binding']
    _require(type(input_binding) is dict and set(input_binding) == set(common) | {'attempt_id','generation'}
        and all(_raw(input_binding[k]) == _raw(v) for k,v in common.items())
        and type(input_binding['attempt_id']) is str
        and type(input_binding['generation']) is int and input_binding['generation'] > 0,
        'decision_replay_conflict')
    record = _record(db, caller, scope, secrets=policy)
    _require(record is not None, 'decision_terminal_record_missing')
    return record[1], metadata


def load_task_release(db, child_id, *, forbidden_values=()):
    body, metadata = load_finalized_task(db, child_id, forbidden_values=forbidden_values)
    rows = db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='workflow.decided_child_released' LIMIT 2",
                      (child_id,)).fetchall()
    if not rows:
        return None
    _require(len(rows) == 1, 'decision_release_conflict')
    scope = {key:body[key] for key in ('attempt_id','generation','session_id','caller_spec_sha256',
        'launch_binding_sha256','runtime_id','plan_sha256','input_revision_sha256',
        'tested_revision_sha256','carried_revision_sha256','candidate_artifact_id','input_binding')}
    payload = _json(rows[0]['payload'])
    _validate_release(payload, body, scope, metadata['sha256'])
    _scan(_raw(payload), validate_forbidden_values(forbidden_values))
    return payload
