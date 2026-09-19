"""Bounded caller observations. Worker records never authorize verification or cleanup."""
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import time

from .adapters import public_spool_event, EventSpoolError


class CollectionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CollectionNotReady(CollectionError):
    pass


@dataclass(frozen=True)
class CollectionLimits:
    max_event_bytes: int = 16 * 1024**2
    max_events: int = 10000
    timeout_seconds: float = 30

    def __post_init__(self):
        if (type(self.max_event_bytes) is not int or not 1 <= self.max_event_bytes <= 16 * 1024**2
                or type(self.max_events) is not int or not 1 <= self.max_events <= 10000
                or type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120):
            raise CollectionError('invalid_collection_limits')


@dataclass(frozen=True)
class FileEvidence:
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class WorkerEvent:
    type: str
    payload_json: str
    byte_offset: int
    next_offset: int
    provenance: str = field(default='worker_reported', init=False)

    @property
    def payload(self):
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class ExecutionObservation:
    status: str
    failure: str | None
    exit_code: int | None
    stderr_bytes: int
    launch_receipt_sha256: str
    events: tuple[WorkerEvent, ...]
    result_evidence: FileEvidence
    events_evidence: FileEvidence
    result_json: str = field(default='', repr=False)
    events_jsonl: str = field(default='', repr=False)
    provenance: str = field(default='worker_reported', init=False)
    verification_pass: bool = field(default=False, init=False)
    outer_cleanup_required: bool = field(default=True, init=False)


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise CollectionError('malformed_worker_json') from None


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def _read(read_file, name, maximum, deadline, clock):
    offset, parts, identity = 0, [], None
    while True:
        if clock() >= deadline:
            raise CollectionError('collection_deadline')
        limit = min(1024**2 if name == 'events' else 65536, maximum - offset or 1)
        # Exceptions from the trusted authority-checking reader propagate; never
        # reinterpret revocation or an uncertain Docker RPC as worker completion.
        value = read_file(name=name, offset=offset, max_bytes=limit)
        if clock() >= deadline:
            raise CollectionError('collection_deadline')
        if type(value) is not dict or type(value.get('present')) is not bool:
            raise CollectionError('invalid_collection_record')
        if value == {'present': False}:
            if offset:
                raise CollectionError('worker_file_changed')
            raise CollectionNotReady('worker_' + name + '_absent')
        keys = {'present', 'offset', 'next_offset', 'inode', 'size', 'mtime_ns', 'data', 'provenance'}
        if set(value) != keys or value['present'] is not True or value['provenance'] != 'worker_reported':
            raise CollectionError('invalid_collection_record')
        for key in ('offset', 'next_offset', 'inode', 'size', 'mtime_ns'):
            if type(value[key]) is not int or value[key] < 0:
                raise CollectionError('invalid_collection_record')
        data = value['data']
        if (type(data) is not bytes or len(data) > limit or value['inode'] == 0
                or value['offset'] != offset or value['next_offset'] != offset + len(data)
                or value['next_offset'] > value['size']):
            raise CollectionError('invalid_collection_record')
        if value['size'] > maximum:
            raise CollectionError('worker_file_limit')
        current = value['inode'], value['size'], value['mtime_ns']
        if identity is not None and identity != current:
            raise CollectionError('worker_file_changed')
        identity = current
        if len(data) != min(limit, value['size'] - offset):
            raise CollectionError('worker_file_truncated')
        parts.append(data)
        offset += len(data)
        if offset == value['size']:
            raw = b''.join(parts)
            return raw, FileEvidence(*identity, hashlib.sha256(raw).hexdigest())


