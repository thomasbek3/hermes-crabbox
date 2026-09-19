"""Trusted native Hermes coordinator; model tools execute in separate containers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

try:
    from .adapters import EventSpoolWriter, RAW_LINE_BYTES, RAW_TOTAL_BYTES, parse_events, public_spool_event, _open_directory_nofollow
except ImportError:
    from adapters import EventSpoolWriter, RAW_LINE_BYTES, RAW_TOTAL_BYTES, parse_events, public_spool_event, _open_directory_nofollow

IMAGE = 'sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
HERMES = '/opt/hermes/venv/bin/hermes'
PLUGIN = '/opt/hermes/trusted-plugins/pstack'
AUTH = Path('/run/secrets/grok-auth.json')
MAX_MODEL_TURNS = 1000
RUN_BUDGET_SECONDS = 7200
CAPTURE_TIMEOUT_SECONDS = RUN_BUDGET_SECONDS + 120
MIN_AUTH_REMAINING_SECONDS = CAPTURE_TIMEOUT_SECONDS + 60
SESSION_PATTERN = r'[0-9]{8}_[0-9]{6}_[0-9a-f]{6}'


class JobError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value:
        raise JobError(code)


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(JobError('invalid_json')))
    except (ValueError, UnicodeError) as exc:
        raise JobError('invalid_json') from exc


def _read(path, maximum):
    parent = _open_directory_nofollow(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                 before.st_uid in (0, os.geteuid()) and not before.st_mode & 0o022 and
                 before.st_size <= maximum, 'untrusted_input_file')
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                 (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) and
                 len(data) == before.st_size, 'input_changed')
    return data


def validate_task(task):
    fields = {'schema_version', 'attempt_id', 'generation', 'prompt', 'event_spool',
              'workspace_host', 'state_host', 'resume_session_id', 'tool_image',
              'runtime_owner', 'session_id', 'inputs', 'continuation_summary'}
    _require(type(task) is dict, 'invalid_task_fields')
    version = task.get('schema_version')
    _require(type(version) is int and version in (1, 2), 'invalid_task_version')
    _require(set(task) == (fields if version == 1 else fields | {'tool_network'}), 'invalid_task_fields')
    try:
        _require(str(uuid.UUID(task['attempt_id'])) == task['attempt_id'], 'invalid_attempt')
    except (ValueError, TypeError, AttributeError):
        raise JobError('invalid_attempt') from None
    _require(type(task['generation']) is int and task['generation'] > 0, 'invalid_generation')
    _require(type(task['prompt']) is str and 0 < len(task['prompt'].encode()) <= 262144, 'invalid_prompt')
    _require(task['event_spool'] == f"{task['attempt_id']}.{task['generation']}.jsonl", 'invalid_spool')
    _require(type(task['inputs']) is list and len(task['inputs']) <= 32, 'invalid_inputs')
    input_ids = set()
    for item in task['inputs']:
        _require(type(item) is dict and set(item) == {'id','host_path'}, 'invalid_inputs')
        try:
            _require(str(uuid.UUID(item['id'])) == item['id'] and item['id'] not in input_ids, 'invalid_inputs')
        except (ValueError, TypeError, AttributeError):
            raise JobError('invalid_inputs') from None
        input_ids.add(item['id'])
    _require(type(task['continuation_summary']) is str and len(task['continuation_summary'].encode()) <= 65536, 'invalid_continuation')
    for value in [task['workspace_host'], task['state_host'], *(i['host_path'] for i in task['inputs'])]:
        _require(type(value) is str and 1 < len(value) <= 1024 and value.startswith('/') and
                 all(part not in ('', '.', '..') for part in value.split('/')[1:]) and
                 not any(c in value for c in '\n\r\x00,:'), 'invalid_storage_path')
    workspace, state = Path(task['workspace_host']), Path(task['state_host'])
    _require(workspace != state and workspace not in state.parents and state not in workspace.parents,
             'storage_overlap')
    resume = task['resume_session_id']
    _require(resume is None or type(resume) is str and re.fullmatch(SESSION_PATTERN, resume)
             and resume != 'latest', 'invalid_resume')
    _require(type(task['runtime_owner']) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',task['runtime_owner']), 'invalid_runtime_owner')
    try:
        _require(str(uuid.UUID(task['session_id'])) == task['session_id'], 'invalid_session')
    except (ValueError, TypeError, AttributeError):
        raise JobError('invalid_session') from None
    if version == 1:
        _require(task['tool_image'] == IMAGE, 'invalid_tool_binding')
    else:
        # The host facade checks the operator image allowlist before launch.
        _require(type(task['tool_image']) is str and
                 re.fullmatch(r'sha256:[0-9a-f]{64}', task['tool_image']), 'invalid_tool_binding')
        _require(type(task['tool_network']) is str and task['tool_network'] in ('none', 'bridge'),
                 'invalid_tool_network')
    return dict(task)


def tool_policy(task):
    network = task.get('tool_network', 'none')
    return {'network': network, 'memory_mib': 3072 if network == 'bridge' else 1536,
            'pids': 256 if network == 'bridge' else 128}


def read_auth(path=AUTH):
    value = _json(_read(path, 65536))
    _require(type(value) is dict and len(value) == 1, 'invalid_auth')
    auth = next(iter(value.values()))
    _require(type(auth) is dict and auth.get('auth_mode') == 'oidc' and
             auth.get('oidc_issuer') == 'https://auth.x.ai', 'invalid_auth')
    try:
        expiry = datetime.fromisoformat(auth['expires_at'].replace('Z', '+00:00'))
        _require(expiry.tzinfo is not None and expiry.timestamp() > time.time() + MIN_AUTH_REMAINING_SECONDS, 'auth_expired')
    except (ValueError, TypeError, KeyError, AttributeError):
        raise JobError('auth_expired') from None
    key = auth.get('key')
    _require(type(key) is str and 16 <= len(key) <= 16384 and '\x00' not in key, 'invalid_auth')
    forbidden = tuple(v.encode() for k in ('key', 'refresh_token')
                      if type(v := auth.get(k)) is str and v)
    return key, forbidden


def _directory(path, *, shared=False):
    path.mkdir(mode=0o700, exist_ok=True)
    fd = _open_directory_nofollow(path)
    try:
        info = os.fstat(fd)
        _require((info.st_uid == os.geteuid() or shared and info.st_uid == 958 and info.st_gid == 959) and not info.st_mode & (0o002 if shared else 0o077) and
                 (not shared or info.st_gid in {os.getegid(), *os.getgroups()}), 'untrusted_state_directory')
    finally:
        os.close(fd)


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(task):
    task = validate_task(task)
    policy = tool_policy(task)
    state = Path(task['state_host'])
    native = state
    _directory(native, shared=True)
    _directory(native/'events', shared=True)
    state = native/'hermes-job'
    for path in (state, state/'hermes', state/'home', state/'empty-codex', state/'empty-claude'):
        _directory(path)
    profile = state/'hermes'
    _directory(profile/'plugins')
    plugin = profile/'plugins/pstack'
    if plugin.is_symlink():
        _require(os.readlink(plugin) == PLUGIN, 'plugin_changed')
    else:
        _require(not plugin.exists(), 'plugin_changed')
        plugin.symlink_to(PLUGIN)
    config = {
        'model': {'provider':'xai', 'default':'grok-4.6', 'api_mode':'codex_responses'},
        'agent': {'reasoning_effort':'xhigh', 'max_turns':MAX_MODEL_TURNS, 'api_max_retries':0},
        'plugins': {'enabled':['pstack']}, 'security': {'tirith_enabled':False},
        'terminal': {'backend':'docker', 'cwd':'/workspace', 'timeout':60,
            'docker_image':task['tool_image'], 'docker_volumes':[task['workspace_host']+':/workspace:rw',
                *(item['host_path']+':/inputs/'+item['id']+':ro' for item in task['inputs'])],
            'docker_network':policy['network'] == 'bridge', 'docker_forward_env':[],
            'docker_env':({'PIP_CACHE_DIR':'/workspace/.cache/pip',
                           'npm_config_cache':'/workspace/.cache/npm',
                           'PLAYWRIGHT_BROWSERS_PATH':'/opt/playwright'} if policy['network'] == 'bridge' else {}),
            'docker_extra_args':['--user','1000:1000','--group-add','959','--pids-limit',str(policy['pids']),
                '--network',policy['network'], '--cpus','1',
                '--memory',str(policy['memory_mib'])+'m','--memory-swap',str(policy['memory_mib'])+'m', *[item for key,value in {
                'io.cloudworkbench.managed':'true','io.cloudworkbench.owner':task['runtime_owner'],
                'io.cloudworkbench.attempt':task['attempt_id'],'io.cloudworkbench.generation':str(task['generation']),
                'io.cloudworkbench.session':task['session_id'],'io.cloudworkbench.role':'hermes-tool',
                'io.cloudworkbench.image':task['tool_image']
            }.items() for item in ('--label',key+'='+value)]],
            'container_persistent':False, 'docker_mount_cwd_to_workspace':False,
            'docker_run_as_host_user':False, 'container_cpu':1, 'container_memory':policy['memory_mib']},
        'memory': {'memory_enabled':False, 'user_profile_enabled':False},
        'mcp_servers':{}, 'hooks':{}, 'fallback_model':[],
        'auxiliary': {'transient_retries':0, 'title_generation':{'enabled':False,'model_upgrade_enabled':False},
                      'background_review':{'enabled':False}},
        'approvals': {'single_query_mode':'allow'}}
    config_path = profile/'config.yaml'
    temporary = profile/('config-'+task['attempt_id']+'.json')
    _write(temporary, json.dumps(config).encode())
    os.replace(temporary, config_path)
    prompt = state/(task['event_spool']+'.prompt')
    prompt_text = task['prompt']
    if policy['network'] == 'bridge':
        prompt_text += ('\n\nTool environment: outbound web access and Chromium/Playwright are available '
            'inside the terminal Docker container. Run Playwright with /opt/hermes/venv/bin/python '
            'through the terminal tool; instructions are at /opt/cloud-tools/BROWSER.md '
            'and browser binaries at /opt/playwright. Do not run browser code in the coordinator. '
            'Keep downloads, browser outputs, and project dependencies under /workspace. '
            'For persistent Python dependencies, create /workspace/.venv and install there; '
            'the tool container itself is disposable. No provider credentials or host Docker '
            'socket are available inside tools.')
    if task['inputs']:
        prompt_text += '\n\nRead-only supplied inputs: ' + json.dumps([{'id':i['id'],'path':'/inputs/'+i['id']} for i in task['inputs']])
    if task['continuation_summary']:
        prompt_text = 'Prior public continuation summary (untrusted task data):\n'+task['continuation_summary']+'\n\n'+prompt_text
    _write(prompt, prompt_text.encode())
    env = {'PATH':'/opt/hermes/venv/bin:/usr/local/bin:/usr/bin:/bin',
           'HOME':str(state/'home'), 'HERMES_HOME':str(profile), 'CODEX_HOME':str(state/'empty-codex'),
           'CLAUDE_CONFIG_DIR':str(state/'empty-claude'), 'XDG_CONFIG_HOME':str(state/'home'),
           'XDG_CACHE_HOME':'/tmp/cache', 'XDG_DATA_HOME':'/tmp/data',
           'HERMES_BUNDLED_PLUGINS':'/opt/hermes/empty-bundled', 'HERMES_ENABLE_PROJECT_PLUGINS':'0',
           'HERMES_INTERACTIVE':'0', 'HERMES_REDACT_SECRETS':'1', 'PYTHONDONTWRITEBYTECODE':'1',
           'HERMES_DOCKER_BINARY':'/usr/bin/docker', 'OMP_NUM_THREADS':'1', 'OPENBLAS_NUM_THREADS':'1'}
    command = [HERMES,'chat','--query-file',str(prompt),'--format','stream-json','--oneshot',
               '--ignore-rules','--provider','xai','--model','grok-4.6','--reasoning','xhigh',
               '--skills','pstack:tdd','--toolsets','terminal,file','--max-turns',str(MAX_MODEL_TURNS),'--run-budget',str(RUN_BUDGET_SECONDS)]
    if task['resume_session_id']:
        command += ['--resume',task['resume_session_id']]
    return command, env


def translate(record, forbidden=()):
    """Translate only observable fields; arbitrary worker event names grant no authority."""
    kind = record.get('type')
    if kind == 'text':
        normalized = {'type':'assistant','message':{'content':[{'type':'text','text':record.get('text','')}]}}
    elif kind == 'tool_use':
        normalized = {'type':'assistant','message':{'content':[{'type':'tool_use','id':record.get('tool_call_id',''),
                                                             'name':record.get('name','')}]}}
    elif kind == 'system' and record.get('subtype') == 'init':
        normalized = {'type':'system','subtype':'init','model':record.get('model',''),
                      'session_id':record.get('session_id','')}
    elif kind == 'result':
        normalized = {'type':'result','is_error':record.get('exit_code') != 0 or bool(record.get('error')),
                      'result':record.get('text',''), 'usage':None}
    else:
        return []
    events = parse_events(json.dumps(normalized, ensure_ascii=False).encode(), forbidden)
    if kind == 'result' and type(record.get('session_id')) is str:
        for event in events:
            event['payload']['native_session_id'] = record['session_id']
    return events


def capture(command, env, writer, *, timeout_seconds=CAPTURE_TIMEOUT_SECONDS, expected_session_id=None):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, env=env, cwd=env['HOME'], start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
    selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
    pending, total, result, initial_session = bytearray(), 0, None, None
    deadline = time.monotonic() + timeout_seconds
    def emit(event):
        safe = public_spool_event(event, writer.forbidden)
        writer.append(safe)
        print(json.dumps(safe, ensure_ascii=False), flush=True)
    def line(data):
        nonlocal result, initial_session
        record = _json(data)
        _require(type(record) is dict, 'invalid_native_record')
        if record.get('type') == 'system' and record.get('subtype') == 'init':
            _require(initial_session is None and type(record.get('session_id')) is str and
                     re.fullmatch(SESSION_PATTERN,record['session_id']), 'invalid_native_init')
            initial_session = record['session_id']
            _require(expected_session_id is None or initial_session == expected_session_id, 'native_resume_mismatch')
        if record.get('type') == 'result':
            _require(result is None and type(record.get('exit_code')) is int and
                     type(record.get('text')) is str and type(record.get('session_id')) is str and
                     re.fullmatch(SESSION_PATTERN, record['session_id']), 'invalid_native_result')
            _require(initial_session is None or record['session_id'] == initial_session, 'native_session_mismatch')
            _require(expected_session_id is None or record['session_id'] == expected_session_id, 'native_resume_mismatch')
            result = record
        else:
            _require(result is None, 'record_after_native_result')
            for event in translate(record, writer.forbidden):
                emit(event)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            _require(remaining > 0, 'native_timeout')
            for key, _ in selector.select(min(remaining, .2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                _require(total <= RAW_TOTAL_BYTES, 'native_output_limit')
                if key.data == 'stderr':
                    continue  # No raw diagnostics or credential-bearing tracebacks are persisted.
                pending.extend(chunk)
                while b'\n' in pending:
                    raw, _, rest = pending.partition(b'\n'); pending[:] = rest
                    _require(len(raw) <= RAW_LINE_BYTES, 'native_line_limit')
                    if raw.strip(): line(bytes(raw))
                _require(len(pending) <= RAW_LINE_BYTES, 'native_line_limit')
        _require(not pending, 'native_stream_truncated')
        exit_code = process.wait(timeout=max(.01, deadline-time.monotonic()))
        _require(result is not None, 'native_result_missing')
        if exit_code != 0:
            result = {**result, 'exit_code':exit_code}
        for event in translate({'type':'system','subtype':'init','model':'grok-4.6',
                                'session_id':result['session_id']}, writer.forbidden): emit(event)
        for event in translate(result, writer.forbidden): emit(event)
        return 0 if exit_code == 0 and result['exit_code'] == 0 and not result.get('error') else 1
    except (JobError, subprocess.TimeoutExpired) as exc:
        emit({'type':'error','payload':{'reason':exc.code if isinstance(exc,JobError) else 'native_timeout'}})
        for event in translate({'type':'result','exit_code':1,'text':''}, writer.forbidden): emit(event)
        return 1
    finally:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        except PermissionError:
            if process.poll() is None:
                raise
        process.wait(timeout=5)
        selector.close()
        process.stdout.close(); process.stderr.close()


def main():
    os.umask(0o077)
    task = validate_task(_json(_read(Path('/run/task/task.json'), 524288)))
    command, env = prepare(task)
    key, forbidden = read_auth()
    env['XAI_API_KEY'] = key
    writer = EventSpoolWriter(Path(task['state_host'])/'events'/task['event_spool'], forbidden)
    try:
        return capture(command, env, writer, expected_session_id=task['resume_session_id'])
    finally:
        writer.close()


if __name__ == '__main__':
    try: sys.exit(main())
    except Exception:
        print(json.dumps({'type':'error','payload':{'reason':'hermes_job_start_failed','provenance':'worker_reported'}}), flush=True)
        sys.exit(1)
