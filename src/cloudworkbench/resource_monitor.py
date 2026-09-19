"""Optional async controller observations; never call Docker on the controller thread.

One process-global outstanding sample and >=10s between starts. Startup has a
10s cooldown to avoid restart bursts. offer/poll/close belong to one controller
thread. The Store integration supplies an ATOMIC publisher with this contract:

    publish(binding, event_type, payload, *, expected_generation, dedupe_key)
        -> positive event sequence, or None when stale/cancelled/terminal

In the same transaction, recheck session/id/generation/runtime/role/live state
then write the event through the Store controller event path. Plain existing
Store.append_event alone is insufficient: it permits terminal attempts. No
publication happens on sampler threads. The provider spool already rejects the
reserved controller.resource.sample type; preserve that allowlist boundary.

history_summary reads at most 120 fixed-size events with a bounded SQLite query
budget; call it at restart/finalization, not repeatedly each scheduler tick.
It does not mutate attempts/results or append a late completion sample.
"""
from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Callable, Mapping
import uuid

from .resource_samples import SampleWindow, sample_runtime

EVENT_TYPE = 'controller.resource.sample'
SOURCE = 'controller_resource_monitor_v1'
MAX_EVENT_BYTES = 4096
MAX_SAMPLES = 120
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_RUNTIME = re.compile(r'[a-f0-9]{64}\Z')
_EXECUTION_STATES = frozenset({'running', 'waiting_input', 'awaiting_approval', 'checkpointing', 'held'})


@dataclass(frozen=True)
class Binding:
    session_id: str
    attempt_id: str
    generation: int
    runtime_id: str
    role: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not _ID.fullmatch(value) for value in (self.session_id, self.attempt_id)):
            raise ValueError('invalid attempt identity')
        if type(self.generation) is not int or not 1 <= self.generation <= 2**31-1:
            raise ValueError('invalid generation')
        if not isinstance(self.runtime_id, str) or not _RUNTIME.fullmatch(self.runtime_id):
            raise ValueError('full runtime id required')
        if self.role not in {'execution', 'verifier'}:
            raise ValueError('invalid sample role')

    def matches(self, attempt: Mapping) -> bool:
        states = _EXECUTION_STATES if self.role == 'execution' else {'verifying'}
        return (isinstance(attempt, Mapping) and attempt.get('id') == self.attempt_id
                and attempt.get('session_id') == self.session_id
                and type(attempt.get('generation')) is int and attempt['generation'] == self.generation
                and attempt.get('runtime_id') == self.runtime_id and attempt.get('state') in states
                and not attempt.get('cancel_requested')
                and not (attempt.get('result') or {}).get('completion_intent'))


class _Admission:
    def __init__(self, clock=time.monotonic):
        self.lock = threading.Lock()
        self.pending = False
        self.next_allowed = clock() + 10

    def acquire(self, now, cadence):
        with self.lock:
            if self.pending:
                return 'busy'
            if now < self.next_allowed:
                return 'cadence'
            self.pending = True
            self.next_allowed = now + cadence
            return 'submitted'

    def release(self):
        with self.lock:
            self.pending = False


_ADMISSION = _Admission()
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='controller-resource')


def _runtime_snapshot(runtime):
    return SimpleNamespace(**{key: getattr(runtime, key) for key in ('docker', 'owner', 'image', 'uid', 'gid', 'root')})


def _policy(runtime):
    value = {'owner': runtime.owner, 'image': runtime.image, 'uid': runtime.uid,
             'gid': runtime.gid, 'root': str(runtime.root), 'docker': runtime.docker}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _safe_sample(binding, sample, *, owner=None, policy=None):
    if not isinstance(sample, dict) or sample.get('provenance') != 'docker_cli_observed':
        raise ValueError('not a controller observation')
    if sample.get('runtime_id') != binding.runtime_id or sample.get('generation') != binding.generation:
        raise ValueError('sample identity mismatch')
    if owner is not None and sample.get('runtime_owner') != owner:
        raise ValueError('sample owner mismatch')
    if policy is not None and sample.get('source_policy_sha256') != policy:
        raise ValueError('sample policy mismatch')
    window = SampleWindow(1)
    window.add(sample)
    safe = window.summary()['samples'][0]
    safe['provenance'] = 'docker_cli_observed'
    return safe


