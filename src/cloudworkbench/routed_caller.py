"""Fixed unprivileged caller entrypoints; no Docker, provider auth or scheduling."""
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from .hermes_adapter import RoutedLaunchPlan


class CallerError(ValueError):
    pass


def _read(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise CallerError('invalid_caller_file')
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(data) > limit or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise CallerError('caller_file_changed')
        return data
    except OSError:
        raise CallerError('caller_file_unavailable') from None


def _json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError()
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise CallerError('invalid_caller_config') from None


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _publish_record(path, value):
    """Atomically publish a complete worker record from its single launched writer."""
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix='.record-', dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,allow_nan=False);out.flush();os.fsync(out.fileno())
        if path.exists() or path.is_symlink(): raise FileExistsError(path.name)
        # Same-UID code can forge any worker output; the controller must not
        # treat this atomic publication as protected verification evidence.
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def load_plan(task_root=Path('/run/task')):
    value = _json(_read(task_root / 'launch.json', 262144))
    if type(value) is not dict or set(value) != {f.name for f in fields(RoutedLaunchPlan)}:
        raise CallerError('invalid_caller_plan')
    try:
        for name in ('argv','required_readonly_paths','required_empty_directories'):
            value[name] = tuple(value[name])
        value['environment'] = tuple(tuple(pair) for pair in value['environment'])
        plan = RoutedLaunchPlan(**value)
        receipt = _json(plan.receipt_json)
        env = dict(plan.environment)
        if (len(env) != len(plan.environment) or plan.capability_environment != 'CWB_INFERENCE_CAPABILITY'
                or plan.request_path not in ('/v1/responses','/v1/chat/completions')
                or plan.argv[:2] != ('/opt/hermes/venv/bin/hermes','chat')
                or type(plan.workspace_readonly) is not bool
                or receipt['config_sha256'] != _hash(plan.config_json)
                or receipt['prompt_sha256'] != _hash(plan.prompt)
                or receipt['argv_sha256'] != _hash(_canonical(plan.argv))
                or receipt['environment_sha256'] != _hash(_canonical(env))
                or receipt['workspace_readonly'] != plan.workspace_readonly
                or receipt['request_path'] != plan.request_path):
            raise CallerError('caller_plan_binding_mismatch')
        if _read(task_root / 'prompt.txt', 131072) != plan.prompt.encode():
            raise CallerError('caller_prompt_mismatch')
        if _read(Path('/run/tool/hermes/config.yaml'), 65536) != plan.config_json.encode():
            raise CallerError('caller_config_mismatch')
        return plan
    except CallerError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise CallerError('invalid_caller_plan') from None


def _capability(path):
    try: value = _read(path, 128).decode('ascii')
    except UnicodeError: raise CallerError('invalid_caller_capability') from None
    if not re.fullmatch('[0-9a-f]{64}', value):
        raise CallerError('invalid_caller_capability')
    return value


