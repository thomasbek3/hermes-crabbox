"""Qualified environment and output contract together at the decision boundary."""
import hashlib
import pytest

from cloudworkbench.environments import EnvironmentRegistry
from cloudworkbench.workflow_submission import enqueue_qualified_workflow
from cloudworkbench.routed_decision import DecisionError
from tests.test_environments import passed
from tests import test_routed_decision as legacy
from tests.test_qualified_decision_contract import driven, setup, cleanup, result_phase, no_decision
from tests.test_routed_verification import composed, prepare, execute
from tests.test_routed_decision import complete, release


@pytest.fixture
def stage(tmp_path,monkeypatch):
    script=b'print("protected check")\n'
    source=tmp_path.resolve()/'protected.py';source.write_bytes(script);source.chmod(0o600)
    registry=EnvironmentRegistry(tmp_path/'registry.db')
    registry.register({'project_id':'demo','version':'v1','architecture':'amd64',
        'base_image_digest':'sha256:'+'a'*64,'image_digest':'sha256:'+'a'*64,
        'cli_versions':{'python':'3'},'readiness_probes':[{'id':'python','argv':['python3','--version']}],
        'checks':[{'id':'check','description':'Expected behavior','argv':['/usr/bin/python3','/run/task/check.py'],
            'script_id':'protected','script_name':'check.py','script_sha256':hashlib.sha256(script).hexdigest()}]})
    registry.qualify('demo','v1',passed)
    def qualified(scheduler,principal,request,key,**workflow):
        return enqueue_qualified_workflow(scheduler,principal,{**request,'environment_version':'v1'},key,
            registry=registry,allowed_versions=('v1',),script_sources={'protected':source},
            forbidden_values=(),**workflow)
    monkeypatch.setattr(legacy,'enqueue_workflow',qualified)
    return legacy.stage.__wrapped__(tmp_path,monkeypatch)


@pytest.mark.parametrize('exit_code,expected',[(0,'verified'),(7,'rejected')])
def test_qualified_root_contract_and_protected_result_compose(composed,exit_code,expected):
    composed.exit_code=exit_code
    prepared=prepare(composed);execute(composed,prepared)
    result=complete(composed,prepared)
    assert result.outcome==expected
    before=list(composed.calls)
    assert complete(composed,prepared)==result and composed.calls==before
    receipt=release(composed,prepared,result)
    assert release(composed,prepared,result)==receipt


@pytest.mark.parametrize('driven',['malformed'],indirect=True)
def test_qualified_protected_pass_cannot_override_bad_answer(composed):
    prepared=prepare(composed);assert execute(composed,prepared).outcome=='passed'
    with pytest.raises(DecisionError,match='decision_stage_answer_invalid'):
        complete(composed,prepared)
    no_decision(composed)
