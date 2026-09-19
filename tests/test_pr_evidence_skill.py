"""Evidence skill packaging and profile wiring, without browser/provider calls."""
import importlib.util
import json
from pathlib import Path
import re

import pytest

from cloudworkbench import crabbox_guest, pstack_child

PLUGIN_ROOT = Path(__file__).parents[1] / 'integrations' / 'hermes-pr-evidence'


def test_plugin_registers_only_shared_skills():
    spec = importlib.util.spec_from_file_location('evidence_plugin_test', PLUGIN_ROOT / '__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    class SkillsOnly:
        def __init__(self):
            self.skills = {}
        def register_skill(self, name, path):
            self.skills[name] = path
    context = SkillsOnly()
    plugin.register(context)
    assert context.skills == {name: PLUGIN_ROOT / 'skills' / name / 'SKILL.md'
                              for name in ('pr-evidence', 'agent-browser')}
    assert context.skills['pr-evidence'].is_file()
    manifest = (PLUGIN_ROOT / 'plugin.yaml').read_text()
    assert re.search(r'^name: cloud-evidence$', manifest, re.MULTILINE)


def test_manifest_example_matches_parent_handoff_contract():
    skill = (PLUGIN_ROOT / 'skills/pr-evidence/SKILL.md').read_text()
    example, = re.findall(r'```json\n(.*?)\n```', skill, re.DOTALL)
    value = json.loads(example)
    assert set(value) == {'schema_version', 'assets', 'summary'}
    assert value['schema_version'] == 1
    assert type(value['summary']) is str
    for asset in value['assets']:
        assert set(asset) == {'path', 'label'}
        path = Path(asset['path'])
        assert path.parts[0] == 'pr-evidence' and '..' not in path.parts and not path.is_absolute()
    markdown, = re.findall(r'```markdown\n(.*?)\n```', skill, re.DOTALL)
    paths = re.findall(r'\]\(([^)]+)\)', markdown)
    assert set(paths) == {asset['path'] for asset in value['assets']}
    assert '\n\n![](pr-evidence/interaction.mp4)\n' in markdown + '\n'


@pytest.mark.parametrize('routed', [False, True])
def test_all_parent_profiles_install_evidence_without_expanding_tools(tmp_path, monkeypatch, routed):
    monkeypatch.setattr(crabbox_guest, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(crabbox_guest, 'EVIDENCE_PLUGIN', str(PLUGIN_ROOT))
    if routed:
        from cloudworkbench import agent_pstack
        monkeypatch.setattr(agent_pstack, 'initialize', lambda deadline: None)
    task = {'prompt': 'A coding assignment', 'resume_session_id': None, 'api_key': 'synthetic-grok-token'}
    if routed:
        task['pstack'] = {'credentials': {'anthropic': 'a', 'openai-codex': 'b', 'jev': 'c'}}
    argv, env, prompt = crabbox_guest.prepare(task)
    assert '--ignore-rules' not in argv
    assert (Path(env['HERMES_HOME']) / 'SOUL.md').read_bytes() == (PLUGIN_ROOT / 'SOUL.md').read_bytes()
    config = json.loads((crabbox_guest.STATE / 'hermes/config.yaml').read_text())
    assert 'cloud-evidence' in config['plugins']['enabled']
    assert (crabbox_guest.STATE / 'hermes/plugins/cloud-evidence').readlink() == Path(crabbox_guest.EVIDENCE_PLUGIN)
    assert env['AGENT_BROWSER_EXECUTABLE_PATH'] == '/opt/playwright/chromium-1243/chrome-linux64/chrome'
    skills = argv[argv.index('--skills') + 1].split(',')
    assert ('cloud-evidence:pr-evidence' in skills) is not routed
    assert argv[argv.index('--toolsets') + 1] == ('cloud_pstack,cloud_workspace_read' if routed else 'terminal,file')


@pytest.mark.parametrize('role,readonly', [('judgment', True), ('code_review', True),
                                         ('swarm_worker', False), ('acceptance_verification', False)])
def test_child_capture_guidance_does_not_give_reviewers_write_tools(tmp_path, role, readonly):
    root = tmp_path.resolve() / 'state'
    root.mkdir(mode=0o700)
    trusted = tmp_path.resolve() / 'trusted'
    for name in ('pstack', 'cloud-pstack', 'cloud-evidence'):
        (trusted / name).mkdir(parents=True)
    (trusted / 'cloud-evidence/SOUL.md').write_bytes((PLUGIN_ROOT / 'SOUL.md').read_bytes())
    from cloudworkbench.workflow_instructions import _reference
    try:
        reference = trusted / 'pstack' / _reference(role)
    except KeyError:
        pass
    else:
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text('Assigned role guidance.')
    payload = {'role': role, 'prompt': 'Inspect or implement this assignment.', 'api_key': 'synthetic-token',
               'deadline': 1100, 'profile_root': str(root / 'role'), 'max_turns': 10}
    plan = pstack_child.prepare(payload, allowed_root=root, trusted_plugins=trusted, now=1000)
    assert not plan.cli_kwargs['ignore_rules']
    assert (Path(plan.environment['HERMES_HOME']) / 'SOUL.md').read_bytes() == (PLUGIN_ROOT / 'SOUL.md').read_bytes()
    assert 'cloud-evidence' in plan.config['plugins']['enabled']
    assert (plan.profile_root / 'hermes/plugins/cloud-evidence').resolve() == trusted / 'cloud-evidence'
    assert plan.environment['AGENT_BROWSER_EXECUTABLE_PATH'].endswith('/chrome')
    assert plan.cli_kwargs['toolsets'] == ('cloud_workspace_read' if readonly else 'terminal,file')
    assert plan.cli_kwargs.get('skills') == (None if readonly else 'cloud-evidence:pr-evidence')
    assert '/workspace/pr-evidence/' in plan.cli_kwargs['query']


def test_soul_installation_requires_content_and_preserves_existing_identity(tmp_path):
    profile = tmp_path / 'profile'
    profile.mkdir()
    source = tmp_path / 'SOUL.md'
    with pytest.raises(FileNotFoundError):
        crabbox_guest.install_soul(profile, source)
    source.write_text('   ')
    with pytest.raises(crabbox_guest.GuestError):
        crabbox_guest.install_soul(profile, source)
    source.write_bytes((PLUGIN_ROOT / 'SOUL.md').read_bytes())
    crabbox_guest.install_soul(profile, source)
    crabbox_guest.install_soul(profile, source)
    target = profile / 'SOUL.md'
    target.write_text('Existing custom identity')
    with pytest.raises(crabbox_guest.GuestError, match='differs'):
        crabbox_guest.install_soul(profile, source)
    assert target.read_text() == 'Existing custom identity'