def _result(raw, expected):
    result = _json(raw)
    keys = {'status', 'failure', 'exit_code', 'stderr_bytes', 'provenance',
            'outer_cleanup_required', 'verification_pass', 'launch_receipt_sha256'}
    if (type(result) is not dict or set(result) != keys or result['provenance'] != 'worker_reported'
            or result['outer_cleanup_required'] is not True or result['verification_pass'] is not False
            or result['launch_receipt_sha256'] != expected
            or type(result['status']) is not str or result['status'] not in ('completed', 'failed')
            or type(result['stderr_bytes']) is not int or not 0 <= result['stderr_bytes'] <= 16 * 1024**2 + 65536
            or (result['exit_code'] is not None and (type(result['exit_code']) is not int
                                                   or not -255 <= result['exit_code'] <= 255))):
        raise CollectionError('invalid_worker_result')
    failures = {'hermes_execution_failed', 'caller_execution_failed', 'caller_stop_unconfirmed',
                'caller_wall_timeout', 'caller_stderr_limit'}
    if result['status'] == 'completed':
        if result['failure'] is not None or result['exit_code'] != 0:
            raise CollectionError('conflicting_worker_terminal')
    elif type(result['failure']) is not str or result['failure'] not in failures:
        raise CollectionError('invalid_worker_result')
    return result


def _events(raw, maximum):
    if not raw or not raw.endswith(b'\n'):
        raise CollectionError('worker_events_incomplete')
    events, offset = [], 0
    for line in raw.splitlines(keepends=True):
        if len(line) > 65536 or len(events) >= maximum:
            raise CollectionError('worker_events_limit')
        event = _json(line)
        if type(event) is not dict or set(event) != {'type', 'payload'} or type(event['type']) is not str:
            raise CollectionError('invalid_worker_event')
        try:
            normalized = public_spool_event(event)
            # Canonical equality keeps strict JSON booleans distinct from 0/1 and
            # refuses missing/extra fields, coercions and reserved controller types.
            if _canonical(normalized) != _canonical(event):
                raise CollectionError('invalid_worker_event')
        except (EventSpoolError, ValueError, TypeError, RecursionError, UnicodeError):
            raise CollectionError('invalid_worker_event') from None
        events.append(WorkerEvent(event['type'], _canonical(event['payload']), offset, offset + len(line)))
        offset += len(line)
    return tuple(events)


def collect_execution(read_file, *, launch_receipt_sha256, limits=CollectionLimits(), clock=None):
    """Read terminal records while init is alive, before stop/remove or verification.

    The reader closure binds exact spec/runtime identity and checks authority per
    RPC. Its own deadline must bound blocking calls; this deadline is cooperative.
    No observation authorizes a pass gate, revision promotion or physical cleanup.
    """
    if not isinstance(launch_receipt_sha256, str) or not re.fullmatch('[0-9a-f]{64}', launch_receipt_sha256):
        raise CollectionError('invalid_launch_binding')
    if type(limits) is not CollectionLimits or not callable(read_file):
        raise CollectionError('invalid_collection_configuration')
    clock = time.monotonic if clock is None else clock
    if not callable(clock):
        raise CollectionError('invalid_collection_configuration')
    deadline = clock() + limits.timeout_seconds
    result_raw, result_evidence = _read(read_file, 'result', 65536, deadline, clock)
    result = _result(result_raw, launch_receipt_sha256)
    events_raw, events_evidence = _read(read_file, 'events', limits.max_event_bytes, deadline, clock)
    events = _events(events_raw, limits.max_events)
    terminals = [event for event in events if event.type == 'adapter.result']
    if len(terminals) > 1:
        raise CollectionError('conflicting_worker_terminal')
    if result['status'] == 'completed' and (len(terminals) != 1 or terminals[0] != events[-1]
            or terminals[0].payload['is_error'] or terminals[0].payload['permission_denials']
            or any(event.type == 'error' for event in events)):
        raise CollectionError('conflicting_worker_terminal')
    # Result publication follows closing the event spool. Re-read both snapshots:
    # inode/stat metadata alone cannot detect same-size overwritten worker bytes.
    try:
        repeated_events = _read(read_file, 'events', limits.max_event_bytes, deadline, clock)
        repeated_result = _read(read_file, 'result', 65536, deadline, clock)
    except CollectionNotReady:
        raise CollectionError('worker_file_changed') from None
    if repeated_events != (events_raw, events_evidence) or repeated_result != (result_raw, result_evidence):
        raise CollectionError('worker_file_changed')
    return ExecutionObservation(result['status'], result['failure'], result['exit_code'],
                                result['stderr_bytes'], launch_receipt_sha256, events,
                                result_evidence, events_evidence,
                                result_raw.decode('utf-8'), events_raw.decode('utf-8'))
