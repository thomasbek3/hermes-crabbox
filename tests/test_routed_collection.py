import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from cloudworkbench.adapters import public_spool_event
from cloudworkbench.routed_collection import (
    CollectionError, CollectionNotReady, CollectionLimits, collect_execution,
)

HASH = 'a' * 64


def event(kind='adapter.result', **payload):
    return public_spool_event({'type': kind, 'payload': payload})


def encode(value):
    return json.dumps(value).encode()


class Reader:
    def __init__(self, result=None, events=None):
        self.result = result or {'status': 'completed', 'failure': None, 'exit_code': 0,
            'stderr_bytes': 0, 'provenance': 'worker_reported', 'outer_cleanup_required': True,
            'verification_pass': False, 'launch_receipt_sha256': HASH}
        self.files = {'result': encode(self.result),
                      'events': b''.join(encode(x) + b'\n' for x in (events or [event(summary='done')]))}
        self.calls = []
        self.alter = lambda value, call: value

    def __call__(self, *, name, offset, max_bytes):
        self.calls.append((name, offset, max_bytes))
        if name not in self.files:
            return {'present': False}
        raw = self.files[name]
        data = raw[offset:offset + max_bytes]
        value = {'present': True, 'offset': offset, 'next_offset': offset + len(data),
                 'inode': 12 if name == 'result' else 13, 'size': len(raw), 'mtime_ns': 99,
                 'data': data, 'provenance': 'worker_reported'}
        return self.alter(value, self.calls[-1])

    def run(self, **kwargs):
        return collect_execution(self, launch_receipt_sha256=HASH, **kwargs)


def test_completed_is_only_a_worker_observation_and_payload_copy():
    reader = Reader()
    value = reader.run()
    assert value.status == 'completed' and value.exit_code == 0
    assert value.provenance == 'worker_reported'
    assert value.outer_cleanup_required and not value.verification_pass
    assert value.result_evidence.sha256 == hashlib.sha256(reader.files['result']).hexdigest()
    assert value.events_evidence.sha256 == hashlib.sha256(reader.files['events']).hexdigest()
    assert value.result_json.encode('utf-8') == reader.files['result']
    assert value.events_jsonl.encode('utf-8') == reader.files['events']
    # Exact worker bytes survive caller removal for later controller publication.
    reader.files.clear()
    assert hashlib.sha256(value.result_json.encode()).hexdigest() == value.result_evidence.sha256
    assert hashlib.sha256(value.events_jsonl.encode()).hexdigest() == value.events_evidence.sha256
    payload = value.events[0].payload
    payload['is_error'] = True
    assert value.events[0].payload['is_error'] is False
    assert value.events[0].next_offset == len(value.events_jsonl.encode())
    assert [call[0] for call in reader.calls] == ['result', 'events', 'events', 'result']


def test_real_supervisor_records_are_accepted(tmp_path):
    from cloudworkbench.routed_caller import supervise_hermes
    code = "import json; print(json.dumps({'type':'system','subtype':'init','model':'fixture'})); print(json.dumps({'type':'result','exit_code':0,'text':'done'}))"
    plan = SimpleNamespace(argv=(sys.executable, '-c', code, '--run-budget', '10'), receipt_json='{}')
    assert supervise_hermes(plan, {'PATH': '/usr/bin:/bin'}, 'f' * 64, tmp_path, cwd=tmp_path) == 0
    reader = Reader()
    reader.files = {name: (tmp_path / filename).read_bytes() for name, filename in
                    [('events', 'events.jsonl'), ('result', 'result.json')]}
    observed = collect_execution(reader, launch_receipt_sha256=hashlib.sha256(b'{}').hexdigest())
    assert observed.status == 'completed' and observed.events[-1].payload['usage'] is None


