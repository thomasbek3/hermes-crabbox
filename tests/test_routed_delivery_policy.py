"""Real frozen six-stage/report controller flows; synthetic inference and Docker boundaries."""
import json
import pytest
from tests import test_six_stage_composition as six
from cloudworkbench import routed_delivery_policy as policy
from cloudworkbench.workflow_revisions import RevisionError


def collect(flow,**kw):
    return policy.collect_root_delivery_proof(flow.s,flow.root['attempt_id'],expected_generation=1,
        selected_revision=kw.pop('selected_revision',flow.revision),forbidden_values=kw.pop('forbidden_values',()),**kw)


def finish(flow,path,*,reject=False):
    for index in range(5 if reject else 6):
        root=path/f'stage-{index}';root.mkdir(mode=0o700)
        with pytest.MonkeyPatch.context() as patch:
            six.execute_stage(flow,index,root,patch,reject=reject and index==4)
    return flow


@pytest.fixture(scope='module')
def completed(tmp_path_factory):
    path=tmp_path_factory.mktemp('delivery-full')
    flow=finish(six.flow.__wrapped__(path),path)
    return flow,collect(flow)


def rewrite(proof,change):
    value=proof.to_dict();change(value)
    return policy._raw(value).decode()


def test_full_six_stage_proof_is_exact_safe_and_inert(completed):
    flow,proof=completed;public=proof.public_record()
    assert proof.kind=='workspace' and proof.outcome=='verified'
    assert public['selected_revision_sha256']==flow.revision.sha256!=flow.initial.sha256
    assert len(public['decision_refs'])==6
    assert [(r['step_id'],r['role']) for r in public['decision_refs']]==[(r[0],r[1]) for r in six.EXPECTED]
    assert public['final_task_decision']==public['decision_refs'][-1]
    assert public['report_refs']==[{'artifact_id':r['artifact_id'],'sha256':r['sha256']} for r in public['decision_refs']]
    assert 'storage_path' not in json.dumps(public) and str(flow.revision.path) not in json.dumps(public)
    assert policy.RootDeliveryProof.from_json(proof.canonical_json)==proof
    assert collect(flow)==proof
    public['outcome']='not-authority';assert proof.outcome=='verified'
    assert flow.store.get_attempt(flow.root['attempt_id'])['state']=='running'
    assert flow.s.leases.current('account')['reservation'] is not None


@pytest.mark.parametrize('change',[
    lambda v:v.update(extra=True),
    lambda v:v.update(schema_version=True),
    lambda v:v['public'].update(schema_version=True),
    lambda v:v['public'].update(generation=True),
    lambda v:v['public'].update(generation=2**64),
    lambda v:v['public'].update(project_id='../other'),
    lambda v:v['public'].update(outcome='unverified'),
    lambda v:v['public'].update(kind='report'),
    lambda v:v['public'].update(selected_revision_sha256=None),
    lambda v:v['public'].update(final_task_decision=None),
    lambda v:v['public']['final_task_decision'].update(generation=True),
    lambda v:v['public']['decision_refs'][1].update(input_revision_sha256='0'*64),
    lambda v:v['public']['decision_refs'][0].update(generation=False),
    lambda v:v['public']['decision_refs'][1].update(child_id=v['public']['decision_refs'][0]['child_id']),
    lambda v:v['public']['decision_refs'][0].update(extra='unsupported'),
    lambda v:v['public']['decision_refs'].reverse(),
    lambda v:v['public']['report_refs'].clear(),
    lambda v:v['private'].update(database_path='relative.db'),
    lambda v:v['private'].update(revision_path='/tmp/../other'),
    lambda v:v['private'].update(snapshot_sha256='A'*64),
    lambda v:v['private']['revision_binding'].update(root_generation=True),
    lambda v:v['private']['revision_binding'].update(attempt_id=v['public']['root_id']),
    lambda v:v['private'].update(extra='unsupported'),
])
def test_strict_frozen_proof_schema(completed,change):
    with pytest.raises(policy.DeliveryPolicyError):
        policy.RootDeliveryProof.from_json(rewrite(completed[1],change))


@pytest.mark.parametrize('raw',[None,{},'', '{}','{"schema_version":1,"schema_version":1}', 'NaN', 'x'*262145])
def test_malformed_proof_is_fixed_error(raw):
    with pytest.raises(policy.DeliveryPolicyError):policy.RootDeliveryProof.from_json(raw)


