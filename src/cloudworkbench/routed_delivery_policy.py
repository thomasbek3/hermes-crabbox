"""Read-only final workflow evidence policy; no root transition or pointer mutation."""
from contextlib import closing
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
import re

from .store import encode
from .workflow_routing import resolve_catalog, WorkflowError
from .workflow_revisions import WorkspaceRevision, RevisionBinding, RevisionLimits, _load_verified
from .routed_progression import _input, VERSION as PROGRESSION_VERSION
from .stage_contracts import VERSION as CONTRACT_VERSION
from .routed_environment import validate_routed_environment_snapshot
from .routed_stage_decision import load_finalized_stage, load_stage_release, CODING_ROLES
from .routed_decision import load_finalized_task, load_task_release
from .routed_observation_store import validate_forbidden_values
from .routed_publication import _read
from .routed_verification_policy import CheckExecution
from .scheduler import _child_control_tx

MAX_ROWS = 8192
MAX_SNAPSHOT_BYTES = 16 * 1024**2


class DeliveryPolicyError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value: raise DeliveryPolicyError(code)


def _raw(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def _sha(value): return hashlib.sha256(value).hexdigest()


def _json(raw):
    def pairs(items):
        value={}
        for key,item in items:
            _require(key not in value,'delivery_json_invalid');value[key]=item
        return value
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,TypeError,UnicodeError,RecursionError):raise DeliveryPolicyError('delivery_json_invalid') from None


def _scan(value, policy):
    pending=[value]
    while pending:
        item=pending.pop()
        if type(item) is str:_require(not any(secret in item.encode() for secret in policy),'delivery_secret_refused')
        elif type(item) is dict:pending.extend(item.keys());pending.extend(item.values())
        elif type(item) is list:pending.extend(item)


def _snapshot(db,root_id):
    _require(db.in_transaction,'delivery_transaction_required')
    root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
    _require(root is not None,'delivery_root_missing')
    identity='root-input-'+_sha(root_id.encode())
    selects={
        'workflow_roots':('root_id=?',(root_id,)),
        'sessions':('id=?',(root['session_id'],)),
        'turns':('id=?',(root['turn_id'],)),
        'attempts':('workflow_root_id=?',(root_id,)),
        'workflow_steps':('root_id=?',(root_id,)),
        'workflow_step_gates':('root_id=?',(root_id,)),
        'workflow_requests':('root_id=?',(root_id,)),
        'role_broker_pending':('root_id=?',(root_id,)),
        'workflow_seats':('request_id IN (SELECT id FROM workflow_requests WHERE root_id=?)',(root_id,)),
        'workflow_child_launch':('child_id IN (SELECT id FROM attempts WHERE workflow_root_id=?)',(root_id,)),
        'artifacts':('attempt_id IN (SELECT id FROM attempts WHERE workflow_root_id=? AND id!=?) OR id=?',(root_id,root_id,identity)),
        'events':("attempt_id IN (SELECT id FROM attempts WHERE workflow_root_id=? AND id!=?) OR (attempt_id=? AND (type='workflow.root_input' OR (type='artifact.created' AND json_extract(payload,'$.id')=?)))",(root_id,root_id,root_id,identity)),
    }
    tables={};total=0
    for table,(clause,params) in selects.items():
        rows=[dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE {clause} ORDER BY rowid LIMIT ?',(*params,MAX_ROWS+1))]
        total+=len(rows);_require(total<=MAX_ROWS,'delivery_snapshot_limit')
        ddl=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0]
        tables[table]={'ddl':ddl,'rows':rows}
    _require(len(_raw(tables))<=MAX_SNAPSHOT_BYTES,'delivery_snapshot_limit')
    return tables


def _stable(snapshot,root_id):
    value=_json(_raw(snapshot))
    for row in value['workflow_roots']['rows']:
        for key in ('state','child_attempt_id','cancel_requested','admitted_at','deadline_at','version'):row.pop(key,None)
    for row in value['attempts']['rows']:
        if row['id']==root_id:
            for key in ('state','result','outcome','reason','exit_code','cancel_requested','updated_at','started_at','runtime_id'):row.pop(key,None)
    for row in value['sessions']['rows']:
        keep={'id','owner_id','project_id'}
        for key in set(row)-keep:row.pop(key)
    return _sha(_raw(value))


