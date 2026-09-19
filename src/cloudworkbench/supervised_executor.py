"""Attempt-local execution wrapper: budget required, supervisor closed before delivery."""
import threading
from dataclasses import dataclass
import hashlib
from .inference_relay import AttemptBinding, DispatchContext, DispatchResult, _json
from .provider_leases import Reservation
from .provider_protocol import admit_provider_request
from .provider_recovery import recover_request
from .cancellation_supervisor import CancellationSupervisor, SupervisorError
from .provider_executor import ProviderExecutor, _failure
from .budget_authority import BudgetAuthority


@dataclass(frozen=True)
class ExecutorQuiescenceReceipt:
    binding: AttemptBinding
    reservation: Reservation
    grant_id: str
    controller_instance_id: str
    executor_stopped: bool
    supervisors_stopped: bool


class ExecutorQuiescenceError(RuntimeError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


class _CancellationView:
    def __init__(self, supervisor):
        self.supervisor = supervisor

    def is_set(self):
        try:
            self.supervisor.raise_if_cancelled()
            return False
        except SupervisorError:
            return True


class SupervisedProviderExecutor:
    def __init__(self, executor, *, supervisor_factory=CancellationSupervisor):
        if type(executor) is not ProviderExecutor or executor.dispatch.budget is None or not callable(supervisor_factory):
            raise ValueError('budgeted_provider_executor_required')
        self.executor=executor
        self.supervisor_factory=supervisor_factory
        self._execution_lock=threading.Lock()
        self._state_lock=threading.Lock()
        self._current=None
        self.last_receipt=None
        self.last_recovery_receipt=None
        self.last_recovery_error=None
        self._fenced=False
        self._quiesced=False

    def authorize(self,binding):
        with self._state_lock:
            if self._quiesced or self._fenced:return False
        allowed=self.executor.authorize(binding)
        with self._state_lock:
            return allowed and not self._quiesced and not self._fenced

    def quiesce(self):
        """Close admission permanently; after service drain, prove executor/observer stop.

        A busy call is not waited on or adopted. Retry after it finishes. This is
        process-local thread proof only, never provider physical/material cleanup.
        """
        with self._state_lock:self._quiesced=True
        if not self._execution_lock.acquire(False):
            raise ExecutorQuiescenceError('executor_quiesce_busy')
        try:
            with self._state_lock:
                stopped=self._current is None
                observers=stopped and not self._fenced and (self.last_receipt is None or self.last_receipt.stopped is True)
                return ExecutorQuiescenceReceipt(self.executor.binding,self.executor.reservation,
                    self.executor.grant_id,self.executor.dispatch.leases.instance_id,stopped,observers)
        finally:self._execution_lock.release()

    def cancel_check(self,spec):
        with self._state_lock:
            supervisor=self._current
        return supervisor is None or supervisor.cancel_check(spec)

    def _recover_failed_call(self,context,payload,result):
        if result.outer_cleanup_confirmed or 200<=result.response.status<300:
            return result
        executor=self.executor
        try:
            if (type(context) is not DispatchContext or context.binding!=executor.binding
                    or hashlib.sha256(_json(payload)).hexdigest()!=context.payload_digest):
                self.last_recovery_error='recovery_call_mismatch'
                return result
            # Recovery must hash exactly the bytes used by dispatch admission.
            transcript=admit_provider_request(payload,executor.profile).request_json
            digest=hashlib.sha256(transcript).hexdigest()
            with executor.dispatch.store._connect() as db:
                row=db.execute('''SELECT d.request_id FROM provider_dispatch d
                    JOIN provider_request_leases l ON l.id=d.request_id
                    WHERE d.reservation_id=? AND l.grant_id=? AND d.attempt_id=?
                    AND d.generation=? AND d.profile_digest=? AND d.request_nonce=?
                    AND d.payload_digest=? AND d.state IN ('quarantined','cleaned','delivered')''',
                    (executor.reservation.reservation_id,executor.grant_id,
                     context.binding.attempt_id,context.binding.generation,
                     context.binding.profile_digest,context.request_nonce,digest)).fetchone()
            if row is None:
                self.last_recovery_error='recovery_candidate_not_found'
                return result
            # Helper locks the account and rechecks immutable owner/grant/binding
            # and current state; running or unknown effects cannot be cleaned here.
            receipt=recover_request(executor.dispatch,row['request_id'],
                reservation=executor.reservation,grant_id=executor.grant_id,
                binding=context.binding)
            self.last_recovery_receipt=receipt
            if receipt.cleanup_confirmed:
                return DispatchResult(result.response,True)
        except Exception as exc:
            code=getattr(exc,'code',None)
            known=('operation_still_unknown','cleanup_outcome_unknown','recovery_binding_invalid',
                   'recovery_requires_owner_reconciliation','recovery_requires_quarantine',
                   'recovery_cleanup_unconfirmed','account_lock_timeout')
            self.last_recovery_error=code if type(code) is str and code in known else 'recovery_failed'
        return result

    def __call__(self,context,payload,cancel):
        if not self._execution_lock.acquire(False):
            return _failure('attempt_execution_busy',409,cleaned=True)
        result=None
        supervisor=None
        entered=False
        executor=self.executor
        try:
            if self._quiesced or self._fenced:
                return _failure('attempt_executor_fenced',503,cleaned=False)
            self.last_recovery_receipt=None
            self.last_recovery_error=None
            try:
                supervisor=self.supervisor_factory(executor.dispatch.store,
                    reservation=executor.reservation,grant_id=executor.grant_id,
                    binding=executor.binding,caller_cancel=cancel,
                    controller_instance_id=executor.dispatch.leases.instance_id,
                    budget_authority=BudgetAuthority(executor.dispatch.budget_scope))
                with supervisor:
                    entered=True
                    with self._state_lock:
                        self._current=supervisor
                    result=executor(context,payload,_CancellationView(supervisor))
            except SupervisorError:
                if result is None or 200<=result.response.status<300:
                    result=_failure('attempt_supervision_failed',409,
                        cleaned=result.outer_cleanup_confirmed if result is not None else not entered)
            except Exception:
                if result is None or 200<=result.response.status<300:
                    result=_failure('attempt_execution_failed',503,
                        cleaned=result.outer_cleanup_confirmed if result is not None else False)
            finally:
                with self._state_lock:
                    self._current=None
                if supervisor is not None:
                    self.last_receipt=supervisor.receipt
                    if self.last_receipt is None or not self.last_receipt.stopped:
                        self._fenced=True
                        # Reuse the supervisor's bounded SQLite operation. Failure
                        # retains the local fence and never grants reuse authority.
                        try:
                            def quarantine(db):
                                r=executor.reservation
                                db.execute("UPDATE provider_reservations SET state='quarantined',reason='supervisor_shutdown_failed' WHERE id=? AND account_id=? AND epoch=? AND controller_instance_id=?",(r.reservation_id,r.account_id,r.epoch,executor.dispatch.leases.instance_id))
                            supervisor._database(quarantine,track=False)
                        except Exception:
                            pass
            # Recovery is subordinate to successful observer shutdown. It changes
            # only the cleanup flag, never the original non-success response.
            if (not self._fenced and supervisor is not None
                    and self.last_receipt is not None and self.last_receipt.stopped):
                result=self._recover_failed_call(context,payload,result)
            return result
        finally:
            self._execution_lock.release()
