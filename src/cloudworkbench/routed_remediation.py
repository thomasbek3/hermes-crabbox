"""Explicit, bounded same-session correction admission; never automatic retry."""
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path

from . import delivery_schema
from .inference_budget import BudgetPolicy
from .remediation_input import collect_remediation_input
from .remediation_revision import rebind_revision
from .root_cleanup_history import load_root_cleanup
from .routed_progression import _input
from .routed_stop import _saved, load_stopped_workflow
from .routed_stop_policy import collect_stopped_workflow_proof, validate_db
from .routed_delivery_policy import _artifact, _workspace
from .routed_observation_store import validate_forbidden_values
from .routed_publication import _read
from .scheduler import _child_control_tx
from .store import StoreError, encode, now, uid
from .workflow_instructions import bind_instructions, STAGE_BOUNDARY
from .workflow_revisions import RevisionBinding, _canonical
from .workflow_routing import resolve_remediation, remediation_policy_digest

VERSION = 'workflow-remediation-v1'
_BASE_KEYS = {'delivery_version','delivered_revision_sha256','delivered_artifact_id','last_delivery_artifact_id'}


def _require(value, detail):
    if not value: raise StoreError(409, detail)


def _sha(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _principal(db, principal, session_id):
    row=db.execute('SELECT * FROM clients WHERE id=?',(principal['id'],)).fetchone()
    session=db.execute('SELECT * FROM sessions WHERE id=?',(session_id,)).fetchone()
    _require(row is not None and row['revoked_at'] is None and 'submit' in json.loads(row['scopes'])
        and session is not None and session['owner_id']==principal['id'] and not session['archived']
        and session['project_id'] in json.loads(row['projects']), 'Remediation authority unavailable')
    return session


def _source(scheduler, source_root_id, generation, policy):
    receipt=load_stopped_workflow(scheduler,source_root_id,expected_generation=generation,forbidden_values=policy)
    proof=collect_stopped_workflow_proof(scheduler,source_root_id,expected_generation=generation,forbidden_values=policy)
    brief=collect_remediation_input(scheduler,source_root_id,expected_generation=generation,forbidden_values=policy)
    with _child_control_tx(scheduler.store,write=False) as db:
        validate_db(db,proof)
        root=dict(db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(source_root_id,)).fetchone())
        attempt=scheduler.store._attempt(db,source_root_id)
        frozen=json.loads(root['frozen'])
        request=json.loads(db.execute('SELECT request FROM turns WHERE id=?',(root['turn_id'],)).fetchone()[0])
        budget=dict(db.execute('SELECT * FROM inference_budget_roots WHERE root_id=?',(source_root_id,)).fetchone())
        metadata=_input(db,root)
        binding=metadata['revision_binding']
        for ref in proof.public_record()['decision_refs']:
            if ref['role'] in ('feature','bug_fix','refactoring','perf_issue','hillclimb'):
                metadata=_artifact(db,ref['candidate_artifact_id'])
                child=scheduler.store._attempt(db,ref['child_id'])
                binding=asdict(RevisionBinding(root['owner_id'],root['project_id'],root['session_id'],root['turn_id'],
                    source_root_id,generation,child['id'],child['generation']))
        _require(metadata['sha256']==proof.public_record()['carried_revision_sha256'],'Remediation revision changed')
    revision,manifest=_workspace(scheduler.store,metadata,binding,policy)
    content_sha256=hashlib.sha256(_canonical({k:manifest[k] for k in ('files','directories','selected_paths')})).hexdigest()
    return receipt,proof,brief,root,attempt,frozen,request,budget,revision,metadata['id'],content_sha256


