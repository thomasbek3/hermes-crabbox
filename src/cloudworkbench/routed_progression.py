"""Controller-owned root input and exact finalized-output admission bindings."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .store import StoreError, encode
from .workflow_revisions import WorkspaceRevision, verify_revision
from .routed_publication import _read

VERSION = 'stage-progression-v1'


def _require(condition, message):
    if not condition:
        raise StoreError(409,message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _policy(db, scope):
    row=db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(scope.root_attempt_id,)).fetchone()
    _require(row is not None,'Workflow progression root missing')
    root=dict(row)
    frozen=json.loads(root['frozen'])
    version=frozen['provenance'].get('stage_progression_version')
    if version is None:
        return root,False
    _require(version==VERSION and _sha(root['frozen'].encode())==root['frozen_digest'],
             'Workflow progression policy changed')
    _require(root['generation']==scope.generation and root['owner_id']==scope.owner_id
        and root['project_id']==scope.project_id and scope.parent_attempt_id==root['root_id'],
        'Workflow progression scope changed')
    return root,True


def _input(db,root):
    identity='root-input-'+_sha(root['root_id'].encode())
    row=db.execute('SELECT metadata FROM artifacts WHERE id=? AND attempt_id=? AND session_id=?',
        (identity,root['root_id'],root['session_id'])).fetchone()
    _require(row is not None,'Registered workflow input required')
    metadata=json.loads(row['metadata'])
    binding=metadata.get('revision_binding')
    _require(metadata.get('id')==identity and metadata.get('attempt_id')==root['root_id']
        and metadata.get('session_id')==root['session_id'] and metadata.get('generation')==root['generation']
        and metadata.get('provenance')=='controller_root_input'
        and binding=={'owner_id':root['owner_id'],'project_id':root['project_id'],
            'session_id':root['session_id'],'turn_id':root['turn_id'],'root_attempt_id':root['root_id'],
            'root_generation':root['generation'],'attempt_id':root['root_id'],'generation':root['generation']},
        'Registered workflow input changed')
    expected={k:v for k,v in metadata.items() if k!='storage_path'}
    events=db.execute("SELECT payload FROM events WHERE attempt_id=? AND type='workflow.root_input' LIMIT 2",
        (root['root_id'],)).fetchall()
    _require(len(events)==1 and json.loads(events[0]['payload'])==expected,
             'Registered workflow input event changed')
    return metadata


def register_root_input(scheduler,scope,revision):
    """Pin a captured root input before any child admission; never infer one from latest files."""
    _require(type(revision) is WorkspaceRevision,'Captured root revision required')
    verify_revision(scheduler.store,revision)
    raw=_read(Path(revision.path)/'manifest.json',2*1024**2,private=False)
    _require(_sha(raw)==revision.sha256,'Captured root revision changed')
    with scheduler.store._tx() as db:
        root,enabled=_policy(db,scope)
        _require(enabled and scheduler.authorize_parent(db,scope),'Workflow input authority unavailable')
        binding=asdict(revision.binding)
        _require(binding=={'owner_id':root['owner_id'],'project_id':root['project_id'],
            'session_id':root['session_id'],'turn_id':root['turn_id'],'root_attempt_id':root['root_id'],
            'root_generation':root['generation'],'attempt_id':root['root_id'],'generation':root['generation']},
            'Root input revision scope changed')
        identity='root-input-'+_sha(root['root_id'].encode())
        metadata={'id':identity,'attempt_id':root['root_id'],'session_id':root['session_id'],
            'generation':root['generation'],'provenance':'controller_root_input','sha256':revision.sha256,
            'bytes':len(raw),'mime':'application/json','path':'routed/root-input/manifest.json',
            'storage_path':str(Path(revision.path)/'manifest.json'),'revision_binding':binding}
        exists=db.execute('SELECT 1 FROM artifacts WHERE id=?',(identity,)).fetchone()
        if exists:
            _require(_input(db,root)==metadata,'Workflow root input is immutable')
            return {'artifact_id':identity,'sha256':revision.sha256}
        _require(not root['child_attempt_id'] and not db.execute(
            'SELECT 1 FROM workflow_requests WHERE root_id=?',(root['root_id'],)).fetchone(),
            'Workflow root input must precede children')
        db.execute('INSERT INTO artifacts(id,session_id,attempt_id,metadata) VALUES(?,?,?,?)',
            (identity,root['session_id'],root['root_id'],encode(metadata)))
        public={k:v for k,v in metadata.items() if k!='storage_path'}
        scheduler.store._event(db,root['session_id'],root['root_id'],'artifact.created',public)
        scheduler.store._event(db,root['session_id'],root['root_id'],'workflow.root_input',public)
        return {'artifact_id':identity,'sha256':revision.sha256}


def _expected_inputs(db,root,step):
    prior=db.execute('SELECT * FROM workflow_steps WHERE root_id=? AND ordinal<? ORDER BY ordinal',
        (root['root_id'],step['ordinal'])).fetchall()
    carried=_input(db,root)['sha256']
    refs=[]
    if prior:
        from .routed_stage_decision import load_finalized_stage, load_stage_release
        for previous in prior:
            children=db.execute('SELECT a.id FROM attempts a JOIN workflow_seats s ON s.id=a.role_seat_id '
                'WHERE s.request_id=? AND a.workflow_root_id=?',(previous['request_id'],root['root_id'])).fetchall()
            _require(len(children)==1,'Workflow predecessor must have one finalized child')
            body,metadata=load_finalized_stage(db,children[0]['id'])
            _require(body['root_id']==root['root_id'] and body['root_generation']==root['generation']
                and body['session_id']==root['session_id'] and body['step_id']==previous['step_id']
                and body['role']==previous['role'] and body['gate']=='pass'
                and body['input_revision_sha256']==previous['input_revision_sha256']==carried,
                'Workflow predecessor output chain changed')
            _require(load_stage_release(db,children[0]['id']) is not None,
                     'Workflow predecessor cleanup release required')
            refs.append({'artifact_id':metadata['id'],'sha256':metadata['sha256']})
            carried=body['carried_revision_sha256']
    return carried,refs


def stage_inputs(scheduler,scope,step_id):
    """Read controller-selected broker inputs; admission independently rechecks them."""
    with scheduler.store._tx() as db:
        root,enabled=_policy(db,scope)
        _require(enabled and scheduler.authorize_parent(db,scope),'Workflow progression authority unavailable')
        step=db.execute('SELECT * FROM workflow_steps WHERE root_id=? AND step_id=?',
            (root['root_id'],step_id)).fetchone()
        _require(step is not None,'Frozen workflow step required')
        carried,refs=_expected_inputs(db,root,step)
        return {'step_id':step['step_id'],'role':step['role'],'input_revision_sha256':carried,'context_refs':refs}


def validate_stage_admission(db,scope,step,input_revision_sha256,payload):
    """Called in the broker admission transaction; model context is compared, never selected."""
    root,enabled=_policy(db,scope)
    if not enabled:
        return
    carried,refs=_expected_inputs(db,root,step)
    _require(input_revision_sha256==carried,'Workflow assigned revision differs from finalized output')
    _require(payload['arguments']['context_refs']==refs,'Workflow context differs from finalized stage outputs')
