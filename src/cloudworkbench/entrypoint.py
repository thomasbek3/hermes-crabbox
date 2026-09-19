"""Executed only inside the bounded worker container; no host-side task execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import resource
import re
import signal
import threading

try:
    from .adapters import claude_command, parse_events, EventSpoolWriter, EventSpoolError, RAW_LINE_BYTES, RAW_TOTAL_BYTES, read_credential, usage_status, FAILURE_CODES, classify_cli_failure
except ImportError:
    from adapters import claude_command, parse_events, EventSpoolWriter, EventSpoolError, RAW_LINE_BYTES, RAW_TOTAL_BYTES, read_credential, usage_status, FAILURE_CODES, classify_cli_failure


def make_event_spool(task, forbidden=()):
    name = task.get('event_spool')
    if not isinstance(name,str) or not re.fullmatch(r'[0-9a-f-]{36}\.[1-9][0-9]*\.jsonl',name):
        raise EventSpoolError('event_spool_invalid_name')
    root = os.open('/state',os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir('events',0o770,dir_fd=root)
        except FileExistsError:
            pass
        events = os.open('events',os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,dir_fd=root)
        os.close(events)
    finally:
        os.close(root)
    return EventSpoolWriter(Path('/state/events')/name,forbidden)


def emit_provider_record(record, writer):
    for event in parse_events(json.dumps(record,ensure_ascii=False).encode(),writer.forbidden):
        writer.append(event)
        print(json.dumps(event,ensure_ascii=False),flush=True)


def kill_process_group(process):
    try:
        os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can report EPERM for an already-exited process group.
        if process.poll() is None:
            raise


def capture_provider(command, prompt, env, writer, *, raw_total_bytes=RAW_TOTAL_BYTES, raw_line_bytes=RAW_LINE_BYTES):
    """Drain pipes without logging raw provider output or accumulating full history."""
    process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,start_new_session=True)
    def feed_prompt():
        try:
            process.stdin.write(prompt.encode())
            process.stdin.close()
        except (BrokenPipeError,OSError):
            pass
    feeder = threading.Thread(target=feed_prompt,daemon=True)
    feeder.start()
    buffered, consumed = bytearray(), 0
    last_failure, result_seen = None, False
    def publish(event):
        nonlocal last_failure, result_seen
        payload = event['payload']
        if event['type'] == 'error' and payload.get('reason') in FAILURE_CODES and payload.get('classification_basis'):
            last_failure = {'code':payload['reason'],'basis':payload['classification_basis']}
        if event['type'] == 'adapter.result':
            result_seen = True
            if payload['is_error'] and not payload.get('failure_code') and payload.get('api_error_status') is None and payload.get('subtype') == 'error_during_execution' and last_failure:
                payload.update(failure_code=last_failure['code'],failure_basis=last_failure['basis'])
            if not payload['is_error']:
                last_failure = None
        writer.append(event)
        print(json.dumps(event,ensure_ascii=False),flush=True)
    def publish_line(line):
        nonlocal last_failure
        try:
            record = json.loads(line)
        except (ValueError,UnicodeError):
            record = None
        if isinstance(record,dict) and record.get('type') == 'assistant' and classify_cli_failure(record) is None:
            last_failure = None
        for event in parse_events(line,writer.forbidden):
            publish(event)
    try:
        while chunk := os.read(process.stdout.fileno(),65536):
            consumed += len(chunk)
            if consumed > raw_total_bytes:
                raise EventSpoolError('provider_output_total_limit')
            buffered.extend(chunk)
            while (boundary := buffered.find(b'\n')) >= 0:
                line = bytes(buffered[:boundary])
                del buffered[:boundary+1]
                if len(line) > raw_line_bytes:
                    raise EventSpoolError('provider_output_line_limit')
                publish_line(line)
            if len(buffered) > raw_line_bytes:
                raise EventSpoolError('provider_output_line_limit')
        if buffered:
            publish_line(bytes(buffered))
        exit_code = process.wait()
        if exit_code < 0:
            last_failure = None
        if not result_seen:
            publish({'type':'adapter.result','payload':{'is_error':True,'summary':'','usage':None,
                    'usage_status':usage_status(None,missing_result=True),
                    'failure_code':last_failure['code'] if last_failure else None,
                    'failure_basis':last_failure['basis'] if last_failure else None,'provenance':'worker_reported'}})
        return exit_code
    except BaseException as exc:
        if isinstance(exc,EventSpoolError):
            event = {'type':'error','payload':{'reason':exc.code}}
            try:
                writer.append(event)
            except Exception:
                pass
            print(json.dumps(event),flush=True)
        kill_process_group(process)
        process.wait()
        raise
    finally:
        feeder.join(timeout=1)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def run_verification_check(check):
    """Bound each check and descendants remaining in its process group."""
    def limit_output():
        resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))

    process = None
    timed_out = False
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(check["argv"], stdout=stdout, stderr=stderr,
                                           preexec_fn=limit_output, start_new_session=True)
                try:
                    process.wait(timeout=check.get("timeout", 60))
                except subprocess.TimeoutExpired:
                    timed_out = True
            finally:
                if process is not None:
                    kill_process_group(process)
                    process.wait()
            outputs = {}
            for name, stream in (("stdout", stdout), ("stderr", stderr)):
                stream.seek(max(0, stream.seek(0, os.SEEK_END) - 32000))
                outputs[name] = stream.read(32000).decode(errors="replace")
            result = {"id": check["id"], "result": "passed" if process.returncode == 0 and not timed_out else "failed",
                      "exit_code": process.returncode, **outputs}
            if timed_out:
                result["reason"] = "check_timeout"
            return result
    except OSError:
        return {"id": check["id"], "result": "infrastructure_error", "reason": "check_execution_error"}


def main() -> int:
    path = Path(sys.argv[1])
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Task exceeds limit")
    task = json.loads(path.read_text())
    os.umask(0o007)
    os.chdir("/workspace")
    kind = task["adapter"]
    if kind == "claude":
        token_bytes, credential_error = read_credential(Path("/run/secrets/claude-token"))
        if credential_error:
            writer = make_event_spool(task)
            try:
                event = {'type':'adapter.result','payload':{'is_error':True,'summary':'',
                         'failure_code':credential_error,'failure_basis':'credential_file',
                         'usage':None,'usage_status':usage_status(None,missing_result=True),'provenance':'worker_reported'}}
                writer.append(event)
                print(json.dumps(event),flush=True)
            finally:
                writer.close()
            return 1
        token = token_bytes.decode('ascii')
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("ANTHROPIC_", "CLAUDE_")):
                env.pop(key)
        env.update({"HOME": "/state", "CLAUDE_CONFIG_DIR": "/state/claude", "CLAUDE_CODE_OAUTH_TOKEN": token,
                    "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        Path("/state/claude").mkdir(parents=True, exist_ok=True)
        prompt = task["prompt"]
        if task.get("inputs"):
            prompt += "\n\nRead-only supplied inputs: " + json.dumps(task["inputs"])
        if task.get("continuation_summary"):
            prompt = "Continuation reconstructed from saved workspace and prior public summary; prior model conversation is not restored.\n" + task["continuation_summary"] + "\n\n" + prompt
        writer = make_event_spool(task,(token.encode(),))
        try:
            return capture_provider(claude_command(task),prompt,env,writer)
        finally:
            writer.close()
    if kind == "fixture":
        writer = make_event_spool(task)
        import uuid
        for item in task.get("inputs", []):
            identifier = item["id"]
            if str(uuid.UUID(identifier)) != identifier or item["path"] != "/inputs/" + identifier:
                raise ValueError("Invalid synthetic input reference")
            source_fd = os.open(item["path"], os.O_RDONLY | os.O_NOFOLLOW)
            target_fd = os.open("/workspace/input-" + identifier + ".bin", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o660)
            with os.fdopen(source_fd, "rb") as source, os.fdopen(target_fd, "wb") as target:
                copied = 0
                while chunk := source.read(65536):
                    copied += len(chunk)
                    if copied > 100 * 1024 * 1024:
                        raise ValueError("Synthetic input exceeds byte cap")
                    target.write(chunk)
        # This adapter is installed only for controlled synthetic acceptance tests.
        if task.get("fixture_operation") == "fix_booking":
            Path("booking.py").write_text("def valid_date(value):\n    from datetime import date\n    try:\n        date.fromisoformat(value)\n        return True\n    except (ValueError, TypeError):\n        return False\n")
            emit_provider_record({"type": "assistant", "message": {"content": [{"type": "text", "text": "Synthetic fixture updated booking.py; this is not an LLM result."}]}},writer)
        elif task.get("fixture_operation") == "report":
            Path("report.md").write_text("# Fixture report\n\nCreated by deterministic test adapter, not an LLM.\n")
        elif task.get("fixture_operation") == "wait":
            import time
            time.sleep(120)
        else:
            raise ValueError("Unknown fixture operation")
        emit_provider_record({"type": "result", "is_error": False, "result": "Synthetic fixture complete"},writer)
        writer.close()
        return 0
    if kind == "verify":
        checks = [run_verification_check(check) for check in task["checks"]]
        print(json.dumps({"type": "verification", "checks": checks}))
        return 0
    raise ValueError("Unsupported adapter")


if __name__ == "__main__":
    sys.exit(main())