def enqueue_remediation(scheduler, principal, source_root_id, key, *, expected_generation,
                       expected_stop_sha256, expected_delivery_base, router, parent,
                       trusted_pstack_root, forbidden_values=()):
    """Create one queued correction in the same session; no model or runtime launch."""
    policy=validate_forbidden_values(forbidden_values)
    _require(type(expected_delivery_base) is dict and set(expected_delivery_base)==_BASE_KEYS,
        'Exact remediation delivery base required')
    with _child_control_tx(scheduler.store,write=False) as db:
        original=db.execute('SELECT session_id FROM workflow_roots WHERE root_id=?',(source_root_id,)).fetchone()
        _require(original is not None,'Remediation source unavailable')
        _principal(db,principal,original['session_id'])
    receipt,proof,brief,source,attempt,frozen,request,budget,revision,artifact_id,content_sha256=_source(
        scheduler,source_root_id,expected_generation,policy)
    _require(receipt.proof_sha256==expected_stop_sha256==proof.sha256,'Remediation stop proof changed')
    blocker=proof.public_record()['blocker']
    # Resolving a plan only inspects configured seats; it performs no inference.
    plan=resolve_remediation(proof.public_record()['workflow'],blocker['role'],blocker['gate'],blocker['outcome'],
        router=router,parent=parent)
    references=bind_instructions(plan['workflow'],trusted_pstack_root)
    logical={'source_root_id':source_root_id,'generation':expected_generation,'stop_sha256':expected_stop_sha256,
        'delivery_base':expected_delivery_base,'routing_sha256':router.sha256,'parent':asdict(parent),
        'catalog_sha256':remediation_policy_digest(),'instructions':references,'input_sha256':brief.sha256}
    with _child_control_tx(scheduler.store) as db:
        _principal(db,principal,source['session_id'])
        def create():
            _require(delivery_schema.enabled(db),'Remediation requires delivery schema')
            validate_db(db,proof);_saved(db,scheduler,source_root_id,expected_generation,proof)
            release=load_root_cleanup(db,source_root_id,expected_generation)
            _require(_sha(release)==receipt.cleanup_sha256,'Remediation cleanup changed')
            _require(plan['ready'] is True,'Remediation backend unavailable')
            _require('remediation' not in frozen['provenance'],'Remediation lineage limit reached')
            current=scheduler.store._attempt(db,source_root_id)
            _require(not current['cancel_requested'] and not source['cancel_requested'],'Cancelled source cannot remediate')
            latest=db.execute('SELECT id,root_sequence FROM attempts WHERE session_id=? AND root_sequence IS NOT NULL ORDER BY root_sequence DESC LIMIT 1',
                (source['session_id'],)).fetchone()
            _require(latest is not None and latest['id']==source_root_id,'Remediation source is no longer latest')
            _require(not db.execute("SELECT 1 FROM attempts WHERE session_id=? AND state NOT IN ('completed','failed','cancelled','interrupted','paused') LIMIT 1",
                (source['session_id'],)).fetchone(),'Session has active work')
            session=_principal(db,principal,source['session_id'])
            _require(encode({k:session[k] for k in _BASE_KEYS})==encode(expected_delivery_base),'Remediation delivery base changed')
            _require(db.execute("SELECT COUNT(*) FROM attempts WHERE execution_kind IN ('legacy','hermes_root') AND state IN ('queued','preparing','running','waiting_input','awaiting_approval','checkpointing','held','verifying')").fetchone()[0]
                < scheduler.store.policy['max_pending_attempts'],'Pending root capacity reached')
            observed=scheduler.clock()
            live_budget=dict(db.execute('SELECT * FROM inference_budget_roots WHERE root_id=?',(source_root_id,)).fetchone())
            _require(live_budget==budget and type(observed) in (int,float) and math.isfinite(observed)
                and budget['high_water_us']/1_000_000<=observed<budget['deadline_us']/1_000_000,
                'Remediation lineage budget expired or changed')
            old_policy=json.loads(budget['policy_json']);remaining=old_policy['root_requests']-budget['used']
            _require(remaining>0,'Remediation request budget exhausted')
            new_policy={**old_policy,'root_requests':remaining,'attempt_requests':min(old_policy['attempt_requests'],remaining)}
            lineage={'version':VERSION,'source_root_id':source_root_id,'source_generation':expected_generation,
                'source_proof_sha256':proof.sha256,'source_cleanup_sha256':receipt.cleanup_sha256,
                'source_blocker':blocker,'source_revision_sha256':revision.sha256,
                'source_revision':{'sha256':revision.sha256,'binding':asdict(revision.binding),'artifact_id':artifact_id},
                'source_content_sha256':content_sha256,
                'input':brief.to_dict(),'input_sha256':brief.sha256,'lineage_ordinal':1,'lineage_root_id':source_root_id,
                'admitted_at':budget['admitted_us']/1_000_000,'deadline_at':budget['deadline_us']/1_000_000,
                'remaining_requests':remaining,'budget_policy':new_policy,'source_sequence':attempt['root_sequence'],
                'request_sha256':_sha(request)}
            plans={s['role']:[{'ready':True,'profile':s['profile']}] for s in plan['steps']}
            new_frozen={'accounts':frozen['accounts'],'role_plans':plans,'provenance':{
                'workflow':plan,'pstack_references':references,'stage_boundary':STAGE_BOUNDARY,
                'environment':frozen['provenance']['environment'],
                'stage_contract_version':frozen['provenance']['stage_contract_version'],
                'stage_progression_version':frozen['provenance']['stage_progression_version'],'remediation':lineage}}
            raw=encode(new_frozen);_require(len(raw.encode())<=65536,'Remediation snapshot limit')
            turn_id,root_id=uid(),uid();stamp=now()
            ordinal=db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM turns WHERE session_id=?',(source['session_id'],)).fetchone()[0]
            sequence=latest['root_sequence']+1
            db.execute('INSERT INTO turns VALUES(?,?,?,?,?)',(turn_id,source['session_id'],ordinal,encode(request),stamp))
            db.execute("INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,parent_attempt_id,created_at,updated_at,execution_kind,workflow_root_id,root_sequence) VALUES(?,?,?,'hermes',1,'queued',?,?,?,'hermes_root',?,?)",
                (root_id,source['session_id'],turn_id,source_root_id,stamp,stamp,root_id,sequence))
            db.execute("INSERT INTO workflow_roots(root_id,generation,owner_id,project_id,session_id,turn_id,frozen,frozen_digest,state) VALUES(?,1,?,?,?,?,?,?,'queued')",
                (root_id,source['owner_id'],source['project_id'],source['session_id'],turn_id,raw,hashlib.sha256(raw.encode()).hexdigest()))
            delivery_schema.freeze_base(db,root_id,1)
            for index,step in enumerate(plan['steps']):
                db.execute('INSERT INTO workflow_steps(root_id,step_id,ordinal,role,profile) VALUES(?,?,?,?,?)',
                    (root_id,step['id'],index,step['role'],encode(step['profile'])))
            scheduler.store._event(db,source['session_id'],root_id,'attempt.state',{'state':'queued','execution_kind':'hermes_root','generation':1})
            scheduler.store._event(db,source['session_id'],root_id,'workflow.remediation_admitted',{
                'source_root_id':source_root_id,'source_proof_sha256':proof.sha256,'input_sha256':brief.sha256,
                'root_sequence':sequence,'lineage_ordinal':1,'automatic_retry':False})
            return {'session_id':source['session_id'],'turn_id':turn_id,'attempt_id':root_id,'generation':1,'state':'queued'}
        return scheduler.store._idem(db,principal,key,['explicit-remediation',logical],create)


