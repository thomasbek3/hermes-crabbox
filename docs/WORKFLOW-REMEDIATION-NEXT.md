# Explicit workflow remediation: implemented boundary and next work

Updated 2026-09-18. Status: implemented local controller candidate, not production
activation. Explicit correction creates a fresh root in the same session after
an authenticated stopped root has released its exact resources. Historical gates,
original task/acceptance, and earlier delivery remain intact. No automatic retry
or uncertain-work replay is authorized. See [ROUTED-REMEDIATION-CHECKPOINT.md](ROUTED-REMEDIATION-CHECKPOINT.md)
for receipts, retained failures, and pending review.

## Controller API

`routed_remediation.enqueue_remediation(scheduler, principal, source_root_id, key,
*, expected_generation, expected_stop_sha256, expected_delivery_base, router,
parent, trusted_pstack_root, forbidden_values=())` creates one queued correction.
It authenticates the source stop/release, exact blocker, immutable carried revision
and rejection input; resolves fixed ready seats and pinned instructions; and
atomically creates the new turn/root, frozen session delivery base and events.
It preserves the complete original request, including mandatory acceptance.
Conflicting idempotency payloads, a stale source/base, active work, cancellation,
revocation, exhausted budget or unsupported blocker refuse admission.

`prepare_remediation_input(scheduler, principal, root_id, *, expected_generation,
revision_root, forbidden_values=())` copies and rebinds the source revision into
the queued root. It registers exact root-input and remediation-input receipts
only after content validation and a fresh authority check. A retry reuses the
same verified immutable bytes; unexplained existing material is not adopted.

`admission_budget(db, root, stamp)` is the scheduler's compute-admission hook.
It requires the registered input, preparation/admission events, exact original
request and source release, then inherits the original deadline and remaining
request allowance. Reservation and budget registration share the scheduler's
transaction. A queued root without its material cannot start.

One correction is currently allowed per original root. A correction root cannot
spawn a second correction. This bound is frozen in provenance; new root IDs or
idempotency keys do not reset it. The original admitted timestamp determines the
wall deadline, and previously consumed requests reduce the new allowance. Budget
registration retains a current-clock high-water observation. Generic root
admission remains unchanged; legacy `Store.resume` is not used.

## Fixed internal catalog

`workflow_routing.remediation_workflow` selects only a definitive authenticated
rejection: plan/code review `needs_review` with gate `reject`, or protected task
acceptance `rejected` with gate `reject`. Missing evidence with no gate is not a
code-fix instruction. Selection is pure and grants no retry authority.

- `repair_plan_<original>`: Fable `plan_revision`, Astra `plan_review`, then the
  original coding/review/verification suffix when present. Standalone planning
  and plan-review repairs contain only the two plan stages.
- `repair_code`: Grok `bug_fix`, Astra `code_review`, Sol
  `acceptance_verification`, retaining final step ID `verify`.

The agreed Fable/max, Grok/xhigh, Astra/high and Sol/max identities are enforced.
`resolve_remediation` returns the frozen plan; `resolve_catalog` checks the
applicable digest. Instruction binding and strict delivery/stop validation
recognize internal catalogs. The normal catalog, normal policy digest and Jev
selectable list are unchanged; internal IDs cannot be selected through Jev.
Disputed design and arbitrary additional rounds remain unsupported. Missing
original acceptance criteria are not invented to make a repair pass.

## Content and context

`remediation_revision.rebind_revision` copies exact verified files, modes,
directories and selected paths into a fresh root/turn-bound manifest. Both source
and new manifest hashes are retained alongside the normalized content digest.
A plan rejection carries the original workspace; later rejection carries the
exact finalized coding revision. No mutable latest-workspace lookup is used.

`remediation_input.collect_remediation_input` authenticates source decisions,
releases, structured findings and protected-check evidence where applicable.
The canonical sidecar is bounded to 16 KiB, secret-scanned, frozen in provenance
and reauthenticated before prompt rendering. It remains untrusted data; it does
not grant tools or make model recommendations authoritative. Existing ordinary
`context_refs` retain same-root/prior-stage rules. New-root stage progression
continues through fresh finalized outputs and exact carried revisions.

A new passing protected acceptance may publish the repair through the existing
atomic delivery path. A rejected recheck closes another stopped root without
changing old gates or delivered workspace. Explicit retry of uncertain cleanup
reconciles its exact saved target; it does not relaunch a model stage.

## Remaining work

- Grok 4.6/xhigh review is complete: five findings independently checked, one
  confirmed missing-root error fixed. See reviews/remediation-grok-disposition.md.
  Its original REVISE verdict is retained; Fable review remains pending its
  previously observed account limit, with no substitute approval.
- Resolve or bound the retained child-control 503 fixture failure before making
  a globally green claim. Local focused passes do not erase failed receipts.
- Expose this trusted operation through authorized product/API/worker plumbing,
  explicit user action and lifecycle recovery. This module is not an endpoint,
  background retry loop, production root worker or new-controller takeover.
- Qualify the frozen source on target SQLite/Linux/Docker with actual worker
  provisioning, then the exact permitted providers/models/effort. Current repair
  composition uses synthetic provider/runtime boundaries.
- Prepare a separately reviewed migration/deployment and prove a real end-to-end
  workflow plus explicit correction and delivery before activation. Existing
  direct-Claude jobs and personal credentials must remain unaffected.
- Keep general follow-up/import, richer remediation rounds, disputed-design
  routes, product surfaces and remaining original Release A/B/C scope explicit.
  Same-root rounds would require new immutable step-instance/gate semantics,
  progression selection and delivery aggregation; they are not implemented here.


Post-review update: Grok4.6/xhigh completed with REVISE. Independent source
checks confirmed one missing-root error-handling defect, now fixed; four findings
were unsupported by the actual call paths. The correction run recorded17 passes
and two retained SQLite503 failures during source preparation. The new regression
passed; the suite is not globally green. See
`reviews/remediation-grok-disposition.md` and
`evidence/routed-remediation-final-binding.json`. No deployment or Fable approval.
