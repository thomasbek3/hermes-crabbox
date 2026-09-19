# Worker operations and recovery

[Host installation](HOST-INSTALL.md) · [Caller setup](QUICKSTART.md)

Use the host and paths recorded in `/etc/cloud-workbench/install-receipt.json`.
Identify the actual machine before making changes. Do not use hostnames or
addresses from historical test records as deployment targets.

## Read-only checks

```sh
hostname
systemctl is-active cloud-workbench-api cloud-workbench-worker cloud-workbench-mcp
systemctl is-enabled cloud-workbench-api cloud-workbench-worker cloud-workbench-mcp
systemctl status cloud-workbench-grok-refresh.timer
curl --fail --silent http://127.0.0.1:7780/v1/health
```

Then run the caller's `scripts/check_connection.py` with its dedicated service
credential. Service liveness, caller read access and provider execution are
separate checks. Never print auth JSON or raw headers during diagnosis.

## Existing installations and interrupted setup

The fresh-host installer refuses conflicting paths and existing unmanaged
services. Do not remove those checks or delete state to make a rerun pass.
Inspect the installation error and its completed steps. Preserve existing jobs,
credentials and image identities. An upgrade or takeover needs an operator plan
that matches the current configuration; historical activation scripts are not
an upgrade mechanism.

## Maintenance and recovery

Drain or finish active tasks before intentional service disruption. Preserve a
consistent backup of state, configuration and credentials using the owner's
approved encrypted backup process. See [backup design](OFFLINE-BACKUP-RESTORE.md)
for implementation details and limitations.

For machines with disk encryption, arrange console access before rebooting.
A laptop may sleep or lose network when its lid closes; test that machine's power
policy separately. Neither SSH access nor an installer receipt proves reboot or
sleep recovery. Avoid restarting an entire host to diagnose one service.

After maintenance, check services, authenticated task access, retained session
state and result hashes. Confirm interrupted attempts are reported truthfully
and cleanup releases only resources belonging to those attempts. A container
stopping does not mean the assignment succeeded.
