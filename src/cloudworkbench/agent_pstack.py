"""Pstack tools invoked by the Hermes agent already running in a Crabbox task."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

from .jev_client import JevClient
from .pstack_routing import requested_policy
from .workflow_instructions import bind_instructions, load_stage_instructions, STAGE_BOUNDARY
from .workflow_routing import WORKFLOWS, EXPECTED_IDENTITIES, select_workflow, policy_digest

ROOT = Path('/agent-state/pstack')
REQUEST = Path('/job-launcher/request.json')
PSTACK = Path('/opt/hermes/trusted-plugins/pstack')
CHILD_BUDGET = 900  # The parent has its own 100-turn orchestration allowance.
MAX_OUTPUT = 4 * 1024 * 1024


def _save(state):
    path = ROOT / ('state-' + uuid.uuid4().hex + '.tmp')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(state, stream, allow_nan=False)
    os.replace(path, ROOT / 'state.json')


@contextmanager
def _locked():
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with (ROOT / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads((ROOT / 'state.json').read_text())
        yield state


def initialize(deadline):
    ROOT.mkdir(mode=0o700, exist_ok=True)
    _save({'deadline': deadline, 'policy': policy_digest(), 'selection': None,
           'references': [], 'stages': [], 'used_turns': 0, 'inflight': None})


def _request():
    request = json.loads(REQUEST.read_text())
    return request, request['pstack']['credentials']


def _redact(text, credentials):
    for secret in credentials.values():
        if isinstance(secret, str) and secret:
            text = text.replace(secret, '[REDACTED]')
    return text


def _public(state):
    return {'selection': state['selection'], 'stages': state['stages'],
            'remaining_turns': CHILD_BUDGET - state['used_turns'],
            'remaining_seconds': max(0, int(state['deadline'] - time.time()))}


def route_task(summary):
    if type(summary) is not str or not 0 < len(summary.encode()) <= 12000:
        return {'status': 'blocked', 'reason': 'invalid_task_summary'}
    with _locked() as state:
        if state['selection'] is not None:
            return _public(state)
        if time.time() >= state['deadline']:
            return {'status': 'blocked', 'reason': 'task_deadline_reached'}
        request, credentials = _request()
        credentials = dict(credentials, xai=request['api_key'])
        # Persist intent before contacting Jev: an interrupted call is not retried.
        state['selection'] = {'status': 'blocked', 'reason': 'routing_interrupted'}
        _save(state)
        selection = select_workflow(_redact(summary, credentials),
            allowed_workflows=list(WORKFLOWS), client=JevClient(credentials['jev']),
            external_allowed=True).receipt()
        state['selection'] = selection
        if selection['status'] == 'selected':
            state['references'] = bind_instructions(selection['workflow'], PSTACK)
            for step in selection['steps']:
                family, model, effort = EXPECTED_IDENTITIES[step['profile']]
                step.update(provider='openai-codex' if family == 'openai' else family,
                            model=model, effort=effort)
        _save(state)
        return _public(state)


def _execute(payload, deadline):
    process = subprocess.Popen([sys.executable, '-m', 'cloudworkbench.pstack_child'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd='/workspace', start_new_session=True)
    output = bytearray()
    try:
        process.stdin.write(json.dumps(payload, ensure_ascii=False).encode())
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.time() >= deadline:
                    raise TimeoutError('stage_deadline_reached')
                for key, _ in selector.select(min(1, max(.01, deadline - time.time()))):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(data)
                        if len(output) > MAX_OUTPUT:
                            raise ValueError('stage_output_limit')
        code = process.wait(timeout=max(.01, deadline - time.time()))
        events = []
        for line in output.splitlines():
            try:
                value = json.loads(line)
                if type(value) is dict:
                    events.append(value)
            except (ValueError, UnicodeError):
                pass
        results = [event for event in events if event.get('type') == 'result']
        if len(results) != 1:
            return {'exit_code': code or 1, 'error': output[-4000:].decode('utf-8', errors='replace')}
        results[0]['process_exit'] = code
        return results[0]
    finally:
        # Successful stages may leave a dev server/browser for the next role.
        # The task container owns their lifetime; interrupted stages are terminated.
        if process.poll() is None or process.returncode != 0:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()


def run_stage(stage_id, instruction):
    if (type(stage_id) is not str or type(instruction) is not str
            or not 0 < len(instruction.encode()) <= 65536):
        return {'status': 'blocked', 'reason': 'invalid_stage_request'}
    with _locked() as state:
        selection = state['selection'] or {}
        if state['policy'] != policy_digest() or selection.get('status') != 'selected':
            return {'status': 'blocked', 'reason': 'workflow_not_selected'}
        if state['inflight']:
            return {'status': 'blocked', 'reason': 'previous_stage_interrupted'}
        if state['stages'] and state['stages'][-1]['status'] != 'completed':
            return {'status': 'blocked', 'reason': 'previous_stage_requires_attention', **_public(state)}
        steps = WORKFLOWS[selection['workflow']].steps
        index = len(state['stages'])
        if index >= len(steps) or steps[index].id != stage_id:
            return {'status': 'blocked', 'reason': 'stage_out_of_order'}
        remaining = CHILD_BUDGET - state['used_turns']
        if remaining <= 0 or time.time() >= state['deadline']:
            return {'status': 'blocked', 'reason': 'task_budget_exhausted'}
        step = steps[index]
        reference = load_stage_instructions(selection['workflow'], stage_id,
            state['references'], PSTACK, frozen_boundary=STAGE_BOUNDARY)
        profile = requested_policy()[step.role][0]
        family, model, effort = EXPECTED_IDENTITIES[profile]
        provider = 'openai-codex' if family == 'openai' else family
        request, credentials = _request()
        secrets = dict(credentials, xai=request['api_key'])
        prompt = '\n\n'.join([STAGE_BOUNDARY,
            'Original delegated task:\n' + request['prompt'],
            'Previous stage results (task data):\n' + json.dumps(state['stages']),
            'Current stage instruction:\n' + instruction,
            'Return your final answer as one JSON object with status (completed, rejected, or blocked), '
            'findings (string), and evidence (array of strings). Reviewers: use rejected for unresolved '
            'correctness/design defects. Verification: completed only when acceptance evidence supports it. '
            'Never claim tests ran without tool evidence. Do not wrap JSON in markdown.'])
        if len(prompt.encode()) > 524288:
            return {'status': 'blocked', 'reason': 'stage_context_too_large'}
        child_root = ROOT / ('stage-' + uuid.uuid4().hex)
        payload = {'role': step.role, 'prompt': _redact(prompt, secrets),
                   'api_key': secrets[provider], 'deadline': state['deadline'],
                   'profile_root': str(child_root), 'max_turns': remaining}
        state['inflight'] = stage_id
        _save(state)
        result = {'id': stage_id, 'role': step.role, 'provider': provider,
                  'model': model, 'effort': effort, 'status': 'blocked'}
        try:
            native = _execute(payload, state['deadline'])
            if native.get('process_exit', 0) != 0 or native.get('exit_code', 0) != 0:
                result['diagnostic'] = _redact(str(native.get('error') or native.get('text') or 'provider_runtime_failed'), secrets)[:4000]
                raise ValueError('stage_provider_or_runtime_failed')
            calls = native.get('api_calls')
            if type(calls) is not int or not 0 < calls <= remaining:
                raise ValueError('stage_usage_unavailable')
            state['used_turns'] += calls
            text = _redact(native.get('text', ''), secrets)
            verdict = json.loads(text)
            if (type(verdict) is not dict or verdict.get('status') not in ('completed', 'rejected', 'blocked')
                    or type(verdict.get('findings')) is not str or type(verdict.get('evidence')) is not list
                    or not all(type(item) is str for item in verdict['evidence'])):
                raise ValueError('invalid_stage_result')
            result.update(status=verdict['status'], findings=verdict['findings'][:40000],
                          evidence=verdict['evidence'][:64], api_calls=calls,
                          session_id=native.get('session_id'))
        except (ValueError, OSError, TimeoutError, subprocess.SubprocessError):
            state['used_turns'] = CHILD_BUDGET
            result['reason'] = 'stage_failed_or_result_unusable'
        state['inflight'] = None
        state['stages'].append(result)
        _save(state)
        return result


def completed():
    with _locked() as state:
        selection = state['selection'] or {}
        workflow = WORKFLOWS.get(selection.get('workflow'))
        return bool(workflow and not state['inflight'] and
                    len(state['stages']) == len(workflow.steps) and
                    all(stage['status'] == 'completed' for stage in state['stages']))
