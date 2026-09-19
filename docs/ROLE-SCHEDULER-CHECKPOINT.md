# Role scheduler first implementation checkpoint

Local SQL foundation only. Schema 1 remains the default; opening Store never
migrates it. There has been no production migration, provider call, image change,
or runtime launch from this checkpoint. Models in tests are synthetic fixtures.

`Store.migrate_scheduler()` explicitly migrates a stopped, backed-up controller DB
to schema 2 in one transaction. It refuses live attempts, active account reservations,
and ambiguous legacy generations. Existing rows become `legacy`; their previous
columns, events, results, artifacts and provider history remain unchanged. A new
root_sequence encodes legacy ordering. New roots/children start at generation 1.
All three broad indexes and both provider legacy guards are replaced, plus guarded
root/child identities and one-child occupancy. Reopening verifies required guards.
The old schema-1 binary rejects schema 2. Backup/restore and stopped-writer discipline
remain operator requirements; this API is not an online migration protocol.

## Executable local seam

`RoleScheduler(store, leases, clock=...)` requires the explicitly migrated DB.

1. `enqueue_root(principal, request, key, frozen=...)`: creates session/turn/root
   atomically and idempotently. `agent` must be Hermes. Controller-supplied frozen
   snapshot has exactly `accounts`, `role_plans`, `provenance`; the first maps registered
   account IDs to persistent owner IDs. The second maps role IDs to complete ordered
   lists of `{ready: true, profile: {...}}`. Root provenance, plans, and seats are
   durable immutable records. This is a controller interface, not model arguments.
2. `admit_root(id, accounts=...)`: matches frozen accounts, obtains ordered account
   locks before SQLite, then atomically reserves all accounts, registers explicit
   root budget ancestry and claims root preparing. One admitted root reserves both
   host slots. Existing legacy runtime or another retained root blocks admission.
3. Construct RoleBroker on the **same DB**, with `authorize_parent=scheduler.authorize_parent`
   (`shared_group=True` if using the existing 0660 API/worker service boundary). Root-only
   authorization rejects child scopes, expired budget/lifecycle, revoked clients,
   cancelled root, and un-reconciled ownership after controller restart.
4. Native request `broker.prepare(...)` durably records correlation/payload first.
   `admit_request(broker, scope, pending_id, plan=...)` atomically creates a bounded
   full panel, ordered seats, queued child attempts, events, and broker linkage.
   It compares the plan to the frozen root policy. Repeated correlation replays the
   committed mapping, without another panel. SQL callback failure rolls back all
   admission writes. Maximum 8 seats per request and 32 per root; committed failed
   seats still consume quota. Pending public-root capacity excludes bounded children.
5. `claim_child(root_id)` borrows the reserved child slot, installs explicit budget
   parent ancestry, and advances one child to preparing. Panel order is request/seat
   ordinal. Parent and child share turn/session but remain separate attempts; default
   Store claim/active monitoring selects **legacy only**. Call `active_attempts` with
   explicit `hermes_root` or `hermes_child` only from a typed routed dispatcher.
6. `child_assignment(child_id, expected_generation=...)` projects the native role
   task/context refs and frozen profile for a controller runtime launcher. The parent
   turn's request/goal in ordinary Store attempt metadata is NOT the child task.
7. `release_child(..., verifier=...)` requires terminal child plus exact trusted
   `ChildCleanupReceipt(ChildCleanupTarget(...), 'confirmed_stopped', evidence_sha256)`.
   Inspection occurs outside SQLite and CAS protects changed generation/runtime/root
   occupancy. Missing/failed inspection retains capacity. A terminal DB state alone
   never frees the slot. The next child can then claim serially.

`ProviderLeases.reserve_in_transaction()` is SQL-only, same DB, already-held account
lock required; it cannot acquire its flock while holding SQLite. Legacy account
checks filter explicit legacy jobs so the Hermes root does not block itself.

## Deliberate integration gates

