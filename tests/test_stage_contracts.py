from dataclasses import FrozenInstanceError, replace
import json

import pytest

from cloudworkbench.stage_contracts import (
    ContextRef, MAX_OUTPUT_BYTES, ROLES, StageContractError, VERSION,
    parse_stage_output, render_stage_instructions, stage_contract,
)
from cloudworkbench.workflow_routing import WORKFLOWS

H = 'a' * 64
REFS = [{'artifact_id': 'z-artifact', 'sha256': 'b' * 64},
        {'artifact_id': 'a-artifact', 'sha256': 'c' * 64}]


def contract(role='feature', refs=REFS):
    return stage_contract(role, H, refs)


def answer(c):
    v = {'schema_version': 1, 'contract_sha256': c.digest, 'role': c.role,
         'input_revision_sha256': c.input_revision_sha256,
         'context_refs': c.to_dict()['context_refs'], 'summary': 'Inspected the relevant code.',
         'evidence': [{'location': 'src/example.py:12', 'reason': 'The branch handles empty input.'}]}
    if c.role in ('plan_review', 'code_review'):
        v.update(recommendation='approve', findings=[])
    else:
        v['status'] = 'complete'
        if c.role in ('planning', 'plan_revision'):
            v['plan'] = [{'step': 1, 'action': 'Add a guard for empty input.',
                          'validation': 'Test empty input and ordinary input.'}]
    return v


def parse(v, c):
    return parse_stage_output(json.dumps(v, ensure_ascii=False), c)


@pytest.mark.parametrize('role', sorted(ROLES))
def test_all_frozen_workflow_roles_roundtrip(role):
    c = contract(role)
    output = parse(answer(c), c)
    assert output.to_dict() == answer(c)
    assert output.provenance == 'worker_reported'
    assert output.task_acceptance_verified is False
    assert parse(output.to_dict(), c) == output
    with pytest.raises(FrozenInstanceError):
        output.summary = 'changed'


def test_declared_roles_match_current_workflows():
    assert ROLES == {step.role for workflow in WORKFLOWS.values() for step in workflow.steps}


def test_contract_order_digest_copy_and_version():
    refs = [dict(v) for v in REFS]
    c = contract(refs=refs)
    assert c.context_refs[0].artifact_id == 'z-artifact'
    assert c.digest != contract(refs=list(reversed(refs))).digest
    refs[0]['sha256'] = 'd' * 64
    assert c.digest == contract().digest
    obj = c.to_dict()
    assert obj['version'] == VERSION
    obj['context_refs'].clear()
    assert len(c.context_refs) == 2
    assert json.loads(c.canonical_json) == c.to_dict()


@pytest.mark.parametrize('role', [None, [], True, 1, 'judgment', 'explanation', ''])
def test_invalid_role_stable_error(role):
    with pytest.raises(StageContractError, match='stage_role_invalid'):
        stage_contract(role, H)


@pytest.mark.parametrize('revision', [True, None, [], H.upper(), 'x'*64, H+'a'])
def test_invalid_revision(revision):
    with pytest.raises(StageContractError):
        stage_contract('feature', revision)


@pytest.mark.parametrize('refs', [None, {}, 'x', [REFS[0], REFS[0]],
    [{'id':'old-key', 'sha256':H}], [{'artifact_id':'x', 'sha256':H, 'extra':True}],
    [{'artifact_id':'../x', 'sha256':H}], [{'artifact_id':True, 'sha256':H}],
    [{'artifact_id':'x', 'sha256':False}],
    [{'artifact_id':str(i), 'sha256':H} for i in range(33)]])
def test_invalid_context_refs(refs):
    with pytest.raises(StageContractError):
        stage_contract('feature', H, refs)


@pytest.mark.parametrize('field,value', [('schema_version',True), ('schema_version',1.0),
    ('schema_version',2), ('contract_sha256','d'*64), ('role','bug_fix'),
    ('input_revision_sha256','e'*64), ('context_refs',list(reversed(REFS))),
    ('context_refs',[]), ('context_refs',None), ('context_refs',[REFS[0],REFS[0]]),
    ('summary',None), ('summary',True), ('summary',' '), ('summary','x'*8193),
    ('summary','a\nb'), ('summary','a\tb'), ('summary','a\x00b'),
    ('summary','a\x7fb'), ('summary','a\u202eb'), ('summary','\ud800'),
    ('status','verified'), ('status',True), ('status',[]), ('evidence',{})])
def test_invalid_common_fields(field, value):
    c = contract()
    v = answer(c); v[field] = value
    with pytest.raises(StageContractError):
        parse(v,c)


@pytest.mark.parametrize('raw', ['', '[]', 'null', 'true', '1', '{}', '{}{}',
    '```json\n{}\n```', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}',
    '['*2000+']'*2000, '{"summary":"a","summary":"b"}',
    '{"evidence":[{"location":"a","location":"b"}]}', '\ud800'])
def test_invalid_json_stable_errors(raw):
    with pytest.raises(StageContractError):
        parse_stage_output(raw, contract())


def test_duplicate_valid_binding_rejected_at_any_level():
    c = contract()
    raw = json.dumps(answer(c)).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    with pytest.raises(StageContractError, match='stage_duplicate_key'):
        parse_stage_output(raw,c)
    raw = json.dumps(answer(c)).replace('"location": "src/example.py:12"',
                                        '"location":"x", "location":"src/example.py:12"')
    with pytest.raises(StageContractError, match='stage_duplicate_key'):
        parse_stage_output(raw,c)


