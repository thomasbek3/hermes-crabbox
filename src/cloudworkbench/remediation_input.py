"""Bounded untrusted remediation data authenticated from a closed source workflow."""
from contextlib import closing
from dataclasses import dataclass,field
import json

from .routed_delivery_policy import (_snapshot,_detached,_json,_raw,_sha,_scan,_identifier,_positive,_digest,_artifact)
from .routed_stop import load_stopped_workflow
from .routed_stop_policy import collect_stopped_workflow_proof,validate_db as validate_stop_db,_document as validate_stop_document
from .routed_stage_decision import load_finalized_stage
from .routed_decision import load_finalized_task
from .routed_observation_store import validate_forbidden_values
from .routed_publication import _read
from .stage_contracts import stage_contract,parse_stage_output
from .routed_verification_policy import CheckExecution
from .scheduler import _child_control_tx
from pathlib import Path

VERSION='workflow-remediation-v1'
MAX_INPUT_BYTES=16*1024
_SOURCE_KEYS={'root_id','generation','owner_id','project_id','session_id','turn_id','workflow','workflow_sha256',
    'proof_sha256','cleanup_sha256','stopped_sequence','input_revision_sha256','carried_revision_sha256',
    'environment_sha256','environment_manifest_sha256','request_sha256'}


class RemediationInputError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def _require(value,code):
    if not value:raise RemediationInputError(code)


def _validate(value):
    _require(type(value) is dict and set(value)=={'schema_version','kind','source','goal','acceptance','blocker','records'}
        and type(value['schema_version']) is int and value['schema_version']==1
        and value['kind']=='untrusted_remediation_input','remediation_input_invalid')
    source=value['source']
    _require(type(source) is dict and set(source)==_SOURCE_KEYS
        and all(_identifier(source[k]) for k in ('root_id','owner_id','project_id','session_id','turn_id','workflow'))
        and _positive(source['generation']) and _positive(source['stopped_sequence'])
        and all(_digest(v) for k,v in source.items() if k.endswith('_sha256'))
        and type(value['goal']) is str and bool(value['goal'])
        and type(value['acceptance']) is list and len(value['acceptance'])<=100
        and type(value['records']) is list and 1<=len(value['records'])<=32,'remediation_input_invalid')
    seen=set()
    for criterion in value['acceptance']:
        _require(type(criterion) is dict and set(criterion)=={'id','description','mandatory'}
            and _identifier(criterion['id']) and criterion['id'] not in seen
            and type(criterion['description']) is str and bool(criterion['description'])
            and type(criterion['mandatory']) is bool,'remediation_input_invalid')
        seen.add(criterion['id'])
    for item in value['records']:
        _require(type(item) is dict and set(item)=={'reference','content'} and type(item['content']) is dict,
            'remediation_input_invalid')
    refs=[r['reference'] for r in value['records']]
    public={k:source[k] for k in ('root_id','generation','owner_id','project_id','session_id','turn_id','workflow',
        'workflow_sha256','environment_sha256','input_revision_sha256','carried_revision_sha256')}
    public.update(schema_version=1,kind='stopped_workflow',outcome=value['blocker']['outcome'],
        blocker=value['blocker'],decision_refs=refs)
    # Reuse the stop record's pure shape/chain validator; this synthetic private
    # placeholder is never returned or used for database authentication.
    validate_stop_document({'schema_version':1,'public':public,'private':{
        'database_path':'/schema-only','snapshot_sha256':'0'*64}})
    expected=[]
    for item in value['records']:
        ref,content=item['reference'],item['content'];task=ref['role']=='acceptance_verification'
        keys={'provenance','verification_scope','stage_output'}|({'verification'} if task else set())
        _require(set(content)==keys and content['provenance']=='worker_reported'
            and content['verification_scope']==('task_acceptance' if task else 'stage_contract'),'remediation_input_invalid')
        contract=stage_contract(ref['role'],ref['input_revision_sha256'],expected)
        output=parse_stage_output(json.dumps(content['stage_output'],ensure_ascii=False,separators=(',',':')),contract)
        _require(_raw(output.to_dict())==_raw(content['stage_output']),'remediation_input_invalid')
        if task:
            verification=content['verification']
            _require(type(verification) is dict and set(verification)=={'artifact_id','sha256','plan_sha256','environment_sha256','checks','assessment'}
                and _identifier(verification['artifact_id'])
                and all(_digest(verification[k]) for k in ('sha256','plan_sha256','environment_sha256'))
                and verification['environment_sha256']==source['environment_manifest_sha256']
                and type(verification['checks']) is list and len(verification['checks'])<=32
                and type(verification['assessment']) is dict,'remediation_input_invalid')
            assessment=verification['assessment']
            _require(set(assessment)=={'schema_version','plan_sha256','records','outcome','remaining_criteria'}
                and type(assessment['schema_version']) is int and assessment['schema_version']==1
                and assessment['plan_sha256']==verification['plan_sha256']
                and assessment['outcome']=={'rejected':'rejected','needs_review':'needs_review','verified':'passed'}[ref['outcome']]
                and type(assessment['records']) is list and type(assessment['remaining_criteria']) is list,
                'remediation_input_invalid')
            for check in verification['checks']:
                _require(type(check) is dict and set(check)=={'id','description','script_sha256'}
                    and _identifier(check['id']) and type(check['description']) is str and _digest(check['script_sha256']),
                    'remediation_input_invalid')
            parsed=[CheckExecution(**record) for record in assessment['records']]
            _require(len(parsed)<=32 and [record.check_id for record in parsed]==[check['id'] for check in verification['checks']]
                and len({record.check_id for record in parsed})==len(parsed)
                and len({record.runtime_id for record in parsed})==len(parsed)
                and all(record.cleanup_confirmed and not record.oom for record in parsed), 'remediation_input_invalid')
            passed={record.check_id for record in parsed if record.exit_code==0 and not record.timed_out}
            definitions={check['id']:check['description'] for check in verification['checks']}
            remaining=[criterion['id'] for criterion in value['acceptance'] if criterion['mandatory']
                and (criterion['id'] not in passed or definitions.get(criterion['id'])!=criterion['description'])]
            outcome='rejected' if len(passed)!=len(parsed) else 'needs_review' if not parsed or remaining else 'passed'
            _require(assessment['remaining_criteria']==remaining and assessment['outcome']==outcome,
                'remediation_input_invalid')
        expected.append({'artifact_id':ref['artifact_id'],'sha256':ref['sha256']})


