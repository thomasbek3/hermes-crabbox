> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Durable D9 inference accounting foundation

This module is a local controller foundation. It is **not wired to ProviderDispatch,
Store, the relay, live accounts, or runtime cancellation**. No live database/schema
has been changed. Parent/turn/owner/project lineage must come from a separately
qualified controller root-admission record, never inferred from historical attempts.

## Contract

`inference_budget.ensure_schema(db)` explicitly initializes five private-named
additive tables inside an existing transaction. A partial/incompatible version is
refused. The caller chooses/protects the database and SQLite durability settings;
this module does not open connections, change Store schema, commit, or perform IO
outside supplied SQL. Use the **same database and transaction as dispatch creation**.
All operations require `db.in_transaction`. **The controller must begin with
`BEGIN IMMEDIATE` before any authorization or other reads.** A defensive no-op
write inside budget operations also obtains a write lock for deferred callers that
have not read yet; it cannot rescue a previously established read snapshot under
contention. Python SQLite exposes `in_transaction`, not a reliable transaction-mode
query, so the outer transaction requirement is a caller contract. SQLite busy/storage
errors remain caller-visible; retry the entire transaction, never only its tail.

`BudgetPolicy()` freezes defaults: 128 requests/root, 32/attempt, refill60/minute,
burst4, root wall14400seconds, cleanup reserve160seconds. Explicit bounded operator
configuration is accepted and stored canonically with its SHA256. This policy is
not a token/spending authorization. Rate is a token bucket (one token per second
at defaults), not a fixed calendar-minute window. An idle bucket holds at most4. This is explicitly **per attempt**, as specified
in D9;32independently admitted attempts could consume the128root count at one
clock instant. A root/account rate bucket is not part of this policy. The role
controller still must enforce D4 depth2 /32total seats and D2 active-slot/account
serialization limits before execution. This module bounds requests, not seat
registrations or retained-generation metadata; those need the controller admission
limits and later explicit retention, with no silent deletion/reset here.

`RootScope(owner_id, project_id, session_id, turn_id, root_attempt_id,
root_generation)` is immutable provenance. `register_root(db, scope, policy,
admitted_at=...)` is controller-only and runs at compute admission; queue time is
separate. Re-registering exact values is idempotent; changed policy/scope/admission
is a conflict. Root deadlines never move after registration. A recovery generation
is not a new root scope and cannot restart its clock.

`AttemptScope(root, attempt_id, generation)` is registered with
`register_attempt(db, scope, parent=parent_scope, deadline_at=...)`. Only the root
has no parent. A child's parent must already be registered at its current budget
generation and in the identical root scope. Attempt deadlines cannot outlive the
root or parent. Omitted deadlines mean the root deadline; descendants of a shorter
parent must pass that shorter deadline explicitly. An attempt's root, parent and
parent-generation reference, and deadline cannot be rebound. No legacy-field
ancestry fallback exists.

An explicit generation advance uses `previous_generation=old`, `generation=old+1`.
The attempt's lifetime counter and rate bucket are shared across generations.
The old generation is refused. The original parent-generation reference stays
frozen. Admission/replay and new descendant registration refuse stale ancestor
generations, including grandparent changes. A stale child cannot advance by rebinding
its frozen parent reference; the controller must fence/cancel that lineage and, if
its reconstruction policy permits, register a distinct child attempt under the new
parent. Counter/rate history on the original child and root never resets. This
module does not terminate containers or perform that reconstruction. The defensive
ancestry walk refuses cycles and chains beyond64links; actual role admission must
enforce its much smaller configured depth/seat limits. Initial registration, generation advancement
and all admission entry points must remain inaccessible to model/plugin callers.

`admit(db, scope, nonce=64hex, payload_digest=64hex, profile_digest=64hex)` returns
an immutable `Admission`. Identity is `(attempt_id,generation,nonce)`; exact replay
returns `decision='replay'` with its original request deadline and no charge.
Changed payload/profile is `BudgetError('budget_nonce_conflict')`. A new nonce is
a new call even for identical payload. An identical nonce in a deliberately new
generation is a different request identity, sharing the lifetime attempt/root
counters. Capability/account IDs are absent from accounting; rotating those cannot
reset a budget. Replay does not authorize runtime start or relax dispatch cleanup,
expiry, grant, cancellation, or response-delivery rules.

