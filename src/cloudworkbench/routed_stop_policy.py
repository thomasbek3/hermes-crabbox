"""Authenticate a stopped workflow prefix without granting success or cleanup authority."""
from contextlib import closing
from dataclasses import dataclass, field

from .routed_delivery_policy import (_snapshot, _stable, _detached, _json, _raw, _sha,
    _scan, _identifier, _positive, _digest, _path, _artifact, _workspace)
from .routed_progression import _input, VERSION as PROGRESSION_VERSION
from .routed_environment import validate_routed_environment_snapshot
from .routed_stage_decision import load_finalized_stage, load_stage_release, CODING_ROLES
from .routed_decision import load_finalized_task, load_task_release
from .routed_observation_store import validate_forbidden_values
from .stage_contracts import VERSION as CONTRACT_VERSION
from .workflow_routing import resolve_catalog, WorkflowError
from .scheduler import _child_control_tx
from .store import encode


class StoppedPolicyError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def _require(value,code):
    if not value:raise StoppedPolicyError(code)


_REFERENCE_KEYS={'step_id','role','child_id','generation','artifact_id','sha256',
    'input_revision_sha256','carried_revision_sha256','release_sha256','gate','outcome',
    'candidate_artifact_id','candidate_revision_sha256'}
_PUBLIC_KEYS={'schema_version','kind','root_id','generation','session_id','owner_id','project_id','turn_id',
    'workflow','workflow_sha256','environment_sha256','outcome','blocker','decision_refs',
    'input_revision_sha256','carried_revision_sha256'}


def _document(value):
    _require(type(value) is dict and set(value)=={'schema_version','public','private'}
        and type(value['schema_version']) is int and value['schema_version']==1,'stopped_proof_invalid')
    public,private=value['public'],value['private']
    _require(type(public) is dict and set(public)==_PUBLIC_KEYS
        and type(private) is dict and set(private)=={'database_path','snapshot_sha256'},'stopped_proof_invalid')
    _require(type(public['schema_version']) is int and public['schema_version']==1 and public['kind']=='stopped_workflow'
        and all(_identifier(public[k]) for k in ('root_id','session_id','owner_id','project_id','turn_id','workflow'))
        and _positive(public['generation']) and all(_digest(public[k]) for k in (
            'workflow_sha256','environment_sha256','input_revision_sha256','carried_revision_sha256'))
        and _path(private['database_path']) and _digest(private['snapshot_sha256']),'stopped_proof_invalid')
    try:catalog=resolve_catalog(public['workflow'])
    except WorkflowError:raise StoppedPolicyError('stopped_proof_invalid') from None
    refs=public['decision_refs']
    _require(catalog is not None and type(refs) is list and 1<=len(refs)<=len(catalog.steps),'stopped_proof_invalid')
    carried=public['input_revision_sha256'];children=set();artifacts=set()
    for index,(ref,step) in enumerate(zip(refs,catalog.steps)):
        _require(type(ref) is dict and set(ref)==_REFERENCE_KEYS
            and ref['step_id']==step.id and ref['role']==step.role
            and _identifier(ref['child_id']) and ref['child_id']!=public['root_id'] and ref['child_id'] not in children
            and _positive(ref['generation']) and _identifier(ref['artifact_id']) and ref['artifact_id'] not in artifacts
            and _identifier(ref['candidate_artifact_id']) and all(_digest(ref[k]) for k in (
                'sha256','input_revision_sha256','carried_revision_sha256','release_sha256','candidate_revision_sha256'))
            and ref['input_revision_sha256']==carried,'stopped_proof_invalid')
        task=ref['role']=='acceptance_verification'
        if index<len(refs)-1:
            _require(not task and ref['gate']=='pass' and ref['outcome']=='unverified','stopped_proof_invalid')
        else:
            expected=('rejected','reject') if task and ref['outcome']=='rejected' else ('needs_review',None)
            allowed=((ref['outcome'],ref['gate'])==expected if task else
                     ref['outcome']=='needs_review' and ref['gate'] in ('reject',None))
            _require(allowed,'stopped_proof_invalid')
        _require(ref['carried_revision_sha256']==(
            ref['candidate_revision_sha256'] if step.role in CODING_ROLES else carried),'stopped_proof_invalid')
        carried=ref['carried_revision_sha256'];children.add(ref['child_id']);artifacts.add(ref['artifact_id'])
    _require(public['carried_revision_sha256']==carried
        and _raw(public['blocker'])==_raw(refs[-1])
        and public['outcome']==refs[-1]['outcome'],'stopped_proof_invalid')


