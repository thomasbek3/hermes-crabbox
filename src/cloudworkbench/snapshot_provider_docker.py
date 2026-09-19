"""Per-request native auth staging on the existing Docker/lease lifecycle."""
from dataclasses import asdict, replace
import json

from .provider_auth_snapshot import AuthSnapshots, SnapshotError
from .provider_docker import DockerConfig, DockerError, ProviderDocker
from .provider_leases import CleanupTarget, RequestLease


class OwnerSnapshotCleanupError(DockerError):
    def __init__(self, checked_request_ids, pending_request_ids):
        super().__init__('snapshot_owner_material_pending')
        self.checked_request_ids = tuple(checked_request_ids)
        self.pending_request_ids = tuple(pending_request_ids)


class SnapshotProviderDocker(ProviderDocker):
    def __init__(self, config, snapshots, *, cancel_check, command=None):
        if (type(config) is not DockerConfig or type(snapshots) is not AuthSnapshots
                or config.profile_digest != snapshots.profile.digest
                or config.credential_path != snapshots.source or config.gid != snapshots.gid):
            raise DockerError('snapshot_configuration_mismatch')
        self.snapshots = snapshots
        # This facade's base adapter can inspect and terminate without any
        # credential/profile file read. Execution uses a fresh validated adapter.
        super().__init__(config, cancel_check=cancel_check, command=command,
                         recovery_only=True, credential_target='/run/secrets/native-auth.json')

    def validate_leases(self, leases):
        if leases is not self.snapshots.leases:
            raise DockerError('snapshot_lease_manager_mismatch')

    @staticmethod
    def _scope(spec):
        return {'attempt_id': spec.attempt_id, 'generation': spec.generation}

    def _credential_path(self, spec):
        return self.snapshots.expected_path(spec.lease, **self._scope(spec))

    def _execution_adapter(self, spec):
        path = self.snapshots.mount_path(spec.lease, **self._scope(spec))
        config = replace(self.config, credential_path=path)
        return ProviderDocker(config, cancel_check=self.cancel_check, command=self.command)

    def create(self, spec, payload, *, deadline):
        with self.snapshots.leases.account_lock(spec.lease.reservation.account_id):
            # A staging failure leaves a durable zero-RPC journal for existing
            # reconciliation; missing journals are never evidence of no effect.
            self.prepare_request_journal(spec, payload)
            self.snapshots.prepare(spec.lease, **self._scope(spec))
            return self._execution_adapter(spec).create_prepared(spec, payload, deadline=deadline)

    def start(self, spec, resources, *, deadline):
        with self.snapshots.leases.account_lock(spec.lease.reservation.account_id):
            return self._execution_adapter(spec).start(spec, resources, deadline=deadline)

    def after_request_cleanup(self, spec):
        # ProviderDispatch invokes this after durable lease release, and again
        # on cleaned/delivered retries. Failure never reverses physical proof.
        self.snapshots.cleanup(spec.lease, **self._scope(spec))

    def after_owner_cleanup(self, target):
        """Explicit orchestrator step, never called before durable owner proof.

        Returns the exact request IDs checked, not a global material-clean claim.
        More than128 retained request records requires separate bounded recovery.
        """
        if (type(target) is not CleanupTarget or target.scope != 'owner'
                or target.reservation.account_id != self.snapshots.account_id
                or target.reservation.persistent_owner_id != self.snapshots.owner_id):
            raise DockerError('snapshot_owner_cleanup_unconfirmed')
        reservation = target.reservation
        leases = self.snapshots.leases
        with leases.account_lock(reservation.account_id):
            with leases.store._tx() as db:
                owner = db.execute('SELECT * FROM provider_reservations WHERE id=?',
                                   (reservation.reservation_id,)).fetchone()
                if (not owner or owner['account_id'] != reservation.account_id
                        or owner['epoch'] != reservation.epoch
                        or owner['controller_instance_id'] != reservation.controller_instance_id
                        or owner['cleanup_id'] != target.cleanup_id or owner['frozen_at'] != target.frozen_at
                        or not (owner['state'] == 'released' or
                                owner['state'] == 'cleaning' and owner['reason'] == 'root_cleanup_verified')
                        or not owner['cleanup_receipt'] or len(owner['cleanup_receipt']) > 262144):
                    raise DockerError('snapshot_owner_cleanup_unconfirmed')
                try:
                    receipt = json.loads(owner['cleanup_receipt'])
                    expected_target = json.loads(json.dumps(asdict(target)))
                    if receipt['target'] != expected_target or receipt['outcome'] not in ('terminated', 'fenced'):
                        raise ValueError()
                except (ValueError, TypeError, KeyError, RecursionError):
                    raise DockerError('snapshot_owner_cleanup_unconfirmed') from None
                if db.execute("SELECT 1 FROM provider_request_leases WHERE reservation_id=? AND state!='released'",
                              (reservation.reservation_id,)).fetchone():
                    raise DockerError('snapshot_owner_cleanup_unconfirmed')
                rows = db.execute('''SELECT q.*,g.attempt_id,g.generation FROM provider_request_leases q
                    JOIN provider_execution_grants g ON g.id=q.grant_id
                    JOIN provider_dispatch d ON d.request_id=q.id
                    WHERE q.reservation_id=? AND d.profile_digest=? ORDER BY q.id LIMIT 129''',
                    (reservation.reservation_id, self.config.profile_digest)).fetchall()
                if len(rows) > 128:
                    raise DockerError('snapshot_owner_cleanup_scope_limit')
            checked = []; pending = []
            for row in rows:
                lease = RequestLease(reservation, row['id'], row['grant_id'], row['purpose'], row['expires_at'])
                try:
                    self.snapshots.cleanup(lease, attempt_id=row['attempt_id'], generation=row['generation'])
                except SnapshotError:
                    pending.append(row['id'])
                else:
                    checked.append(row['id'])
            if pending:
                raise OwnerSnapshotCleanupError(checked, pending)
            return tuple(checked)