This is not a live scheduler. It does not validate actual provider qualification or
turn `ready:true` into evidence; the trusted caller must resolve and qualify the exact
requested product policy before freeze. Jev/model policy is owned by the parent.
Unavailable plans are not admitted; blocked full-plan durability is a next seam with
`RoleRouter.plan`, not silent panel shrinkage or model substitution.

Not yet implemented: typed worker runtime dispatch; root/child filesystem and native
state isolation; verified context bytes/materialization; child completion result
projection; protected verification/promotion/delivered revision; root cleanup and
release/reconciliation; cold root followup/resume; typed routed generation recovery;
phase-specific preparation/execution deadlines. Current children have a conservative
70-minute total cap bounded by the root wall budget. The legacy resume/followup/fence
methods refuse routed workloads rather than reinterpret their ancestry. Root capacity
is deliberately retained after terminal state/restart until trusted reconciliation
is implemented. This prevents cleanup uncertainty from freeing host/provider capacity.

Never mount this DB, account locks, or controller calls into an untrusted job. Correlation
IDs identify broker requests; only the scoped grant plus actual controller authority
permits admission. The SQL APIs do not provide model authority or provider credentials.

## Corrective review and ordered workflow integration

Fable code review returned REVISE; independent disposition is in
`reviews/scheduler-code-disposition.md`. Public Store.transition now refuses routed
attempts. Use `RoleScheduler.transition(...)`, which checks current process/account
ownership within the same transaction. Provider SQL guards are compared exactly;
child cleanup also requires current owner and held root. Root cleanup after crash
remains a release blocker; that disposition contains the no-force-clear runbook.

When `frozen.provenance.workflow` is present, enqueue stores its complete ordered
step IDs/roles/profiles. Parent `workflow_submission.enqueue_workflow` produces this
receipt only after exact workflow/role resolution and all-step readiness, plus the
trusted Pstack reference hashes and stage boundary. Scheduler does not read those
files: the runtime must validate the protected reference digest before use.

For these roots, use:

- `admit_request(..., step_id='...', input_revision_sha256='<64hex>', plan=...)`:
  explicit controller input revision becomes immutable in the transaction that
  binds the native request and creates its child, before launch. Every preceding
  step must have a passing gate. Repeated roles remain distinct ordered step IDs;
  a native call cannot be linked to another step. No automatic retry or repair loop.
- `child_assignment(...)` also returns workflow_step_id/ordinal/input_revision_sha256.
  These are the launch inputs; verify materialized bytes against that revision.
- `record_step_gate(child_id, expected_generation=..., decision='pass'|'reject',
  revision_sha256=..., reviewed_artifact_id=..., reviewed_artifact_sha256=...,
  evidence_sha256=...)` is controller-only. The artifact must come from that exact
  child and generation, with immutable registered reviewed_revision_sha256 metadata
  matching the pre-launch assigned revision. Passing requires completed/verified.
  Conflicting gate replay is refused; rejected reviews never permit a later step.

This is a durable correlation and authorization chain for a trusted evaluator's
receipt. It does not itself prove that bytes were materialized correctly, a model
actually reviewed them, or protected checks ran; those remain runtime/evaluator
qualification. Raw model text cannot invoke these methods or manufacture verified
artifact metadata. No API endpoint exposes this authority.

Root completion is refused until every workflow step has a passing gate, all child
attempts are terminal, and child occupancy is released after cleanup. Failed/cancelled
root transitions still work. Completed root state deliberately retains its overall
reservation: delivery/completion is not provider/runtime cleanup evidence.

A rejected/REVISE gate currently stops this root's sequence. This is deliberately
not a complete remediation lifecycle: an Astra plan challenge that requests changes
must eventually route to Fable revision, then Astra recheck before Grok builds. A
review merely completing is never approval. The executor must implement explicit
revised-plan/resubmission identity and recheck gates using the frozen failure_rules;
no automatic retry, mutation of a rejected gate, or bypass to the existing later
Fable step is permitted by this checkpoint. That lifecycle remains an activation gap.