def prepare_remediation_input(scheduler, principal, root_id, *, expected_generation, revision_root, forbidden_values=()):
    """Rebind and register immutable material while the new root is still queued."""
    policy=validate_forbidden_values(forbidden_values)
    with _child_control_tx(scheduler.store,write=False) as db:
        row=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone()
        _require(row is not None,'Remediation root unavailable')
        root=dict(row)
        _principal(db,principal,root['session_id'])
        attempt=scheduler.store._attempt(db,root_id);frozen=json.loads(root['frozen'])
        _require(hashlib.sha256(root['frozen'].encode()).hexdigest()==root['frozen_digest'],
            'Remediation snapshot changed')
        lineage=frozen['provenance'].get('remediation')
        _require(lineage and lineage['version']==VERSION and root['generation']==expected_generation==1
            and attempt['state']=='queued' and root['state']=='queued' and not attempt['cancel_requested'],
            'Queued remediation input authority required')
    source=_source(scheduler,lineage['source_root_id'],lineage['source_generation'],policy)
    receipt,proof,brief,_,_,_,_,_,revision,_,content_sha256=source
    _require(proof.sha256==lineage['source_proof_sha256'] and receipt.cleanup_sha256==lineage['source_cleanup_sha256']
        and brief.sha256==lineage['input_sha256'] and revision.sha256==lineage['source_revision_sha256']
        and content_sha256==lineage['source_content_sha256'],
        'Remediation source changed')
    binding=RevisionBinding(root['owner_id'],root['project_id'],root['session_id'],root['turn_id'],root_id,1,root_id,1)
    rebound=rebind_revision(scheduler.store,revision,binding,revision_root,
        expected_content_sha256=lineage['source_content_sha256'],forbidden_values=policy)
    manifest=Path(rebound.revision.path)/'manifest.json';raw=_read(manifest,2*1024**2,private=False)
    identity='root-input-'+hashlib.sha256(root_id.encode()).hexdigest()
    metadata={'id':identity,'attempt_id':root_id,'session_id':root['session_id'],'generation':1,
        'provenance':'controller_root_input','sha256':rebound.revision.sha256,'bytes':len(raw),'mime':'application/json',
        'path':'routed/root-input/manifest.json','storage_path':str(manifest),'revision_binding':asdict(binding)}
    with _child_control_tx(scheduler.store) as db:
        _principal(db,principal,root['session_id']);current=scheduler.store._attempt(db,root_id)
        saved=dict(db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone())
        _require(saved==root and current['state']=='queued' and not current['cancel_requested']
            and not db.execute('SELECT 1 FROM workflow_requests WHERE root_id=?',(root_id,)).fetchone(),
            'Remediation input authority changed')
        validate_db(db,proof);load_root_cleanup(db,lineage['source_root_id'],lineage['source_generation'])
        event={'source_root_id':lineage['source_root_id'],'source_revision_sha256':revision.sha256,
            'revision_sha256':rebound.revision.sha256,'content_sha256':rebound.content_sha256,
            'input_sha256':brief.sha256,'artifact_id':identity}
        exists=db.execute('SELECT metadata FROM artifacts WHERE id=?',(identity,)).fetchone()
        if exists:
            _require(json.loads(exists[0])==metadata,'Remediation input conflict')
            events=db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='workflow.remediation_input' LIMIT 2",(root_id,)).fetchall()
            _require(len(events)==1 and json.loads(events[0][0])==event,'Remediation input event changed')
        else:
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?)',(identity,root['session_id'],root_id,encode(metadata)))
            public={k:v for k,v in metadata.items() if k!='storage_path'}
            for kind,body in [('artifact.created',public),('workflow.root_input',public),('workflow.remediation_input',event)]:
                scheduler.store._event(db,root['session_id'],root_id,kind,body)
    return rebound


