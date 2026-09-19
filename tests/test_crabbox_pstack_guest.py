"""Offline opt-in and completion-boundary checks for the Crabbox guest."""
import io
import json
from pathlib import Path
import types

import pytest

from cloudworkbench import agent_pstack, crabbox_guest as guest


def task(pstack=True):
    request = {'prompt': 'Implement the requested change.', 'resume_session_id': None,
               'api_key': 'synthetic-grok-access-token'}
    if pstack:
        request['pstack'] = {'credentials': {'anthropic': 'synthetic-claude-token',
                                            'openai-codex': 'synthetic-codex-token',
                                            'jev': 'synthetic-jev-key'}}
    return request


def read(request):
    return guest.read_request(io.BytesIO(json.dumps(request).encode()))


def test_pstack_is_explicit_opt_in():
    assert 'pstack' not in read(task(False))
    assert read(task())['pstack']['credentials']['jev'] == 'synthetic-jev-key'


@pytest.mark.parametrize('replacement', [None, True, [], {}, {'credentials': {}},
    {'credentials': {'anthropic': 'a', 'openai-codex': 'b'}},
    {'credentials': {'anthropic': 'a', 'openai-codex': 'b', 'jev': 'c', 'refresh_token': 'forbidden'}},
    {'credentials': {'anthropic': 'a', 'openai-codex': 'b', 'jev': 'c'}, 'workflow': 'feature'},
])
def test_rejects_malformed_pstack_opt_in(replacement):
    request = task()
    request['pstack'] = replacement
    with pytest.raises(guest.GuestError):
        read(request)


@pytest.mark.parametrize('value', ['', 'with space', 'line\nvalue', '\x00', 'é', 123, 'x' * 32769])
def test_rejects_invalid_provider_snapshots(value):
    request = task()
    request['pstack']['credentials']['anthropic'] = value
    with pytest.raises(guest.GuestError):
        read(request)


@pytest.mark.parametrize('enabled', [True, False])
def test_valid_resume_identity_supported_for_both_modes(enabled):
    request = task(enabled)
    request['resume_session_id'] = '20260918_123456_abcdef'
    assert read(request)['resume_session_id'] == '20260918_123456_abcdef'


@pytest.mark.parametrize('enabled', [True, False])
def test_invalid_resume_identity_rejected_for_both_modes(enabled):
    request = task(enabled)
    request['resume_session_id'] = '../untrusted-session'
    with pytest.raises(guest.GuestError):
        read(request)


def test_credentials_cannot_expand_request_authority():
    request = task()
    request['model'] = 'unapproved'
    with pytest.raises(guest.GuestError):
        read(request)
    raw = json.dumps(task()).replace('"pstack":', '"api_key":"duplicate-token-value", "pstack":')
    with pytest.raises(guest.GuestError):
        guest.read_request(io.BytesIO(raw.encode()))


@pytest.fixture
def private_guest(tmp_path, monkeypatch):
    state = tmp_path / 'agent-state'
    state.mkdir(mode=0o700)
    monkeypatch.setattr(guest, 'STATE', state)
    monkeypatch.setattr(guest, 'EVIDENCE_PLUGIN', str(Path(__file__).parents[1] / 'integrations/hermes-pr-evidence'))
    monkeypatch.setattr(agent_pstack, 'ROOT', state / 'pstack')
    monkeypatch.setattr(guest.time, 'time', lambda: 1000)
    return state


def flag(command, name):
    return command[command.index(name) + 1]


