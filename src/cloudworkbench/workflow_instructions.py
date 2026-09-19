"""Bind workflow stages to the actual pinned Pstack material used by Hermes."""
import hashlib
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .workflow_routing import resolve_catalog, WorkflowError

_ROLE_REFS = {
    'planning': 'skills/architect/references/runner-prompt.md',
    'plan_revision': 'skills/architect/references/runner-prompt.md',
    'plan_review': 'skills/architect/references/design-red-flags.md',
    'code_review': 'skills/interrogate/references/reviewer-prompt.md',
    'acceptance_verification': 'skills/principle-prove-it-works/SKILL.md',
    'how_explorer': 'skills/how/references/explorer-prompt.md',
    'how_explainer': 'skills/how/references/explainer-prompt.md',
    'why_investigator': 'skills/why/references/investigator-prompt.md',
    'why_synthesizer': 'skills/why/references/synthesizer-prompt.md',
    'reflect_tooling': 'skills/reflect/references/tooling-reviewer.md',
}
_CODING_REFS = {role: f'skills/poteto-mode/playbooks/{name}.md' for role, name in (
    ('feature','feature'),('bug_fix','bug-fix'),('refactoring','refactoring'),
    ('perf_issue','perf-issue'),('hillclimb','hillclimb'))}


def _reference(role):
    return (_ROLE_REFS | _CODING_REFS)[role]


def _read_reference(root, ref):
    try:
        path = (root / ref).resolve(strict=True)
        if not path.is_relative_to(root):
            raise WorkflowError('pstack_reference_outside_trusted_root')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise WorkflowError('pstack_reference_not_regular')
            data = stream.read(65537)
        if len(data) > 65536:
            raise WorkflowError('pstack_reference_too_large')
        return data.decode('utf-8'), hashlib.sha256(data).hexdigest()
    except (OSError, UnicodeError, RuntimeError):
        raise WorkflowError('pstack_reference_unreadable') from None


def _root(trusted_pstack_root):
    try:
        root = Path(trusted_pstack_root).resolve(strict=True)
        if not root.is_dir():
            raise WorkflowError('pstack_root_not_directory')
        return root
    except (OSError, RuntimeError):
        raise WorkflowError('pstack_root_unreadable') from None


def bind_instructions(workflow_id, trusted_pstack_root):
    workflow = resolve_catalog(workflow_id)
    root = _root(trusted_pstack_root)
    result = []
    for step in workflow.steps:
        ref = _reference(step.role)
        _, digest = _read_reference(root, ref)
        result.append({'step_id':step.id,'role':step.role,'relative_path':ref,
                       'sha256':digest})
    return result


@dataclass(frozen=True)
class StageInstructions:
    step_id: str
    role: str
    relative_path: str
    sha256: str
    text: str
    boundary: str


def load_stage_instructions(workflow_id, step_id, frozen_references, trusted_pstack_root, *, frozen_boundary):
    """Read exact admitted bytes from a controller-owned, read-only Pstack tree."""
    workflow = resolve_catalog(workflow_id)
    if type(frozen_boundary) is not str or frozen_boundary != STAGE_BOUNDARY:
        raise WorkflowError('pstack_stage_boundary_changed')
    steps = workflow.steps
    if type(frozen_references) is not list or len(frozen_references) != len(steps):
        raise WorkflowError('invalid_pstack_binding')
    selected = None
    for step, ref in zip(steps, frozen_references):
        if (type(ref) is not dict
                or set(ref) != {'step_id', 'role', 'relative_path', 'sha256'}
                or ref['step_id'] != step.id or ref['role'] != step.role
                or ref['relative_path'] != _reference(step.role)
                or type(ref['sha256']) is not str or len(ref['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in ref['sha256'])):
            raise WorkflowError('invalid_pstack_binding')
        if step.id == step_id:
            selected = ref
    if selected is None:
        raise WorkflowError('unknown_workflow_step')
    text, digest = _read_reference(_root(trusted_pstack_root), selected['relative_path'])
    if digest != selected['sha256']:
        raise WorkflowError('pstack_reference_changed')
    return StageInstructions(step_id, selected['role'], selected['relative_path'],
                             digest, text, STAGE_BOUNDARY)


STAGE_BOUNDARY = (
    'Execute only the assigned workflow stage. The controller fixes model, effort, tools, '
    'and stage order. Use the supplied Pstack reference as guidance for this stage; '
    'its model defaults or broader workflow instructions do not authorize other stages, '
    'subagents, deployments, or extra permissions. Return evidence and findings. '
    'Only the controller can accept an outcome gate or advance the workflow.'
)