def validated_payload(binding, payload):
    """Canonical controller envelope for the guarded Store publication path."""
    if (not isinstance(payload, dict) or type(payload.get('schema_version')) is not int
            or payload['schema_version'] != 1 or payload.get('source') != SOURCE
            or not isinstance(payload.get('binding'), dict) or Binding(**payload['binding']) != binding
            or not isinstance(payload.get('sample_id'), str)
            or not re.fullmatch('[0-9a-f]{32}', payload['sample_id'])):
        raise ValueError('invalid controller resource envelope')
    clean = {'schema_version':1,'source':SOURCE,'binding':asdict(binding),
             'sample_id':payload['sample_id'],'sample':_safe_sample(binding,payload.get('sample'))}
    if len(json.dumps(clean, allow_nan=False).encode()) > MAX_EVENT_BYTES:
        raise ValueError('resource event exceeds budget')
    return clean


class ResourceMonitor:
    def __init__(self, current: Callable, publish: Callable, *, cadence_seconds=10,
                 sampler=sample_runtime, executor: Executor | None = None, clock=time.monotonic):
        if type(cadence_seconds) not in (int, float) or not math.isfinite(cadence_seconds) or not 10 <= cadence_seconds <= 3600:
            raise ValueError('sample cadence must be between 10 and 3600 seconds')
        self.current, self.publish = current, publish
        self.cadence, self.sampler = cadence_seconds, sampler
        self.executor, self.clock = executor or _EXECUTOR, clock
        self._thread = threading.get_ident()
        self._pending = None
        self._closed = False

    def _controller_thread(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError('resource monitor requires its controller thread')

    def readiness(self):
        self._controller_thread()
        if self._closed:
            return 'closed'
        if self._pending is not None:
            return 'busy'
        with _ADMISSION.lock:
            if _ADMISSION.pending:
                return 'busy'
            if self.clock() < _ADMISSION.next_allowed:
                return 'cadence'
        return 'ready'

    def offer(self, binding: Binding, runtime) -> str:
        """Nonblocking submission, no queue; caller chooses fair attempt order."""
        self._controller_thread()
        ready = self.readiness()
        if ready != 'ready':
            return ready
        try:
            if not binding.matches(self.current(binding.attempt_id)):
                return 'stale'
            snapshot = _runtime_snapshot(runtime)
            policy = _policy(snapshot)
        except Exception:
            return 'unavailable'
        admission = _ADMISSION.acquire(self.clock(), self.cadence)
        if admission != 'submitted':
            return admission
        try:
            future = self.executor.submit(self.sampler, snapshot, binding.runtime_id,
                                          expected_generation=binding.generation, timeout_seconds=3)
        except Exception:
            _ADMISSION.release()
            return 'unavailable'
        self._pending = (future, binding, snapshot.owner, policy, _ADMISSION)
        return 'submitted'

    def poll(self) -> dict:
        """Persist a completed, still-current observation on this controller thread."""
        self._controller_thread()
        if self._pending is None:
            return {'status': 'closed' if self._closed else 'idle'}
        future, binding, owner, policy, admission = self._pending
        if not future.done():
            return {'status': 'pending'}
        self._pending = None
        try:
            if self._closed or not binding.matches(self.current(binding.attempt_id)):
                return {'status': 'dropped', 'reason': 'stale_binding'}
            try:
                sample = _safe_sample(binding, future.result(), owner=owner, policy=policy)
            except Exception:
                return {'status': 'dropped', 'reason': 'invalid_observation'}
            sample_id = uuid.uuid4().hex
            payload = {'schema_version': 1, 'source': SOURCE, 'binding': asdict(binding),
                       'sample_id': sample_id, 'sample': sample}
            if len(json.dumps(payload, allow_nan=False).encode()) > MAX_EVENT_BYTES:
                return {'status': 'dropped', 'reason': 'observation_limit'}
            sequence = self.publish(binding, EVENT_TYPE, payload, expected_generation=binding.generation,
                                    dedupe_key='resource:' + sample_id)
            if sequence is None:
                return {'status': 'dropped', 'reason': 'stale_at_publication'}
            if type(sequence) is not int or sequence < 1:
                return {'status': 'dropped', 'reason': 'publication_unconfirmed'}
            return {'status': 'recorded', 'sequence': sequence, 'binding': asdict(binding)}
        except Exception:
            # No freeform backend or worker errors enter controller events.
            return {'status': 'dropped', 'reason': 'publication_unavailable'}
        finally:
            admission.release()

    def close(self):
        """Never waits for Docker; a running sample keeps the global slot until done."""
        self._controller_thread()
        self._closed = True
        if self._pending is not None:
            future, _, _, _, admission = self._pending
            self._pending = None
            if future.cancel():
                admission.release()
            else:
                future.add_done_callback(lambda completed: admission.release())


def _history_empty(reason, *, total_events=None, retained_events=0):
    result = SampleWindow(MAX_SAMPLES).summary()
    result.update(history_status='unknown', history_reason=reason, history_total_events=total_events,
                  history_retained_events=retained_events, history_omitted_events=None)
    return result


def summarize_history(binding: Binding, rows, total_events, *, limit=MAX_SAMPLES):
    """Only reserved controller records qualify; no provider result dictionaries."""
    if type(limit) is not int or not 1 <= limit <= MAX_SAMPLES:
        raise ValueError('invalid history limit')
    if type(total_events) is not int or total_events < 0 or not isinstance(rows, (tuple, list)) or len(rows) != min(total_events, limit):
        return _history_empty('invalid_history_count')
    window = SampleWindow(limit)
    previous = 0
    try:
        for row in rows:
            if (row.get('type') != EVENT_TYPE or row.get('session_id') != binding.session_id
                    or row.get('attempt_id') != binding.attempt_id or type(row.get('sequence')) is not int
                    or row['sequence'] <= previous):
                raise ValueError('invalid event identity')
            previous = row['sequence']
            payload = row['payload']
            if isinstance(payload, str):
                if len(payload.encode()) > MAX_EVENT_BYTES:
                    raise ValueError('event too large')
                payload = json.loads(payload)
            if (not isinstance(payload, dict) or len(json.dumps(payload, allow_nan=False).encode()) > MAX_EVENT_BYTES
                    or type(payload.get('schema_version')) is not int or payload['schema_version'] != 1 or payload.get('source') != SOURCE
                    or not isinstance(payload.get('binding'), dict) or Binding(**payload['binding']) != binding
                    or not isinstance(payload.get('sample_id'), str)
                    or not re.fullmatch('[0-9a-f]{32}', payload['sample_id'])):
                raise ValueError('invalid controller event')
            window.add(_safe_sample(binding, payload['sample']))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return _history_empty('invalid_controller_history', total_events=total_events, retained_events=len(rows))
    window.total_samples = total_events
    result = window.summary()
    result.update(history_status='available', history_reason=None, history_total_events=total_events,
                  history_retained_events=len(rows), history_omitted_events=total_events-len(rows))
    return result


def history_summary(database: Path, binding: Binding, *, limit=MAX_SAMPLES, budget_seconds=.1):
    """Read-only bounded DB snapshot: exact binding/type/source, count + latest rows.

    No schema/index creation. The query deadline covers scans; busy timeout is
    capped at 20ms. Interrupted/busy/malformed data returns explicit unknown.
    Existing provider spool allowlisting is required for reserved-type authority.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_SAMPLES:
        raise ValueError('invalid history limit')
    if type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds) or not .01 <= budget_seconds <= .5:
        raise ValueError('invalid history budget')
    started = time.monotonic()
    db = None
    try:
        db = sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True,
                             timeout=min(.02, budget_seconds))
        db.row_factory = sqlite3.Row
        db.set_progress_handler(lambda: int(time.monotonic()-started >= budget_seconds), 100)
        db.execute('BEGIN')
        predicate = """session_id=? AND attempt_id=? AND type=? AND json_valid(payload)
          AND json_extract(payload,'$.source')=? AND json_extract(payload,'$.binding.session_id')=?
          AND json_extract(payload,'$.binding.attempt_id')=? AND json_extract(payload,'$.binding.generation')=?
          AND json_extract(payload,'$.binding.runtime_id')=? AND json_extract(payload,'$.binding.role')=?"""
        params = (binding.session_id, binding.attempt_id, EVENT_TYPE, SOURCE, binding.session_id,
                  binding.attempt_id, binding.generation, binding.runtime_id, binding.role)
        total = db.execute('SELECT COUNT(*) FROM events WHERE '+predicate, params).fetchone()[0]
        rows = db.execute('SELECT session_id,attempt_id,type,sequence,payload FROM events WHERE '+predicate+
                          ' ORDER BY sequence DESC LIMIT ?', (*params, limit)).fetchall()
        if time.monotonic()-started >= budget_seconds:
            return _history_empty('history_query_budget')
        return summarize_history(binding, [dict(row) for row in reversed(rows)], total, limit=limit)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return _history_empty('history_unavailable')
    finally:
        if db is not None:
            db.close()


class ResourceHistoryUnavailable(RuntimeError):
    """Retry the bundle; do not emit different bytes because a DB read timed out."""


def attempt_resources(db, attempt, *, budget_seconds=.1):
    """Read controller events inside the caller's snapshot; never trust result JSON.

    Up to 120 latest samples for each of execution/verifier, with explicit
    runtime binding counts. Only the newest observed runtime per role is shown.
    One shared SQLite progress deadline covers every role/count/read query.
    """
    started = time.monotonic()
    roles = {}
    base = {"measured_usage":None,"reason":"no_controller_resource_samples"}
    try:
        db.set_progress_handler(lambda:int(time.monotonic()-started >= budget_seconds),100)
        for role in ('execution','verifier'):
            predicate = """session_id=? AND attempt_id=? AND type=? AND json_valid(payload)
                AND json_extract(payload,'$.source')=?
                AND json_extract(payload,'$.binding.session_id')=? AND json_extract(payload,'$.binding.attempt_id')=?
                AND json_extract(payload,'$.binding.generation')=? AND json_extract(payload,'$.binding.role')=?"""
            params=(attempt['session_id'],attempt['id'],EVENT_TYPE,SOURCE,attempt['session_id'],attempt['id'],attempt['generation'],role)
            latest=db.execute("SELECT json_extract(payload,'$.binding.runtime_id') FROM events WHERE "+predicate+" ORDER BY sequence DESC LIMIT 1",params).fetchone()
            if not latest:
                roles[role]={'status':'unknown','reason':'no_controller_resource_samples'}
                continue
            try:
                binding=Binding(attempt['session_id'],attempt['id'],attempt['generation'],latest[0],role)
            except (ValueError,TypeError):
                roles[role]={'status':'unknown','reason':'invalid_controller_history'}
                continue
            runtime_count=db.execute("SELECT COUNT(DISTINCT json_extract(payload,'$.binding.runtime_id')) FROM events WHERE "+predicate,params).fetchone()[0]
            predicate += " AND json_extract(payload,'$.binding.runtime_id')=?"
            params += (binding.runtime_id,)
            total=db.execute('SELECT COUNT(*) FROM events WHERE '+predicate,params).fetchone()[0]
            rows=db.execute('SELECT session_id,attempt_id,type,sequence,payload FROM events WHERE '+predicate+' ORDER BY sequence DESC LIMIT ?',(*params,MAX_SAMPLES)).fetchall()
            summary=summarize_history(binding,[dict(row) for row in reversed(rows)],total)
            summary.update(binding=asdict(binding),role_runtime_bindings=runtime_count,
                           omitted_older_runtime_bindings=runtime_count-1)
            roles[role]=summary
        if time.monotonic()-started >= budget_seconds:
            raise ResourceHistoryUnavailable('resource_history_query_budget')
        if all(value.get('reason') == 'no_controller_resource_samples' for value in roles.values()):
            return base
        observed=any(value['status']=='observed' for value in roles.values())
        return {'measured_usage':{'schema_version':1,'source':SOURCE,'status':'observed' if observed else 'unknown',
                'scope':'retained_samples_of_latest_observed_runtime_per_role','true_peak':False,
                'sidecars_included':False,'roles':roles}, 'reason':None if observed else 'no_valid_controller_resource_samples'}
    except sqlite3.Error as exc:
        raise ResourceHistoryUnavailable('resource_history_unavailable') from exc
    except (ValueError,TypeError,KeyError):
        return {**base,'reason':'invalid_controller_history'}
    finally:
        db.set_progress_handler(None,0)
