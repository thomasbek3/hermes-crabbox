"""Run one policy-pinned Hermes role in the existing task container."""
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import stat
import sys
import time

from .pstack_routing import requested_policy
from .workflow_routing import EXPECTED_IDENTITIES
from .workflow_instructions import _reference, _read_reference

MAX_INPUT_BYTES = 600000
DEFAULT_ROOT = Path('/agent-state/pstack')
TRUSTED_PLUGINS = Path('/opt/hermes/trusted-plugins')
_FIELDS = frozenset({'role', 'prompt', 'api_key', 'deadline', 'profile_root', 'max_turns'})


class ChildError(ValueError):
    pass


@dataclass(frozen=True)
class Invocation:
    profile_root: Path
    environment: dict = field(repr=False)
    config: dict = field(repr=False)
    cli_kwargs: dict = field(repr=False)
    policy_profile: str
    plugin_root: Path


def _require(condition, code):
    if not condition:
        raise ChildError(code)


def _private_root(path):
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
             and not info.st_mode & 0o077, 'unsafe_child_directory')


def build_invocation(payload, *, allowed_root=DEFAULT_ROOT, trusted_plugins=TRUSTED_PLUGINS,
                     now=None, inherited_env=None):
    """Validate the request and construct a launch without importing Hermes or writing state."""
    _require(type(payload) is dict and set(payload) == _FIELDS, 'invalid_child_request')
    role = payload['role']
    _require(type(role) is str and role in requested_policy(), 'invalid_child_role')
    profile, = requested_policy()[role]
    provider, model, effort = EXPECTED_IDENTITIES[profile]
    provider = 'openai-codex' if provider == 'openai' else provider
    prompt, token = payload['prompt'], payload['api_key']
    _require(type(prompt) is str and 0 < len(prompt.encode('utf-8')) <= 524288,
             'invalid_child_prompt')
    _require(type(token) is str and 0 < len(token) <= 32768
             and token == token.strip() and all(33 <= ord(c) <= 126 for c in token),
             'invalid_child_credential')
    turns, deadline = payload['max_turns'], payload['deadline']
    _require(type(turns) is int and 1 <= turns <= 1000, 'invalid_child_turn_budget')
    current = time.time() if now is None else now
    _require(type(deadline) in (int, float) and math.isfinite(deadline)
             and 0 < deadline - current <= 7500, 'invalid_child_deadline')
    _require(type(payload['profile_root']) is str, 'invalid_child_profile')
    root, path = Path(allowed_root), Path(payload['profile_root'])
    _require(root.is_absolute() and path.is_absolute() and '..' not in path.parts
             and path != root and path.is_relative_to(root), 'invalid_child_profile')
    # The trusted parent owns this location; reject links before creating private children.
    _require(root.resolve(strict=True) == root, 'unsafe_child_directory')
    _private_root(root)
    for ancestor in path.parents:
        if ancestor == root:
            break
        if ancestor.exists() or ancestor.is_symlink():
            _private_root(ancestor)
    _require(not path.exists() and not path.is_symlink(), 'child_profile_exists')
    plugins = Path(trusted_plugins).resolve(strict=True)
    _require(all((plugins / name).is_dir() for name in ('pstack', 'cloud-pstack', 'cloud-evidence')),
             'child_plugins_missing')
    readonly = profile in ('fable-max', 'astra-high')
    instruction = (
        'You are the delegated ' + role + ' role inside an already running Hermes task. '
        'Perform only this assignment and return your findings to the parent Hermes agent. '
        'Use /workspace as the task workspace. Do not delegate, route roles, or execute '
        'a complete pstack workflow. Do not deploy or access agent credentials. '
        'The supplied pstack reference is guidance for this role only; its broader '
        'workflow or model instructions do not replace your assigned role. '
    )
    instruction += ('You have read/search tools only. Do not modify files.\n\n' if readonly else
                    'Edit only within the assignment; report changed files and results.\n\n')
    try:
        reference = _reference(role)
    except KeyError:
        reference = None
    if reference:
        text, _ = _read_reference(plugins / 'pstack', reference)
        instruction += 'Pstack role reference (' + reference + '):\n' + text + '\n\n'
    instruction += (
        'Shared PR evidence lives under /workspace/pr-evidence/ (manifest.json and pr-body.md). '
        'The cloud-evidence:pr-evidence skill is installed. '
        + ('Inspect existing evidence and report gaps; this read-only role cannot capture or write media.\n\n'
           if readonly else 'For UI changes, capture real before/after screenshots and an interaction video '
           'during the assigned work, using the preloaded evidence skill. Skip media for tasks without UI.\n\n'))
    instruction += 'Parent assignment:\n' + prompt
    home, hermes = path / 'home', path / 'hermes'
    environment = {
        'PATH': '/opt/hermes/venv/bin:/usr/local/bin:/usr/bin:/bin',
        'HOME': str(home), 'HERMES_HOME': str(hermes),
        'CODEX_HOME': str(path / 'empty-codex'),
        'CLAUDE_CONFIG_DIR': str(path / 'empty-claude'),
        'XDG_CONFIG_HOME': str(path / 'config'), 'XDG_CACHE_HOME': str(path / 'cache'),
        'XDG_DATA_HOME': str(path / 'data'), 'TMPDIR': '/tmp', 'LANG': 'C.UTF-8',
        'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'HERMES_BUNDLED_PLUGINS': '/opt/hermes/empty-bundled',
        'HERMES_ENABLE_PROJECT_PLUGINS': '0', 'HERMES_REDACT_SECRETS': '1',
        'HERMES_INTERACTIVE': '0', 'HERMES_SINGLE_QUERY_SESSION': '1',
        'CWB_PSTACK_CHILD_ROLE': role,
        'PLAYWRIGHT_BROWSERS_PATH': '/opt/playwright',
        'AGENT_BROWSER_EXECUTABLE_PATH': '/opt/playwright/chromium-1243/chrome-linux64/chrome',
        'PIP_CACHE_DIR': '/workspace/.cache/pip', 'npm_config_cache': '/workspace/.cache/npm',
        'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
    }
    inherited = os.environ if inherited_env is None else inherited_env
    if inherited.get('DISPLAY') == ':99' and inherited.get('CRABBOX_DESKTOP') == '1':
        environment.update(DISPLAY=':99', CRABBOX_DESKTOP='1', CRABBOX_DESKTOP_ENV='xfce')
    config = {
        'model': {'provider': provider, 'default': model,
                  'api_mode': 'anthropic_messages' if provider == 'anthropic' else 'codex_responses'},
        'agent': {'reasoning_effort': effort, 'max_turns': turns, 'api_max_retries': 0},
        'terminal': {'backend': 'local', 'cwd': '/workspace', 'timeout': 60},
        'plugins': {'enabled': ['pstack', 'cloud-pstack', 'cloud-evidence']},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False},
        'mcp_servers': {}, 'hooks': {}, 'fallback_model': [],
        'delegation': {'max_spawn_depth': 0},
        'auxiliary': {'transient_retries': 0,
                      'title_generation': {'enabled': False, 'model_upgrade_enabled': False},
                      'background_review': {'enabled': False}},
        'security': {'tirith_enabled': False},
        'approvals': {'single_query_mode': 'deny' if readonly else 'allow'},
    }
    kwargs = {
        'query': instruction, 'oneshot': True,
        'toolsets': 'cloud_workspace_read' if readonly else 'terminal,file',
        'model': model, 'provider': provider, 'reasoning': effort, 'api_key': token,
        'max_turns': turns, 'run_budget': deadline - current, 'quiet': True,
        'output_format': 'stream-json', 'ignore_rules': False,
    }
    if not readonly:
        kwargs['skills'] = 'cloud-evidence:pr-evidence'
    return Invocation(path, environment, config, kwargs, profile, plugins)