def test_validate_db_no_files_or_callbacks_and_lifecycle_changes_allowed(completed,monkeypatch):
    flow,proof=completed
    for name in ('_read','_load_verified','load_finalized_stage','load_finalized_task','load_stage_release','load_task_release'):
        monkeypatch.setattr(policy,name,lambda *a,**kw:pytest.fail('No filesystem/runtime callbacks inside validate_db'))
    with flow.store._connect() as db:
        db.execute('BEGIN')
        try:
            db.execute("UPDATE attempts SET state='verifying',result='{}',cancel_requested=1 WHERE id=?",(flow.root['attempt_id'],))
            db.execute("UPDATE workflow_roots SET state='releasing',version=version+1,cancel_requested=1 WHERE root_id=?",(flow.root['attempt_id'],))
            assert policy.validate_db(db,proof)
        finally:db.rollback()


@pytest.mark.parametrize('mutation',[
    "DELETE FROM workflow_step_gates WHERE rowid=(SELECT MIN(rowid) FROM workflow_step_gates)",
    "DELETE FROM events WHERE type='workflow.stage_child_released'",
    "UPDATE artifacts SET metadata=json_set(metadata,'$.sha256',printf('%064d',0)) WHERE json_extract(metadata,'$.provenance')='controller_stage_contract'",
    "UPDATE attempts SET result='{}' WHERE execution_kind='hermes_child'",
])
def test_final_db_compare_refuses_changed_evidence(completed,mutation):
    flow,proof=completed
    with flow.store._connect() as db:
        db.execute('BEGIN')
        try:
            db.execute(mutation)
            with pytest.raises(policy.DeliveryPolicyError,match='delivery_evidence_changed'):
                policy.validate_db(db,proof)
        finally:db.rollback()


def test_valid_schema_public_projection_cannot_be_rewritten(completed):
    flow,proof=completed
    value=proof.to_dict();value['public']['environment_sha256']='0'*64
    modified=policy.RootDeliveryProof.from_json(policy._raw(value).decode())
    with flow.store._tx() as db:
        with pytest.raises(policy.DeliveryPolicyError,match='delivery_evidence_changed'):policy.validate_db(db,modified)


def test_original_root_snapshot_is_revalidated_even_after_coding(completed):
    flow,_=completed;manifest=flow.initial.path/'manifest.json';raw=manifest.read_bytes();mode=manifest.stat().st_mode&0o7777
    try:
        manifest.chmod(0o640);manifest.write_bytes(raw+b' ');manifest.chmod(mode)
        with pytest.raises(RevisionError,match='manifest_digest_mismatch'):collect(flow)
    finally:
        manifest.chmod(0o640);manifest.write_bytes(raw);manifest.chmod(mode)


def test_different_selected_revision_refused(completed):
    flow,_=completed
    with pytest.raises(policy.DeliveryPolicyError,match='delivery_selected_revision_changed'):
        collect(flow,selected_revision=flow.initial)


def test_known_secrets_refused_from_saved_decision(completed):
    with pytest.raises(ValueError,match='secret'):
        collect(completed[0],forbidden_values=(b'Synthetic',))


def test_rejected_code_review_cannot_deliver(tmp_path):
    flow=finish(six.flow.__wrapped__(tmp_path),tmp_path,reject=True)
    with pytest.raises(policy.DeliveryPolicyError,match='delivery_children_incomplete'):collect(flow)


@pytest.mark.parametrize('workflow,expected',[('code_review',[six.EXPECTED[4]]),('planning',six.EXPECTED[:3])])
def test_report_only_complete_is_unverified_and_preserves_workspace(tmp_path,monkeypatch,workflow,expected):
    original_select=six.select_workflow;original_enqueue=six.enqueue_qualified_workflow
    monkeypatch.setattr(six,'EXPECTED',expected)
    monkeypatch.setattr(six,'select_workflow',lambda goal,**kw:original_select(goal,allowed_workflows=[workflow],explicit_workflow=workflow))
    def enqueue(*args,**kwargs):
        kwargs['allowed_workflows']=[workflow];return original_enqueue(*args,**kwargs)
    monkeypatch.setattr(six,'enqueue_qualified_workflow',enqueue)
    flow=six.flow.__wrapped__(tmp_path)
    for index in range(len(expected)):
        path=tmp_path/f'stage-{index}';path.mkdir(mode=0o700)
        with monkeypatch.context() as boundary:six.execute_stage(flow,index,path,boundary)
    proof=collect(flow);public=proof.public_record()
    assert proof.kind=='report' and proof.outcome=='unverified'
    assert public['selected_revision_sha256'] is None and public['selected_artifact_id'] is None
    assert public['final_task_decision'] is None
    assert flow.revision==flow.initial and public['carried_revision_sha256']==flow.initial.sha256
    assert len(public['report_refs'])==len(expected)
    with flow.store._tx() as db:assert policy.validate_db(db,proof)


