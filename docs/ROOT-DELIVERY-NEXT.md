> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Atomic root delivery checkpoint

Local implementation exists in `routed_delivery.py`, `routed_delivery_policy.py`,
`delivery_schema.py`, and the scheduler's dedicated delivery-cleanup methods.
This supersedes the design-only status previously recorded here. No live schema
migration, routed-worker activation, remote qualification, or deployment was
performed for this checkpoint. The full six-stage workflow and Release A/B/C
requirements remain unchanged.

## Implemented behavior

Explicit quiescent schema v2-to-v3 migration adds session delivery pointers and
version, immutable admission bases, and immutable delivery intents. Opening a
store does not migrate it or relabel legacy completions as verified deliveries.
The expected base is frozen at admission; finalization never refreshes it.

`prepare_root_delivery` authenticates every frozen stage, the original and
selected revision bytes, carried input/output relationships, exact prior context,
protected acceptance where required, and child release evidence. It stages a
private immutable document outside SQLite, records the exact intent, revokes
broker grants and moves the root to verifying. Both raw and decoded JSON strings
are checked against the controller's supplied forbidden-value policy.

`finalize_root_delivery` revalidates evidence and performs exact-target physical
cleanup outside database transactions. Root and account capacity remains held
until the final transaction. That transaction checks current authorization,
project/session state, budget, cancellation, generation, pending work, evidence,
intent and admission base; registers the delivery artifact; compares and advances
the session version; completes the root; releases capacity; and appends all
publication events atomically. Failure rolls back these database changes.
Physical cleanup already proven is reused, never treated as an instruction to
rerun a job. Unknown cleanup retains capacity until the exact frozen target is
reconciled by the trusted verifier.

Successful coding delivery requires protected acceptance and publishes the
selected workspace revision. Report-only workflows remain unverified, update
the last artifact/version, and preserve the existing delivered workspace pair.
The public document excludes private controller storage paths.

`load_root_delivery` authenticates the immutable intent, workflow proof, artifact
bytes/metadata/events, and original cleanup target/runtime/provider/release
receipts. It ignores newer active account owners and current session pointers.
A lost final-commit acknowledgement can be resolved with this historical read;
there is no automatic inference or transaction-body retry. Contending callers
may fail closed under the existing bounded transaction deadline; a published
winner is unique and later explicit reads return its exact receipt.

`cleanup_cancelled_root_delivery` performs or reuses exact cleanup and releases
capacity in one cancellation transaction, without publishing or modifying the
session delivery. Its historical replay uses the same saved-cleanup validator
as success. Both a root cancelled before cleanup preparation and cancellation
after the verifying target freezes retain their exact physical target.

The Store cancellation fix omits a redundant state assignment for live attempts:
only the cancellation flag changes. Reassigning verifying had incorrectly
re-triggered the scheduler start guard once cleanup began retaining fences.

## Evidence and boundaries

`evidence/root-delivery-integrated-tests.xml` records 154 passing delivery,
cleanup, schema, Store and root-cleanup tests. This includes atomic rollback,
post-commit lost acknowledgement, cancellation, escaped-secret scanning,
staged-document tampering, stale-base/authorization refusal, concurrent
finalizers and inert historical replay.
`evidence/delivery-historical-cleanup-final-tests.xml` records ten independent
historical-proof corruption and terminal-cancellation replay checks.
Report-only parent publication is recorded separately in the final checkpoint.

The fixtures use actual local controller/database/broker/decision/revision paths
and local protected scripts. Provider/model inference, Docker execution and
physical cleanup inspectors are synthetic. Offline corruption tests intentionally
bypass immutable guards only in disposable fixture databases. No target-host,
real-model, current-source Linux isolation or production acceptance follows from
these test counts. Original failed runs remain available with their disposition
in `docs/CHECKPOINTS.md`.

## Remaining work

1. Explicit bounded remediation/resubmission for rejected or needs-review stages;
   preserve the original decisions.
2. New-controller ownership recovery and exact cleanup reconciliation.
3. Trusted follow-up revision import/rebinding. WorkspaceRevision hashes include
   root/attempt bindings, so a session pointer alone does not authorize reuse.
4. Production root-worker composition and source-bound Linux/Docker qualification,
   including schema migration/reopen on the target SQLite version.
5. Real provider/model/effort qualification and full six-stage acceptance, then a
   concrete activation candidate preserving existing jobs and credentials.
6. Remaining SPEC.md and ACCEPTANCE-MATRIX.md requirements. Fable checkpoint
   review remains pending its previously recorded account limit; Grok's earlier
   REVISE is not approval of these subsequent fixes.
