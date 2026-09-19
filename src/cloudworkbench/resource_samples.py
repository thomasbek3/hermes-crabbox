"""Bounded Docker observations; maxima are sampled, not lifetime resource peaks."""
from __future__ import annotations

from collections import deque
import copy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
import re
import selectors
import signal
import subprocess
import time

from .runtime import Runtime, RuntimeError as DockerRuntimeError

MAX_OUTPUT_BYTES = 4096
UNKNOWN_REASONS = frozenset({
    'invalid_identity', 'invalid_configuration', 'deadline_exceeded',
    'ownership_unconfirmed', 'runtime_missing', 'runtime_not_running',
    'stats_unavailable', 'stats_failed', 'stats_output_limit',
    'stats_empty', 'stats_malformed', 'stats_identity_mismatch',
    'inspection_output_limit', 'runtime_unavailable',
})
_ID = re.compile(r'[a-f0-9]{64}\Z')
_NUMBER = r'[0-9]{1,16}(?:\.[0-9]{1,8})?'
_MEMORY = re.compile(r'(' + _NUMBER + r')\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\Z')
_UNITS = {'B': 1, 'kB': 1000, 'KB': 1000, 'MB': 1000**2,
          'GB': 1000**3, 'TB': 1000**4, 'KiB': 1024,
          'MiB': 1024**2, 'GiB': 1024**3, 'TiB': 1024**4}
_METRICS = ('cpu_percent', 'memory_bytes_approx', 'memory_limit_bytes_approx', 'pids')


class SampleError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in UNKNOWN_REASONS else 'stats_malformed'
        super().__init__(self.reason)


def _memory_bytes(value):
    match = _MEMORY.fullmatch(value.strip())
    if not match:
        raise SampleError('stats_malformed')
    result = int(Decimal(match[1]) * _UNITS[match[2]])
    if not 0 < result <= 2**60:
        raise SampleError('stats_malformed')
    return result


def parse_stats(raw: bytes, runtime_id: str) -> dict:
    """Parse one full-ID JSON stats row; never return raw names or daemon text."""
    if not isinstance(runtime_id, str) or not _ID.fullmatch(runtime_id):
        raise SampleError('invalid_identity')
    if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT_BYTES:
        raise SampleError('stats_output_limit')
    if not raw.strip():
        raise SampleError('stats_empty')
    try:
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise SampleError('stats_malformed')
                result[key] = value
            return result
        row = json.loads(raw.decode('utf8'), object_pairs_hook=pairs)
        if not isinstance(row, dict):
            raise SampleError('stats_malformed')
        if row.get('ID') != runtime_id:
            raise SampleError('stats_identity_mismatch')
        cpu, memory, pids = (row.get(k) for k in ('CPUPerc', 'MemUsage', 'PIDs'))
        if not all(isinstance(v, str) for v in (cpu, memory, pids)):
            raise SampleError('stats_malformed')
        if not re.fullmatch(_NUMBER + '%', cpu) or not re.fullmatch(r'[0-9]{1,10}', pids):
            raise SampleError('stats_malformed')
        cpu = float(Decimal(cpu[:-1]))
        pids = int(pids)
        if not math.isfinite(cpu) or not 0 <= cpu <= 100000 or not 1 <= pids <= 2**31-1:
            raise SampleError('stats_malformed')
        parts = memory.split('/')
        if len(parts) != 2:
            raise SampleError('stats_malformed')
        return {'cpu_percent': cpu, 'memory_bytes_approx': _memory_bytes(parts[0]),
                'memory_limit_bytes_approx': _memory_bytes(parts[1]), 'pids': pids}
    except (UnicodeError, json.JSONDecodeError, TypeError, ArithmeticError, RecursionError):
        raise SampleError('stats_malformed') from None


