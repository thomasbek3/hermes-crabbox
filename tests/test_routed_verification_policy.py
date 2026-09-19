from dataclasses import asdict, FrozenInstanceError, replace
import copy
import hashlib
import json

import pytest

from cloudworkbench.routed_verification_policy import (
    build_verification_plan, assess_verification, validate_receipt, require_exact_replay,
    VerificationError, VerificationLimits, CheckExecution,
)
from cloudworkbench.workflow_revisions import RevisionBinding
from cloudworkbench.environments import validate_manifest


@pytest.fixture
def args():
    script=b'print("protected-private-source")\n'
    return dict(binding=RevisionBinding('owner','demo','session','turn','root',1,'child',1),
        input_revision_sha256='1'*64,candidate_revision_sha256='2'*64,
        launch_binding_sha256='3'*64,cleanup_sha256='4'*64,
        environment={'project_id':'demo','version':'v1','architecture':'amd64',
            'base_image_digest':'sha256:'+'5'*64,'image_digest':'sha256:'+'6'*64,
            'cli_versions':{'python':'3'},'readiness_probes':[{'id':'python','argv':['python3','--version']}],
            'checks':[{'id':'check','description':'Expected behavior','argv':['python3','/run/task/check.py'],
                       'script_id':'protected','script_name':'check.py',
                       'script_sha256':hashlib.sha256(script).hexdigest()}]},
        scripts={'protected':script},acceptance=[{'id':'check','description':'Expected behavior','mandatory':True}])


def records():
    return [dict(check_id='check',runtime_id='7'*64,exit_code=0,oom=False,timed_out=False,
                 cleanup_confirmed=True,logs_sha256='8'*64)]


def test_plan_freezes_input_and_binds_canonical_policy_without_script_bytes(args):
    plan=build_verification_plan(**args)
    expected=validate_manifest(args['environment'])[1]
    args['environment']['checks'][0]['argv'][0]='evil'
    args['scripts']['protected']=b'changed'
    args['acceptance'][0]['description']='changed'
    assert plan.environment_sha256==expected
    assert plan.checks[0].argv==('python3','/run/task/check.py')
    assert plan.acceptance[0].description=='Expected behavior'
    assert 'protected-private-source' not in repr(plan)
    assert 'protected-private-source' not in plan.canonical_json
    assert plan.digest==hashlib.sha256(plan.canonical_json.encode()).hexdigest()
    assert json.loads(plan.canonical_json)==plan.to_dict()
    assert 'script_bytes' not in plan.to_dict()['checks'][0]
    with pytest.raises(FrozenInstanceError):plan.image='bad'


@pytest.mark.parametrize('field', ['input_revision_sha256','candidate_revision_sha256','launch_binding_sha256','cleanup_sha256'])
def test_scope_hash_changes_change_plan(args,field):
    one=build_verification_plan(**args)
    assert build_verification_plan(**{**args,field:'9'*64}).digest!=one.digest


@pytest.mark.parametrize('mutation', ['missing_script','missing_name','missing_id','missing_digest','wrong_digest',
    'wrong_argv','extra_script','empty_script','oversize_script','wrong_project','bool_timeout','duplicate_check','too_many_checks'])
def test_invalid_or_unprotected_plan_refused(args,mutation):
    check=args['environment']['checks'][0]
    if mutation=='missing_script':args['scripts']={}
    elif mutation=='missing_name':check.pop('script_name')
    elif mutation=='missing_id':check.pop('script_id')
    elif mutation=='missing_digest':check.pop('script_sha256')
    elif mutation=='wrong_digest':check['script_sha256']='f'*64
    elif mutation=='wrong_argv':check['argv']=['python3','/workspace/check.py']
    elif mutation=='extra_script':args['scripts']['extra']=b'private'
    elif mutation=='empty_script':args['scripts']['protected']=b''
    elif mutation=='oversize_script':args['scripts']['protected']=b'x'*(1024**2+1)
    elif mutation=='wrong_project':args['environment']['project_id']='other'
    elif mutation=='bool_timeout':check['timeout_seconds']=True
    elif mutation=='duplicate_check':args['environment']['checks']*=2
    else:args['environment']['checks']=[{**check,'id':f'check{i}'} for i in range(33)]
    with pytest.raises(VerificationError) as exc:build_verification_plan(**args)
    assert 'private' not in str(exc.value)


def test_total_script_budget_and_name_alias_conflict(args):
    check=args['environment']['checks'][0]
    args['environment']['checks']=[{**check,'id':f'c{i}','script_id':f's{i}'} for i in range(5)]
    args['scripts']={f's{i}':b'x'*(1024**2) for i in range(5)}
    with pytest.raises(VerificationError):build_verification_plan(**args)
    args['scripts']={'s0':b'one','s1':b'two'}
    args['environment']['checks']=[{**check,'id':f'c{i}','script_id':f's{i}',
        'script_sha256':hashlib.sha256(args['scripts'][f's{i}']).hexdigest()} for i in range(2)]
    with pytest.raises(VerificationError,match='name_conflict'):build_verification_plan(**args)


