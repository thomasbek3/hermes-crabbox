# Authenticated stopped workflow closure

The local controller can now close a qualified workflow whose finalized,
released prefix ends in a rejected review or unmet acceptance. This is the
prerequisite for explicit remediation; it does not start a repair attempt.
The root outcome remains separate from physical cleanup completion.

## Controller operations

`collect_stopped_workflow_proof` independently authenticates the exact frozen
catalog prefix. Every earlier stage must pass. The final stage must contain a
nonpassing decision, and no later step may have been assigned. It checks one
child/request/seat per executed step, broker linkage, ordered prior context in
both launch and request, carried revision/binding relationships, original input,
strict saved decision artifacts and release receipts, qualified environment,
and stable database evidence. File inspection happens against a detached bounded
snapshot; the committing controller rechecks the original database state.
Candidate references do not authorize promotion or cross-root reuse: a future
import must verify and rebind the actual immutable content.

`close_stopped_workflow` rechecks current ownership, authorization, cancellation,
budget and quiescent descendants. It atomically records a public stop result and
its proof hash, revokes grants and completes the root with the evidence-derived
outcome. A rejected review or missing evidence produces `needs_review`; a failed
protected task check produces `rejected`. Assertion failure is not mislabeled
as execution infrastructure failure. Generic successful-root completion guards
remain unchanged; this narrow controller cannot produce a verified outcome.

The existing exact-target root cleanup then stops/releases original runtimes and
provider reservations. Unknown cleanup raises an error and retains capacity.
The completed nonpassing result remains durable, so an explicit retry resumes
cleanup rather than rerunning a stage or rewriting terminal history. Previously
proven cleanup is reused after a failed release commit. No session delivery
pointer/version is advanced and no delivery artifact is created.

`load_stopped_workflow` rereads authenticated evidence and the exact saved stop,
then validates original root/runtime/provider cleanup and matching release event.
The cleanup reader requires a caller-owned transaction, uses bounded canonical
receipts and original identities, and does not consult active account ownership,
invoke callbacks or read files. A newer reservation is left untouched. Stop
results/events contain only the public proof projection; private database paths
remain local. Historical reads require retained evidence and the supported
frozen policy version.

## Verification

Parent integration covers stopped review closure, unchanged child evidence and
session delivery, publication-event rollback, lost terminal-commit acknowledgement,
unknown cleanup and exact reconciliation, release-event rollback with proven
cleanup reuse, current-authority refusal and historical replay after a new account
owner. Additional composed cases use actual protected exit-1 and empty-check
fixtures through task finalization/release, then root closure.

Independent suites exercise strict prefix/projection corruption and generic
historical cleanup corruption, ownership, membership and dispatch receipts.
Final counts and exact source/test receipts are recorded in
`evidence/routed-stop-final-binding.json` and `CHECKPOINTS.md`. Original unsuccessful
fixture runs are retained with their explanations.

These are actual local controller/database/script checks with synthetic model,
Docker and physical cleanup inspectors. No Omarchy service, live schema, image,
credential or deployed worker changed. No real-model or target-host qualification
is claimed. Fable checkpoint approval is still pending its recorded account limit;
no new review call or substitute approval was made here.

## Next dependency

`WORKFLOW-REMEDIATION-NEXT.md` defines explicit same-session fresh-root admission,
versioned failure catalogs, immutable revision import/rebinding, authenticated
rejection context and bounded retry lineage. Existing `enqueue_root` always makes
a new session; legacy resume is not a routed continuation. Those seams must be
implemented before claiming executable remediation or follow-up. Preserve the
original goal/acceptance criteria and historical decisions. The full SPEC and
Release A/B/C requirements remain open.