def create_observation_root(scratch=Path('/scratch')):
    """Fresh private worker records on the caller's durable scratch bind."""
    parent_fd = None
    output_fd = None
    try:
        parent_fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent = os.fstat(parent_fd)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid not in (0, 959, os.getuid())
                or parent.st_gid != os.getgid() or parent.st_mode & 0o007):
            raise CallerError('untrusted_observation_scratch')
        os.mkdir('.cwb-observations', mode=0o700, dir_fd=parent_fd)
        output_fd = os.open('.cwb-observations', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        os.fchmod(output_fd, 0o700)
        output = os.fstat(output_fd)
        current = scratch.lstat()
        if (output.st_uid != os.getuid() or output.st_gid != os.getgid()
                or stat.S_IMODE(output.st_mode) != 0o700
                or (current.st_dev, current.st_ino) != (parent.st_dev, parent.st_ino)
                or not stat.S_ISDIR(current.st_mode)):
            raise CallerError('observation_directory_changed')
        return scratch / '.cwb-observations'
    except FileExistsError:
        raise CallerError('observation_directory_exists') from None
    except OSError:
        raise CallerError('observation_directory_unavailable') from None
    finally:
        if output_fd is not None:
            os.close(output_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def run_hermes():
    if os.getuid() != 1000 or os.getgid() != 1000:
        raise CallerError('wrong_caller_identity')
    plan = load_plan()
    for raw in plan.required_empty_directories:
        path = Path(raw)
        if path.parent != Path('/run/tool') or path.name not in {'home','hermes','codex','claude','config','cache','data','tmp'}:
            raise CallerError('invalid_isolation_directory')
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise CallerError('invalid_isolation_directory')
        allowed = {'config.yaml'} if path.name == 'hermes' else set()
        if {p.name for p in path.iterdir()} != allowed:
            raise CallerError('nonempty_isolation_directory')
    capability = _capability(Path('/run/task/http-capability'))
    env = dict(plan.environment)
    env[plan.capability_environment] = capability
    output_root = create_observation_root()
    return supervise_hermes(plan, env, capability, output_root, cwd='/workspace')


def supervise_hermes(plan, environment, capability, output_root, *, cwd):
    """Bound detached exec output; records are worker evidence, never pass gates."""
    import selectors
    import signal
    import subprocess
    import time
    from .adapters import EventSpoolWriter
    from .hermes_adapter import JsonlParser
    budget = int(plan.argv[plan.argv.index('--run-budget') + 1])
    if not 10 <= budget <= 3600: raise CallerError('invalid_caller_budget')
    writer = EventSpoolWriter(output_root / 'events.jsonl', forbidden=(capability.encode(),))
    parser = JsonlParser((capability.encode(),),
        strict_result='stage_contract_sha256' in _json(plan.receipt_json))
    process = None
    selector = selectors.DefaultSelector()
    status, failure, exit_code, stderr_bytes = 'failed', None, None, 0
    result_seen = False
    group_stopped = False
    try:
        process = subprocess.Popen(plan.argv, cwd=cwd, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        for pipe, name in ((process.stdout,'stdout'),(process.stderr,'stderr')):
            os.set_blocking(pipe.fileno(),False);selector.register(pipe,selectors.EVENT_READ,name)
        deadline = time.monotonic() + budget
        while selector.get_map():
            if not group_stopped and process.poll() is not None:
                # An exited Hermes may leave helpers holding its stream descriptors.
                # Stop that group, then drain its buffered output before parsing ends.
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                except OSError: raise CallerError('caller_stop_unconfirmed') from None
                group_stopped = True
            if time.monotonic() >= deadline: raise CallerError('caller_wall_timeout')
            for key, _ in selector.select(min(.2, max(0,deadline-time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(),65536)
                if not chunk:
                    selector.unregister(key.fileobj);continue
                if key.data == 'stderr':
                    stderr_bytes += len(chunk)
                    if stderr_bytes > 16*1024*1024: raise CallerError('caller_stderr_limit')
                    continue
                for event in parser.feed(chunk):
                    # The shared public spool exposes tool start, not raw tool output.
                    if event['type'] == 'tool.completed': continue
                    if event['type'] == 'adapter.provenance':
                        event['payload']['model'] = event['payload'].get('reported_model')
                    if event['type'] == 'adapter.result':
                        result_seen = not event['payload']['is_error']
                        # Hermes emits default zero counters; retain unknown accounting.
                        event['payload']['usage'] = None
                    writer.append(event)
        parser.finish()
        exit_code = process.wait(timeout=max(.01, deadline-time.monotonic()))
        if exit_code == 0 and result_seen: status = 'completed'
        else: failure = 'hermes_execution_failed'
    except Exception as exc:
        failure = str(exc) if type(exc) is CallerError else 'caller_execution_failed'
    finally:
        if process is not None:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            except OSError: status='failed';failure='caller_stop_unconfirmed'
            try: exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired: status='failed';failure='caller_stop_unconfirmed'
            process.stdout.close();process.stderr.close()
        selector.close();writer.close()
    receipt={'status':status,'failure':failure,'exit_code':exit_code,
        'stderr_bytes':stderr_bytes,'provenance':'worker_reported',
        'outer_cleanup_required':True,'verification_pass':False,
        'launch_receipt_sha256':_hash(plan.receipt_json)}
    _publish_record(output_root/'result.json',receipt)
    return 0 if status=='completed' else 1


def run_relay():
    if os.getuid() != 1001 or os.getgid() != 1001:
        raise CallerError('wrong_relay_identity')
    from .inference_relay import AttemptBinding, InferenceRelay, make_loopback_service
    config = _json(_read(Path('/run/worker-inference/client.json'), 8192))
    if type(config) is not dict or set(config) != {'binding','worker_capability','http_capability','request_path','port'}:
        raise CallerError('invalid_relay_config')
    if (type(config['port']) is not int or not 1024 <= config['port'] <= 65535
            or config['request_path'] not in ('/v1/responses','/v1/chat/completions')
            or any(not isinstance(config[k], str) or not re.fullmatch('[0-9a-f]{64}', config[k])
                   for k in ('worker_capability','http_capability'))):
        raise CallerError('invalid_relay_config')
    root = Path('/run/relay')
    def fence(code):
        # Only the relay's fixed error code is recorded; no request contents.
        (root / 'fenced.json').write_text(json.dumps({'code': code}))
    relay = InferenceRelay(socket_path=Path('/run/worker-inference/socket'), journal_path=root / 'relay.db',
        binding=AttemptBinding(**config['binding']), capability=config['worker_capability'], fence=fence, deadline_seconds=180)
    server = make_loopback_service(relay, address=('127.0.0.1', config['port']),
        request_path=config['request_path'], capability_sha256=_hash(config['http_capability']),
        authorize=lambda: not (root / 'fenced.json').exists(), execution_timeout=190)
    _publish_record(root/'ready.json',{'pid': os.getpid(), 'uid': os.getuid(),
        'request_path': config['request_path'], 'binding': config['binding']})
    try: server.serve_forever()
    finally: server.server_close()


def main():
    try:
        if sys.argv[1:] == ['hermes']: return run_hermes()
        elif sys.argv[1:] == ['relay']: run_relay()
        else: raise CallerError('invalid_caller_role')
    except Exception:
        # Never expose capability files, native diagnostics or request contents.
        print('routed_caller_bootstrap_failed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