Fresh accepted requests consume one root count, attempt count and bucket token.
Their request deadline is the **end of the whole operation**, bounded by
root/attempt remaining time and the frozen160second admission/execution/cleanup
window. Despite the policy field name `cleanup_reserve_seconds`, this is the full
minimum window, not160seconds added after provider execution: D9 defines120s provider
execution +10s stop grace +20s forced cleanup +5s collection +5s admission. Execution
must stop in time to leave those cleanup components; `request_deadline_at` is not
permission for160seconds of provider execution. No new request is accepted with
less than that reserve. Admission with exactly160seconds remaining is intentional; its all-inclusive
deadline equals the root deadline. Root expiration yields `root_budget_exceeded`; attempt
expiration, rate, count and reserve refusals have distinct fixed reasons. Rate
refusals include a bounded retry delay. These are public policy outcomes, not
provider errors. Integration must make them visible and implement descendant
fencing on root expiration; returning a reason alone does not stop running work.

## Same-transaction integration seam

The controller must check current attempt/generation/cancellation, profile/grant,
root lineage and account authorization **inside the supplied transaction**, then
call budget admission and create the dispatch row in that same transaction.
`charged` and `replay` are accounting decisions, never authorization. All ID
arguments are trusted database references, not bearer capabilities.

```python
with controller_transaction_begin_immediate() as db:
    authorize_current_attempt_and_grant(db, trusted_scope)
    outcome = budget.admit(db, trusted_scope, nonce=nonce,
                           payload_digest=payload_hash, profile_digest=profile_hash)
    if outcome.decision != 'refused':
        dispatch = create_or_replay_dispatch(db, outcome.request_deadline_at)
# Emit a refusal/error only AFTER the transaction commits its observation.
if outcome.decision == 'refused':
    raise PublicBudgetRefusal(outcome.reason)
```

A failure after charging must roll back **the complete caller transaction**, which
rolls back counters, nonce and dispatch together. Module operations also have a
savepoint: an internal SQL failure cannot leave half a charge if the caller catches
it. Callbacks must not commit inside this seam or perform Docker/network calls.
ProviderDispatch's current exception-rollback wrapper requires a staged refusal
path before integration; blindly raising on `decision='refused'` inside it is wrong.

## Clock/restart behavior and limits

Times use validated finite wall-clock seconds, persisted as integer microseconds.
A root-wide high-water timestamp is updated for every normally returned admission,
including refusal and replay. A clock behind this committed observation refuses
all requests/replays with `budget_clock_rollback` until it catches up. This prevents
refilling buckets or reviving an observed deadline through clock rollback, including
new controller instances. All descendants share that clock fence. Committed expiry
remains fenced, and new capabilities do not participate in the state.

**Ordinary refusals must commit their high-water observation.** A caller rollback,
including a process death before commit, necessarily discards that observation.
This module cannot remember an uncommitted future timestamp, detect elapsed real
time during a backward clock interval never observed in a committed transaction,
or make wall time monotonic across arbitrary host-clock rewrites. Production must
also supervise elapsed deadlines with a monotonic clock during execution and
handle host clock health; wall-clock accounting alone is not that operational proof.

## Clock incident operations

On `budget_clock_rollback`, stop new admissions for the affected root and inspect
host clock health without changing stored timestamps or counters. If time safely
catches up before its original deadline, recheck the same root. If it cannot,
controller-cancel/fence descendants, reconcile exact runtime/provider cleanup, and
report the interrupted root. A separately authorized reconstructed follow-up may
create a new root under its ordinary admission policy; never reset the old root,
rewrite its high-water value, or reuse a generation to bypass the clock fence.
This is a manual runbook; a production incident command is not implemented here.

## Evidence and remaining integration

`tests/test_inference_budget.py` uses disposable SQLite DBs, multiple connections,
threaded concurrent admissions and actual subprocess exits before/after the combined
charge/dispatch commit. It covers changed payloads, immutable ancestry/policy,
shared descendant caps/clock, generation/restart persistence, bucket refill, cleanup
reserve boundaries, rejected schemas and caller/internal rollback. It uses no
provider, credentials, personal files or live services.

Remaining: qualified root/attempt admission lineage, same-transaction ProviderDispatch
wiring, visible refusal reporting, cancellation/fencing, controller monotonic runtime
deadlines, worker-death reconciliation, real relay/provider lifecycle proof and
explicit deployment. The standalone module does not claim any of these completed.
