# Omarchy operations and recovery

Target: separate Intel MacBook Pro, hostname `omarchy`, Tailscale address `100.83.74.92`. The Mac minis are clients/backup destinations. Confirm hostname before a host operation.

## Current deployment

- `cloud-workbench-api.service`: loopback port7780, account cloud-control, no Docker/provider credential access.
- `cloud-workbench-worker.service`: account cloud-worker, Docker dependency, bounded job containers and root-owned volume helper.
- `cloudd`: existing legacy service on7777; preserve it and its data during all v2 rehearsals.
- API and worker were active but disabled at boot. Both v2 units were enabled on2026-09-17; no restart or legacy change occurred. Boot execution is still unqualified.
- Private dashboard uses Tailscale Serve HTTPS. Application client authentication remains required.

Process `active` and HTTP `/health` only prove liveness. Authenticated `/v1/ready` must also pass. The credential-readiness change is currently local pending coordinated auth deployment; the deployed callback only checks worker heartbeat/errors.

## Read-only checks

```sh
hostname
systemctl is-active cloud-workbench-api cloud-workbench-worker cloudd
systemctl is-enabled cloud-workbench-api cloud-workbench-worker
systemctl show -p After -p Requires cloud-workbench-worker
curl --fail --silent http://127.0.0.1:7780/health
lsblk -o NAME,TYPE,FSTYPE,MOUNTPOINTS
```

Use the named client's existing private token file through `cloud2`; never paste a token into command arguments, a URL, a checkpoint or logs. Readiness, active attempts, reservations and runtime labels must agree before maintenance. Keep useful existing jobs and credentials intact.

## Reboot/lid/network rehearsal: pending

The root disk is LUKS encrypted. A reboot can require physical disk unlock, so first arrange Thomas's availability at the laptop and an agreed maintenance window. Do not reboot solely because SSH/sudo is available. AC/lid/sleep policy has not been changed or qualified.

Before rehearsal: finish or safely drain v2 work, save a consistent encrypted off-host backup, record v1 and v2 active-job IDs plus service/config/image/manifest identities, and ensure physical console access. Retain the exact prior configuration and immutable environment versions. Do not force an outage while a personal task is using the laptop.

After unlock/reconnection: verify hostname, services, mounts, authenticated readiness, session/attempt state and artifact hashes. Confirm interrupted attempts are truthful, reservations are released only after stop is confirmed, and no automatic replay duplicates tool effects. A follow-up must explicitly select the supported reconstructed continuation mode. Container disappearance is not successful task completion.

The fixed-purpose volume helper verifies root-owned backing image/metadata, loop backing identity, filesystem and mount options. It can remount an existing volume, but helper logic is not physical reboot proof. Never replace a missing mount with an empty directory or delete its image to get a green health check.

Test client disconnect separately from host network loss. Live SSE disconnect/reconnect is proven in `evidence/repository-provider-smoke.json`; it is not proof of laptop sleep/network recovery.

## Backup and rollback

See [OFFLINE-BACKUP-RESTORE.md](OFFLINE-BACKUP-RESTORE.md) for canonical quiesced encrypted backup and isolated restore receipts. Restored clients remain disabled. No restore is allowed over the live database during a rehearsal.

A v2 code/config rollback requires idle/drained v2 attempts and the recorded matching source/image/environment configuration. Stop only the two v2 services, restore that verified snapshot, restart, then verify readiness and durable session state. Do not lower immutable environment records or replay completed attempts. If migration changed schema incompatibly, keep the live database intact and inspect an isolated restored copy before proceeding.

Port7777 has not been cut over. Legacy rollback currently means leave the existing legacy service and endpoint unchanged; no production migration has been claimed. Future endpoint cutover needs an independently verified compatibility/import/rollback plan.

No automatic purge or retention schedule is enabled. Retention manifests are proposals until the owner adopts a policy; live attempts, shared inputs and credentials need distinct treatment.