@dataclass(frozen=True)
class StoppedWorkflowProof:
    canonical_json:str=field(repr=False)

    @classmethod
    def from_json(cls,raw):
        try:
            _require(type(raw) is str and 0<len(raw.encode())<=256*1024,'stopped_proof_invalid')
            value=_json(raw);_document(value)
            _require(_raw(value).decode()==raw,'stopped_proof_invalid')
        except (KeyError,TypeError,UnicodeError,RecursionError,OverflowError):
            raise StoppedPolicyError('stopped_proof_invalid') from None
        return cls(raw)

    @property
    def sha256(self):return _sha(self.canonical_json.encode())
    @property
    def outcome(self):return self.public_record()['outcome']
    def to_dict(self):return _json(self.canonical_json)
    def public_record(self):return self.to_dict()['public']


def _projection(db,value):
    public=value['public'];root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(public['root_id'],)).fetchone()
    _require(root is not None and all(root[k]==public[k] for k in (
        'root_id','generation','session_id','owner_id','project_id','turn_id'))
        and root['frozen_digest']==public['workflow_sha256'],'stopped_evidence_changed')
    provenance=_json(root['frozen'])['provenance']
    _require(provenance['workflow']['workflow']==public['workflow']
        and provenance['environment']['snapshot_sha256']==public['environment_sha256']
        and _input(db,root)['sha256']==public['input_revision_sha256'],'stopped_evidence_changed')
    for ref in public['decision_refs']:
        child=db.execute('SELECT * FROM attempts WHERE id=?',(ref['child_id'],)).fetchone()
        task=ref['role']=='acceptance_verification'
        step=db.execute('SELECT w.* FROM workflow_steps w JOIN workflow_seats s ON s.request_id=w.request_id WHERE s.id=?',
            (child['role_seat_id'],)).fetchone() if child else None
        _require(child is not None and step is not None and child['workflow_root_id']==public['root_id']
            and child['generation']==ref['generation'] and child['outcome']==ref['outcome']
            and step['step_id']==ref['step_id'] and step['role']==ref['role']
            and _json(child['result']).get('routed_decision' if task else 'routed_stage_decision')=={
                'artifact_id':ref['artifact_id'],'sha256':ref['sha256']},'stopped_evidence_changed')
        metadata=_artifact(db,ref['artifact_id']);candidate=_artifact(db,ref['candidate_artifact_id'])
        _require(metadata['attempt_id']==child['id'] and metadata['generation']==child['generation']
            and metadata['sha256']==ref['sha256'] and metadata['input_revision_sha256']==ref['input_revision_sha256']
            and metadata['carried_revision_sha256' if task else 'output_revision_sha256']==ref['carried_revision_sha256']
            and candidate['attempt_id']==child['id'] and candidate['generation']==child['generation']
            and candidate['provenance']=='controller_captured_candidate'
            and candidate['sha256']==ref['candidate_revision_sha256'],'stopped_evidence_changed')
        gates=db.execute('SELECT * FROM workflow_step_gates WHERE root_id=? AND step_id=?',
            (public['root_id'],ref['step_id'])).fetchall()
        _require((not gates and ref['gate'] is None) or (len(gates)==1 and gates[0]['decision']==ref['gate']
            and gates[0]['artifact_id']==ref['artifact_id'] and gates[0]['artifact_sha256']==ref['sha256']),
            'stopped_evidence_changed')
        kind='workflow.decided_child_released' if task else 'workflow.stage_child_released'
        events=db.execute('SELECT payload FROM events WHERE attempt_id=? AND type=? LIMIT 2',(child['id'],kind)).fetchall()
        _require(len(events)==1 and _json(events[0][0])['receipt_sha256']==ref['release_sha256']
            and _json(events[0][0])['decision_sha256']==ref['sha256'],'stopped_evidence_changed')