@pytest.mark.parametrize('mutation,code',[
    ('missing_gate','stage_decision_gate_changed'),
    ('missing_release','delivery_child_not_released'),
    ('extra_request','delivery_children_incomplete'),
    ('wrong_context','delivery_context_chain_changed'),
    ('pending_call','delivery_pending_calls'),
])
def test_actual_completed_snapshot_requires_all_gates_releases_and_exact_context(completed,monkeypatch,mutation,code):
    flow,_=completed;original=policy._snapshot
    def changed(db,root_id):
        snapshot=original(db,root_id)
        if mutation=='missing_gate':snapshot['workflow_step_gates']['rows'].pop(0)
        if mutation=='missing_release':
            rows=snapshot['events']['rows']
            row=next(r for r in rows if r['type']=='workflow.stage_child_released');rows.remove(row)
        if mutation=='extra_request':
            rows=snapshot['workflow_requests']['rows'];row=dict(rows[0]);row.update(id='unregistered-request',broker_pending_id='unregistered-pending',ordinal=1000);rows.append(row)
        if mutation=='pending_call':
            rows=snapshot['role_broker_pending']['rows'];row=dict(rows[0])
            row.update(id='new-pending',request_key='new-request-key',native_call_id='new-call',controller_request_id=None,terminal_reason=None);rows.append(row)
        if mutation=='wrong_context':
            row=snapshot['workflow_requests']['rows'][1];payload=json.loads(row['payload'])
            payload['arguments']['context_refs']=[];row['payload']=policy._raw(payload).decode()
        return snapshot
    monkeypatch.setattr(policy,'_snapshot',changed)
    with pytest.raises(ValueError,match=code):collect(flow)


def test_input_binding_must_equal_carried_revision_binding(completed,monkeypatch):
    original=policy.load_finalized_stage
    def changed(*args,**kwargs):
        body,metadata=original(*args,**kwargs)
        body['input_binding']['attempt_id']='other-attempt'
        return body,metadata
    monkeypatch.setattr(policy,'load_finalized_stage',changed)
    with pytest.raises(policy.DeliveryPolicyError,match='delivery_input_binding_changed'):collect(completed[0])


def test_final_task_must_be_verified_not_needs_review(completed,monkeypatch):
    original=policy.load_finalized_task
    def changed(*args,**kwargs):
        body,metadata=original(*args,**kwargs);body['outcome']='needs_review'
        return body,metadata
    monkeypatch.setattr(policy,'load_finalized_task',changed)
    with pytest.raises(policy.DeliveryPolicyError,match='delivery_acceptance_incomplete'):collect(completed[0])


def test_stronger_secret_policy_scans_retained_original_and_selected_contents(completed):
    for secret in (b'answer = 42',b'answer = 43'):
        with pytest.raises(policy.DeliveryPolicyError,match='delivery_secret_refused'):
            collect(completed[0],forbidden_values=(secret,))


def test_structurally_valid_candidate_cannot_replace_decision_in_public_projection(completed):
    flow,proof=completed;value=proof.to_dict();ref=value['public']['decision_refs'][3]
    with flow.store._connect() as db:
        metadata=json.loads(db.execute("SELECT metadata FROM artifacts WHERE attempt_id=? AND json_extract(metadata,'$.provenance')='controller_captured_candidate'",(ref['child_id'],)).fetchone()[0])
    ref.update(artifact_id=metadata['id'],sha256=metadata['sha256'])
    value['public']['report_refs'][3]={'artifact_id':metadata['id'],'sha256':metadata['sha256']}
    changed=policy.RootDeliveryProof.from_json(policy._raw(value).decode())
    with flow.store._tx() as db:
        with pytest.raises(policy.DeliveryPolicyError,match='delivery_evidence_changed'):policy.validate_db(db,changed)