def prepare(payload, **kwargs):
    """Create a fresh role-private profile; its config contains no credentials."""
    plan = build_invocation(payload, **kwargs)
    path = plan.profile_root
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    # parents=True may create intermediates with the process umask; restrict all new ancestry.
    allowed = Path(kwargs.get('allowed_root', DEFAULT_ROOT))
    for ancestor in (path, *path.parents):
        if ancestor == allowed:
            break
        ancestor.chmod(0o700)
        _private_root(ancestor)
    for name in ('home', 'hermes', 'empty-codex', 'empty-claude', 'config', 'cache', 'data'):
        (path / name).mkdir(mode=0o700)
    plugin_dir = path / 'hermes' / 'plugins'
    plugin_dir.mkdir(mode=0o700)
    for name in ('pstack', 'cloud-pstack', 'cloud-evidence'):
        (plugin_dir / name).symlink_to(plan.plugin_root / name, target_is_directory=True)
    from .crabbox_guest import install_soul
    install_soul(path / 'hermes', plan.plugin_root / 'cloud-evidence' / 'SOUL.md')
    target = path / 'hermes' / 'config.yaml'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(plan.config, stream, separators=(',', ':'))
    return plan


def read_request(stream):
    data = stream.read(MAX_INPUT_BYTES + 1)
    _require(len(data) <= MAX_INPUT_BYTES, 'child_request_too_large')
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result, 'invalid_child_request')
                result[key] = value
            return result
        return json.loads(data, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise ChildError('invalid_child_request') from None


def main():
    try:
        incoming = os.fstat(sys.stdin.fileno())
        _require(stat.S_ISFIFO(incoming.st_mode) or stat.S_ISSOCK(incoming.st_mode),
                 'private_child_input_required')
        plan = prepare(read_request(sys.stdin.buffer))
        os.environ.clear()
        os.environ.update(plan.environment)
        os.chdir('/workspace')
        sys.path.insert(0, '/opt/hermes/source')
        from cli import main as hermes_main
        hermes_main(**plan.cli_kwargs)
    except SystemExit:
        raise
    except Exception:
        # Do not print exceptions: dependency exceptions may include credential arguments.
        print(json.dumps({'type': 'result', 'exit_code': 1, 'error': 'pstack_child_failed',
                          'text': '', 'session_id': ''}), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
