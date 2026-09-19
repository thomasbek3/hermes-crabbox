"""Authenticated historical remediation data; synthetic execution, real controllers."""
import copy
import json
from pathlib import Path

import pytest

from cloudworkbench import remediation_input as brief
from cloudworkbench import routed_context as context
from cloudworkbench.routed_stop import close_stopped_workflow
from cloudworkbench.store import Store, encode
from tests.test_routed_stop_policy import build_stopped
from tests.test_routed_delivery import ready as delivery_ready


def canonical(value): return encode(value)


@pytest.fixture(scope='module', params=['stage_rejected', 'task_rejected', 'task_needs_review'])
def stopped_seed(request, tmp_path_factory):
    source = tmp_path_factory.mktemp('remediation-' + request.param)
    migrate = Store.migrate_scheduler
    with pytest.MonkeyPatch.context() as patch:
        def v3(store):
            migrate(store); store.migrate_delivery()
        patch.setattr(Store, 'migrate_scheduler', v3)
        flow = build_stopped(source, request.param)
    return flow, request.param


@pytest.fixture
def closed(stopped_seed, tmp_path):
    flow, case = stopped_seed
    value = delivery_ready.__wrapped__((flow, None), tmp_path)
    value.case = case
    value.receipt = close_stopped_workflow(value.s, value.root, expected_generation=1,
        cleanup_verifier=value.verifier)
    return value


def collect(value, **kwargs):
    return brief.collect_remediation_input(value.s, value.root, expected_generation=1, **kwargs)


def provenance(value):
    document=value.to_dict(); source=document['source']
    return {'version':brief.VERSION, 'source_root_id':source['root_id'],
        'source_generation':source['generation'], 'source_proof_sha256':source['proof_sha256'],
        'source_cleanup_sha256':source['cleanup_sha256'], 'source_blocker':document['blocker'],
        'source_revision_sha256':source['carried_revision_sha256'],
        'input':document, 'input_sha256':value.sha256,
        'lineage_ordinal':1, 'lineage_root_id':source['root_id'], 'deadline_at':2000,
        'future_parent_field':'compatible'}


def destination(value):
    return {'root_id':'new-root', **{k:value.to_dict()['source'][k]
        for k in ('owner_id', 'project_id', 'session_id')}}


def test_actual_closed_source_projection_is_complete_and_inert(closed):
    value=collect(closed); body=value.to_dict()
    assert value == brief.RemediationInput.from_json(value.canonical_json)
    assert value.public_record() == body
    assert len(value.canonical_json.encode()) <= brief.MAX_INPUT_BYTES
    assert body['source']['proof_sha256'] == closed.receipt.proof_sha256
    assert body['source']['cleanup_sha256'] == closed.receipt.cleanup_sha256
    assert body['source']['stopped_sequence'] == closed.receipt.event_sequence
    assert body['blocker'] == body['records'][-1]['reference']
    assert len(body['records']) == (5 if closed.case == 'stage_rejected' else 6)
    with closed.store._connect() as db:
        request=json.loads(db.execute('SELECT request FROM turns WHERE id=?',
            (body['source']['turn_id'],)).fetchone()[0])
    assert body['goal'] == request['goal'] and body['acceptance'] == request['acceptance']
    assert all(record['content']['provenance'] == 'worker_reported' for record in body['records'])
    assert 'storage_path' not in value.canonical_json and str(closed.store.path) not in value.canonical_json
    assert collect(closed) == value
    assert len(closed.runtime_calls) == len(closed.provider_calls) == 1
    body['goal']='detached'; assert value.to_dict()['goal'] == request['goal']


def test_actual_protected_check_evidence_is_preserved(closed):
    value=collect(closed).to_dict(); content=value['records'][-1]['content']
    if closed.case == 'stage_rejected':
        assert 'verification' not in content
    else:
        verified=content['verification']
        assert verified['environment_sha256'] == value['source']['environment_manifest_sha256']
        assessment=verified['assessment']
        if closed.case == 'task_rejected':
            assert assessment['outcome'] == 'rejected'
            assert assessment['records'][0]['exit_code'] == 1
            assert assessment['records'][0]['cleanup_confirmed'] is True
        else:
            assert assessment['outcome'] == 'needs_review'
            assert assessment['records'] == [] and assessment['remaining_criteria']
        assert 'binding' not in canonical(verified) and 'storage_path' not in canonical(verified)