def _detached(snapshot):
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    for table,data in snapshot.items():
        db.execute(data['ddl'])
        for row in data['rows']:
            names=tuple(row)
            db.execute(f'INSERT INTO {table}({",".join(names)}) VALUES({",".join("?" for _ in names)})',tuple(row[k] for k in names))
    db.commit();db.execute('BEGIN')
    return db


def _identifier(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value) is not None


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None


def _positive(value):
    return type(value) is int and 0 < value <= 2**63-1


def _path(value):
    return (type(value) is str and 0 < len(value.encode()) <= 4096 and '\x00' not in value
        and Path(value).is_absolute() and str(Path(value)) == value and '..' not in Path(value).parts)


def _validate_document(value):
    public,private=value['public'],value['private']
    keys={'schema_version','kind','outcome','root_id','generation','session_id','owner_id','project_id','turn_id',
        'workflow','workflow_sha256','environment_sha256','input_revision_sha256','carried_revision_sha256',
        'selected_revision_sha256','selected_artifact_id','decision_refs','report_refs','final_task_decision'}
    _require(type(public) is dict and set(public)==keys and type(private) is dict
        and set(private)=={'database_path','snapshot_sha256','revision_path','revision_sha256','revision_binding'},
        'delivery_proof_invalid')
    _require(type(public['schema_version']) is int and public['schema_version']==1
        and all(_identifier(public[k]) for k in ('root_id','session_id','owner_id','project_id','turn_id','workflow'))
        and _positive(public['generation'])
        and all(_digest(public[k]) for k in ('workflow_sha256','environment_sha256','input_revision_sha256','carried_revision_sha256'))
        and _path(private['database_path']) and _path(private['revision_path'])
        and _digest(private['snapshot_sha256']) and private['revision_sha256']==public['carried_revision_sha256']
        and Path(private['revision_path']).name==private['revision_sha256'],'delivery_proof_invalid')
    binding=private['revision_binding']
    common=dict(owner_id=public['owner_id'],project_id=public['project_id'],session_id=public['session_id'],
        turn_id=public['turn_id'],root_attempt_id=public['root_id'],root_generation=public['generation'])
    _require(type(binding) is dict and set(binding)==set(common)|{'attempt_id','generation'}
        and all(_raw(binding[k])==_raw(v) for k,v in common.items())
        and _identifier(binding['attempt_id']) and _positive(binding['generation']),'delivery_proof_invalid')
    try:catalog=resolve_catalog(public['workflow'])
    except WorkflowError:raise DeliveryPolicyError('delivery_proof_invalid') from None
    refs=public['decision_refs']
    _require(catalog is not None and type(refs) is list and len(refs)==len(catalog.steps),'delivery_proof_invalid')
    reference_keys={'step_id','role','child_id','generation','artifact_id','sha256','input_revision_sha256',
        'carried_revision_sha256','release_sha256'}
    carried=public['input_revision_sha256'];seen_children=set();seen_artifacts=set()
    selected_attempt,selected_generation=public['root_id'],public['generation']
    for ref,step in zip(refs,catalog.steps):
        _require(type(ref) is dict and set(ref)==reference_keys
            and ref['step_id']==step.id and ref['role']==step.role
            and _identifier(ref['child_id']) and ref['child_id']!=public['root_id']
            and ref['child_id'] not in seen_children and _identifier(ref['artifact_id'])
            and ref['artifact_id'] not in seen_artifacts and _positive(ref['generation'])
            and all(_digest(ref[k]) for k in ('sha256','input_revision_sha256','carried_revision_sha256','release_sha256'))
            and ref['input_revision_sha256']==carried,'delivery_proof_invalid')
        if step.role not in CODING_ROLES:
            _require(ref['carried_revision_sha256']==carried,'delivery_proof_invalid')
        else:selected_attempt,selected_generation=ref['child_id'],ref['generation']
        carried=ref['carried_revision_sha256'];seen_children.add(ref['child_id']);seen_artifacts.add(ref['artifact_id'])
    _require(carried==public['carried_revision_sha256']
        and binding['attempt_id']==selected_attempt and binding['generation']==selected_generation
        and public['report_refs']==[{'artifact_id':ref['artifact_id'],'sha256':ref['sha256']} for ref in refs],
        'delivery_proof_invalid')
    acceptance=any(step.role=='acceptance_verification' for step in catalog.steps)
    if acceptance:
        _require(catalog.steps[-1].role=='acceptance_verification' and public['kind']=='workspace'
            and public['outcome']=='verified' and public['selected_revision_sha256']==carried
            and _identifier(public['selected_artifact_id']) and _raw(public['final_task_decision'])==_raw(refs[-1]),
            'delivery_proof_invalid')
    else:
        _require(not any(step.role in CODING_ROLES for step in catalog.steps)
            and public['kind']=='report' and public['outcome']=='unverified'
            and public['selected_revision_sha256'] is None and public['selected_artifact_id'] is None
            and public['final_task_decision'] is None and carried==public['input_revision_sha256'],
            'delivery_proof_invalid')