def test_retained_records_preserve_worker_formatting_and_utf8():
    from dataclasses import asdict
    reader = Reader(events=[event(summary='caf\u00e9')])
    reader.files['result'] = json.dumps(reader.result, indent=2).encode() + b'\n'
    reader.files['events'] = json.dumps(event(summary='caf\u00e9'), ensure_ascii=False).encode() + b'\n'
    observed = reader.run()
    assert observed.result_json.encode() == reader.files['result']
    assert observed.events_jsonl.encode() == reader.files['events']
    # Existing qualification receipts serialize dataclasses; bytes stay JSON-safe.
    restored = json.loads(json.dumps(asdict(observed)))
    assert restored['events_jsonl'].encode() == reader.files['events']


@pytest.mark.parametrize('name', ['result', 'events'])
def test_absence_is_distinct_not_ready(name):
    reader = Reader(); del reader.files[name]
    with pytest.raises(CollectionNotReady, match='absent'):
        reader.run()


@pytest.mark.parametrize('key,value', [('offset', True), ('next_offset', 0), ('inode', 0),
    ('size', -1), ('mtime_ns', False), ('data', 'unsafe'), ('provenance', 'controller'),
    ('present', 1), ('unexpected', True)])
def test_bad_read_envelope_is_refused(key, value):
    reader = Reader(); reader.alter = lambda item, _: {**item, key: value}
    with pytest.raises(CollectionError, match='invalid_collection_record'):
        reader.run()


@pytest.mark.parametrize('field,value', [('verification_pass', True), ('outer_cleanup_required', False),
    ('provenance', 'controller'), ('launch_receipt_sha256', 'b' * 64), ('exit_code', True),
    ('stderr_bytes', -1), ('status', []), ('failure', 'provider-secret'), ('extra', 'value')])
def test_bad_result_schema_or_binding_refused(field, value):
    reader = Reader(); reader.result[field] = value; reader.files['result'] = encode(reader.result)
    with pytest.raises(CollectionError):
        reader.run()


@pytest.mark.parametrize('raw', [b'{"status":1,"status":2}', b'{"a":NaN}', b'{"a":Infinity}', b'\xff', b'[]', b'{}'])
def test_invalid_json_result_refused(raw):
    reader = Reader(); reader.files['result'] = raw
    with pytest.raises(CollectionError):
        reader.run()


@pytest.mark.parametrize('kind', ['partial', 'empty', 'duplicate', 'reserved', 'coerced_bool', 'extra_field', 'error', 'wrong_order'])
def test_event_terminal_failures(kind):
    reader = Reader()
    if kind == 'partial': reader.files['events'] = reader.files['events'].rstrip(b'\n')
    elif kind == 'empty': reader.files['events'] = b''
    elif kind == 'duplicate': reader.files['events'] *= 2
    elif kind == 'reserved': reader.files['events'] = encode({'type':'controller.resource.sample', 'payload':{}}) + b'\n'
    elif kind == 'coerced_bool':
        value = event(); value['payload']['is_error'] = 0
        reader.files['events'] = encode(value) + b'\n'
    elif kind == 'extra_field':
        value = event(); value['payload']['verification_pass'] = True
        reader.files['events'] = encode(value) + b'\n'
    elif kind == 'error': reader.files['events'] = encode(event('error')) + b'\n' + reader.files['events']
    else: reader.files['events'] += encode(event('assistant.message', text='late')) + b'\n'
    with pytest.raises(CollectionError): reader.run()


def test_explicit_failed_observation_does_not_require_success_result():
    reader = Reader(events=[event('error', reason='fixture')])
    reader.result.update(status='failed', failure='caller_execution_failed', exit_code=-9)
    reader.files['result'] = encode(reader.result)
    assert reader.run().status == 'failed'


@pytest.mark.parametrize('field', ['inode', 'mtime_ns', 'size', 'bytes'])
def test_second_snapshot_drift_refused_even_same_size(field):
    reader = Reader()
    def change(value, call):
        if len(reader.calls) == 3:
            if field == 'bytes': value['data'] = value['data'].replace(b'done', b'evil')
            else: value[field] += 1
        return value
    reader.alter = change
    with pytest.raises(CollectionError): reader.run()


