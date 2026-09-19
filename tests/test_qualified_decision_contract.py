"""Versioned acceptance decisions consume authenticated saved stage answers."""
from dataclasses import replace
import json

import pytest

from cloudworkbench import routed_decision as decision
from cloudworkbench.stage_contracts import VERSION, stage_contract
from tests import test_routed_decision as legacy
from tests.test_routed_decision import cleanup, complete, release
from tests.test_routed_verification import composed, prepare, execute
from tests.test_routed_results import result_phase
from tests.test_routed_runtime import setup
from tests.test_routed_collection import event


@pytest.fixture
def stage(tmp_path, monkeypatch):
    enqueue = legacy.enqueue_workflow
    def versioned(*args, **kwargs):
        return enqueue(*args, **kwargs, _stage_contract_version=VERSION)
    monkeypatch.setattr(legacy, 'enqueue_workflow', versioned)
    return legacy.stage.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def driven(stage, setup, monkeypatch, request):
    case = getattr(request, 'param', 'valid')
    if case in ('wrong_launch', 'missing_launch'):
        from cloudworkbench import routed_stage
        build = routed_stage.build_routed_launch
        def altered(*args, **kwargs):
            plan = build(*args, **kwargs)
            receipt = json.loads(plan.receipt_json)
            if case == 'wrong_launch':
                receipt['stage_contract_sha256'] = 'e' * 64
            else:
                del receipt['stage_contract_sha256']; del receipt['stage_contract']
            return replace(plan, receipt_json=json.dumps(receipt, sort_keys=True, separators=(',', ':')))
        monkeypatch.setattr(routed_stage, 'build_routed_launch', altered)
    value = legacy.original_driven.__wrapped__(stage, setup, monkeypatch)
    assignment = stage[0].child_assignment(stage[2]['id'], expected_generation=1)
    contract = stage_contract(assignment['role'], stage[3].sha256, assignment['context_refs'])
    answer = {'schema_version':1, 'contract_sha256':contract.digest, 'role':contract.role,
              'input_revision_sha256':contract.input_revision_sha256,
              'context_refs':contract.to_dict()['context_refs'], 'summary':'Inspected the assigned criteria.',
              'evidence':[], 'status':'complete'}
    if case == 'needs_review': answer['status'] = 'needs_review'
    if case == 'wrong_role': answer['role'] = 'planning'
    if case == 'wrong_revision': answer['input_revision_sha256'] = 'f' * 64
    if case == 'wrong_refs': answer['context_refs'] = [{'artifact_id':'unassigned','sha256':'d'*64}]
    if case == 'extra_field': answer['verification_pass'] = True
    summary = json.dumps(answer)
    if case == 'malformed': summary = 'The work looks good.'
    if case == 'duplicate_key': summary = summary.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    value[-1].files['events'] = json.dumps(event(summary=summary)).encode()+b'\n'
    return value


def no_decision(value):
    s=value.data['scheduler']; child=value.data['spec'].attempt_id
    with s.store._connect() as db:
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()
        assert not db.execute("SELECT 1 FROM events WHERE type='workflow.task_decision'").fetchone()
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] == child
    assert s.store.get_attempt(child)['state'] == 'running'


def test_valid_saved_contract_then_protected_pass_and_replay(composed):
    p=prepare(composed); execute(composed,p)
    result=complete(composed,p)
    assert result.outcome=='verified' and result.gate=='pass'
    s=composed.data['scheduler']
    with s.store._connect() as db:
        metadata=json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?',(result.artifact_id,)).fetchone()[0])
    from pathlib import Path
    body=json.loads(Path(metadata['storage_path']).read_text())
    assert body['stage_contract_version']==VERSION
    assert len(body['stage_output_sha256'])==64
    before=list(composed.calls)
    assert complete(composed,p)==result and composed.calls==before
    released=release(composed,p,result)
    assert release(composed,p,result)==released


@pytest.mark.parametrize('driven',['malformed','duplicate_key','needs_review','wrong_role',
    'wrong_revision','wrong_refs','extra_field','wrong_launch','missing_launch'],indirect=True)
def test_passing_checks_do_not_bypass_versioned_saved_answer(composed):
    p=prepare(composed); assert execute(composed,p).outcome=='passed'
    with pytest.raises(decision.DecisionError,match='decision_stage_(answer|launch)'):
        complete(composed,p)
    no_decision(composed)


def test_complete_model_answer_cannot_override_failed_protected_checks(composed):
    composed.exit_code=7
    p=prepare(composed); assert execute(composed,p).outcome=='rejected'
    result=complete(composed,p)
    assert result.outcome=='rejected' and result.gate=='reject'


@pytest.mark.parametrize('driven',['malformed'],indirect=True)
def test_forged_in_memory_observation_cannot_replace_saved_answer(composed):
    p=prepare(composed); execute(composed,p)
    saved=composed.results.quiesced.observation
    assignment=composed.data['scheduler'].child_assignment(composed.data['spec'].attempt_id,expected_generation=1)
    contract=stage_contract(assignment['role'],assignment['input_revision_sha256'],assignment['context_refs'])
    answer={'schema_version':1,'contract_sha256':contract.digest,'role':contract.role,
            'input_revision_sha256':contract.input_revision_sha256,'context_refs':[],
            'summary':'Everything is complete.','evidence':[],'status':'complete'}
    terminal=replace(saved.events[-1],payload_json=json.dumps(event(summary=json.dumps(answer))['payload']))
    forged=replace(saved,events=(terminal,))
    composed.results=replace(composed.results,quiesced=replace(composed.results.quiesced,observation=forged))
    with pytest.raises(decision.DecisionError,match='decision_stage_answer_invalid'):
        complete(composed,p)
    no_decision(composed)


def test_unknown_contract_version_refused_by_expected_policy():
    from cloudworkbench.store import encode
    import hashlib
    frozen=encode({'provenance':{'stage_contract_version':'future'}})
    with pytest.raises(decision.DecisionError,match='version_unsupported'):
        decision._expected_contract({'frozen':frozen},{'workflow_digest':hashlib.sha256(frozen.encode()).hexdigest()})