@dataclass(frozen=True)
class RootDeliveryProof:
    canonical_json: str = field(repr=False)

    @classmethod
    def from_json(cls, raw):
        try:
            _require(type(raw) is str and 0 < len(raw.encode()) <= 256*1024,'delivery_proof_invalid')
            value=_json(raw)
            _require(type(value) is dict and set(value)=={'schema_version','public','private'}
                and type(value['schema_version']) is int and value['schema_version']==1
                and _raw(value).decode()==raw,'delivery_proof_invalid')
            _validate_document(value)
        except (KeyError,TypeError,UnicodeError,RecursionError,OverflowError):
            raise DeliveryPolicyError('delivery_proof_invalid') from None
        return cls(raw)

    @property
    def sha256(self):return _sha(self.canonical_json.encode())
    @property
    def kind(self):return self.public_record()['kind']
    @property
    def outcome(self):return self.public_record()['outcome']
    def to_dict(self):return _json(self.canonical_json)
    def public_record(self):return self.to_dict()['public']


def validate_db(db,proof):
    """Compare immutable evidence only. Caller owns current execution/cancel/CAS authority."""
    _require(type(proof) is RootDeliveryProof,'delivery_proof_invalid')
    value=RootDeliveryProof.from_json(proof.canonical_json).to_dict()
    actual=db.execute('PRAGMA database_list').fetchone()[2]
    _require(actual==value['private']['database_path'],'delivery_database_changed')
    snapshot=_snapshot(db,value['public']['root_id'])
    _require(_stable(snapshot,value['public']['root_id'])==value['private']['snapshot_sha256'],
        'delivery_evidence_changed')
    _validate_projection(db,value)
    return True


def _validate_projection(db,value):
    public,private=value['public'],value['private']
    root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(public['root_id'],)).fetchone()
    _require(all(root[k]==public[k] for k in ('root_id','generation','session_id','owner_id','project_id','turn_id'))
        and root['frozen_digest']==public['workflow_sha256'],'delivery_evidence_changed')
    frozen=_json(root['frozen'])['provenance']
    _require(frozen['workflow']['workflow']==public['workflow']
        and frozen['environment']['snapshot_sha256']==public['environment_sha256'],'delivery_evidence_changed')
    initial=_input(db,root)
    _require(initial['sha256']==public['input_revision_sha256'],'delivery_evidence_changed')
    for ref in public['decision_refs']:
        metadata=_artifact(db,ref['artifact_id'])
        task=ref['role']=='acceptance_verification'
        child=db.execute('SELECT * FROM attempts WHERE id=?',(ref['child_id'],)).fetchone()
        step=db.execute('SELECT w.* FROM workflow_steps w JOIN workflow_seats s ON s.request_id=w.request_id WHERE s.id=?',
            (child['role_seat_id'],)).fetchone() if child is not None else None
        pointer=_json(child['result']).get('routed_decision' if task else 'routed_stage_decision') if child is not None else None
        _require(child is not None and step is not None and child['workflow_root_id']==public['root_id']
            and child['session_id']==public['session_id'] and child['generation']==ref['generation']
            and step['step_id']==ref['step_id'] and step['role']==ref['role']
            and pointer=={'artifact_id':ref['artifact_id'],'sha256':ref['sha256']}
            and metadata['provenance']==('controller_task_acceptance_decision' if task else 'controller_stage_contract'),
            'delivery_evidence_changed')
        _require(metadata['attempt_id']==ref['child_id'] and metadata['generation']==ref['generation']
            and metadata['sha256']==ref['sha256'] and metadata['session_id']==public['session_id']
            and metadata['input_revision_sha256']==ref['input_revision_sha256']
            and metadata['carried_revision_sha256' if task else 'output_revision_sha256']==ref['carried_revision_sha256'],
            'delivery_evidence_changed')
        kind='workflow.decided_child_released' if task else 'workflow.stage_child_released'
        rows=db.execute('SELECT payload FROM events WHERE attempt_id=? AND type=? LIMIT 2',(ref['child_id'],kind)).fetchall()
        _require(len(rows)==1 and _json(rows[0][0])['receipt_sha256']==ref['release_sha256']
            and _json(rows[0][0])['decision_sha256']==ref['sha256'],'delivery_evidence_changed')
    selected=_artifact(db,public['selected_artifact_id']) if public['kind']=='workspace' else initial
    _require(selected['sha256']==private['revision_sha256']
        and str(Path(selected['storage_path']).parent)==private['revision_path']
        and selected['attempt_id']==private['revision_binding']['attempt_id']
        and selected['generation']==private['revision_binding']['generation'],'delivery_evidence_changed')