def test_multichunk_offsets_and_stable_identity():
    reader = Reader(events=[event('assistant.message', text='x' * 29000)] * 40 + [event()])
    observed = reader.run()
    assert len(observed.events) == 41
    assert [offset for name, offset, _ in reader.calls if name == 'events'] == [0, 1024**2, 0, 1024**2]


def test_disappearing_second_snapshot_is_not_transient_not_ready():
    reader = Reader()
    reader.alter = lambda value, _: {'present': False} if len(reader.calls) == 3 else value
    with pytest.raises(CollectionError, match='worker_file_changed') as raised: reader.run()
    assert not isinstance(raised.value, CollectionNotReady)


def test_inode_change_between_chunks_refused():
    reader = Reader(events=[event('assistant.message', text='x' * 29000)] * 40 + [event()])
    reader.alter = lambda value, call: {**value, 'inode': 90} if call[1] else value
    with pytest.raises(CollectionError, match='worker_file_changed'): reader.run()


def test_short_read_without_eof_refused():
    reader = Reader()
    reader.alter = lambda value, _: {**value, 'data': value['data'][:-1], 'next_offset': value['next_offset']-1}
    with pytest.raises(CollectionError, match='truncated'): reader.run()


@pytest.mark.parametrize('limits', [CollectionLimits(max_events=1), CollectionLimits(max_event_bytes=10)])
def test_limits_refused(limits):
    reader = Reader(events=[event('assistant.message', text='first'), event()])
    with pytest.raises(CollectionError, match='limit'): reader.run(limits=limits)


def test_cancellation_exception_from_bound_reader_propagates():
    reader = Reader()
    def change(value, call):
        if call[0] == 'events': raise RuntimeError('authority_revoked')
        return value
    reader.alter = change
    with pytest.raises(RuntimeError, match='authority_revoked'): reader.run()
    assert len(reader.calls) == 2


def test_overall_deadline_checked_after_rpc(monkeypatch):
    import cloudworkbench.routed_collection as module
    ticks = iter([0, 0, 31])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(CollectionError, match='deadline'): Reader().run()


@pytest.mark.parametrize('kwargs', [{'max_events':True}, {'max_event_bytes':17*1024**2},
    {'timeout_seconds':float('nan')}, {'timeout_seconds':True}, {'max_events':0}])
def test_limits_configuration_is_validated(kwargs):
    with pytest.raises(CollectionError): CollectionLimits(**kwargs)


def test_eight_mib_scales_with_one_mib_reads_and_injected_rpc_clock():
    reader = Reader(events=[event('assistant.message', text='x' * 29000)] * 290 + [event()])
    now = [0.0]
    def timed_reader(**kwargs):
        now[0] += .1
        return reader(**kwargs)
    observed = collect_execution(timed_reader, launch_receipt_sha256=HASH,
        limits=CollectionLimits(timeout_seconds=3), clock=lambda: now[0])
    assert 8 * 1024**2 < observed.events_evidence.size < 9 * 1024**2
    assert len(reader.calls) == 20 and now[0] < 3
    assert max(call[2] for call in reader.calls if call[0] == 'events') == 1024**2
    assert max(call[2] for call in reader.calls if call[0] == 'result') == 65536


def test_injected_deadline_remains_failure_without_restart_or_not_ready():
    reader = Reader()
    now = [0.0]
    def slow(**kwargs):
        now[0] += 2
        return reader(**kwargs)
    with pytest.raises(CollectionError, match='collection_deadline') as raised:
        collect_execution(slow, launch_receipt_sha256=HASH,
            limits=CollectionLimits(timeout_seconds=1), clock=lambda: now[0])
    assert not isinstance(raised.value, CollectionNotReady)
    assert len(reader.calls) == 1
    assert reader.files['result'] and reader.files['events']
