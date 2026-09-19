import io
import json
from pathlib import Path
import stat

import pytest

from cloudworkbench import pstack_child as child
from cloudworkbench.pstack_routing import requested_policy
from cloudworkbench.workflow_instructions import _reference


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    plugins = tmp_path / 'trusted'
    for name in ('pstack', 'cloud-pstack', 'cloud-evidence'):
        (plugins / name).mkdir(parents=True)
    (plugins / 'cloud-evidence/SOUL.md').write_bytes((Path(__file__).parents[1] / 'integrations/hermes-pr-evidence/SOUL.md').read_bytes())
    for role in requested_policy():
        try:
            reference = _reference(role)
        except KeyError:
            continue
        file = plugins / 'pstack' / reference
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('Role reference: ' + role)
    payload = {'role': 'planning', 'prompt': 'Assess the design.', 'api_key': 'synthetic-token',
               'deadline': 1100, 'profile_root': str(root / 'one'), 'max_turns': 12}
    return payload, {'allowed_root': root, 'trusted_plugins': plugins, 'now': 1000, 'inherited_env': {}}


@pytest.mark.parametrize('role,provider,model,effort,tools', [
    ('planning', 'anthropic', 'claude-fable-5-1', 'max', 'cloud_workspace_read'),
    ('plan_revision', 'anthropic', 'claude-fable-5-1', 'max', 'cloud_workspace_read'),
    ('judgment', 'anthropic', 'claude-fable-5-1', 'max', 'cloud_workspace_read'),
    ('plan_review', 'openai-codex', 'gpt-6-astra', 'high', 'cloud_workspace_read'),
    ('code_review', 'openai-codex', 'gpt-6-astra', 'high', 'cloud_workspace_read'),
    ('feature', 'xai', 'grok-4.6', 'xhigh', 'terminal,file'),
    ('acceptance_verification', 'openai-codex', 'gpt-5.6-sol', 'max', 'terminal,file'),
])
def test_fixed_role_route(layout, role, provider, model, effort, tools):
    payload, options = layout
    payload['role'] = role
    plan = child.build_invocation(payload, **options)
    assert (plan.cli_kwargs['provider'], plan.cli_kwargs['model'], plan.cli_kwargs['reasoning'],
            plan.cli_kwargs['toolsets']) == (provider, model, effort, tools)
    assert plan.cli_kwargs['api_key'] == 'synthetic-token'
    assert plan.cli_kwargs['oneshot'] and not plan.cli_kwargs['ignore_rules']
    assert plan.cli_kwargs['output_format'] == 'stream-json'
    assert plan.cli_kwargs['run_budget'] == 100
    assert plan.cli_kwargs.get('skills') == (None if tools == 'cloud_workspace_read' else 'cloud-evidence:pr-evidence')
    assert not plan.profile_root.exists()


def test_private_profile_and_no_persisted_credentials(layout):
    payload, options = layout
    options['inherited_env'] = {'OPENAI_API_KEY': 'ambient-secret', 'CLAUDE_CODE_OAUTH_TOKEN': 'ambient',
                                'DISPLAY': ':99', 'CRABBOX_DESKTOP': '1'}
    plan = child.prepare(payload, **options)
    config = json.loads((plan.profile_root / 'hermes/config.yaml').read_text())
    assert config['fallback_model'] == []
    assert config['plugins']['enabled'] == ['pstack', 'cloud-pstack', 'cloud-evidence']
    assert config['approvals']['single_query_mode'] == 'deny'
    assert plan.environment['DISPLAY'] == ':99'
    assert 'OPENAI_API_KEY' not in plan.environment and 'CLAUDE_CODE_OAUTH_TOKEN' not in plan.environment
    assert payload['api_key'] not in repr(plan)
    assert payload['api_key'] not in json.dumps(config)
    assert payload['api_key'] not in json.dumps(plan.environment)
    assert (plan.profile_root / 'hermes/plugins/pstack').resolve() == options['trusted_plugins'] / 'pstack'
    assert (plan.profile_root / 'hermes/plugins/cloud-pstack').is_symlink()
    for name in ('HOME', 'HERMES_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
        path = Path(plan.environment[name])
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert path.is_relative_to(plan.profile_root)
    assert stat.S_IMODE((plan.profile_root / 'hermes/config.yaml').stat().st_mode) == 0o600
    assert not list(plan.profile_root.rglob('auth.json'))
    with pytest.raises(child.ChildError, match='child_profile_exists'):
        child.prepare(payload, **options)


@pytest.mark.parametrize('change', [
    {'provider': 'other'}, {'model': 'other'}, {'effort': 'low'}, {'role': 'not-a-role'},
    {'max_turns': True}, {'max_turns': 0}, {'max_turns': 1001},
    {'deadline': 999}, {'deadline': float('inf')}, {'deadline': 10000},
    {'api_key': 'secret\n'}, {'api_key': ''}, {'prompt': ''}, {'prompt': 'x' * 524289},
])
def test_invalid_requests_fail_before_state_creation(layout, change):
    payload, options = layout
    payload.update(change)
    with pytest.raises(child.ChildError):
        child.prepare(payload, **options)
    assert not Path(payload['profile_root']).exists()


def test_profile_escape_and_symlinks_rejected(layout, tmp_path):
    payload, options = layout
    for path in (tmp_path / 'escape', options['allowed_root'], options['allowed_root'] / '../escape'):
        payload['profile_root'] = str(path)
        with pytest.raises(child.ChildError):
            child.prepare(payload, **options)
    link = options['allowed_root'] / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    payload['profile_root'] = str(link / 'role')
    with pytest.raises(child.ChildError, match='unsafe_child_directory'):
        child.prepare(payload, **options)
    assert not (tmp_path / 'role').exists()


def test_read_request_bounded_and_rejects_duplicate_keys():
    with pytest.raises(child.ChildError, match='too_large'):
        child.read_request(io.BytesIO(b' ' * (child.MAX_INPUT_BYTES + 1)))
    with pytest.raises(child.ChildError, match='invalid_child_request'):
        child.read_request(io.BytesIO(b'{"role":"planning","role":"feature"}'))
    assert child.read_request(io.BytesIO(b'{"role":"planning"}')) == {'role': 'planning'}


def test_every_policy_role_builds(layout):
    payload, options = layout
    for role in requested_policy():
        payload['role'] = role
        assert child.build_invocation(payload, **options).policy_profile == requested_policy()[role][0]


def test_large_role_prompt_is_not_truncated(layout):
    payload, options = layout
    payload['prompt'] = 'x' * 524288
    plan = child.build_invocation(payload, **options)
    assert plan.cli_kwargs['query'].endswith(payload['prompt'])
    assert child.MAX_INPUT_BYTES == 600000