def admission_budget(db, root, stamp):
    """Scheduler hook: require prepared material and inherit the original bounds."""
    frozen=json.loads(root['frozen']);lineage=frozen['provenance'].get('remediation')
    if lineage is None:return BudgetPolicy(),stamp,stamp+14400
    _require(hashlib.sha256(root['frozen'].encode()).hexdigest()==root['frozen_digest'],
        'Remediation snapshot changed')
    _principal(db,{'id':root['owner_id']},root['session_id'])
    _require(lineage['version']==VERSION and lineage['lineage_ordinal']==1
        and lineage['lineage_root_id']==lineage['source_root_id']
        and lineage['admitted_at']<=stamp<lineage['deadline_at'],'Remediation lineage expired')
    material=_input(db,root)
    events=db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='workflow.remediation_input' LIMIT 2",(root['root_id'],)).fetchall()
    expected_input={'source_root_id':lineage['source_root_id'],'source_revision_sha256':lineage['source_revision_sha256'],
        'revision_sha256':material['sha256'],'content_sha256':lineage['source_content_sha256'],
        'input_sha256':lineage['input_sha256'],'artifact_id':material['id']}
    _require(len(events)==1 and encode(json.loads(events[0][0]))==encode(expected_input),'Remediation preparation proof missing')
    source=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(lineage['source_root_id'],)).fetchone()
    _require(source is not None and source['session_id']==root['session_id'] and source['state']=='released'
        and 'remediation' not in json.loads(source['frozen'])['provenance'],'Remediation lineage changed')
    release=load_root_cleanup(db,source['root_id'],source['generation'])
    source_attempt=db.execute('SELECT * FROM attempts WHERE id=?',(source['root_id'],)).fetchone()
    target_attempt=db.execute('SELECT * FROM attempts WHERE id=?',(root['root_id'],)).fetchone()
    source_record=json.loads(source_attempt['result'])['routed_stopped']
    _require(_sha(release)==lineage['source_cleanup_sha256'] and source_record['proof_sha256']==lineage['source_proof_sha256']
        and source_attempt['root_sequence']==lineage['source_sequence']
        and target_attempt['parent_attempt_id']==source['root_id']
        and target_attempt['root_sequence']==source_attempt['root_sequence']+1,'Remediation source identity changed')
    events=db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='workflow.remediation_admitted' LIMIT 2",(root['root_id'],)).fetchall()
    expected_admission={'source_root_id':source['root_id'],'source_proof_sha256':lineage['source_proof_sha256'],
        'input_sha256':lineage['input_sha256'],'root_sequence':target_attempt['root_sequence'],
        'lineage_ordinal':1,'automatic_retry':False}
    _require(len(events)==1 and encode(json.loads(events[0][0]))==encode(expected_admission),
        'Remediation admission record changed')
    source_request=db.execute('SELECT request FROM turns WHERE id=?',(source['turn_id'],)).fetchone()[0]
    target_request=db.execute('SELECT request FROM turns WHERE id=?',(root['turn_id'],)).fetchone()[0]
    _require(source_request==target_request and _sha(json.loads(source_request))==lineage['request_sha256'],
        'Remediation task criteria changed')
    budget=db.execute('SELECT * FROM inference_budget_roots WHERE root_id=?',(source['root_id'],)).fetchone()
    original=json.loads(budget['policy_json']);remaining=original['root_requests']-budget['used']
    expected={**original,'root_requests':remaining,'attempt_requests':min(original['attempt_requests'],remaining)}
    _require(expected==lineage['budget_policy'] and remaining==lineage['remaining_requests']>0
        and budget['admitted_us']/1_000_000==lineage['admitted_at']
        and budget['deadline_us']/1_000_000==lineage['deadline_at']
        and stamp>=budget['high_water_us']/1_000_000,'Remediation budget changed')
    return BudgetPolicy(**expected),lineage['admitted_at'],lineage['deadline_at']