def test_opt_in_initializes_state_but_runs_hermes_before_routing(private_guest, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('prepare must not perform Jev or role work')
    monkeypatch.setattr(agent_pstack, 'route_task', forbidden)
    monkeypatch.setattr(agent_pstack, 'run_stage', forbidden)
    request = read(task())
    command, env, prompt = guest.prepare(request)
    config = json.loads((private_guest / 'hermes/config.yaml').read_text())
    ledger = json.loads((private_guest / 'pstack/state.json').read_text())
    assert ledger['selection'] is None and ledger['stages'] == []
    assert ledger['used_turns'] == 0 and ledger['deadline'] == 8200
    assert config['plugins']['enabled'] == ['pstack', 'cloud-evidence', 'cloud-pstack']
    assert config['agent']['max_turns'] == 100
    assert config['agent']['reasoning_effort'] == 'xhigh'
    assert flag(command, '--max-turns') == '100'
    assert flag(command, '--skills') == 'cloud-pstack:orchestrate'
    assert flag(command, '--toolsets') == 'cloud_pstack,cloud_workspace_read'
    assert flag(command, '--provider') == 'xai' and flag(command, '--model') == 'grok-4.6'
    assert '--resume' not in command
    assert (private_guest / 'hermes/plugins/cloud-pstack').readlink() == Path('/opt/hermes/trusted-plugins/cloud-pstack')
    assert env['XAI_API_KEY'] == request['api_key']
    for credential in request['pstack']['credentials'].values():
        assert credential not in json.dumps(env)
        assert credential not in json.dumps(config)
        assert credential not in prompt.read_text()
        assert credential not in json.dumps(command)


def test_legacy_launch_keeps_existing_budget_and_tools(private_guest):
    command, env, prompt = guest.prepare(read(task(False)))
    config = json.loads((private_guest / 'hermes/config.yaml').read_text())
    assert config['plugins']['enabled'] == ['pstack', 'cloud-evidence']
    assert config['agent']['max_turns'] == 1000
    assert flag(command, '--skills') == 'pstack:tdd,cloud-evidence:pr-evidence'
    assert flag(command, '--toolsets') == 'terminal,file'
    assert flag(command, '--max-turns') == '1000'
    assert not (private_guest / 'pstack').exists()


def test_desktop_environment_is_preserved(private_guest, monkeypatch):
    monkeypatch.setenv('DISPLAY', ':99')
    monkeypatch.setenv('CRABBOX_DESKTOP', '1')
    monkeypatch.setenv('CRABBOX_DESKTOP_ENV', 'xfce')
    command, env, prompt = guest.prepare(read(task()))
    assert env['DISPLAY'] == ':99' and env['CRABBOX_DESKTOP'] == '1'
    assert 'graphical XFCE desktop' in prompt.read_text()


@pytest.mark.parametrize('enabled,returncode,complete,expected,check_calls', [
    (True, 0, True, 0, 1), (True, 0, False, 1, 1),
    (True, 2, True, 2, 0), (True, -15, True, 143, 0),
    (False, 0, False, 0, 0),
])
def test_guest_exit_requires_completed_pstack(private_guest, monkeypatch, enabled, returncode,
                                              complete, expected, check_calls):
    prompt = private_guest / 'test-prompt.txt'
    prompt.write_text('synthetic prompt')
    request = task(enabled)
    monkeypatch.setattr(guest, 'read_request', lambda stream: request)
    monkeypatch.setattr(guest, 'prepare', lambda request: (['synthetic-hermes'], {}, prompt))
    monkeypatch.setattr(guest.sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(guest.os, 'umask', lambda value: 0o077)
    monkeypatch.setattr(guest.signal, 'signal', lambda *args: None)
    launched = []
    class Process:
        pid = 987654
        def wait(self):
            return returncode
        def poll(self):
            return returncode
    def launch(command, **kwargs):
        launched.append((command, kwargs))
        return Process()
    monkeypatch.setattr(guest.subprocess, 'Popen', launch)
    checks = []
    monkeypatch.setattr(agent_pstack, 'completed', lambda: checks.append(True) or complete)
    assert guest.main() == expected
    assert len(checks) == check_calls
    assert len(launched) == 1 and launched[0][1]['cwd'] == '/workspace'
    assert not prompt.exists()


def test_routed_followup_resumes_parent_with_fresh_attempt_ledger(private_guest):
    request = task()
    request['resume_session_id'] = '20260918_123456_abcdef'
    ledger_root = private_guest / 'pstack'
    ledger_root.mkdir(mode=0o700)
    (ledger_root / 'state.json').write_text(json.dumps({
        'selection': {'workflow': 'feature'}, 'stages': [{'status': 'completed'}],
        'used_turns': 200, 'inflight': 'previous-stage'}))
    command, env, prompt = guest.prepare(read(request))
    assert flag(command, '--resume') == request['resume_session_id']
    assert flag(command, '--skills') == 'cloud-pstack:orchestrate'
    ledger = json.loads((ledger_root / 'state.json').read_text())
    assert ledger['selection'] is None and ledger['stages'] == []
    assert ledger['used_turns'] == 0 and ledger['inflight'] is None
