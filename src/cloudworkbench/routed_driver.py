"""Run one already-provisioned child to quiescence; never grant acceptance.

The caller's worker socket/service must already be provisioned by the controller.
Provider cleanup and protected verification retain the scheduler seat afterward.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
import os
import stat
import time

from .routed_stage import PreparedStage, _authority
from .routed_runtime import CallerSpec
from .provider_protocol import provider_profile_digest
from .store import StoreError
from .workflow_revisions import verify_unlaunched_materialization, RevisionError


class StageDriveError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class QuiescedStage:
    attempt_id: str
    generation: int
    runtime_id: str
    binding_digest: str
    observation: object | None
    execution_status: str
    grant_fence_confirmed: bool
    caller_cleanup: dict | None
    cleanup_error: str | None
    verification_pass: bool = field(default=False, init=False)
    provider_cleanup_qualified: bool = field(default=False, init=False)
    scheduler_seat_released: bool = field(default=False, init=False)


@contextmanager
def _exclusive_driver(runtime, spec):
    runtime._validate_spec(spec)
    runtime._private(runtime.root, directory=True)
    path = runtime.root / ('driver-' + spec.attempt_id + '.' + str(spec.generation) + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise StageDriveError('unsafe_driver_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StageDriveError('stage_driver_busy') from None
        yield
    finally:
        os.close(fd)


def _validate_prepared(prepared, spec, profile):
    if type(prepared) is not PreparedStage or type(spec) is not CallerSpec:
        raise StageDriveError('invalid_prepared_stage')
    receipt = json.loads(prepared.launch.receipt_json)
    binding = prepared.materialization.consumer
    if (spec.attempt_id != prepared.attempt_id or spec.generation != prepared.generation
            or binding.attempt_id != prepared.attempt_id or binding.generation != prepared.generation
            or spec.session_id != binding.session_id
            or spec.workspace != str(prepared.materialization.source)
            or spec.scratch != str(prepared.materialization.scratch)
            or spec.workspace_readonly != prepared.materialization.readonly
            or spec.workspace_readonly != prepared.launch.workspace_readonly
            or provider_profile_digest(profile) != prepared.profile_digest
            or receipt.get('profile_digest') != prepared.profile_digest
            or receipt.get('input_revision_sha256') != prepared.materialization.revision_sha256):
        raise StageDriveError('prepared_stage_binding_changed')


def _verify_input(prepared):
    try:
        verify_unlaunched_materialization(prepared.materialization)
    except RevisionError:
        raise StageDriveError('materialization_changed') from None


def _run_budget(prepared):
    try:
        raw = prepared.launch.argv[prepared.launch.argv.index('--run-budget') + 1]
        budget = int(raw)
        if str(budget) != raw or not 10 <= budget <= 3600:
            raise ValueError
        return budget
    except (ValueError, IndexError, TypeError):
        raise StageDriveError('invalid_launch_budget') from None


def _relay_ready(value, prepared):
    if value == {'present': False}:
        return False
    if (value.get('present') is not True or value.get('provenance') != 'worker_reported'
            or value.get('offset') != 0 or type(value.get('data')) is not bytes
            or len(value['data']) > 4096 or value.get('next_offset') != value.get('size')
            or value['next_offset'] != len(value['data'])):
        raise StageDriveError('invalid_relay_ready')
    try:
        pairs = json.loads(value['data'], object_pairs_hook=lambda items: items)
        ready = dict(pairs)
        # Duplicate top-level keys cannot certify readiness.
        if len(ready) != len(pairs):
            raise ValueError
        nested = ready.get('binding')
        if type(nested) is not list or len(nested) != 3:
            raise ValueError
        ready['binding'] = dict(nested)
        if (set(ready) != {'pid', 'uid', 'request_path', 'binding'}
                or type(ready['pid']) is not int or ready['pid'] <= 0
                or type(ready['uid']) is not int or ready['uid'] != 1001
                or ready['request_path'] != prepared.launch.request_path
                or ready['binding'] != {'attempt_id': prepared.attempt_id,
                    'generation': prepared.generation, 'profile_digest': prepared.profile_digest}
                or type(ready['binding']['generation']) is not int):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise StageDriveError('invalid_relay_ready') from None
    return True


def drive_prepared_child(scheduler, runtime, prepared, spec, *, execution_profile,
                         timeout_seconds=3600, relay_timeout_seconds=20,
                         poll_seconds=.25, clock=time.monotonic, sleep=time.sleep,
                         forbidden_values=()):
    """One launch attempt only. Unknown RPC outcomes require explicit reconciliation.

    This controller entry point consumes trusted PreparedStage/CallerSpec objects,
    not HTTP/model arguments. Returning an observation never releases capacity.
    """
    from .routed_collection import collect_execution, CollectionNotReady
    from .routed_observation_store import validate_forbidden_values
    forbidden_values = validate_forbidden_values(forbidden_values)
    for value, maximum in ((timeout_seconds, 4200), (relay_timeout_seconds, 60), (poll_seconds, 1)):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
            raise StageDriveError('invalid_driver_deadline')
    _validate_prepared(prepared, spec, execution_profile)
    run_budget = _run_budget(prepared)
    with _exclusive_driver(runtime, spec):
        if runtime._folder(spec).exists() or scheduler.read_child_launch(
                prepared.attempt_id, expected_generation=prepared.generation) is not None:
            raise StageDriveError('stage_requires_reconciliation')
        _authority(scheduler, prepared.attempt_id, prepared.generation)
        _verify_input(prepared)
        # Rebuild the immutable spec from current material before any Docker RPC.
        checked = runtime.prepare_caller(spec.attempt_id, spec.session_id,
            generation=spec.generation, plan=prepared.launch, workspace=spec.workspace,
            scratch=spec.scratch, task_dir=spec.task_dir, worker_socket_dir=spec.worker_socket_dir)
        if checked != spec:
            raise StageDriveError('caller_spec_changed')
        runtime_id = None
        launch = None
        observation = None
        status = 'setup_failed'
        fenced = False
        cleanup = None
        cleanup_error = None
        deadline = clock() + min(timeout_seconds, run_budget + relay_timeout_seconds + 30)
        output_deadline = None

        def authority():
            if output_deadline is not None and clock() >= output_deadline:
                raise StageDriveError('worker_output_missing')
            if clock() >= deadline:
                raise StageDriveError('stage_deadline')
            scheduler.check_child_start_authority(prepared.attempt_id,
                expected_generation=prepared.generation, binding_digest=launch.binding_digest)

        def read_file(**kwargs):
            authority()
            return runtime.read_caller_file(spec, runtime_id, **kwargs)

        try:
            runtime_id = runtime.create_caller(spec)
            if runtime.inspect_caller(spec, runtime_id).get('Status') != 'created':
                raise StageDriveError('caller_not_stopped')
            launch = scheduler.bind_child_caller(prepared.attempt_id,
                expected_generation=prepared.generation, runtime_id=runtime_id,
                caller_spec=spec, execution_profile=execution_profile,
                input_revision_sha256=prepared.materialization.revision_sha256)
            intent = scheduler.begin_child_start(prepared.attempt_id,
                expected_generation=prepared.generation, binding_digest=launch.binding_digest)
            if intent.may_start is not True:
                raise StageDriveError('stage_start_unresolved')
            authority()
            runtime.start_caller(spec, runtime_id)
            if runtime.inspect_caller(spec, runtime_id).get('Status') != 'running':
                raise StageDriveError('caller_start_unconfirmed')
            scheduler.confirm_child_started(prepared.attempt_id,
                expected_generation=prepared.generation, binding_digest=launch.binding_digest)
            authority()
            runtime.start_caller_process(spec, runtime_id, role='relay')
            relay_deadline = min(deadline, clock() + relay_timeout_seconds)
            while not _relay_ready(read_file(name='relay-ready', offset=0, max_bytes=4096), prepared):
                if clock() >= relay_deadline:
                    raise StageDriveError('relay_ready_timeout')
                sleep(min(poll_seconds, max(0, relay_deadline - clock())))
            authority()
            _verify_input(prepared)
            output_deadline = min(deadline, clock() + run_budget + 30)
            runtime.start_caller_process(spec, runtime_id, role='hermes')
            while True:
                authority()
                try:
                    observation = collect_execution(read_file,
                        launch_receipt_sha256=hashlib.sha256(prepared.launch.receipt_json.encode()).hexdigest(),
                        clock=clock)
                    authority()
                    from .routed_observation_store import save_observation
                    try:
                        save_observation(scheduler, runtime.root, spec, runtime_id,
                                         launch.binding_digest, observation,
                                         forbidden_values=forbidden_values)
                    except StoreError:
                        raise
                    except Exception:
                        raise StageDriveError('observation_persistence_failed') from None
                    authority()
                    status = 'execution_observed'
                    break
                except CollectionNotReady:
                    sleep(min(poll_seconds, max(0, deadline - clock())))
        except StoreError:
            status = 'authority_unavailable'
        except StageDriveError as exc:
            status = exc.code
        except Exception:
            status = 'execution_unconfirmed'
        finally:
            if launch is not None:
                try:
                    scheduler.fence_child_execution(prepared.attempt_id,
                        expected_generation=prepared.generation, binding_digest=launch.binding_digest)
                    fenced = True
                except Exception:
                    cleanup_error = 'grant_fence_unconfirmed'
            if runtime_id is not None:
                try:
                    runtime.stop_caller(spec, runtime_id)
                    cleanup = runtime.remove_caller(spec, runtime_id)
                    expected = {'runtime_id': runtime_id, 'attempt_id': prepared.attempt_id,
                        'generation': prepared.generation, 'spec_digest': spec.digest,
                        'caller_stopped': True, 'caller_removed': True,
                        'authority': 'controller_observed_caller_only', 'provider_cleanup_qualified': False}
                    if cleanup != expected:
                        cleanup = None
                        raise StageDriveError('caller_cleanup_receipt_changed')
                except Exception:
                    cleanup_error = cleanup_error or 'caller_cleanup_unconfirmed'
            # No name-based discovery or retry after an unanswered create.
        if runtime_id is None:
            raise StageDriveError('caller_create_unresolved')
        if status != 'execution_observed' or cleanup_error or not fenced or cleanup is None:
            observation = None
        return QuiescedStage(prepared.attempt_id, prepared.generation, runtime_id,
            launch.binding_digest if launch else '', observation, status, fenced,
            cleanup, cleanup_error)
