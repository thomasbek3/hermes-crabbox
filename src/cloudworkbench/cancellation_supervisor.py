"""Bounded controller cancellation polling; never a runtime-cleanup receipt.

The database path and immutable binding come from the trusted controller. No
user callbacks run on this thread. SQLite busy waits and VM work are bounded;
uninterruptible OS I/O is not. A failed shutdown forbids a successful response.
"""
from dataclasses import dataclass
from pathlib import Path
import math
import sqlite3
import threading
import time

from .inference_relay import AttemptBinding
from .budget_authority import BudgetAuthority
from .models import LIVE
from .provider_dispatch import DispatchSpec
from .provider_leases import Reservation, _id

POLL_SECONDS = .250
SQL_SECONDS = .050
JOIN_SECONDS = .500
STALE_SECONDS = 2 * POLL_SECONDS + 2 * SQL_SECONDS


class SupervisorError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ShutdownReceipt:
    stopped: bool
    cancelled: bool
    reason: str | None
    durable_revocation_confirmed: bool
    polls: int


class CancellationSupervisor:
    def __init__(self, store, *, reservation, grant_id, binding, caller_cancel, controller_instance_id, disconnect=None, budget_authority=None):
        if (type(reservation) is not Reservation or type(binding) is not AttemptBinding
                or type(reservation.epoch) is not int or reservation.epoch < 1
                or type(caller_cancel) is not threading.Event
                or (disconnect is not None and type(disconnect) is not threading.Event)):
            raise SupervisorError('invalid_supervisor_config')
        for value in (reservation.account_id, reservation.persistent_owner_id, reservation.reservation_id,
                      reservation.controller_instance_id, grant_id, controller_instance_id):
            _id(value)
        if controller_instance_id != reservation.controller_instance_id:
            raise SupervisorError('foreign_controller_instance')
        if budget_authority is not None and (type(budget_authority) is not BudgetAuthority
                or budget_authority.scope.attempt_id != binding.attempt_id
                or budget_authority.scope.generation != binding.generation):
            raise SupervisorError('invalid_budget_authority')
        self.budget_authority = budget_authority
        self.controller_instance_id = controller_instance_id
        self.path = Path(store.path).absolute()
        self.reservation, self.grant_id, self.binding = reservation, grant_id, binding
        self.caller_cancel, self.disconnect = caller_cancel, disconnect
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancelled = threading.Event()
        self._thread = None
        self._connection = None
        self._started = False
        self._closed = False
        self._reason = None
        self._revoked = False
        self._polls = 0
        self._last_observed = None
        self._shutdown_error = None
        self.receipt = None

    def _cancel(self, reason):
        with self._lock:
            first = self._reason is None
            if first:
                self._reason = reason
            self._cancelled.set()
        if first:
            self._wake.set()

    def _external_cancel(self):
        if self._closed:
            return
        if self.caller_cancel.is_set():
            self._cancel('caller_cancelled')
        elif self.disconnect is not None and self.disconnect.is_set():
            self._cancel('caller_disconnected')

    def _database(self, operation, *, track=True):
        """Single bounded operation, no Store 30s busy timeout or account flock."""
        deadline = time.monotonic() + SQL_SECONDS
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True,
                                 timeout=SQL_SECONDS, isolation_level=None)
            db.row_factory = sqlite3.Row
            if track:
                with self._lock:
                    self._connection = db
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            value = operation(db)
            if time.monotonic() >= deadline:
                raise SupervisorError('authorization_database_timeout')
            return value
        finally:
            if track:
                with self._lock:
                    self._connection = None
            if db is not None:
                db.close()

    def _authorized(self, db):
        r = self.reservation
        row = db.execute('''SELECT g.attempt_id,g.generation,g.expires_at,g.revoked_at,
            a.generation AS actual_generation,a.state AS attempt_state,a.cancel_requested,
            c.revoked_at AS client_revoked,r.account_id,r.epoch,r.controller_instance_id,
            r.state AS reservation_state,pa.persistent_owner_id,pa.active_id
            FROM provider_execution_grants g
            JOIN attempts a ON a.id=g.attempt_id
            JOIN sessions s ON s.id=a.session_id JOIN clients c ON c.id=s.owner_id
            JOIN provider_reservations r ON r.id=g.reservation_id
            JOIN provider_accounts pa ON pa.account_id=r.account_id
            WHERE g.id=? AND g.reservation_id=?''', (self.grant_id, r.reservation_id)).fetchone()
        if not row:
            return False
        deadline = row['expires_at']
        if not (type(deadline) in (int, float) and math.isfinite(deadline)):
            return False
        if not (row['attempt_id'] == self.binding.attempt_id
                and row['generation'] == row['actual_generation'] == self.binding.generation
                and row['attempt_state'] in LIVE and not row['cancel_requested']
                and row['client_revoked'] is None and row['revoked_at'] is None
                and deadline > time.time() and row['account_id'] == r.account_id
                and row['epoch'] == r.epoch and row['controller_instance_id'] == r.controller_instance_id == self.controller_instance_id
                and row['reservation_state'] == 'held'
                and row['persistent_owner_id'] == r.persistent_owner_id
                and row['active_id'] == r.reservation_id):
            return False
        incompatible = db.execute('''SELECT 1 FROM provider_dispatch d
            JOIN provider_request_leases q ON q.id=d.request_id
            WHERE q.grant_id=? AND q.reservation_id=?
            AND (d.attempt_id!=? OR d.generation!=? OR d.profile_digest!=?
                OR (d.cancel_requested=1 AND d.state NOT IN ('cleaned','delivered','failed')))
            LIMIT 1''', (self.grant_id, r.reservation_id, self.binding.attempt_id,
                        self.binding.generation, self.binding.profile_digest)).fetchone()
        if incompatible is not None:
            return False
        return (self.budget_authority is None or
                self.budget_authority.check(db, now=time.time()).allowed)

    def _revoke(self, db):
        args = (self.grant_id, self.reservation.reservation_id,
                self.binding.attempt_id, self.binding.generation)
        db.execute('BEGIN IMMEDIATE')
        try:
            exists = db.execute('''SELECT 1 FROM provider_execution_grants
                WHERE id=? AND reservation_id=? AND attempt_id=? AND generation=?''', args).fetchone()
            if not exists:
                db.rollback()
                return False
            stamp = time.time()
            db.execute('''UPDATE provider_execution_grants
                SET revoked_at=COALESCE(revoked_at,?),reason=COALESCE(reason,?)
                WHERE id=? AND reservation_id=? AND attempt_id=? AND generation=?''',
                       (stamp, 'controller_cancelled', *args))
            db.execute('''UPDATE provider_request_leases SET state='quarantined',reason='controller_cancelled'
                WHERE grant_id=? AND reservation_id=? AND state='held' ''', args[:2])
            db.execute('''UPDATE provider_dispatch SET cancel_requested=1,version=version+1,updated_at=?
                WHERE request_id IN (SELECT id FROM provider_request_leases WHERE grant_id=? AND reservation_id=?)
                AND attempt_id=? AND generation=? AND cancel_requested=0
                AND state NOT IN ('cleaned','delivered','failed')''', (stamp, *args))
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise

    def _poll(self, *, final=False):
        self._external_cancel()
        if not self._cancelled.is_set():
            try:
                if not self._database(self._authorized):
                    self._cancel('execution_not_authorized')
                else:
                    self._last_observed = time.monotonic()
            except Exception:
                if self._stop.is_set() and not final:
                    return
                self._cancel('authorization_database_unavailable')
        if self._cancelled.is_set() and not self._revoked:
            try:
                self._revoked = self._database(self._revoke)
            except Exception:
                # Local cancellation stays sticky; durable revocation is unconfirmed.
                pass
        self._polls += 1

    def _run(self):
        next_poll = time.monotonic() + POLL_SECONDS
        try:
            while not self._stop.is_set():
                self._wake.wait(max(0, next_poll - time.monotonic()))
                self._wake.clear()
                if self._stop.is_set():
                    break
                started = time.monotonic()
                self._poll()
                next_poll = started + POLL_SECONDS
        except BaseException:
            self._cancel('supervisor_failed')

    def __enter__(self):
        if self._started or self._closed:
            raise SupervisorError('supervisor_already_used')
        self._started = True
        self._poll()
        if self._cancelled.is_set():
            self.close()
        self._thread = threading.Thread(target=self._run, name='cwb-cancel-supervisor', daemon=False)
        try:
            self._thread.start()
        except Exception:
            if not self._thread.is_alive():
                self._thread = None
            self._cancel('supervisor_start_failed')
            self.close()
        return self

    def cancel_check(self, spec):
        """In-memory fast path for ProviderDocker, never waits for SQLite."""
        if (type(spec) is not DispatchSpec or spec.lease.reservation != self.reservation
                or spec.lease.grant_id != self.grant_id
                or spec.attempt_id != self.binding.attempt_id or spec.generation != self.binding.generation
                or spec.profile_digest != self.binding.profile_digest):
            return True
        self._external_cancel()
        if (self._started and self._last_observed is not None
                and time.monotonic() - self._last_observed > STALE_SECONDS):
            self._cancel('authorization_observation_stale')
        return (not self._started or self._closed or self._stop.is_set()
                or self._cancelled.is_set() or (self._thread is not None and not self._thread.is_alive()))

    def raise_if_cancelled(self):
        self._external_cancel()
        if self._shutdown_error:
            raise SupervisorError(self._shutdown_error)
        if self._cancelled.is_set():
            raise SupervisorError(self._reason)

    def close(self):
        if self._closed:
            self.raise_if_cancelled()
            return self.receipt
        self._stop.set()
        self._wake.set()
        with self._lock:
            if self._connection is not None:
                self._connection.interrupt()
        if self._thread is not None:
            self._thread.join(JOIN_SECONDS)
        stopped = self._thread is None or not self._thread.is_alive()
        if not stopped:
            self._shutdown_error = 'supervisor_shutdown_failed'
            self._cancel(self._shutdown_error)
            # Separate short connection: do not overwrite the stuck observer's
            # interrupt handle or claim that this proves thread/runtime cleanup.
            try:
                self._revoked = self._database(self._revoke, track=False)
            except Exception:
                pass
        else:
            # Final durable and local check after joining prevents late success.
            self._poll(final=True)
        self._closed = True
        self.receipt = ShutdownReceipt(stopped, self._cancelled.is_set(), self._reason, self._revoked, self._polls)
        self.raise_if_cancelled()
        return self.receipt

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self._cancel('execution_aborted')
            try:
                self.close()
            except SupervisorError:
                if self._shutdown_error:
                    raise
            return False
        self.close()
        return False
