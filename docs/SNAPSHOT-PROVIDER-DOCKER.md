> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Native auth staging in the admitted Docker lifecycle

`SnapshotProviderDocker` composes `AuthSnapshots` with existing `ProviderDocker`
and `ProviderDispatch`. This is locally tested integration, not an activated
provider route. No real login files, remote host, paid inference, or model reviewer
were accessed in this checkpoint.

Construct the facade with the exact native Docker profile and snapshot manager.
`DockerConfig.credential_path` must equal that manager's dedicated **source** path;
it is a trusted configuration identity and is never mounted by the facade. The
snapshot manager must use the same `ProviderLeases` instance as ProviderDispatch,
the profile digest must match, and its provider GID must match Docker's GID959.
ProviderDispatch invokes the facade's trusted `validate_leases` hook during
construction; a different manager object is refused immediately, before admission
or lock acquisition. Generic adapters without that hook retain their behavior.
Use the facade's cleanup callback as that lease manager's trusted verifier.
The worker UID959 stages snapshots; provider UID958 receives only its one readonly
snapshot file, and the gateway receives no credentials.

On admitted-request launch, the existing account flock spans staging and Docker
creation. First the adapter writes its **existing** private request journal and
bounded request file without calling Docker. It then stages/validates the exact
snapshot and constructs a fresh normal ProviderDocker configured with that path.
`create_prepared` requires a pristine, matching zero-effect journal and unchanged
request payload before any Docker RPC. Thus an auth/parser error leaves durable
known-no-RPC state for existing reconciliation. Absence of a journal, or an unknown
Docker operation recorded in it, remains uncertain and cannot release the lease.
No second lifecycle database or credential refresh path was added.

Before start, the facade rechecks the current request and snapshot identity and
constructs a fresh validated execution adapter. Profile/credential failure blocks
start. Inspection and reconciliation derive expected mount identity from trusted
lease metadata without opening either source or snapshot credentials. Container
termination uses the original exact IDs, image, labels, and operation journal.
It does not require current authentication, a readable credential, or an intact
profile file. A plain `ProviderDocker(recovery_only=True, credential_target=...)`
also supports these cleanup operations but categorically rejects `create`,
`create_prepared`, and `start`. Its only pre-execution helper initializes the
existing local journal; it makes no Docker calls and grants no execution access.
The legacy constructor defaults and direct-Claude path remain unchanged.

ProviderDispatch invokes optional `runtime.after_request_cleanup(spec)` **after**
ProviderLeases commits trusted physical cleanup and request release. It also runs
on cleaned/delivered retries. The facade removes only the exact released
snapshot. A hook failure yields fixed `post_cleanup_material_pending`; the already
committed container cleanup is not reverted or labelled unknown. A later retry
only retries local material cleanup, not Docker stop/remove calls. If unknown
content replaced the snapshot, physical cleanup succeeds but that file is
preserved with an explicit material-cleanup failure. Missing snapshot credentials
can be reconciled using the retained exact binding receipt.

Direct `ProviderLeases.cleanup_owner` does not pass through ProviderDispatch's
post-request hook. Owner-recovery callers must explicitly call
`runtime.after_owner_cleanup(target)` with their trusted frozen cleanup target
**after durable owner release** (or the existing verified retained root fence).
This bounded helper verifies that exact target against the committed receipt,
refuses any unreleased request under that reservation, then removes only its
released native-profile snapshots. A newer reservation and its files are never
selected. It returns the exact request IDs checked and refuses scopes with more
than128 historical request rows; it is not a global material-clean claim.
For a typed per-row `SnapshotError`, the helper continues through its already
validated exact rows and then raises `OwnerSnapshotCleanupError` with the fixed
code `snapshot_owner_material_pending` and checked/pending request-ID tuples.
It neither deletes mismatched material nor hides partial cleanup. Other error
types still abort. The128-row bound remains unchanged.
The root orchestrator is not yet wired to call this helper. This checkpoint does
not claim a global retention or orphan sweep. The private `.pending-*` crash-residual limits
in [PROVIDER-AUTH-SNAPSHOT.md](PROVIDER-AUTH-SNAPSHOT.md) also remain explicit.

Tests execute real local lease/dispatch SQLite transactions, flock, filesystem
publication and bootstrap schema/expiry parsing, with synthetic credentials and
fake Docker argv/results. Linux UID/GID ownership metadata is simulated for the
Mac test host; byte copying, private modes, request journals and identity binding
remain real. These tests establish ordering, exact readonly mount selection,
no-secret gateway/argv, cancellation/start fences, durable no-RPC reconciliation,
unknown-effect retention, recovery without auth, and post-release cleanup retry.
Actual Linux two-UID bind mounts, production worker wiring, owner-recovery sweep,
and the external review remain separate activation gates.