@pytest.mark.parametrize('acceptance', [None,['legacy text'],[{'id':'check','description':'Expected behavior'}],
    [{'id':'check','description':'Expected behavior','mandatory':1}],
    [{'id':'check','description':'Expected behavior','mandatory':True,'pass':True}],
    [{'id':'check','description':'Expected behavior','mandatory':True}]*2])
def test_acceptance_exact_shapes(args,acceptance):
    with pytest.raises(VerificationError):build_verification_plan(**{**args,'acceptance':acceptance})


def test_pass_and_exact_replay_require_only_controller_records(args):
    plan=build_verification_plan(**args)
    receipt=assess_verification(plan,records())
    assert receipt.outcome=='passed' and receipt.remaining_criteria==()
    assert receipt.plan_sha256==plan.digest
    assert validate_receipt(plan,receipt)==receipt
    assert require_exact_replay(plan,receipt,assess_verification(plan,records()))==receipt
    changed=replace(receipt,records=(replace(receipt.records[0],logs_sha256='9'*64),))
    with pytest.raises(VerificationError,match='conflict'):require_exact_replay(plan,receipt,changed)
    with pytest.raises(VerificationError,match='conflict'):validate_receipt(plan,replace(receipt,plan_sha256='9'*64))


@pytest.mark.parametrize('change', [{'exit_code':1},{'timed_out':True}])
def test_failed_check_is_rejected(args,change):
    value=records();value[0].update(change)
    assert assess_verification(build_verification_plan(**args),value).outcome=='rejected'


@pytest.mark.parametrize('change', [{'oom':True},{'cleanup_confirmed':False},{'exit_code':None},
    {'exit_code':True},{'exit_code':-1},{'exit_code':256},{'oom':0},{'timed_out':'false'},
    {'runtime_id':'short'},{'logs_sha256':'no'},{'provenance':'model'}, {'check_id':'other'}])
def test_invalid_or_infrastructure_records_never_pass(args,change):
    value=records();value[0].update(change)
    with pytest.raises(VerificationError):assess_verification(build_verification_plan(**args),value)


@pytest.mark.parametrize('criteria', [
    [{'id':'check','description':'Different behavior','mandatory':True}],
    [{'id':'unknown','description':'Expected behavior','mandatory':True}],
])
def test_mandatory_coverage_requires_id_and_description(args,criteria):
    plan=build_verification_plan(**{**args,'acceptance':criteria})
    result=assess_verification(plan,records())
    assert result.outcome=='needs_review' and result.remaining_criteria==(criteria[0]['id'],)


def test_empty_checks_and_optional_uncovered_criteria(args):
    args['environment']['checks']=[];args['scripts']={}
    result=assess_verification(build_verification_plan(**args),[])
    assert result.outcome=='needs_review'
    assert result.remaining_criteria==('check',)


def test_optional_criterion_does_not_block_protected_pass(args):
    args['acceptance']=[{'id':'optional','description':'uncovered','mandatory':False}]
    assert assess_verification(build_verification_plan(**args),records()).outcome=='passed'


def test_exact_order_complete_unique_check_and_runtime_set(args):
    check=args['environment']['checks'][0]
    args['environment']['checks'].append({**check,'id':'second'})
    plan=build_verification_plan(**args)
    first=records()[0];second={**first,'check_id':'second','runtime_id':'9'*64}
    assert assess_verification(plan,[first,second]).outcome=='passed'
    for wrong in ([first],[second,first],[first,first],[first,{**second,'runtime_id':first['runtime_id']}],[]):
        with pytest.raises(VerificationError):assess_verification(plan,wrong)


@pytest.mark.parametrize('limits', [{'max_seconds':True},{'max_seconds':0},{'max_seconds':601},
    {'cpus':True},{'cpus':3},{'memory_mib':127},{'memory_mib':4097},{'pids':15},{'pids':513},
    {'max_log_bytes':True},{'max_log_bytes':-1},{'max_log_bytes':1024**2+1}])
def test_limit_bounds(limits):
    with pytest.raises(VerificationError):VerificationLimits(**limits)


def test_model_prose_and_forged_outcome_are_not_receipts(args):
    plan=build_verification_plan(**args)
    failed=records();failed[0]['exit_code']=1
    receipt=assess_verification(plan,failed)
    with pytest.raises(VerificationError,match='conflict'):
        validate_receipt(plan,replace(receipt,outcome='passed',remaining_criteria=()))
    for value in ('PASS', [{'decision':'pass'}], {'outcome':'passed'}):
        with pytest.raises(VerificationError):assess_verification(plan,value)


def test_plan_and_record_errors_do_not_echo_operator_material(args):
    args['environment']['checks'][0]['argv']=['sensitive-operator-value\x00']
    with pytest.raises(VerificationError) as exc:build_verification_plan(**args)
    assert str(exc.value)=='verification_environment_invalid'


def test_digest_and_binding_validation_reject_substitutions(args):
    for field in ('input_revision_sha256','candidate_revision_sha256','launch_binding_sha256','cleanup_sha256'):
        with pytest.raises(VerificationError):build_verification_plan(**{**args,field:True})
    with pytest.raises(VerificationError):build_verification_plan(**{**args,'binding':asdict(args['binding'])})
