from pathlib import Path
import os
import pytest
from cloudworkbench.workflow_instructions import bind_instructions, load_stage_instructions, STAGE_BOUNDARY
from cloudworkbench.workflow_routing import WORKFLOWS,WorkflowError

PSTACK_SOURCE = os.environ.get('CWB_PSTACK_SOURCE')

@pytest.mark.parametrize('workflow',list(WORKFLOWS))
def test_pinned_pstack_material_exists_for_every_stage(workflow):
    if not PSTACK_SOURCE or not Path(PSTACK_SOURCE).is_dir():
        pytest.skip('Set CWB_PSTACK_SOURCE for optional pinned workflow material integration')
    refs=bind_instructions(workflow,Path(PSTACK_SOURCE))
    assert [r['step_id'] for r in refs]==[s.id for s in WORKFLOWS[workflow].steps]
    assert all(len(r['sha256'])==64 and r['relative_path'].startswith('skills/') for r in refs)

def test_reference_cannot_escape_trusted_root(tmp_path):
    (tmp_path/'skills').mkdir()
    (tmp_path/'skills/architect').mkdir()
    outside=tmp_path.parent/'outside-reference';outside.write_text('outside')
    (tmp_path/'skills/architect/references').mkdir()
    (tmp_path/'skills/architect/references/runner-prompt.md').symlink_to(outside)
    with pytest.raises(WorkflowError,match='outside_trusted_root'):
        bind_instructions('planning',tmp_path)


@pytest.fixture
def bound_stage(tmp_path):
    path=tmp_path/'skills/interrogate/references/reviewer-prompt.md'
    path.parent.mkdir(parents=True)
    path.write_text('Review correctness and edge cases.\n', encoding='utf-8')
    return path, bind_instructions('code_review',tmp_path)


def test_loads_exact_admitted_stage_bytes(tmp_path,bound_stage):
    path,refs=bound_stage
    result=load_stage_instructions('code_review','review_code',refs,tmp_path,frozen_boundary=STAGE_BOUNDARY)
    assert result.text==path.read_text()
    assert result.sha256==refs[0]['sha256']
    assert result.boundary==STAGE_BOUNDARY
    assert result.role=='code_review'


def test_changed_reference_refused_before_stage_execution(tmp_path,bound_stage):
    path,refs=bound_stage
    path.write_text('Modified after admission')
    with pytest.raises(WorkflowError,match='pstack_reference_changed'):
        load_stage_instructions('code_review','review_code',refs,tmp_path,frozen_boundary=STAGE_BOUNDARY)


@pytest.mark.parametrize('field,value',[
    ('relative_path','../../outside'),('role','planning'),('step_id','plan'),
    ('sha256','z'*64),('sha256',None),('extra','unexpected')])
def test_malformed_frozen_binding_refused(tmp_path,bound_stage,field,value):
    _,refs=bound_stage
    refs[0][field]=value
    with pytest.raises(WorkflowError,match='invalid_pstack_binding'):
        load_stage_instructions('code_review','review_code',refs,tmp_path,frozen_boundary=STAGE_BOUNDARY)


def test_stage_must_belong_to_selected_workflow(tmp_path,bound_stage):
    _,refs=bound_stage
    with pytest.raises(WorkflowError,match='unknown_workflow_step'):
        load_stage_instructions('code_review','implement',refs,tmp_path,frozen_boundary=STAGE_BOUNDARY)
    with pytest.raises(WorkflowError,match='invalid_pstack_binding'):
        load_stage_instructions('feature','review_code',refs,tmp_path,frozen_boundary=STAGE_BOUNDARY)


def test_missing_reference_is_controlled_failure(tmp_path,bound_stage):
    path,refs=bound_stage
    path.unlink()
    with pytest.raises(WorkflowError,match='pstack_reference_unreadable'):
        load_stage_instructions('code_review','review_code',refs,tmp_path,frozen_boundary=STAGE_BOUNDARY)


def test_invalid_utf8_is_controlled_failure(tmp_path,bound_stage):
    path,_=bound_stage
    path.write_bytes(b'\xff')
    with pytest.raises(WorkflowError,match='pstack_reference_unreadable'):
        bind_instructions('code_review',tmp_path)


def test_fifo_reference_refused_without_blocking(tmp_path,bound_stage):
    import os
    path,refs=bound_stage
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(WorkflowError,match='pstack_reference_not_regular'):
        load_stage_instructions('code_review','review_code',refs,tmp_path,frozen_boundary=STAGE_BOUNDARY)


def test_oversized_reference_refused(tmp_path,bound_stage):
    path,_=bound_stage
    path.write_bytes(b'x'*65537)
    with pytest.raises(WorkflowError,match='pstack_reference_too_large'):
        bind_instructions('code_review',tmp_path)


def test_stage_boundary_must_match_frozen_policy(tmp_path,bound_stage):
    _,refs=bound_stage
    with pytest.raises(WorkflowError,match='pstack_stage_boundary_changed'):
        load_stage_instructions('code_review','review_code',refs,tmp_path,
                                frozen_boundary='Different admission boundary')