@pytest.mark.parametrize('kind', ['missing', 'unknown', 'review_on_report'])
def test_exact_object_shape(kind):
    c = contract(); v = answer(c)
    if kind == 'missing':
        del v['evidence']
    elif kind == 'unknown':
        v['task_acceptance_verified'] = True
    else:
        v['recommendation'] = 'approve'
    with pytest.raises(StageContractError):
        parse(v,c)


@pytest.mark.parametrize('field,value', [('step',True),('step',1.0),('step',0),('step',2),
    ('action',''),('action','x'*2049),('validation',''),('validation',{}),('extra','x')])
def test_plan_strict_fields(field,value):
    c=contract('planning'); v=answer(c); v['plan'][0][field]=value
    with pytest.raises(StageContractError):
        parse(v,c)


def test_plan_requires_concrete_steps_for_complete_but_allows_blocked():
    c=contract('plan_revision'); v=answer(c); v['plan']=[]
    with pytest.raises(StageContractError, match='stage_plan_missing'):
        parse(v,c)
    v['status']='needs_review'
    assert parse(v,c).plan == ()
    v['plan']=[{'step':i,'action':'Do the scoped change.', 'validation':'Run the named test.'} for i in (1,2)]
    assert len(parse(v,c).plan)==2
    v['plan'][1]['step']=1
    with pytest.raises(StageContractError):
        parse(v,c)


@pytest.mark.parametrize('severity', ['critical','high'])
@pytest.mark.parametrize('role', ['plan_review','code_review'])
def test_unresolved_severe_cannot_approve(role,severity):
    c=contract(role); v=answer(c)
    v['findings']=[{'severity':severity,'location':'src/a.py:1','reason':'Input can escape scope.', 'resolved':False}]
    with pytest.raises(StageContractError,match='stage_severe_unresolved'):
        parse(v,c)
    for rec in ('revise','needs_review'):
        v['recommendation']=rec
        assert parse(v,c).recommendation==rec
    v['recommendation']='approve'; v['findings'][0]['resolved']=True
    assert parse(v,c).recommendation=='approve'
    assert parse(v,c).task_acceptance_verified is False


@pytest.mark.parametrize('field,value', [('severity','urgent'),('severity',[]),('location',1),
    ('reason',''),('resolved',1),('resolved','false'),('extra',False)])
def test_findings_strict_fields(field,value):
    c=contract('code_review'); v=answer(c)
    v['findings']=[{'severity':'medium','location':'x','reason':'y','resolved':False}]
    v['findings'][0][field]=value
    with pytest.raises(StageContractError):
        parse(v,c)


@pytest.mark.parametrize('row', [{'location':'x'}, {'location':'x','reason':'y','extra':0},
    {'location':'x'*1025,'reason':'y'}, {'location':'x','reason':'y'*2049},
    {'location':[], 'reason':'y'}])
def test_evidence_exact_shape_and_bounds(row):
    c=contract(); v=answer(c); v['evidence']=[row]
    with pytest.raises(StageContractError):
        parse(v,c)


@pytest.mark.parametrize('role,array', [('feature','evidence'),('planning','plan'),('code_review','findings')])
def test_array_bounds(role,array):
    c=contract(role); v=answer(c)
    item = {'location':'x','reason':'y'} if array=='evidence' else {'step':1,'action':'x','validation':'y'} if array=='plan' else {'severity':'low','location':'x','reason':'y','resolved':False}
    v[array]=[item]*65
    with pytest.raises(StageContractError):
        parse(v,c)


def test_aggregate_and_utf8_byte_bound():
    c=contract(); v=answer(c)
    v['summary']='é'*4096
    assert parse(v,c).summary==v['summary']
    v['summary']+='é'
    with pytest.raises(StageContractError):
        parse(v,c)
    v['summary']='x'
    v['evidence']=[{'location':'x','reason':'y'*2000} for _ in range(10)]
    with pytest.raises(StageContractError,match='stage_output_too_large'):
        parse(v,c)
    raw=json.dumps(answer(c))
    padded=raw+' '*(MAX_OUTPUT_BYTES-len(raw.encode()))
    assert parse_stage_output(padded,c).summary==answer(c)['summary']
    with pytest.raises(StageContractError,match='stage_output_too_large'):
        parse_stage_output(padded+' ',c)


@pytest.mark.parametrize('role',sorted(ROLES))
def test_render_binding_bounded_and_schema_usable(role):
    c=contract(role)
    text=render_stage_instructions(c)
    assert c.digest in text and '16384 bytes' in text
    assert 'unverified model claims' in text
    assert len(text.encode()) < MAX_OUTPUT_BYTES
    template=json.loads(text.split('Exact object shape: ',1)[1])
    assert parse(template,c).role==role


def test_forged_contract_refused():
    c=contract()
    for bad in (None, c.to_dict(), replace(c,role='unknown'), replace(c,context_refs=[]),
                replace(c,context_refs=(ContextRef('x','bad'),))):
        for call in (lambda: parse_stage_output('{}',bad),lambda:render_stage_instructions(bad)):
            with pytest.raises(StageContractError):
                call()