def test_scope_authentication_and_known_secret_policy(closed):
    value=collect(closed); frozen=provenance(value)
    sidecar=brief.authenticate_frozen_remediation(closed.s, frozen, destination=destination(value))
    assert sidecar == {'input_sha256':value.sha256, 'input':value.to_dict()}
    for field in ('owner_id','project_id','session_id','root_id'):
        target=destination(value); target[field]=value.to_dict()['source']['root_id'] if field=='root_id' else 'foreign'
        with pytest.raises(brief.RemediationInputError, match='remediation_scope_changed'):
            brief.authenticate_frozen_remediation(closed.s, frozen, destination=target)
    with pytest.raises(ValueError):
        collect(closed, forbidden_values=(value.to_dict()['goal'].encode(),))


def test_frozen_provenance_cannot_substitute_source_or_brief(closed):
    value=collect(closed)
    for field in ('input_sha256','source_proof_sha256','source_cleanup_sha256','source_revision_sha256'):
        frozen=provenance(value);frozen[field]='0'*64
        with pytest.raises(brief.RemediationInputError, match='remediation_scope_changed'):
            brief.authenticate_frozen_remediation(closed.s, frozen, destination=destination(value))
    frozen=provenance(value);frozen['input']['goal']='Modified goal'
    frozen['input_sha256']=brief.RemediationInput.from_json(canonical(frozen['input'])).sha256
    with pytest.raises(brief.RemediationInputError, match='remediation_source_changed'):
        brief.authenticate_frozen_remediation(closed.s, frozen, destination=destination(value))


def test_unclosed_source_is_refused(stopped_seed, tmp_path):
    value=delivery_ready.__wrapped__((stopped_seed[0],None),tmp_path)
    with pytest.raises(ValueError,match='stopped_workflow_record_changed'):collect(value)


def test_schema2_sidecar_is_separate_and_does_not_add_foreign_refs(closed):
    value=collect(closed)
    body={'schema_version':2,'kind':'untrusted_prior_stage_context','consumer_id':'new-child',
        'generation':1,'root_id':'new-root','root_generation':1,'assignment_sha256':'a'*64,
        'input_revision_sha256':'b'*64,'records':[],
        'remediation':{'input_sha256':value.sha256,'input':value.to_dict()}}
    raw=canonical(body)
    result=context.StageContext(raw,context._render(raw),context._sha(raw.encode()),'a'*64,'b'*64)
    assert context.validate_stage_context(result,input_revision_sha256='b'*64,context_refs=()) == result
    assert 'BEGIN UNTRUSTED REMEDIATION DATA' in result.text
    assert body['records'] == []
    prefix=result.text.split('END UNTRUSTED PRIOR-STAGE DATA.')[0]
    assert value.to_dict()['source']['root_id'] not in prefix
    altered=copy.deepcopy(body);altered['remediation']['input_sha256']='0'*64
    raw=canonical(altered)
    with pytest.raises(context.ContextError,match='context_remediation_invalid'):
        context.validate_stage_context(context.StageContext(raw,context._render(raw),context._sha(raw.encode()),'a'*64,'b'*64),
            input_revision_sha256='b'*64,context_refs=())


def test_strict_schema_canonical_and_size_bounds(closed):
    value=collect(closed)
    for modify in (lambda d:d.update(extra=True), lambda d:d['source'].update(generation=True),
        lambda d:d['source'].update(proof_sha256='invalid'), lambda d:d['records'].reverse(),
        lambda d:d['records'][0]['content'].update(provenance='controller_verified')):
        invalid=value.to_dict();modify(invalid)
        with pytest.raises(ValueError):brief.RemediationInput.from_json(canonical(invalid))
    with pytest.raises(ValueError):brief.RemediationInput.from_json(value.canonical_json+' ')
    with pytest.raises(ValueError):brief.RemediationInput.from_json('{"x":1,"x":2}')
    with pytest.raises(ValueError):brief.RemediationInput.from_json('{"x":NaN}')
    invalid=value.to_dict();invalid['goal']='x'*brief.MAX_INPUT_BYTES
    with pytest.raises(brief.RemediationInputError,match='remediation_input_limit'):
        brief.RemediationInput.from_json(canonical(invalid))