def validate_db(db,proof):
    """Evidence comparison only; caller owns current terminalization/cleanup authority."""
    _require(type(proof) is StoppedWorkflowProof,'stopped_proof_invalid')
    value=StoppedWorkflowProof.from_json(proof.canonical_json).to_dict()
    _require(db.in_transaction,'stopped_transaction_required')
    _require(db.execute('PRAGMA database_list').fetchone()[2]==value['private']['database_path'],'stopped_database_changed')
    snapshot=_snapshot(db,value['public']['root_id'])
    _require(_stable(snapshot,value['public']['root_id'])==value['private']['snapshot_sha256'],'stopped_evidence_changed')
    _projection(db,value)
    return True


def collect_stopped_workflow_proof(scheduler,root_id,*,expected_generation,forbidden_values=()):
    policy=validate_forbidden_values(forbidden_values)
    _require(_identifier(root_id) and _positive(expected_generation),'stopped_scope_invalid')
    with _child_control_tx(scheduler.store,write=False) as live:
        snapshot=_snapshot(live,root_id);database_path=live.execute('PRAGMA database_list').fetchone()[2]
    with closing(_detached(snapshot)) as db:
        root=dict(db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone())
        _require(root['generation']==expected_generation and root['child_attempt_id'] is None,'stopped_root_occupied')
        frozen=_json(root['frozen']);provenance=frozen['provenance'];workflow=provenance['workflow']
        _require(_sha(root['frozen'].encode())==root['frozen_digest']
            and provenance.get('stage_progression_version')==PROGRESSION_VERSION
            and provenance.get('stage_contract_version')==CONTRACT_VERSION
            and type(workflow['workflow_policy_sha256']) is str,'stopped_policy_changed')
        try:catalog=resolve_catalog(workflow['workflow'],workflow['workflow_policy_sha256'])
        except WorkflowError:raise StoppedPolicyError('stopped_policy_changed') from None
        _require(catalog is not None and [(r['id'],r['role']) for r in workflow['steps']]==[
            (r.id,r.role) for r in catalog.steps],'stopped_catalog_changed')
        request=_json(db.execute('SELECT request FROM turns WHERE id=?',(root['turn_id'],)).fetchone()[0])
        environment=validate_routed_environment_snapshot(provenance['environment'],project_id=root['project_id'],
            allowed_versions=(request['environment_version'],),forbidden_values=policy)
        steps=[dict(row) for row in db.execute('SELECT * FROM workflow_steps ORDER BY ordinal')]
        _require([(s['step_id'],s['role']) for s in steps]==[(s.id,s.role) for s in catalog.steps],'stopped_catalog_changed')
        children=[dict(row) for row in db.execute("SELECT * FROM attempts WHERE execution_kind='hermes_child'")]
        requests=[dict(row) for row in db.execute('SELECT * FROM workflow_requests')]
        count=len(children)
        _require(0<count<=len(steps) and len(requests)==count
            and db.execute('SELECT COUNT(*) FROM workflow_seats').fetchone()[0]==count
            and all(c['state']=='completed' for c in children)
            and all(s['request_id'] is not None for s in steps[:count])
            and all(s['request_id'] is None for s in steps[count:]),'stopped_prefix_incomplete')
        pending=[dict(row) for row in db.execute('SELECT * FROM role_broker_pending')]
        _require(all(row['controller_request_id'] is not None or
            (type(row['terminal_reason']) is str and bool(row['terminal_reason'])) for row in pending),'stopped_pending_calls')
        _require({(r['id'],r['controller_request_id']) for r in pending if r['controller_request_id'] is not None}
            =={(r['broker_pending_id'],r['id']) for r in requests},'stopped_pending_link_changed')
        initial=_input(db,root);_workspace(scheduler.store,initial,initial['revision_binding'],policy)
        carried=initial['sha256'];binding=initial['revision_binding'];refs=[]
        for index,step in enumerate(steps[:count]):
            _require(step['profile']==encode(workflow['steps'][index]['profile']),'stopped_profile_changed')
            rows=db.execute('SELECT a.* FROM attempts a JOIN workflow_seats s ON s.id=a.role_seat_id WHERE s.request_id=?',
                (step['request_id'],)).fetchall()
            _require(len(rows)==1,'stopped_prefix_incomplete');child=rows[0]
            launch=db.execute('SELECT binding FROM workflow_child_launch WHERE child_id=?',(child['id'],)).fetchone()
            _require(launch is not None,'stopped_launch_missing');launch=_json(launch[0])
            expected=[{'artifact_id':r['artifact_id'],'sha256':r['sha256']} for r in refs]
            request_row=db.execute('SELECT * FROM workflow_requests WHERE id=?',(step['request_id'],)).fetchone()
            seat=db.execute('SELECT * FROM workflow_seats WHERE id=?',(child['role_seat_id'],)).fetchone()
            _require(seat is not None and seat['profile']==step['profile']==launch['assignment']['profile']
                and seat['request_id']==request_row['id']==launch['assignment']['request_id']
                and seat['id']==launch['assignment']['seat_id']
                and request_row['role']==step['role'] and request_row['parent_generation']==root['generation']
                and request_row['plan']==encode(frozen['role_plans'][step['role']]),'stopped_profile_changed')
            payload=_json(request_row['payload'])
            _require(payload['arguments']['context_refs']==expected
                and _json(launch['assignment']['payload'])['arguments']['context_refs']==expected,'stopped_context_changed')
            task=step['role']=='acceptance_verification'
            body,metadata=(load_finalized_task if task else load_finalized_stage)(db,child['id'],forbidden_values=policy)
            released=(load_task_release if task else load_stage_release)(db,child['id'],forbidden_values=policy)
            _require(released is not None,'stopped_child_not_released')
            _require(body['root_id']==root_id and body['root_generation']==expected_generation
                and body['session_id']==root['session_id'] and body['workflow_sha256']==root['frozen_digest']
                and body['step_id']==step['step_id'] and body['role']==step['role']
                and body['input_revision_sha256']==step['input_revision_sha256']==carried
                and _raw(body['input_binding'])==_raw(binding),'stopped_chain_changed')
            if index<count-1:
                _require(not task and body['gate']=='pass' and body['outcome']=='unverified','stopped_prior_not_passed')
            else:
                _require(body['gate'] in ('reject',None)
                    and ((task and (body['outcome'],body['gate']) in (('rejected','reject'),('needs_review',None)))
                        or (not task and body['outcome']=='needs_review')),'stopped_blocker_required')
            candidate=_artifact(db,body['candidate_artifact_id'])
            if step['role'] in CODING_ROLES:binding=body['candidate_binding']
            carried=body['carried_revision_sha256']
            refs.append({'step_id':step['step_id'],'role':step['role'],'child_id':child['id'],
                'generation':child['generation'],'artifact_id':metadata['id'],'sha256':metadata['sha256'],
                'input_revision_sha256':body['input_revision_sha256'],'carried_revision_sha256':carried,
                'release_sha256':released['receipt_sha256'],'gate':body['gate'],'outcome':body['outcome'],
                'candidate_artifact_id':candidate['id'],'candidate_revision_sha256':candidate['sha256']})
        _require(db.execute('SELECT COUNT(*) FROM workflow_step_gates').fetchone()[0]
            ==sum(r['gate'] is not None for r in refs),'stopped_gate_membership_changed')
        public={'schema_version':1,'kind':'stopped_workflow','root_id':root_id,'generation':expected_generation,
            'session_id':root['session_id'],'owner_id':root['owner_id'],'project_id':root['project_id'],
            'turn_id':root['turn_id'],'workflow':workflow['workflow'],'workflow_sha256':root['frozen_digest'],
            'environment_sha256':environment['snapshot_sha256'],'outcome':refs[-1]['outcome'],
            'blocker':refs[-1],'decision_refs':refs,'input_revision_sha256':initial['sha256'],
            'carried_revision_sha256':carried}
        _scan(public,policy)
    value={'schema_version':1,'public':public,'private':{
        'database_path':database_path,'snapshot_sha256':_stable(snapshot,root_id)}}
    _scan(value,policy)
    proof=StoppedWorkflowProof.from_json(_raw(value).decode())
    with _child_control_tx(scheduler.store,write=False) as db:validate_db(db,proof)
    return proof
