"""Same-instance recovery of one quarantined request; crash adoption is separate."""
from dataclasses import dataclass
from .inference_relay import AttemptBinding
from .provider_dispatch import ProviderDispatch, DispatchError
from .provider_leases import Reservation


@dataclass(frozen=True)
class RecoveryReceipt:
    request_id: str
    reconciled: bool
    cleanup_confirmed: bool


def recover_request(dispatch, request_id, *, reservation, grant_id, binding):
    if (type(dispatch) is not ProviderDispatch or type(reservation) is not Reservation
            or type(binding) is not AttemptBinding):
        raise DispatchError('recovery_binding_invalid')
    if reservation.controller_instance_id != dispatch.leases.instance_id:
        raise DispatchError('recovery_requires_owner_reconciliation')
    with dispatch.leases.account_lock(reservation.account_id):
        spec = dispatch.spec(request_id)
        if (spec.lease.reservation != reservation or spec.lease.grant_id != grant_id
                or spec.attempt_id != binding.attempt_id or spec.generation != binding.generation
                or spec.profile_digest != binding.profile_digest):
            raise DispatchError('recovery_binding_invalid')
        row = dispatch.read(request_id)
        if row['state'] in ('cleaned', 'delivered'):
            # Durable physical cleanup may precede a failed private-material hook.
            dispatch.cleanup(request_id)
            return RecoveryReceipt(request_id, False, True)
        if row['state'] != 'quarantined':
            raise DispatchError('recovery_requires_quarantine')
        reconciled = False
        if row['uncertain']:
            dispatch.reconcile(request_id)
            reconciled = True
        dispatch.cleanup(request_id)
        if dispatch.read(request_id)['state'] != 'cleaned':
            raise DispatchError('recovery_cleanup_unconfirmed')
        return RecoveryReceipt(request_id, reconciled, True)


def recover_previous_owner(dispatch, *, reservation, takeover_authorized=False):
    """Fence an old controller's reservation, settle evidence, then release it.

    This never adopts old execution grants or starts/retries provider work.
    Unresolved effects leave the old owner quarantined and unavailable.
    """
    if type(dispatch) is not ProviderDispatch or type(reservation) is not Reservation:
        raise DispatchError('recovery_binding_invalid')
    if reservation.controller_instance_id == dispatch.leases.instance_id:
        raise DispatchError('recovery_requires_previous_controller')
    if takeover_authorized is not True:
        raise DispatchError('recovery_requires_takeover_authorization')
    with dispatch.leases.account_lock(reservation.account_id):
        current = dispatch.leases.current(reservation.account_id)
        if current is None or current['reservation'] != reservation:
            with dispatch.store._connect() as db:
                old = db.execute('''SELECT r.*,a.persistent_owner_id FROM provider_reservations r
                    JOIN provider_accounts a ON a.account_id=r.account_id WHERE r.id=?''',
                    (reservation.reservation_id,)).fetchone()
            if (old is not None and old['state']=='released' and old['cleanup_receipt']
                    and old['account_id']==reservation.account_id
                    and old['persistent_owner_id']==reservation.persistent_owner_id
                    and old['epoch']==reservation.epoch
                    and old['controller_instance_id']==reservation.controller_instance_id):
                return ()
            raise DispatchError('recovery_binding_invalid')
        dispatch.leases.quarantine(reservation)
        with dispatch.store._connect() as db:
            rows = db.execute("SELECT request_id,state,uncertain FROM provider_dispatch WHERE reservation_id=? AND state NOT IN ('cleaned','delivered','failed')",
                              (reservation.reservation_id,)).fetchall()
        for row in rows:
            if row['uncertain'] or row['state'] in ('create_intent','start_intent'):
                dispatch.reconcile(row['request_id'])
        dispatch.leases.cleanup_owner(reservation)
        if dispatch.leases.current(reservation.account_id) is not None:
            raise DispatchError('recovery_cleanup_unconfirmed')
        return tuple(row['request_id'] for row in rows)