def test_changed_registered_evidence_is_refused(closed):
    value=collect(closed); identity=value.to_dict()['records'][0]['reference']['artifact_id']
    with closed.store._tx() as db:
        row=db.execute('SELECT metadata FROM artifacts WHERE id=?',(identity,)).fetchone()
        metadata=json.loads(row[0]);metadata['sha256']='0'*64
        db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(encode(metadata),identity))
    with pytest.raises(ValueError):collect(closed)


def test_retained_source_file_drift_is_reauthenticated(closed):
    value=collect(closed); identity=value.to_dict()['records'][0]['reference']['artifact_id']
    with closed.store._connect() as db:
        metadata=json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?',(identity,)).fetchone()[0])
    path=Path(metadata['storage_path']);mode=path.stat().st_mode & 0o777;original=path.read_bytes()
    try:
        path.chmod(0o600);path.write_bytes(b'{}');path.chmod(mode)
        with pytest.raises(ValueError):
            brief.authenticate_frozen_remediation(closed.s,provenance(value),destination=destination(value))
    finally:
        path.chmod(0o600);path.write_bytes(original);path.chmod(mode)


def test_protected_evidence_shape_and_outcome_cannot_be_rewritten(closed):
    value=collect(closed)
    if closed.case=='stage_rejected':return
    for modify in (lambda v:v['assessment'].update(records=[{}]),
        lambda v:v['assessment'].update(remaining_criteria=['made-up-criterion']),
        lambda v:v.update(checks=v['checks']+[{'id':'unexpected','description':'extra','script_sha256':'a'*64}])):
        invalid=value.to_dict();modify(invalid['records'][-1]['content']['verification'])
        with pytest.raises(ValueError):brief.RemediationInput.from_json(canonical(invalid))


# These fixtures use real schema3 remediation admission and same-session rebinding.
from tests.test_routed_remediation import value as admitted_value, enqueue, prepare
from tests.test_routed_stop import blocked_seed, stopped


def initial_child(value):
    from cloudworkbench.role_broker import RoleBroker, ParentScope
    from cloudworkbench.routed_progression import stage_inputs
    created=enqueue(value);prepare(value,created)
    value.s.admit_root(created['attempt_id'],accounts={'account':'owner'})
    value.s.transition(created['attempt_id'],'running',expected_generation=1)
    scope=ParentScope(value.owner['id'],'demo',created['attempt_id'],created['attempt_id'],1,'native')
    broker=RoleBroker(value.store.path,authorize_parent=value.s.authorize_parent,clock=lambda:1000)
    with value.store._connect() as db:
        frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?',(created['attempt_id'],)).fetchone()[0])
    steps=frozen['provenance']['workflow']['steps'];first=steps[0]
    _,token=broker.issue(scope,roles=tuple(step['role'] for step in steps),expires_at=2000)
    chosen=stage_inputs(value.s,scope,first['id'])
    assert chosen['context_refs']==[]
    request=broker.prepare(token,native_session_id='native',native_call_id='repair',role=first['role'],
        task='Repair the saved review findings.',context_refs=())
    value.s.admit_request(broker,scope,request['id'],plan=frozen['role_plans'][first['role']],
        step_id=first['id'],input_revision_sha256=chosen['input_revision_sha256'])
    return value.s.claim_child(created['attempt_id']),frozen


def test_actual_admitted_child_loads_cross_root_brief_as_separate_data(admitted_value):
    value=admitted_value;child,frozen=initial_child(value)
    result=context.load_stage_context(value.s,child['id'],expected_generation=1,forbidden_values=())
    body=json.loads(result.canonical_json)
    assert body['schema_version']==2 and body['records']==[]
    assert body['remediation']['input']==frozen['provenance']['remediation']['input']
    assert body['remediation']['input']['source']['root_id']==value.root
    assert body['root_id']==child['workflow_root_id']!=value.root
    assert 'BEGIN UNTRUSTED REMEDIATION DATA' in result.text
    assert context.validate_stage_context(result,input_revision_sha256=result.input_revision_sha256,context_refs=())==result
    assert value.s.child_assignment(child['id'],expected_generation=1)['context_refs']==[]