class _DeadlineRuntime:
    def __init__(self, runtime, deadline):
        # Runtime already validated these fields; no repeated filesystem/DNS work.
        self._deadline = deadline
        self.output_budget = [MAX_OUTPUT_BYTES]
        for key in ('docker', 'owner', 'image', 'uid', 'gid', 'root'):
            setattr(self, key, getattr(runtime, key))

    def _run(self, args):
        try:
            return _capture(self.docker, args, self._deadline, self.output_budget).decode('utf8').strip()
        except UnicodeError:
            raise SampleError('ownership_unconfirmed') from None
        except SampleError as exc:
            reason = {'stats_failed': 'ownership_unconfirmed', 'stats_unavailable': 'runtime_unavailable',
                      'stats_output_limit': 'inspection_output_limit'}.get(exc.reason, exc.reason)
            raise SampleError(reason) from None

    def status(self, runtime_id, expected_generation):
        labels = lambda key: '{{json (index .Config.Labels "io.cloudworkbench.' + key + '")}}'
        template = ('{"id":{{json .Id}},"managed":' + labels('managed') + ',"owner":' + labels('owner') +
                    ',"attempt":' + labels('attempt') + ',"generation":' + labels('generation') +
                    ',"running":{{json .State.Running}}}')
        try:
            row = json.loads(self._run(['inspect', '--type', 'container', '--format', template, runtime_id]))
            if not isinstance(row, dict) or row.get('id') != runtime_id:
                raise SampleError('ownership_unconfirmed')
            if row.get('managed') != 'true' or row.get('owner') != self.owner:
                return {'state': 'missing'}
            if (not isinstance(row.get('attempt'), str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', row['attempt'])
                    or row.get('generation') != str(expected_generation) or type(row.get('running')) is not bool):
                raise SampleError('ownership_unconfirmed')
            return {'state': 'running' if row['running'] else 'exited'}
        except (ValueError, TypeError) as exc:
            if isinstance(exc, SampleError):
                raise
            raise SampleError('ownership_unconfirmed') from None


def _capture(docker, args, deadline, budget):
    process = None
    io_deadline = deadline - .025  # Reserve cleanup/reaping inside the same total budget.
    try:
        if time.monotonic() >= io_deadline:
            raise SampleError('deadline_exceeded')
        process = subprocess.Popen(
            [docker, *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
        )
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = io_deadline - time.monotonic()
                if remaining <= 0:
                    raise SampleError('deadline_exceeded')
                if not selector.select(min(remaining, .1)):
                    continue
                chunk = os.read(process.stdout.fileno(), min(1024, budget[0] + 1))
                if not chunk:
                    break
                data.extend(chunk)
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    raise SampleError('stats_output_limit')
        remaining = io_deadline - time.monotonic()
        if remaining <= 0:
            raise SampleError('deadline_exceeded')
        if process.wait(timeout=remaining):
            raise SampleError('stats_failed')
        return bytes(data)
    except subprocess.TimeoutExpired:
        raise SampleError('deadline_exceeded') from None
    except OSError:
        raise SampleError('stats_unavailable') from None
    finally:
        if process is not None:
            # Terminate only this observation client's group, never the container.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass  # A pending kill can defer subprocess reaping; preserve the caller deadline.
            if process.stdout is not None:
                process.stdout.close()


def _stats(docker, runtime_id, deadline, budget=None):
    return _capture(docker, ['stats', '--no-stream', '--no-trunc', '--format', '{{json .}}', runtime_id],
                    deadline, [MAX_OUTPUT_BYTES] if budget is None else budget)


def sample_runtime(runtime: Runtime, runtime_id: str, *, expected_generation: int,
                   timeout_seconds: float = 3) -> dict:
    """Read exact ownership before and after stats, sharing one bounded deadline."""
    started = time.monotonic()
    record = {'sampled_at': datetime.now(timezone.utc).isoformat(),
              'status': 'unknown', 'reason': None, 'provenance': 'docker_cli_observed',
              'runtime_id': None, 'generation': None, 'runtime_owner': None,
              'source_policy_sha256': None, **{key: None for key in _METRICS}}
    try:
        if not isinstance(runtime_id, str) or not _ID.fullmatch(runtime_id) or type(expected_generation) is not int or not 1 <= expected_generation <= 2**31-1:
            raise SampleError('invalid_identity')
        record.update(runtime_id=runtime_id, generation=expected_generation)
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not .1 <= timeout_seconds <= 3:
            raise SampleError('invalid_configuration')
        # Separate observer snapshots trusted Runtime fields without live mutations.
        observer = _DeadlineRuntime(runtime, started + timeout_seconds)
        policy = {'owner': observer.owner, 'image': observer.image, 'uid': observer.uid,
                  'gid': observer.gid, 'root': str(observer.root), 'docker': observer.docker}
        record.update(runtime_owner=observer.owner, source_policy_sha256=hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        def check_running():
            state = observer.status(runtime_id, expected_generation=expected_generation)['state']
            if state == 'missing':
                raise SampleError('runtime_missing')
            if state != 'running':
                raise SampleError('runtime_not_running')
        check_running()
        values = parse_stats(_stats(observer.docker, runtime_id, started + timeout_seconds, observer.output_budget), runtime_id)
        check_running()
        if time.monotonic() >= started + timeout_seconds:
            raise SampleError('deadline_exceeded')
        record.update(status='observed', **values)
    except SampleError as exc:
        record['reason'] = exc.reason
    except DockerRuntimeError as exc:
        record['reason'] = 'deadline_exceeded' if exc.code == 'docker_timeout' else 'ownership_unconfirmed'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        record['reason'] = 'invalid_configuration'
    record['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
    return record


class SampleWindow:
    """An in-memory bounded window; each restart begins a new observation window."""
    def __init__(self, max_samples=60):
        if type(max_samples) is not int or not 1 <= max_samples <= 120:
            raise ValueError('sample window must contain between 1 and 120 observations')
        self._samples = deque(maxlen=max_samples)
        self.total_samples = 0
        self._identity = None

    def add(self, sample):
        if not isinstance(sample, dict) or sample.get('status') not in ('observed', 'unknown'):
            raise ValueError('invalid resource sample')
        # Keep only public fixed fields, never caller-supplied additional strings.
        safe = {k: sample.get(k) for k in ('sampled_at', 'status', 'reason', 'runtime_id',
                 'generation', 'runtime_owner', 'source_policy_sha256', 'elapsed_ms', *_METRICS)}
        rid, generation = safe['runtime_id'], safe['generation']
        if not isinstance(rid, str) or not _ID.fullmatch(rid) or type(generation) is not int or generation < 1:
            raise ValueError('sample window requires an exact runtime identity')
        if not isinstance(safe['runtime_owner'], str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', safe['runtime_owner']) or not isinstance(safe['source_policy_sha256'], str) or not _ID.fullmatch(safe['source_policy_sha256']):
            raise ValueError('invalid sample policy')
        identity = (rid, generation, safe['runtime_owner'], safe['source_policy_sha256'])
        if self._identity is not None and identity != self._identity:
            raise ValueError('sample window cannot mix runtimes, generations or policies')
        if safe['status'] == 'unknown':
            if safe['reason'] not in UNKNOWN_REASONS:
                raise ValueError('invalid unknown reason')
            for key in _METRICS:
                safe[key] = None
        else:
            if safe['reason'] is not None or any(type(safe[k]) not in (int, float) or not math.isfinite(safe[k]) or safe[k] < 0 for k in _METRICS):
                raise ValueError('invalid observed metrics')
            if safe['cpu_percent'] > 100000 or type(safe['pids']) is not int or not 1 <= safe['pids'] <= 2**31-1 or any(type(safe[k]) is not int or not 1 <= safe[k] <= 2**60 for k in ('memory_bytes_approx', 'memory_limit_bytes_approx')):
                raise ValueError('invalid observed metric bounds')
        if not isinstance(safe['sampled_at'], str) or len(safe['sampled_at']) > 40:
            raise ValueError('invalid sample timestamp')
        if datetime.fromisoformat(safe['sampled_at']).utcoffset() is None:
            raise ValueError('sample timestamp must have a timezone')
        if type(safe['elapsed_ms']) not in (int, float) or not math.isfinite(safe['elapsed_ms']) or not 0 <= safe['elapsed_ms'] <= 3500:
            raise ValueError('invalid sample duration')
        self._identity = identity
        self.total_samples += 1
        self._samples.append(safe)

    def summary(self):
        samples = list(self._samples)
        observed = [s for s in samples if s['status'] == 'observed']
        return {'status': 'observed' if observed else 'unknown',
                'reason': None if observed else 'no_valid_samples',
                'provenance': 'docker_cli_observed', 'scope': 'retained_sample_window',
                'memory_semantics': 'Linux Docker CLI cache-adjusted, rounded display units converted to approximate bytes',
                'pids_semantics': 'processes_and_kernel_threads', 'true_peak': False,
                'total_samples': self.total_samples, 'retained_samples': len(samples),
                'dropped_samples': self.total_samples - len(samples),
                'observed_samples': len(observed), 'unknown_samples': len(samples) - len(observed),
                'observed_max': {key: max(s[key] for s in observed) if observed else None for key in _METRICS},
                'samples': copy.deepcopy(samples)}
