# Read-only authority check for active provider calls

`budget_authority.py` adds a controller-internal predicate for an **already active**
provider call. It does not charge inference, create tables, issue a grant, stop a
container, or replace the cancellation supervisor's account/grant/profile predicate.
No live state has been touched. Supervisor/Store/dispatch files remain unchanged
in this checkpoint.

## Integration API

```python
authority = BudgetAuthority(frozen_attempt_scope)  # controller-owned AttemptScope
# Inside supervisor._database, while its busy/progress deadline remains installed:
decision = authority.check(db, now=time.time())
# Existing immutable grant/account/profile predicate AND decision.allowed must pass.
```

`BudgetAuthority` and `AuthorityDecision` are frozen dataclasses. A decision contains
`allowed`, a fixed `reason`, and optional minimum `deadline_at`/`policy_digest`.
Constructor scope must be the exact trusted `AttemptScope` already frozen for the
root/attempt, not identity from tool/HTTP arguments. No `None` fallback exists.
The supervisor must verify its own AttemptBinding matches that scope before use.
An allowed decision is only a lifecycle/budget predicate; it is never a bearer
credential or an independent public execution authorization.

`check` requires an existing SQLite connection and an explicit finite wall-clock
observation. It executes one SELECT, opens no connection, starts/ends no transaction,
installs no progress handler, calls no clock/policy callback, and performs no writes.
Its single recursive query provides one coherent snapshot of the entire lineage.
Call it within the supervisor's already bounded database operation, using its fresh
connection/snapshot. Do not retain an old read transaction across supervisor polls.
SQLite busy/interruption/missing-schema errors propagate into the supervisor's
existing fixed fail-closed error handler; no raw SQL error should reach users.

## What is checked

- Exact canonical frozen owner/project/session/turn/root-attempt/root-generation
  scope, exact supported budget schema version, canonical validated policy/hash,
  and original root-generation registration.
- Every ancestor link from leaf to explicit root, current budget generation and
  its registration, and the same current generation in the actual Store attempt.
  Recovery of any ancestor invalidates an old frozen parent-generation edge.
- Every actual Store attempt's session/turn/owner/project and turn-to-session
  relationship, live state, `cancel_requested == 0`, and unrevoked owning client.
  Missing attempts, clients, turns, budget records or parent links refuse access.
- Root deadline equals original admission plus the frozen wall policy, attempt
  deadlines are valid and fit their ancestors/root, and the observation precedes
  every applicable deadline. Equality means expired.
- The observation is not behind the durable root clock high-water mark. All
  descendants inherit that mark. Invalid policy, clock, generation or malformed
  records fail closed; cycles/deep or unterminated ancestry cannot pass.

The query returns at most 65 lineage rows, with each selected string explicitly
bounded. It uses indexed IDs and does not query model prompt/result/credential
fields. The caller's existing SQLite VM progress handler and busy deadline remain
in force; these do not guarantee bounded uninterruptible OS/disk I/O. Normative D4 requires the controller to enforce depth/seat limits much tighter
than this defensive64-link cap; this predicate does not implement or prove that
role-admission policy. Worst-case selected strings are under1MiB across65rows
(excluding Python/SQLite object overhead); repeated root JSON is included in that
conservative bound, not treated as one copy.

The check intentionally does **not** reject exhausted request counters/rate tokens
or apply the new-request 160-second reserve again: a previously admitted last call
must be able to finish within its existing phase deadlines. It does not reinterpret
a client's current project-list grant after execution grant issuance; the existing
ProviderLeases execution-grant semantics govern that, and grant revocation remains
part of the supervisor's other predicate. Scope identity still must match exactly.

## Topology, archive and rollout decisions

Budget ancestry is authoritative only because it is explicitly registered by the
trusted root/role admission controller. `attempts.parent_attempt_id` is deliberately
not consulted: current Store `_new_attempt` / `resume` use it for reconstructed
resume provenance, potentially outside the new root's execution tree. Requiring
root parents to be NULL would reject a real Store resumed attempt registered as
an explicit new budget root. Tests cover that real resume path and a deliberately
divergent legacy pointer. No legacy field may infer, rewrite or weaken budget
ancestry. Future Store topology semantics require an explicit contract/migration;
do not quietly reinterpret the legacy field.

Every seat currently must share the frozen root session/turn. This is an explicit
foundation contract, not proof of future child schema. If future seats require
separate turns, that requires a reviewed authority binding/schema change first;
this implementation will fail closed. `Store.archive` only sets presentation
metadata and emits `session.archived`; it does not cancel or revoke existing work.
The predicate intentionally preserves that behavior; actual archive API coverage
verifies a live un-cancelled root remains allowed. Use cancellation/revocation to
end work, not archive.

**Rollout ordering gate:** enable this conjunct only for a new routed provider
path whose controller has explicitly initialized the budget schema, atomically
registered the trusted root/attempt lineage and policy, and bound that scope before
issuing grants/starting provider dispatch. Do not attach it indiscriminately to
historical direct-Claude or other unregistered live work. Drain/reconcile existing
experimental routed calls before enabling the new mandatory route; preserve their
history, and do not fabricate ancestry/registration from legacy rows. Missing
schema raises into the supervisor's fail-closed handler; missing registration is
an explicit refusal. There is no temporary allow-on-missing switch. Tests reproduce
both cases against an actual live Store attempt without changing its state. This
checkpoint implements no deployment feature flag or live migration; the parent
must qualify activation ordering and sticky revocation in its integration.

## Cancellation and clock contract

Every refusal/error must make supervisor cancellation sticky, trigger its exact
bound grant/request revocation path, stop further starts, and participate in final
post-join validation. A pure predicate alone does none of that. Parent/ancestor
cancellation must remain detectable while Docker collect is running, not just at
next inference admission. Actual stop/remove/credential cleanup proof remains
separate from supervisor-thread termination or a false authority result.

This module reads the **already committed** clock high-water mark; it never advances
it. A forward observation made only by this read operation is not durable. The
supervisor must latch/refuse expiry/rollback and durably revoke its bound authority;
controller runtime phase timers still need monotonic deadlines. Do not claim this
SELECT makes an arbitrary wall clock monotonic or survives a discarded observation
before any cancellation/revocation is persisted.

## Evidence and limitations

Tests instantiate actual Store tables and run unchanged supervisor `_database`
with this new predicate. They prove zero writes under `PRAGMA query_only`, one
SELECT/no transaction commands, preserved progress interruption, exact root scope,
revocation, generation and deadline refusal, missing/cyclic/corrupt ancestry,
policy/hash validation and no accidental cancellation at the request-count cap.

Current Store's legacy unique live-session/turn/agent indexes prohibit simultaneous
live root and child seats in one turn/session. Root-only tests preserve all of those
indexes. An explicitly named disposable fixture removes only those three indexes
for future-seat ancestor tests while retaining real Store table/foreign-key shapes.
That is a **synthetic projection, not evidence of production child admission**.
Future role/Store integration must qualify the real schema/admission invariant.

Remaining acceptance: parent wires this predicate as an additional conjunct in the
bounded supervisor, binds exact AttemptScope/AttemptBinding, and proves root and
ancestor cancellation during an active provider operation, sticky revocation,
cleanup and no successful final response. This checkpoint contains no remote,
provider, token, personal-file or live service operations.
