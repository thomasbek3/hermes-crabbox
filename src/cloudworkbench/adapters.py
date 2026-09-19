"""Provider capability gates and bounded structured event interpretation."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


class AdapterError(ValueError):
    pass


def claude_command(task: dict[str, Any]) -> list[str]:
    command = [
        "claude", "--print", "--verbose", "--output-format", "stream-json",
        "--safe-mode", "--setting-sources", "", "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "bypassPermissions",
        "--permission-prompts", "none", "--tools", "Bash,Read,Write,Edit,Glob,Grep",
    ]
    model = task.get("model")
    if model:
        if not isinstance(model, str) or model.startswith("-") or len(model) > 150 or any(c.isspace() for c in model):
            raise AdapterError("Invalid model identifier")
        command.extend(["--model", model])
    # Native continuation is disabled until the live layout/resume probe passes.
    command.append("--no-session-persistence")
    return command


def capabilities(token_path: Path | None, enabled: bool = False) -> dict:
    configured = bool(token_path and token_path.is_file())
    return {
        "claude": {
            "enabled": configured and enabled,
            "authentication": "dedicated_subscription_token" if configured else "not_configured",
            "native_resume": "unsupported",
            "continuation": "reconstructed_workspace",
            "live_delivery": "unsupported",
            "structured_events": "supported",
            "readiness_reason": None if configured and enabled else "Provider live qualification pending",
        },
        "codex": {"enabled": False, "readiness_reason": "Adapter qualification pending"},
        "hermes": {"enabled": False, "readiness_reason": "Adapter qualification pending; bridge excluded"},
    }


def bounded_text(value, limit=30000):
    return str(value).encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def bounded_metadata(value, limit=8000):
    try:
        return value if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= limit else None
    except (ValueError,TypeError,RecursionError):
        return None


FAILURE_CODES = frozenset({'auth_missing', 'auth_invalid', 'provider_auth_rejected', 'rate_limited'})
FAILURE_BASES = frozenset({'credential_file', 'assistant.error', 'result.api_error_status'})
FAILURE_PAIRINGS = {'auth_missing':{'credential_file'},'auth_invalid':{'credential_file'},
                    'provider_auth_rejected':{'assistant.error','result.api_error_status'},
                    'rate_limited':{'assistant.error','result.api_error_status'}}


def classify_cli_failure(item):
    """Classify only explicit CLI protocol fields, never model-authored prose."""
    if item.get('type') == 'assistant':
        code = {'authentication_failed':'provider_auth_rejected', 'rate_limit':'rate_limited'}.get(item.get('error')) if isinstance(item.get('error'),str) else None
        if code:
            return {'code':code, 'basis':'assistant.error'}
    if item.get('type') == 'result' and item.get('is_error') is True:
        status = item.get('api_error_status')
        code = {401:'provider_auth_rejected',429:'rate_limited'}.get(status) if type(status) is int else None
        if code:
            return {'code':code, 'basis':'result.api_error_status'}
    return None


def usage_status(usage, *, missing_result=False):
    if isinstance(usage,dict) and usage and bounded_metadata(usage) is not None:
        return {'state':'reported','reason':None}
    return {'state':'unknown','reason':'missing_result' if missing_result else 'provider_did_not_report'}


def read_credential(path):
    """Read only a bounded regular credential file without following links."""
    import os
    import stat
    if path is None:
        return b'', 'auth_missing'
    fd = None
    try:
        path = Path(path)
        parent = _open_directory_nofollow(path.parent)
        try:
            fd = os.open(path.name,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=parent)
        finally:
            os.close(parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 4096 or info.st_mode & 0o027:
            return b'', 'auth_invalid'
        raw = os.read(fd,4097)
        token = raw.strip()
        if len(raw) > 4096 or not token.startswith(b'sk-ant-oat') or any(c <= 32 or c >= 127 for c in token):
            return b'', 'auth_invalid'
        return token, None
    except FileNotFoundError:
        return b'', 'auth_missing'
    except (OSError,ValueError):
        return b'', 'auth_invalid'
    finally:
        if fd is not None:
            os.close(fd)


def parse_events(data: bytes, forbidden: tuple[bytes, ...] = ()) -> list[dict]:
    """Only return observable messages/results; never trust model verification claims."""
    for value in forbidden:
        if value:
            data = data.replace(value, b"[REDACTED]")
    events = []
    for line in data.splitlines():
        if len(line) > 1024 * 1024:
            events.append({"type": "error", "payload": {"reason": "oversized_provider_event"}})
            continue
        try:
            item = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(item, dict):
            continue
        item = redact_public(item, forbidden)
        kind = item.get("type")
        failure = classify_cli_failure(item)
        if failure and kind == "assistant":
            events.append({"type":"error","payload":{"reason":failure["code"],"classification_basis":failure["basis"],"provenance":"worker_reported"}})
        if kind == "assistant":
            message = item.get("message", {})
            for block in message.get("content", []) if isinstance(message, dict) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    events.append({"type": "assistant.message", "payload": {"text": bounded_text(block.get("text", ""))}})
                elif block.get("type") == "tool_use":
                    events.append({"type": "tool.started", "payload": {"id": bounded_text(block.get("id"),256), "name": bounded_text(block.get("name"),256), "provenance": "worker_reported"}})
        elif kind == "result":
            events.append({"type": "adapter.result", "payload": {
                "is_error": bool(item.get("is_error")), "subtype": bounded_text(item.get("subtype"),128),
                "summary": bounded_text(item.get("result", "")),
                "permission_denials": bool(item.get("permission_denials")),
                "usage": bounded_metadata(item.get("usage")), "model_usage": bounded_metadata(item.get("modelUsage")),
                "usage_status": usage_status(item.get("usage")),
                "failure_code": failure["code"] if failure else None, "failure_basis": failure["basis"] if failure else None,
                "api_error_status": item.get("api_error_status") if type(item.get("api_error_status")) is int and 100 <= item["api_error_status"] <= 599 else None,
                "reported_cost_usd": item.get("total_cost_usd") if isinstance(item.get("total_cost_usd"), (int,float)) and math.isfinite(item["total_cost_usd"]) else None,
                "provenance": "worker_reported",
            }})
        elif kind == "system" and item.get("subtype") == "init":
            events.append({"type": "adapter.provenance", "payload": {"model": bounded_text(item.get("model"),256), "cli_version": bounded_text(item.get("claude_code_version"),256), "session_id": bounded_text(item.get("session_id"),256), "provenance": "worker_reported"}})
    return events


class EventSpoolError(AdapterError):
    def __init__(self, code):
        self.code = code
        self.operation = 'event_capture'
        super().__init__(code)


EVENT_LINE_BYTES = 65536
EVENT_TOTAL_BYTES = 16 * 1024**2
RAW_LINE_BYTES = 1024**2
RAW_TOTAL_BYTES = 64 * 1024**2
PUBLIC_EVENT_TYPES = frozenset({'assistant.message', 'tool.started', 'adapter.result', 'adapter.provenance', 'error'})


def redact_public(value, forbidden=()):
    """Redact decoded JSON strings too, including escaped provider output."""
    if isinstance(value, str):
        for secret in forbidden:
            if secret:
                value = value.replace(secret.decode('utf-8', errors='replace'), '[REDACTED]')
        return value
    if isinstance(value, list):
        return [redact_public(item, forbidden) for item in value]
    if isinstance(value, dict):
        return {redact_public(str(key), forbidden): redact_public(item, forbidden) for key,item in value.items()}
    return value


def public_spool_event(event, forbidden=()):
    """A worker cannot promote spool text into trusted controller events."""
    if not isinstance(event, dict) or event.get('type') not in PUBLIC_EVENT_TYPES or not isinstance(event.get('payload'), dict):
        raise EventSpoolError('event_spool_invalid_record')
    event = redact_public(event, forbidden)
    kind, payload = event['type'], event['payload']
    if kind == 'assistant.message':
        payload = {'text': bounded_text(payload.get('text', ''))}
    elif kind == 'tool.started':
        payload = {'id': bounded_text(payload.get('id'),256), 'name': bounded_text(payload.get('name'),256), 'provenance':'worker_reported'}
    elif kind == 'adapter.provenance':
        payload = {key:bounded_text(payload.get(key),256) for key in ('model','cli_version','session_id')}
        payload['provenance'] = 'worker_reported'
    elif kind == 'adapter.result':
        native_session_id = payload.get('native_session_id')
        missing_result = payload.get('usage_status') == {'state':'unknown','reason':'missing_result'}
        failure_code = payload.get('failure_code') if isinstance(payload.get('failure_code'),str) and payload.get('failure_code') in FAILURE_CODES and isinstance(payload.get('failure_basis'),str) and payload.get('failure_basis') in FAILURE_PAIRINGS[payload['failure_code']] and payload.get('is_error') is True else None
        failure_basis = payload.get('failure_basis') if failure_code else None
        payload = {'is_error':bool(payload.get('is_error')), 'subtype':bounded_text(payload.get('subtype'),128),
                   'summary':bounded_text(payload.get('summary','')), 'permission_denials':bool(payload.get('permission_denials')),
                   'usage':bounded_metadata(payload.get('usage')), 'model_usage':bounded_metadata(payload.get('model_usage')),
                   'usage_status':usage_status(payload.get('usage'),missing_result=missing_result),
                   'failure_code':failure_code,'failure_basis':failure_basis,
                   'api_error_status':payload.get('api_error_status') if type(payload.get('api_error_status')) is int and 100 <= payload['api_error_status'] <= 599 else None,
                   'reported_cost_usd':payload.get('reported_cost_usd') if isinstance(payload.get('reported_cost_usd'),(int,float)) and math.isfinite(payload['reported_cost_usd']) else None,
                   'provenance':'worker_reported'}
        if (isinstance(native_session_id, str)
                and re.fullmatch(r'[0-9]{8}_[0-9]{6}_[0-9a-f]{6}', native_session_id)):
            payload['native_session_id'] = native_session_id
    else:
        payload = {'reason':bounded_text(payload.get('reason','provider_output_error'),256),
                   'classification_basis':payload.get('classification_basis') if isinstance(payload.get('classification_basis'),str) and payload.get('classification_basis') in FAILURE_BASES else None,
                   'provenance':'worker_reported'}
    return {'type':kind,'payload':payload}


def _open_directory_nofollow(path):
    import os
    absolute = Path(os.path.abspath(path))
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class EventSpoolWriter:
    """Append only bounded, redacted public events to a unique native-state file."""
    def __init__(self, path, forbidden=(), max_bytes=EVENT_TOTAL_BYTES):
        import os
        self.forbidden, self.max_bytes, self.size = forbidden, max_bytes, 0
        path = Path(path)
        parent = _open_directory_nofollow(path.parent)
        try:
            self.fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660, dir_fd=parent)
        finally:
            os.close(parent)

    def append(self, event):
        import os
        event = public_spool_event(event,self.forbidden)
        data = (json.dumps(event,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n').encode()
        if len(data) > EVENT_LINE_BYTES:
            raise EventSpoolError('event_spool_line_limit')
        reserve = 1024 if self.max_bytes >= 4096 and event['type'] != 'error' else 0
        if self.size + len(data) > self.max_bytes - reserve:
            raise EventSpoolError('event_spool_total_limit')
        view = memoryview(data)
        while view:
            written = os.write(self.fd,view)
            if written <= 0:
                raise EventSpoolError('event_spool_write_failed')
            view = view[written:]
        os.fsync(self.fd)
        self.size += len(data)

    def close(self):
        import os
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def read_event_spool(path, cursor=None, *, forbidden=(), final=False, max_bytes=EVENT_TOTAL_BYTES, batch_bytes=1024**2):
    """Return one bounded complete-line batch; replay is keyed by byte offset."""
    import hashlib
    import os
    import stat
    if not EVENT_LINE_BYTES <= batch_bytes <= 4 * 1024**2:
        raise ValueError('Invalid event spool batch limit')
    cursor = cursor or {}
    path = Path(path)
    try:
        parent = _open_directory_nofollow(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
    except FileNotFoundError:
        if cursor or final:
            raise EventSpoolError('event_spool_missing')
        return {'events':[], 'cursor':None, 'complete':False}
    except OSError:
        raise EventSpoolError('event_spool_unsafe_path') from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EventSpoolError('event_spool_unsafe_file')
        if before.st_size > max_bytes:
            raise EventSpoolError('event_spool_total_limit')
        identity = [before.st_dev,before.st_ino]
        if cursor and cursor.get('identity') != identity:
            raise EventSpoolError('event_spool_replaced')
        offset = cursor.get('offset',0)
        if type(offset) is not int or offset < 0 or before.st_size < max(offset,cursor.get('seen_size',0)):
            raise EventSpoolError('event_spool_truncated')
        digest, remaining = hashlib.sha256(), offset
        while remaining:
            data = os.read(fd,min(65536,remaining))
            if not data:
                raise EventSpoolError('event_spool_truncated')
            digest.update(data)
            remaining -= len(data)
        if cursor and cursor.get('prefix_sha256') != digest.hexdigest():
            raise EventSpoolError('event_spool_prefix_changed')
        data = os.read(fd,min(batch_bytes,before.st_size-offset))
        boundary = data.rfind(b'\n') + 1
        trailing = data[boundary:]
        if len(trailing) >= EVENT_LINE_BYTES or (final and offset+len(data)==before.st_size and trailing):
            raise EventSpoolError('event_spool_incomplete_line')
        records, position = [], offset
        for line in data[:boundary].splitlines(keepends=True):
            if len(line)>EVENT_LINE_BYTES:
                raise EventSpoolError('event_spool_line_limit')
            try:
                event = public_spool_event(json.loads(line),forbidden)
            except (ValueError,UnicodeError,TypeError,RecursionError):
                raise EventSpoolError('event_spool_invalid_record') from None
            records.append({'offset':position,'event':event})
            position += len(line)
        after = os.fstat(fd)
        if after.st_size < before.st_size:
            raise EventSpoolError('event_spool_truncated')
        digest.update(data[:boundary])
        next_cursor = {'identity':identity,'offset':position,'seen_size':before.st_size,'prefix_sha256':digest.hexdigest()}
        return {'events':records,'cursor':next_cursor,'complete':position==before.st_size}
    finally:
        os.close(fd)
