"""Aggregate exact caller, worker and provider cleanup without releasing capacity."""
from contextlib import ExitStack
from dataclasses import asdict, dataclass
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .inference_relay import AttemptBinding
from .provider_dispatch import ProviderDispatch
from .routed_driver import QuiescedStage
from .routed_runtime import CallerSpec, RoutedRuntime
from .scheduler import ChildCleanupReceipt, ChildCleanupTarget, RoleScheduler, _child_control_tx
from .supervised_executor import SupervisedProviderExecutor
from .worker_service import WorkerService


class ChildCleanupError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise ChildCleanupError(code)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _publish_exclusive(source, target):
    """Atomically publish a complete single-link receipt without replacement."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'linux' and hasattr(library, 'renameat2'):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    elif sys.platform == 'darwin' and hasattr(library, 'renamex_np'):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(target), 4)
    else:
        raise ChildCleanupError('exclusive_publication_unavailable')
    if result:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(code, os.strerror(code))
        raise OSError(code, os.strerror(code))


@dataclass(frozen=True)
class ChildCleanupEvidence:
    attempt_id: str
    generation: int
    runtime_id: str
    binding_digest: str
    evidence_sha256: str
    evidence_path: Path


class RoutedChildCleanup:
    """Trusted controller handle; services cover every grant issued to this child.

    collect() may run before the protected outcome decision. verifier() is the
    exact combined inspector for RoleScheduler.release_child after that decision.
    Neither method promotes revisions, declares verification, nor releases an
    account reservation. Unknown effects retain occupancy for reconciliation.
    """
    def __init__(self, scheduler, runtime, spec, quiesced, *, services):
        _require(type(scheduler) is RoleScheduler and type(runtime) is RoutedRuntime
                 and type(spec) is CallerSpec and type(quiesced) is QuiescedStage,
                 'invalid_cleanup_context')
        _require(type(services) is tuple and len(services) <= 32
                 and all(type(service) is WorkerService for service in services),
                 'invalid_worker_services')
        _require((quiesced.attempt_id, quiesced.generation) == (spec.attempt_id, spec.generation)
                 and type(quiesced.runtime_id) is str
                 and re.fullmatch('[0-9a-f]{64}', quiesced.runtime_id) is not None
                 and quiesced.grant_fence_confirmed is True
                 and quiesced.cleanup_error is None and quiesced.caller_cleanup is not None,
                 'caller_cleanup_unconfirmed')
        self.scheduler, self.runtime, self.spec = scheduler, runtime, spec
        self.quiesced, self.services = quiesced, services

    def _snapshot(self):
        scheduler, spec, value = self.scheduler, self.spec, self.quiesced
        with _child_control_tx(scheduler.store, write=False) as db:
            child = scheduler.store._attempt(db, spec.attempt_id)
            root = db.execute('SELECT * FROM workflow_roots WHERE root_id=?',
                              (child.get('workflow_root_id'),)).fetchone()
            launch = db.execute('SELECT * FROM workflow_child_launch WHERE child_id=?',
                                (spec.attempt_id,)).fetchone()
            _require(root is not None and launch is not None
                     and root['state'] == 'held' and root['child_attempt_id'] == spec.attempt_id
                     and scheduler._owner_current(db, root['root_id'])
                     and child['generation'] == spec.generation
                     and child['session_id'] == spec.session_id
                     and child['runtime_id'] == value.runtime_id
                     and launch['state'] == 'fenced'
                     and launch['binding_digest'] == value.binding_digest
                     and launch['controller_instance_id'] == scheduler.leases.instance_id,
                     'child_cleanup_authority_changed')
            bound = scheduler._child_binding_record(launch)
            parent = scheduler.store._attempt(db, root['root_id'])
            frozen_binding = json.loads(launch['binding'])
            _require(bound.caller_spec_digest == spec.digest
                     and bound.runtime_id == value.runtime_id
                     and bound.generation == spec.generation
                     and root['generation'] == parent['generation']
                     == frozen_binding['assignment']['root_generation'],
                     'child_cleanup_binding_changed')
            grants = [dict(r) for r in db.execute(
                'SELECT * FROM provider_execution_grants WHERE attempt_id=? ORDER BY id LIMIT 33',
                (spec.attempt_id,))]
            _require(len(grants) <= 32 and all(g['generation'] == spec.generation
                     and g['revoked_at'] is not None for g in grants), 'child_grants_unfenced')
            accounts = [dict(r) for r in db.execute('''SELECT w.account_id,w.reservation_id,
                p.epoch,p.controller_instance_id,a.persistent_owner_id FROM workflow_accounts w
                JOIN provider_reservations p ON p.id=w.reservation_id
                JOIN provider_accounts a ON a.account_id=w.account_id
                WHERE w.root_id=? ORDER BY w.account_id''', (root['root_id'],))]
            _require({r['account_id']:r['persistent_owner_id'] for r in accounts}
                     == json.loads(root['frozen'])['accounts'], 'account_owner_changed')
            reservations = {r['reservation_id'] for r in accounts}
            _require(all(g['reservation_id'] in reservations for g in grants),
                     'foreign_child_grant')
            requests = [dict(r) for r in db.execute('''SELECT l.* FROM provider_request_leases l
                JOIN provider_execution_grants g ON g.id=l.grant_id
                WHERE g.attempt_id=? ORDER BY l.id LIMIT 1025''', (spec.attempt_id,))]
            dispatch_ids = [r[0] for r in db.execute('''SELECT DISTINCT d.request_id FROM provider_dispatch d
                LEFT JOIN provider_request_leases l ON l.id=d.request_id
                LEFT JOIN provider_execution_grants g ON g.id=l.grant_id
                WHERE d.attempt_id=? OR g.attempt_id=? ORDER BY d.request_id LIMIT 1025''',
                (spec.attempt_id, spec.attempt_id))]
            _require(len(requests) <= 1024 and dispatch_ids == [r['id'] for r in requests],
                     'provider_request_membership_unproven')
            by_grant = {g['id']:g for g in grants}
            _require(all(r['reservation_id'] == by_grant[r['grant_id']]['reservation_id']
                     and r['purpose'] == 'inference' for r in requests),
                     'provider_request_binding_changed')
            target = ChildCleanupTarget(root['root_id'], root['generation'], spec.attempt_id,
                                        spec.generation, root['version'], value.runtime_id)
            return target, bound, accounts, grants, requests

    def _drain(self, bound, *, timeout_seconds):
        executors, receipts = {}, []
        for service in self.services:
            wrapped = service.dispatcher.execute_request
            _require(type(wrapped) is SupervisedProviderExecutor, 'supervised_worker_required')
            executor = wrapped.executor
            expected = AttemptBinding(self.spec.attempt_id, self.spec.generation, bound.profile_digest)
            _require(service.binding == expected and executor.binding == expected
                     and type(executor.dispatch) is ProviderDispatch
                     and executor.dispatch.leases is self.scheduler.leases,
                     'worker_cleanup_binding_changed')
            _require(callable(getattr(executor.dispatch.runtime, 'after_request_cleanup', None)),
                     'provider_material_cleanup_required')
            stopped = service.close(timeout_seconds=timeout_seconds)
            _require(stopped.thread_stopped and stopped.resources_closed,
                     'worker_drain_unconfirmed')
            stopped = service.closed_receipt()
            observer = wrapped.quiesce()
            _require(observer.executor_stopped and observer.supervisors_stopped
                     and observer.binding == expected and observer.reservation == executor.reservation
                     and observer.grant_id == executor.grant_id
                     and observer.controller_instance_id == self.scheduler.leases.instance_id,
                     'provider_observer_unconfirmed')
            _require(executor.grant_id not in executors, 'duplicate_worker_grant')
            executors[executor.grant_id] = executor
            receipts.append({'worker':asdict(stopped), 'executor':asdict(observer)})
        return executors, receipts

    def _physical_proof(self, request, dispatch):
        row = dispatch.read(request['id'])
        _require(request['state'] == 'released' and row['state'] in ('cleaned', 'delivered')
                 and row['uncertain'] == 0 and row['operation'] is None
                 and type(request['cleanup_receipt']) is str
                 and len(request['cleanup_receipt']) <= 131072, 'provider_cleanup_unconfirmed')
        try:
            proof = json.loads(request['cleanup_receipt'])
            target = proof['target']
            spec = dispatch.spec(request['id'])
            _require(target['scope'] == 'request'
                     and target['reservation'] == asdict(spec.lease.reservation)
                     and target['request_id'] == request['id']
                     and target['grant_id'] == request['grant_id']
                     and target['attempt_id'] == self.spec.attempt_id
                     and target['generation'] == self.spec.generation
                     and target['cleanup_id'] == request['cleanup_id']
                     and target['frozen_at'] == request['frozen_at']
                     and proof['inspector_id'] == dispatch.leases.inspector_id
                     and proof['outcome'] in ('terminated', 'fenced')
                     and type(proof['observed_at']) in (float,int)
                     and target['frozen_at'] <= proof['observed_at'] <= dispatch.leases._now()
                     and re.fullmatch('[0-9a-f]{64}', proof['evidence_sha256']) is not None,
                     'provider_cleanup_proof_mismatch')
            _require(len(target['dispatches']) == 1, 'provider_cleanup_proof_mismatch')
            frozen = dict(target['dispatches'][0])
            keys = ('request_id','attempt_id','generation','launch_nonce','profile_digest',
                    'payload_digest','provider_name','gateway_name','internal_network_name',
                    'external_network_name','provider_id','gateway_id','internal_network_id',
                    'external_network_id','deadline_at')
            _require(all(frozen[k] == row[k] for k in keys), 'provider_cleanup_proof_mismatch')
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ChildCleanupError):
                raise
            raise ChildCleanupError('provider_cleanup_proof_malformed') from None
        return {'request_id':request['id'], 'grant_id':request['grant_id'],
                'physical_receipt_sha256':hashlib.sha256(request['cleanup_receipt'].encode()).hexdigest(),
                'material_cleanup_confirmed':True}

    def _save(self, body):
        raw = _encoded(body)
        _require(len(raw) <= 2*1024*1024, 'child_cleanup_evidence_limit')
        digest = hashlib.sha256(raw).hexdigest()
        self.runtime._private(self.runtime.root, directory=True)
        path = self.runtime.root / ('child-cleanup-' + digest + '.json')
        fd, temporary_name = tempfile.mkstemp(prefix='.child-cleanup-', suffix='.pending', dir=self.runtime.root)
        temporary = Path(temporary_name)
        identity = os.fstat(fd)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            try:
                _publish_exclusive(temporary, path)
            except FileExistsError:
                existing = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(existing)
                    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                             and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                             and info.st_size == len(raw), 'child_cleanup_evidence_changed')
                    _require(os.read(existing, len(raw)+1) == raw, 'child_cleanup_evidence_changed')
                finally:
                    os.close(existing)
        finally:
            try:
                current = temporary.lstat()
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev,current.st_ino) == (identity.st_dev,identity.st_ino):
                    temporary.unlink()
        directory = os.open(self.runtime.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return ChildCleanupEvidence(self.spec.attempt_id, self.spec.generation,
            self.quiesced.runtime_id, self.quiesced.binding_digest, digest, path)

    def collect(self, *, timeout_seconds=5.0, target=None):
        before = self._snapshot()
        _require(target is None or type(target) is ChildCleanupTarget and target == before[0],
                 'child_cleanup_target_changed')
        executors, drained = self._drain(before[1], timeout_seconds=timeout_seconds)
        _require(set(executors) == {g['id'] for g in before[3]}, 'uncovered_child_grant')
        for grant in before[3]:
            reservation = executors[grant['id']].reservation
            account = next(r for r in before[2] if r['reservation_id'] == grant['reservation_id'])
            _require(asdict(reservation) == account, 'worker_reservation_changed')
        with ExitStack() as locks:
            for account in before[2]:
                locks.enter_context(self.scheduler.leases.account_lock(account['account_id']))
            initial = self._snapshot()
            _require(initial[:4] == before[:4]
                     and [(r['id'],r['grant_id'],r['reservation_id']) for r in initial[4]]
                     == [(r['id'],r['grant_id'],r['reservation_id']) for r in before[4]],
                     'child_cleanup_membership_changed')
            _require(target is None or type(target) is ChildCleanupTarget and target == initial[0],
                     'child_cleanup_target_changed')
            caller = self.runtime.remove_caller(self.spec, self.quiesced.runtime_id)
            _require(caller == self.quiesced.caller_cleanup and caller['caller_removed'] is True,
                     'caller_cleanup_changed')
            dispatches = {}
            for request in initial[4]:
                executor = executors[request['grant_id']]
                dispatch = executor.dispatch
                spec = dispatch.spec(request['id'])
                _require(spec.lease.reservation == executor.reservation
                         and spec.lease.grant_id == executor.grant_id
                         and spec.attempt_id == self.spec.attempt_id
                         and spec.generation == self.spec.generation
                         and spec.profile_digest == initial[1].profile_digest,
                         'provider_dispatch_binding_changed')
                row = dispatch.read(request['id'])
                if row['uncertain'] or row['state'] in ('create_intent','start_intent'):
                    dispatch.reconcile(request['id'])
                # Also replay credential material cleanup for already-cleaned requests.
                dispatch.cleanup(request['id'])
                dispatches[request['id']] = dispatch
            final = self._snapshot()
            _require(final[:4] == initial[:4]
                     and [(r['id'],r['grant_id'],r['reservation_id']) for r in final[4]]
                     == [(r['id'],r['grant_id'],r['reservation_id']) for r in initial[4]],
                     'child_cleanup_membership_changed')
            proofs = [self._physical_proof(r, dispatches[r['id']]) for r in final[4]]
            return self._save({'version':1, 'target':asdict(final[0]),
                'launch_binding':asdict(final[1]), 'accounts_retained':final[2],
                'caller':caller, 'workers':drained, 'providers':proofs})

    def verifier(self, target):
        evidence = self.collect(target=target)
        return ChildCleanupReceipt(target, 'confirmed_stopped', evidence.evidence_sha256)