@dataclass(frozen=True)
class RemediationInput:
    canonical_json:str=field(repr=False)

    @classmethod
    def from_json(cls,raw):
        try:
            _require(type(raw) is str and 0<len(raw.encode())<=MAX_INPUT_BYTES,'remediation_input_limit')
            value=_json(raw);_validate(value)
            _require(_raw(value).decode()==raw,'remediation_input_invalid')
        except (KeyError,TypeError,UnicodeError,RecursionError,OverflowError):
            raise RemediationInputError('remediation_input_invalid') from None
        return cls(raw)

    @property
    def sha256(self):return _sha(self.canonical_json.encode())
    def to_dict(self):return _json(self.canonical_json)
    def public_record(self):return self.to_dict()


def _read_document(db,identity,maximum,policy):
    metadata=_artifact(db,identity)
    _require(type(metadata.get('bytes')) is int and 0<metadata['bytes']<=maximum,'remediation_source_limit')
    raw=_read(Path(metadata['storage_path']),maximum)
    _require(len(raw)==metadata['bytes'] and _sha(raw)==metadata['sha256'],'remediation_source_changed')
    value=_json(raw);_scan(value,policy)
    return metadata,value


def collect_remediation_input(scheduler,source_root_id,*,expected_generation,forbidden_values=()):
    policy=validate_forbidden_values(forbidden_values)
    receipt=load_stopped_workflow(scheduler,source_root_id,expected_generation=expected_generation,forbidden_values=policy)
    proof=collect_stopped_workflow_proof(scheduler,source_root_id,expected_generation=expected_generation,forbidden_values=policy)
    _require(receipt.proof_sha256==proof.sha256,'remediation_source_changed')
    public=proof.public_record()
    with _child_control_tx(scheduler.store,write=False) as live:
        validate_stop_db(live,proof);snapshot=_snapshot(live,source_root_id)
    with closing(_detached(snapshot)) as db:
        root=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(source_root_id,)).fetchone()
        request=_json(db.execute('SELECT request FROM turns WHERE id=?',(root['turn_id'],)).fetchone()[0])
        environment=_json(root['frozen'])['provenance']['environment']
        source={k:public[k] for k in ('root_id','generation','owner_id','project_id','session_id','turn_id','workflow',
            'workflow_sha256','input_revision_sha256','carried_revision_sha256','environment_sha256')}
        source.update(proof_sha256=proof.sha256,cleanup_sha256=receipt.cleanup_sha256,
            stopped_sequence=receipt.event_sequence,environment_manifest_sha256=environment['manifest_sha256'],
            request_sha256=_sha(_raw(request)))
        records=[];expected=[]
        for ref in public['decision_refs']:
            task=ref['role']=='acceptance_verification'
            body,metadata=(load_finalized_task if task else load_finalized_stage)(db,ref['child_id'],forbidden_values=policy)
            _require(metadata['id']==ref['artifact_id'] and metadata['sha256']==ref['sha256'],'remediation_source_changed')
            if task:
                _,observation=_read_document(db,body['observation_artifact_id'],32*1024**2,policy)
                terminal=observation['observation']['events'][-1]
                _require(terminal['type']=='adapter.result','remediation_source_changed')
                output=parse_stage_output(_json(terminal['payload_json'])['summary'],
                    stage_contract(ref['role'],ref['input_revision_sha256'],expected)).to_dict()
                _require(_sha(_raw(output))==body['stage_output_sha256'],'remediation_source_changed')
                verification,document=_read_document(db,body['verification_artifact_id'],256*1024,policy)
                content={'provenance':'worker_reported','verification_scope':'task_acceptance','stage_output':output,
                    'verification':{'artifact_id':verification['id'],'sha256':verification['sha256'],
                        'plan_sha256':body['plan_sha256'],'environment_sha256':document['plan']['environment_sha256'],
                        'checks':[{k:c[k] for k in ('id','description','script_sha256')} for c in document['plan']['checks']],
                        'assessment':document['assessment']}}
            else:content={'provenance':'worker_reported','verification_scope':'stage_contract','stage_output':body['stage_output']}
            records.append({'reference':ref,'content':content})
            expected.append({'artifact_id':ref['artifact_id'],'sha256':ref['sha256']})
        value={'schema_version':1,'kind':'untrusted_remediation_input','source':source,'goal':request['goal'],
            'acceptance':request.get('acceptance',[]),'blocker':public['blocker'],'records':records}
        _scan(value,policy)
    result=RemediationInput.from_json(_raw(value).decode())
    _require(load_stopped_workflow(scheduler,source_root_id,expected_generation=expected_generation,
        forbidden_values=policy)==receipt,'remediation_source_changed')
    return result