def _artifact(db,identity):
    row=db.execute('SELECT metadata FROM artifacts WHERE id=?',(identity,)).fetchone()
    _require(row is not None,'delivery_artifact_missing')
    return _json(row[0])


def _verified_workspace(store,revision,policy):
    manifest,files,directories=_load_verified(store,revision,RevisionLimits())
    _scan(manifest,policy)
    _require(not any(secret in data for data,_ in files.values() for secret in policy),'delivery_secret_refused')
    return manifest


def _workspace(store,metadata,binding,policy=()):
    revision=WorkspaceRevision(Path(metadata['storage_path']).parent,metadata['sha256'],RevisionBinding(**binding))
    return revision,_verified_workspace(store,revision,policy)


def _protected(db,store,root,child,body,environment,selected_manifest,policy):
    metadata=_artifact(db,body['verification_artifact_id'])
    raw=_read(Path(metadata['storage_path']),512*1024)
    _require(_sha(raw)==metadata['sha256']==body['verification_receipt_sha256'] and len(raw)==metadata['bytes'],
        'delivery_verification_changed')
    value=_json(raw);_scan(value,policy)
    _require(type(value) is dict and set(value)=={'schema_version','plan','assessment'}
        and type(value['schema_version']) is int and value['schema_version']==1
        and type(value['plan']) is dict and type(value['assessment']) is dict,'delivery_verification_changed')
    plan=value['plan'];assessment=value['assessment']
    _require(value.get('schema_version')==1 and _sha(_raw(plan))==body['plan_sha256']
        and plan['environment_sha256']==environment['manifest_sha256']
        and plan['image']==environment['manifest']['image_digest']
        and plan['checks']==environment['manifest']['checks']
        and plan['launch_binding_sha256']==body['launch_binding_sha256']
        and plan['cleanup_sha256']==body['cleanup_sha256']
        and plan['input_revision_sha256']==body['input_revision_sha256']
        and plan['candidate_revision_sha256']==body['tested_revision_sha256']
        and plan['acceptance']==_json(db.execute('SELECT request FROM turns WHERE id=?',(root['turn_id'],)).fetchone()[0]).get('acceptance',[]),
        'delivery_verification_changed')
    binding=plan['binding']
    _require(binding['attempt_id']==child['id'] and binding['generation']==child['generation']
        and binding['root_attempt_id']==root['root_id'] and binding['root_generation']==root['generation'],
        'delivery_verification_changed')
    records=[CheckExecution(**row) for row in assessment['records']]
    _require(records and [r.check_id for r in records]==[c['id'] for c in plan['checks']]
        and len({r.runtime_id for r in records})==len(records)
        and all(r.exit_code==0 and not r.oom and not r.timed_out and r.cleanup_confirmed for r in records),
        'delivery_protected_checks_failed')
    definitions={c['id']:c['description'] for c in plan['checks']}
    _require(all(not criterion['mandatory'] or definitions.get(criterion['id'])==criterion['description'] for criterion in plan['acceptance']),
        'delivery_acceptance_incomplete')
    _require(assessment=={'schema_version':1,'plan_sha256':body['plan_sha256'],'records':[asdict(r) for r in records],
        'outcome':'passed','remaining_criteria':[]},'delivery_verification_changed')
    candidate=_artifact(db,body['candidate_artifact_id'])
    _require(candidate['sha256']==body['tested_revision_sha256'],'delivery_verification_changed')
    _,tested=_workspace(store,candidate,binding,policy)
    _require(all(tested[k]==selected_manifest[k] for k in ('files','directories','selected_paths','scope')),
        'delivery_protected_subject_changed')


