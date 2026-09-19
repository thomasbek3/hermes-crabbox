"""Run a native Hermes job inside its owned Crabbox environment."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid


HERMES = '/opt/hermes/venv/bin/hermes'
PLUGIN = '/opt/hermes/trusted-plugins/pstack'
EVIDENCE_PLUGIN = '/opt/hermes/trusted-plugins/cloud-evidence'
STATE = Path('/agent-state')
MAX_INPUT_BYTES = 524288
MAX_MODEL_TURNS = 1000
RUN_BUDGET_SECONDS = 7200
SESSION_PATTERN = r'[0-9]{8}_[0-9]{6}_[0-9a-f]{6}'


class GuestError(ValueError):
    pass


def require(condition):
    if not condition:
        raise GuestError()


def pairs(items):
    value = {}
    for key, item in items:
        require(key not in value)
        value[key] = item
    return value


def invalid_constant(_value):
    raise GuestError()


def read_request(stream):
    data = stream.read(MAX_INPUT_BYTES + 1)
    require(0 < len(data) <= MAX_INPUT_BYTES)
    task = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
    required = {'prompt', 'resume_session_id', 'api_key'}
    require(type(task) is dict and required <= set(task) <= required | {'inputs', 'pstack'})
    prompt = task['prompt']
    require(type(prompt) is str and 0 < len(prompt.encode()) <= 262144 and '\x00' not in prompt)
    resume = task['resume_session_id']
    require(resume is None or type(resume) is str and re.fullmatch(SESSION_PATTERN, resume))
    key = task['api_key']
    require(type(key) is str and 16 <= len(key.encode()) <= 16384 and
            not any(char in key for char in '\x00\r\n'))
    if 'pstack' in task:
        pstack = task['pstack']
        require(type(pstack) is dict and set(pstack) == {'credentials'})
        credentials = pstack['credentials']
        require(type(credentials) is dict and set(credentials) == {'anthropic', 'openai-codex', 'jev'})
        require(all(type(value) is str and 1 <= len(value) <= 32768 and
                    all(33 <= ord(c) <= 126 for c in value) for value in credentials.values()))
    inputs = task.get('inputs', [])
    require(type(inputs) is list and len(inputs) <= 32)
    require(all(type(item) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item)
                for item in inputs))
    require(len(inputs) == len(set(inputs)))
    return task


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
                not info.st_mode & 0o077)
    finally:
        os.close(descriptor)


def write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def install_soul(profile, source):
    """Install the shared identity before Hermes builds its system prompt."""
    data = Path(source).read_bytes()
    if not data.strip() or len(data) > 16000:
        raise GuestError('missing_or_invalid_cloud_soul')
    target = profile / 'SOUL.md'
    if target.is_symlink():
        raise GuestError('unexpected_cloud_soul_symlink')
    if target.exists():
        if target.read_bytes() != data:
            raise GuestError('existing_cloud_soul_differs')
    else:
        write_private(target, data)


def prepare(task):
    desktop = (os.environ.get('CRABBOX_DESKTOP') == '1' and
               os.environ.get('DISPLAY') == ':99' and
               os.environ.get('CRABBOX_DESKTOP_ENV', 'xfce') == 'xfce')
    STATE.mkdir(mode=0o700, exist_ok=True)
    info = STATE.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.geteuid()) and
            not info.st_mode & 0o022)
    profile = STATE / 'hermes'
    for path in (profile, STATE / 'home', STATE / 'empty-codex', STATE / 'empty-claude',
                 STATE / 'cache', STATE / 'data', profile / 'plugins'):
        private_directory(path)
    for name, target in (('pstack', PLUGIN), ('cloud-evidence', EVIDENCE_PLUGIN)):
        plugin = profile / 'plugins' / name
        if plugin.is_symlink():
            require(os.readlink(plugin) == target)
        else:
            require(not plugin.exists())
            plugin.symlink_to(target)
    install_soul(profile, Path(EVIDENCE_PLUGIN) / 'SOUL.md')
    config = {
        'model': {'provider': 'xai', 'default': 'grok-4.6', 'api_mode': 'codex_responses'},
        'agent': {'reasoning_effort': 'xhigh', 'max_turns': MAX_MODEL_TURNS, 'api_max_retries': 0},
        'plugins': {'enabled': ['pstack', 'cloud-evidence']}, 'security': {'tirith_enabled': False},
        'terminal': {'backend': 'local', 'cwd': '/workspace', 'timeout': 60},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False},
        'mcp_servers': {}, 'hooks': {}, 'fallback_model': [],
        'auxiliary': {'transient_retries': 0,
                      'title_generation': {'enabled': False, 'model_upgrade_enabled': False},
                      'background_review': {'enabled': False}},
        'approvals': {'single_query_mode': 'allow'},
    }
    if 'pstack' in task:
        config['plugins']['enabled'].append('cloud-pstack')
        config['agent']['max_turns'] = 100
        config['timeouts'] = {'tools': {'sequential_call': 7350, 'concurrent_batch': 7350}}
        integration = profile / 'plugins' / 'cloud-pstack'
        target = '/opt/hermes/trusted-plugins/cloud-pstack'
        if integration.is_symlink():
            require(os.readlink(integration) == target)
        else:
            require(not integration.exists())
            integration.symlink_to(target)
        from cloudworkbench.agent_pstack import initialize
        initialize(time.time() + RUN_BUDGET_SECONDS)
    identifier = uuid.uuid4().hex
    temporary = profile / ('config-' + identifier + '.json')
    write_private(temporary, json.dumps(config).encode())
    os.replace(temporary, profile / 'config.yaml')
    prompt = STATE / ('prompt-' + identifier + '.txt')
    text = task['prompt'] + (
        '\n\nTool environment: this job runs inside its own Crabbox environment. '
        'Use /workspace for project files and dependencies. Outbound web access and '
        'headless Chromium/Playwright are available. Run Playwright with '
        '/opt/hermes/venv/bin/python through the terminal tool; browser instructions '
        'are at /opt/cloud-tools/BROWSER.md and browser binaries at /opt/playwright. '
        'Keep downloads and browser outputs under /workspace. For project Python '
        'dependencies, create /workspace/.venv. Do not modify /agent-state or inspect '
        'agent credentials; that directory stores private continuation state. '
        'The shared cloud-evidence:pr-evidence skill is available for UI changes. '
        'Implementation and verification roles capture real screenshots and interaction '
        'video under /workspace/pr-evidence/ and prepare manifest.json plus pr-body.md '
        'there for the parent agent to publish. Read-only roles inspect existing evidence '
        'without acquiring capture or write tools. Tasks without UI changes skip media.')
    if desktop:
        text += (
            '\n\nA graphical XFCE desktop is available on DISPLAY=:99 through the task\'s '
            'VNC/noVNC viewer. For browser work the user needs to watch or interact with, '
            'use Playwright headless=False so the Chromium window appears on that desktop; '
            'headless=True remains available for other work. The chromium command also '
            'opens the installed browser. Keep the browser process alive while the user '
            'needs to interact with it. Desktop instructions are at /opt/cloud-tools/BROWSER.md. '
            'Do not disclose VNC passwords or change the private agent HOME.')
    if task.get('inputs'):
        text += '\n\nSupplied inputs: ' + json.dumps(
            [{'id': item, 'path': '/inputs/' + item} for item in task['inputs']])
    write_private(prompt, text.encode())
    env = {
        'PATH': '/opt/hermes/venv/bin:/usr/local/bin:/usr/bin:/bin',
        'HOME': str(STATE / 'home'), 'HERMES_HOME': str(profile),
        'CODEX_HOME': str(STATE / 'empty-codex'),
        'CLAUDE_CONFIG_DIR': str(STATE / 'empty-claude'),
        'XDG_CONFIG_HOME': str(STATE / 'home'), 'XDG_CACHE_HOME': str(STATE / 'cache'),
        'XDG_DATA_HOME': str(STATE / 'data'), 'TMPDIR': '/tmp',
        'HERMES_BUNDLED_PLUGINS': '/opt/hermes/empty-bundled',
        'HERMES_ENABLE_PROJECT_PLUGINS': '0', 'HERMES_INTERACTIVE': '0',
        'HERMES_REDACT_SECRETS': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'PLAYWRIGHT_BROWSERS_PATH': '/opt/playwright',
        'AGENT_BROWSER_EXECUTABLE_PATH': '/opt/playwright/chromium-1243/chrome-linux64/chrome',
        'PIP_CACHE_DIR': '/workspace/.cache/pip', 'npm_config_cache': '/workspace/.cache/npm',
        'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
        'XAI_API_KEY': task['api_key'],
    }
    if desktop:
        env.update(CRABBOX_DESKTOP='1', CRABBOX_DESKTOP_ENV='xfce', DISPLAY=':99')
    command = [HERMES, 'chat', '--query-file', str(prompt), '--format', 'stream-json', '--oneshot',
               '--provider', 'xai', '--model', 'grok-4.6', '--reasoning', 'xhigh',
               '--skills', 'pstack:tdd,cloud-evidence:pr-evidence', '--toolsets', 'terminal,file',
               '--max-turns', str(MAX_MODEL_TURNS), '--run-budget', str(RUN_BUDGET_SECONDS)]
    if 'pstack' in task:
        command[command.index('--skills') + 1] = 'cloud-pstack:orchestrate'
        command[command.index('terminal,file')] = 'cloud_pstack,cloud_workspace_read'
        command[command.index('--max-turns') + 1] = '100'
    if task['resume_session_id']:
        command.extend(['--resume', task['resume_session_id']])
    return command, env, prompt


def main():
    os.umask(0o077)
    task = read_request(sys.stdin.buffer)
    command, env, prompt = prepare(task)
    process = None
    previous_handlers = {}
    try:
        process = subprocess.Popen(command, cwd='/workspace', env=env,
                                   stdin=subprocess.DEVNULL, stdout=None,
                                   stderr=subprocess.DEVNULL, start_new_session=True,
                                   preexec_fn=lambda: os.umask(0o007))

        def forward(signum, _frame):
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, forward)
        code = process.wait()
        if code == 0 and 'pstack' in task:
            from cloudworkbench.agent_pstack import completed
            if not completed():
                return 1
        return code if code >= 0 else 128 - code
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        try:
            prompt.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.stderr.write('crabbox_guest_start_failed\n')
        sys.exit(1)