def authenticate_frozen_remediation(scheduler,remediation,*,destination,forbidden_values=()):
    """Authenticate a frozen sidecar; does not grant admission or infer a retry."""
    policy=validate_forbidden_values(forbidden_values)
    required={'version','source_root_id','source_generation','source_proof_sha256','source_cleanup_sha256',
        'source_blocker','source_revision_sha256','input','input_sha256'}
    _require(type(remediation) is dict and required<=set(remediation) and remediation['version']==VERSION,
        'remediation_provenance_invalid')
    result=RemediationInput.from_json(_raw(remediation['input']).decode());source=result.to_dict()['source']
    _require(result.sha256==remediation['input_sha256']
        and source['root_id']==remediation['source_root_id'] and source['root_id']!=destination['root_id']
        and type(remediation['source_generation']) is int and source['generation']==remediation['source_generation']
        and source['proof_sha256']==remediation['source_proof_sha256']
        and source['cleanup_sha256']==remediation['source_cleanup_sha256']
        and source['carried_revision_sha256']==remediation['source_revision_sha256']
        and _raw(result.to_dict()['blocker'])==_raw(remediation['source_blocker'])
        and all(source[k]==destination[k] for k in ('owner_id','project_id','session_id')),
        'remediation_scope_changed')
    _scan(result.to_dict(),policy)
    current=collect_remediation_input(scheduler,source['root_id'],expected_generation=source['generation'],forbidden_values=policy)
    _require(current==result,'remediation_source_changed')
    return {'input_sha256':result.sha256,'input':result.to_dict()}