def collect_root_delivery_proof(scheduler,root_id,*,expected_generation,selected_revision,forbidden_values=()):
    policy=validate_forbidden_values(forbidden_values)
    _require(_identifier(root_id) and _positive(expected_generation) and type(selected_revision) is WorkspaceRevision,
        'delivery_scope_invalid')
    with _child_control_tx(scheduler.store,write=False) as live:
        snapshot=_snapshot(live,root_id)
        database_path=live.execute('PRAGMA database_list').fetchone()[2]
    selected_manifest=_verified_workspace(scheduler.store,selected_revision,policy)
    with closing(_detached(snapshot)) as db:
        root=dict(db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone())
        _require(root['generation']==expected_generation and root['child_attempt_id'] is None,'delivery_root_occupied')
        frozen=_json(root['frozen']);provenance=frozen['provenance'];workflow=provenance['workflow']
        _require(_sha(root['frozen'].encode())==root['frozen_digest']
            and provenance.get('stage_progression_version')==PROGRESSION_VERSION
            and provenance.get('stage_contract_version')==CONTRACT_VERSION
            and type(workflow['workflow_policy_sha256']) is str,'delivery_policy_changed')
        try:catalog=resolve_catalog(workflow['workflow'],workflow['workflow_policy_sha256'])
        except WorkflowError:raise DeliveryPolicyError('delivery_policy_changed') from None
        _require(catalog is not None and [(s['id'],s['role']) for s in workflow['steps']]==[(s.id,s.role) for s in catalog.steps],
            'delivery_workflow_incomplete')
        request=_json(db.execute('SELECT request FROM turns WHERE id=?',(root['turn_id'],)).fetchone()[0])
        environment=validate_routed_environment_snapshot(provenance['environment'],project_id=root['project_id'],
            allowed_versions=(request['environment_version'],),forbidden_values=policy)
        steps=[dict(r) for r in db.execute('SELECT * FROM workflow_steps ORDER BY ordinal')]
        _require([(s['step_id'],s['role']) for s in steps]==[(s.id,s.role) for s in catalog.steps],
            'delivery_workflow_incomplete')
        children=[dict(r) for r in db.execute("SELECT * FROM attempts WHERE execution_kind='hermes_child'")]
        _require(len(children)==len(steps) and all(c['state']=='completed' for c in children)
            and db.execute('SELECT COUNT(*) FROM workflow_requests').fetchone()[0]==len(steps)
            and db.execute('SELECT COUNT(*) FROM workflow_seats').fetchone()[0]==len(steps),
            'delivery_children_incomplete')
        pending=[dict(row) for row in db.execute('SELECT * FROM role_broker_pending')]
        requests=[dict(row) for row in db.execute('SELECT * FROM workflow_requests')]
        _require(all(row['controller_request_id'] is not None or
            (type(row['terminal_reason']) is str and bool(row['terminal_reason'])) for row in pending),
            'delivery_pending_calls')
        links={(row['id'],row['controller_request_id']) for row in pending if row['controller_request_id'] is not None}
        _require(links=={(row['broker_pending_id'],row['id']) for row in requests},'delivery_pending_link_changed')
        common=dict(owner_id=root['owner_id'],project_id=root['project_id'],session_id=root['session_id'],
            turn_id=root['turn_id'],root_attempt_id=root_id,root_generation=expected_generation)
        _require(all(getattr(selected_revision.binding,k)==v for k,v in common.items()),'delivery_revision_scope_changed')
        initial=_input(db,root)
        _workspace(scheduler.store,initial,initial['revision_binding'],policy)
        carried=initial['sha256'];carried_binding=initial['revision_binding']
        selected_artifact=initial['id'];decisions=[];final=None
        for index,(step,definition) in enumerate(zip(steps,workflow['steps'])):
            _require(step['profile']==encode(definition['profile']),'delivery_assignment_changed')
            found=db.execute('SELECT a.* FROM attempts a JOIN workflow_seats s ON s.id=a.role_seat_id WHERE s.request_id=?',
                (step['request_id'],)).fetchall()
            _require(len(found)==1,'delivery_children_incomplete');child=found[0]
            launch=_json(db.execute('SELECT binding FROM workflow_child_launch WHERE child_id=?',(child['id'],)).fetchone()[0])
            expected_refs=[{'artifact_id':r['artifact_id'],'sha256':r['sha256']} for r in decisions]
            payload=_json(db.execute('SELECT payload FROM workflow_requests WHERE id=?',(step['request_id'],)).fetchone()[0])
            _require(_json(launch['assignment']['payload'])['arguments']['context_refs']==expected_refs
                and payload['arguments']['context_refs']==expected_refs,'delivery_context_chain_changed')
            task=step['role']=='acceptance_verification'
            body,metadata=(load_finalized_task if task else load_finalized_stage)(db,child['id'],forbidden_values=policy)
            released=(load_task_release if task else load_stage_release)(db,child['id'],forbidden_values=policy)
            _require(released is not None,'delivery_child_not_released')
            _require(body['root_id']==root_id and body['root_generation']==expected_generation
                and body['session_id']==root['session_id'] and body['workflow_sha256']==root['frozen_digest']
                and body['step_id']==step['step_id'] and body['role']==step['role']
                and body['input_revision_sha256']==step['input_revision_sha256']==carried and body['gate']=='pass',
                'delivery_chain_not_approved')
            _require(body['input_binding']==carried_binding,'delivery_input_binding_changed')
            if task:
                _require(index==len(steps)-1 and body['outcome']=='verified','delivery_acceptance_incomplete')
                final=body
            else:
                _require(body['outcome']=='unverified','delivery_chain_not_approved')
                if step['role'] in CODING_ROLES:
                    selected_artifact=body['candidate_artifact_id']
                    carried_binding=body['candidate_binding']
            carried=body['carried_revision_sha256']
            decisions.append({'step_id':step['step_id'],'role':step['role'],'child_id':child['id'],
                'generation':child['generation'],'artifact_id':metadata['id'],'sha256':metadata['sha256'],
                'input_revision_sha256':body['input_revision_sha256'],'carried_revision_sha256':carried,
                'release_sha256':released['receipt_sha256']})
        _require(db.execute('SELECT COUNT(*) FROM workflow_step_gates').fetchone()[0]==len(steps)
            and carried==selected_revision.sha256 and asdict(selected_revision.binding)==carried_binding,'delivery_selected_revision_changed')
        selected=_artifact(db,selected_artifact)
        _require(selected['sha256']==selected_revision.sha256
            and Path(selected['storage_path']).parent==selected_revision.path,'delivery_selected_revision_changed')
        has_coding=any(s['role'] in CODING_ROLES for s in steps)
        _require(not has_coding or final is not None,'delivery_acceptance_required')
        kind='workspace' if final is not None else 'report'
        if final is not None:_protected(db,scheduler.store,root,children[[c['id'] for c in children].index(final['attempt_id'])],final,environment,selected_manifest,policy)
        else:_require(not has_coding and selected_revision.sha256==initial['sha256'],'delivery_report_changed_workspace')
        public={'schema_version':1,'kind':kind,'outcome':'verified' if final is not None else 'unverified',
            'root_id':root_id,'generation':expected_generation,'session_id':root['session_id'],
            'owner_id':root['owner_id'],'project_id':root['project_id'],'turn_id':root['turn_id'],
            'workflow':workflow['workflow'],'workflow_sha256':root['frozen_digest'],
            'environment_sha256':environment['snapshot_sha256'],'input_revision_sha256':initial['sha256'],
            'carried_revision_sha256':carried,'selected_revision_sha256':carried if kind=='workspace' else None,
            'selected_artifact_id':selected_artifact if kind=='workspace' else None,
            'decision_refs':decisions,'report_refs':[{'artifact_id':r['artifact_id'],'sha256':r['sha256']} for r in decisions],
            'final_task_decision':decisions[-1] if final is not None else None}
        _scan(public,policy)
    value={'schema_version':1,'public':public,'private':{'database_path':database_path,
        'snapshot_sha256':_stable(snapshot,root_id),'revision_path':str(selected_revision.path),
        'revision_sha256':selected_revision.sha256,'revision_binding':asdict(selected_revision.binding)}}
    proof=RootDeliveryProof.from_json(_raw(value).decode())
    with _child_control_tx(scheduler.store,write=False) as db:validate_db(db,proof)
    return proof
