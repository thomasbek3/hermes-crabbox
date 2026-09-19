"""Controller-only adapter from a bound Jev selection to durable root admission."""
from .models import SessionRequest
from .workflow_routing import WorkflowError, resolve_workflow
from .workflow_instructions import bind_instructions, STAGE_BOUNDARY


def enqueue_workflow(scheduler, principal, request, key, *, selection,
                     allowed_workflows, router, parent, accounts, trusted_pstack_root,
                     _environment_snapshot=None, _stage_contract_version=None,
                     _stage_progression_version=None):
    request = SessionRequest.model_validate(request).model_dump()
    if request['agent'] != 'hermes':
        raise WorkflowError('workflow_requires_hermes')
    plan = resolve_workflow(selection, request['goal'], allowed_workflows=allowed_workflows,
                            router=router, parent=parent)
    if not plan['ready']:
        raise WorkflowError('workflow_backend_not_ready')
    role_plans = {}
    for step in plan['steps']:
        entry = [{'ready': step['ready'], 'profile': step['profile']}]
        if step['role'] in role_plans and role_plans[step['role']] != entry:
            raise WorkflowError('inconsistent_repeated_role')
        role_plans[step['role']] = entry
    instructions = bind_instructions(selection.workflow, trusted_pstack_root)
    frozen = {'accounts': dict(accounts), 'role_plans': role_plans, 'provenance': {
        'workflow': plan, 'pstack_references': instructions, 'stage_boundary': STAGE_BOUNDARY}}
    if _environment_snapshot is not None:
        frozen['provenance']['environment'] = _environment_snapshot
    if _stage_contract_version is not None:
        frozen['provenance']['stage_contract_version'] = _stage_contract_version
    if _stage_progression_version is not None:
        frozen['provenance']['stage_progression_version'] = _stage_progression_version
    return scheduler.enqueue_root(principal, request, key, frozen=frozen)


def enqueue_qualified_workflow(scheduler, principal, request, key, *, registry,
                               allowed_versions, script_sources, forbidden_values, **workflow):
    """Production admission seam: freeze qualified environment and output policy."""
    from .routed_environment import resolve_routed_environment
    from .stage_contracts import VERSION
    from .routed_progression import VERSION as PROGRESSION_VERSION
    if any(name.startswith('_') for name in workflow):
        raise WorkflowError('qualified_workflow_policy_override')
    parsed = SessionRequest.model_validate(request).model_dump()
    resolved = resolve_routed_environment(registry, project_id=parsed['project_id'],
        version=parsed['environment_version'], allowed_versions=allowed_versions,
        script_sources=script_sources, forbidden_values=forbidden_values)
    manifest = resolved.manifest
    if manifest['network_profile'] != 'none' or manifest['secret_refs'] or manifest['startup_commands']:
        raise WorkflowError('qualified_workflow_runtime_profile_unsupported')
    return enqueue_workflow(scheduler, principal, parsed, key, **workflow,
        _environment_snapshot=resolved.snapshot, _stage_contract_version=VERSION,
        _stage_progression_version=PROGRESSION_VERSION)
