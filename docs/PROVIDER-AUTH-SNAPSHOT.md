> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Dedicated native credential snapshots

The subsequent [Docker lifecycle composition](SNAPSHOT-PROVIDER-DOCKER.md) now
implements the per-request adapter and post-release hook described below. This
document records the snapshot module's contract; neither module is activated in
the live worker, and owner-recovery hook wiring remains explicit unfinished work.

Local foundation only. No canonical login has been read, copied, changed, refreshed,
or used for inference by this checkpoint. No live service or provider route is
activated. The current provider image and Docker mount qualification do not prove
this new composition. External review is pending the authorized reviewer reset;
no review model was called for this module.

## Trusted API and integration seam

`AuthSnapshots` in `provider_auth_snapshot.py` requires an existing
`ProviderLeases`, exact absolute source file, dedicated source root, separate
destination root, exact `NativeProfile`, source UID, provider GID, logical account,
and persistent owner. It never discovers credentials from environment variables,
CLI configuration, personal homes, or adjacent files. The common top-level
`/home`, `/Users`, and `/root` prefixes are rejected. This guard is not exhaustive
personal-home classification (for example, `/private/Users` and `/var/home` are
not identified by it). The trusted controller must pin the explicitly authorized
dedicated root; configured paths and logical account mappings are deployment
inputs, never request parameters. The filesystem checks do
not independently attest that an operator-selected service path belongs to the
intended real provider account or prove subscription entitlement.

The proposed Linux identities are worker UID959 and provider UID958:GID959. The
worker needs its existing supplementary GID959 to assign that group to a new
snapshot. Construction refuses an unavailable provider group unless the caller
is root, its effective group is the provider group, or its supplementary groups
contain that group. The original dedicated login file remains UID959:GID960 mode0600.
No chmod/chown of the source occurs. The new `auth.json` is worker-owned mode0440
with GID959, under a worker-only0700 directory. Docker's trusted daemon can bind
that exact file readonly; provider UID958 can read its group inside the mount,
but cannot traverse the host snapshot directory. This DAC behavior still needs
actual two-UID Docker composition proof for this helper.

Expected controller sequence, while holding the **existing account flock** across
the entire prepare/create/bind/start seam:

1. Admit the inference request and obtain its `RequestLease` and exact attempt
   generation. There must be no concurrent refresh lease for the account.
2. `prepare(lease, attempt_id=..., generation=...)` publishes once, returning
   non-bearer `AuthSnapshot` metadata. It refuses duplicate publication.
3. `mount_path(...)` rechecks current lease/grant/attempt/dispatch and exact file
   identity immediately before using its path in a **per-request**
   `ProviderDocker` configuration. Caller continues holding the account flock;
   the returned path is not permanent authority. Existing dispatch cancellation
   fences must still apply immediately before Docker start.
4. Complete existing exact container/network cleanup. After ProviderLeases has
   durably set the request to `released` with a trusted cleanup receipt,
   `cleanup(...)` removes only that binding's snapshot. It does not release or
   renew any lease, clear quarantine, retry inference, or refresh credentials.

`ProviderDocker` currently validates its credential path during construction.
Therefore the worker composition must construct it after staging or provide a
reviewed deferred per-request factory. Adding a static config path does not
integrate this module. No existing shared adapter/dispatcher was edited here.
Refresh writers and dedicated CLI logins must participate in the same account
discipline before activation; this helper cannot make an arbitrary external CLI
obey a flock. Refresh requests cannot prepare inference snapshots.

## Identity, atomicity, and data handling

The destination name hashes a private, canonical binding containing account,
persistent owner, reservation epoch and ID, controller instance, request and
grant IDs, lease expiry, attempt/generation, exact native profile digest, exact
source/destination and UID/GID configuration. It is an internal identity, never a
bearer credential. Changing any field selects a different path and does not grant
access to the old snapshot. Current grant and request rows are checked again;
revoked, expired, quarantined, cancelled, stale, or running dispatches cannot
obtain a new mount path. A present dispatch must have the exact native profile.

The helper is a trusted worker component that **does handle secret bytes** while
copying. It must not run in the HTTP relay or Hermes container. It opens the
source without symlink traversal, rejects nonregular/multiply-linked/public or
group-readable files, and caps reads at64KiB. UID and stat identity are checked
before/after reading, including replacement of the configured pathname. Existing
bootstrap validation checks the exact observed Codex/Grok schema and expiry on
the private staged file. JWT expiry/claim parsing is not signature validation.
The provider revalidates the mount inside its disposable container.

Publication writes a unique0700 temporary directory, fsyncs its files and
directory, performs a final authorization check in the controller DB transaction,
then atomically renames within the same destination filesystem and fsyncs it.
Cancellation committed during copying prevents publication. Cancellation after
publication still blocks `mount_path` and the existing dispatch start fence.
Receipts contain bindings and inode/stat metadata, never tokens or a token hash.
No secret appears in returned results, error strings, or repr output. The mutable
copy buffer is overwritten on normal staged completion; Python/JSON parsing can
create immutable secret copies, so this is not a secure-memory-erasure claim.

## Failure and recovery boundaries

Handled staging failures remove only the invocation's fresh temporary files.
An existing publication is never overwritten. Failed authorization, unknown
container cleanup, or an active lease does not authorize credential deletion or
reuse. Published snapshots survive restart privately; a new controller cannot
mount the old lease and may delete it only after durable trusted cleanup.
Cleanup supports a retry after the secret was unlinked but its binding receipt
remains. A missing entire directory is idempotent success only after the same
released-request proof. Unexpected files, symlinks, changed inode/mode, or missing
binding metadata cause refusal, not recursive deletion.

A process crash before publication can leave a private `.pending-*` directory.
A crash after publication before returning leaves a private snapshot whose reuse
still requires authorization. A crash after deleting the binding but before
removing its empty directory leaves an empty directory that cleanup refuses to
attribute automatically. There is no global janitor or broad prune in this
module. Exact reconciliation of such residuals and a crash-qualified deployment
integration remain gates. Existing account ownership is never released merely
because a snapshot is absent. Same-UID malicious host processes, host root, and a
compromised trusted controller are outside this helper's isolation boundary.

Tests use real local SQLite lease/dispatch state and only synthetic credentials.
They cover both saved credential schemas, expiry, source/path/file attacks,
replacement and write races, concurrent staging, grant cancellation during
copying, blocked refresh acquisition, exact generation/account fences, restart,
released-request cleanup, cleanup interruption, and setgid2700 private roots.
They do not establish live provider authorization, real model inference, billing,
Linux mount behavior, or live worker integration.
