> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Routed child decision transaction: proposed narrow next unit

Status: read-only design against the current candidate schema and verifier. No implementation, schema mutation, tests or live operations were performed for this note. Parent owns the protected receipt reader/controller composition. This proposal concerns one child decision and its cleanup release, not final session promotion or automatic review repair.

## Existing contracts and gaps

- `scheduler.py:RoleScheduler.transition` delegates to `Store._transition`, with current account ownership and root completion checks. `store.py:Store._transition` owns its own transaction, permits explicit completed outcomes, revokes inference grants on terminal transitions, and rejects any subsequent transition of a terminal attempt. It does not validate protected evidence. It cannot be nested to construct an atomic decision/gate transaction.
- `scheduler.py:record_step_gate` independently commits a gate after the child is already terminal. Passing requires completed/verified; the artifact must belong to the exact child/session/generation; its `reviewed_revision_sha256` must equal `workflow_steps.input_revision_sha256`. Those are useful assignment fences, not a parser/authenticator for verifier or model-review evidence.
- `scheduler.py:release_child` snapshots `ChildCleanupTarget(root, generations, root version, runtime)`, invokes an inspector outside SQLite, then CAS-clears occupancy and emits an event. It currently rejects a repeated call after successful release, and its event retains only the cleanup hash, insufficient for a complete typed release replay.
- `scheduler.py:admit_request` freezes a supplied next-step input revision, checks preceding passing gates, matches the complete frozen role plan, and creates/link requests and children in the broker transaction. It does not establish that a supplied revision is the permitted output of a preceding decision.
- `routed_publication.py:result_authority` checks current root/child/session/turn/owner, exact frozen caller binding and assignment, client submit/project scope and revocation, cancellation, account ownership, root deadline and BudgetAuthority. It only accepts preparing/running children. Publication/recovery composition must finish before a decision makes the child terminal; terminal decision replay cannot reuse this predicate unmodified.
- `routed_verification.py:run_verification` publishes `controller_protected_checks` evidence bound to plan, assigned input, tested candidate, launch, cleanup, current acceptance and check records. It intentionally creates no terminal state or workflow gate. `routed_verification_policy.py:assess_verification` distinguishes passed/rejected/needs_review and enforces mandatory ID-plus-description coverage. A typed Python receipt alone is not proof of its file/runtime provenance.

## Input, output, review and acceptance are distinct

Keep four explicit hashes/identities where applicable:

1. `assigned_input_revision_sha256`: the immutable revision from which this child was launched.
2. `output_candidate_revision_sha256`: the child-owned captured candidate, if it produced one.
3. `protected_subject_revision_sha256`: the revision actually checked by the protected verifier.
4. A role-review verdict, if the frozen workflow step requires one, bound to the review child's input revision, reviewer identity, checked artifact and rubric/policy version.

For coding, protected_subject may equal output_candidate while assigned_input differs. Its gate remains an input-bound record of the decision for that assigned step. Do not label the protected output check as a review of the input, or copy output into the gate's input-revision column. For a review/acceptance stage, assign the selected candidate as the next child's input before launch and require its review/verification subject to equal that input. A freshly re-captured revision has a scope-bound manifest hash; equal file contents are not automatically the original assigned revision identity. The reader must bind the actual subject chain rather than infer identity from filenames or prose.

Protected tests are not the Fable/Astra/Sol workflow decisions. The immutable role/model policy and failure rules remain in `workflow_roots.frozen`. A role requiring a model-review verdict must not receive a passing workflow gate merely because executable acceptance checks passed. A future structured review record must be authenticated as the output of the assigned reviewer and independently validated against its revision/rubric; a literal model string `PASS` is never controller authority. If a stage's decision policy is not explicitly supported, it remains needs_review/blocked and cannot advance. Do not infer an execution-gate policy merely from a role name.

## Stage-specific proof scope: required correction before full workflow activation

The current `prepare_verification` obtains the root turn's entire acceptance list from `authority['child']['request']['acceptance']` for every child. This is correct evidence of what that API checks, but it is not a usable all-stage completion policy: a planning stage cannot establish application behavior that the later coding stage has not implemented. Do not change descriptions or drop mandatory criteria silently to make such a stage pass.

Freeze an explicit versioned stage-contract map in the workflow policy. Each step identifies its output schema, allowed revision relation, required referenced artifacts/reviewer identity, stage-contract checks and whether it is the final `task_acceptance` subject. Root acceptance stays immutable and is evaluated on the selected final candidate only. Earlier stages receive their explicit stage criteria, not an accidental copy of the final task criteria. Receipts and artifacts carry `verification_scope='stage_contract'` or `'task_acceptance'` plus the exact corresponding policy digest. An unspecified stage contract is unsupported, never inferred from a role name or global criteria. Existing unscoped verification artifacts can be consumed only by a narrow task-acceptance-compatible step after explicit role/policy validation; they cannot approve arbitrary planning/review stages.

Readonly planning/review stages do not need a newly captured workspace candidate to publish their conclusions. Their assigned input remains unchanged; bounded structured output in the validated public `adapter.result.summary` (or a separately safely exported scratch artifact) is the plan/review document subject. Introduce a bounded exact-field stage-output schema: schema version, role and step ID, assigned input hash, referenced reviewed artifact IDs/hashes, findings and recommendation; planning additionally carries bounded plan text. Duplicate keys, unknown fields, excessive text/counts and mismatched subject/reference/role are refused. Preserve the original output as model-reported content. A valid schema and exact reviewer identity establish provenance/contract compliance; they do not prove the recommendation true or turn model prose into protected acceptance.

A controller stage decision can authorize moving to the next workflow stage when the explicit stage contract permits the authenticated assigned reviewer's recommendation. That progression gate must be distinguished from task verification. Safest initial projection: keep such child outcomes `unverified`/`needs_review` as appropriate and add a narrowly scoped controller gate helper that validates the stage-contract artifact, rather than forcing the existing universal completed/verified prerequisite. If `verified` is instead reused for contract checks, all readers/UI must expose its limited scope before activation; a bare verified label must not misrepresent semantic judgment or final task acceptance. The old standalone `record_step_gate` is not silently loosened.

`routed_stage.plan_child_stage` currently rejects nonempty context_refs with503. Full planning→challenge→revision→coding→review→acceptance requires a separately implemented bounded reference loader that verifies the frozen artifact hashes and explicit role/subject relationships before mounting/including them. Passing empty references or rereading arbitrary files is not a substitute. Preserve the complete frozen workflow, including its required reviews; do not skip early stages to demonstrate the final-acceptance path.

A final root requires the final task-acceptance receipt on the exact selected revision plus every required authenticated review/progression gate. Stage-contract success alone cannot promote the candidate or satisfy root acceptance. The first code unit can therefore be limited to the parent's load-only protected receipt reader and a task-acceptance-compatible decision, while the full six-stage path remains explicitly unimplemented pending stage contracts, artifact references and remediation support.

## Proposed interfaces

Controller composition, outside any SQLite transaction:

```python
validate_child_decision_inputs(
    scheduler, prepared, caller, child_results, protected_verification,
    *, frozen_step_policy, review_verdict=None,
) -> ValidatedChildDecision
```

This is the parent's protected reader seam, not a model/API endpoint. Recompute bounded private evidence bytes/hashes, verifier assessment, candidate/observation artifacts, exact protected subject and required review evidence. Recollect caller/provider cleanup and load/reconcile all verifier receipts, requiring exact physical and material cleanup. Stage an immutable decision artifact outside SQLite. Filesystem/Docker inspection must not run while holding the control transaction.

Scheduler decision seam:

```python
commit_child_decision(
    scheduler, caller, quiesced, validated_decision,
    *, expected_generation, expected_assignment_digest,
) -> CommittedChildDecision
```

No free-form `state`, `outcome`, `gate='pass'` or unbound revision argument is accepted. The decision document includes an explicit verification_scope and stage-policy version, is schema-versioned, and contains root/session/turn/owner/project and both generations, step/seat/profile/workflow hashes, frozen caller/runtime and assignment hashes, all relevant artifact IDs/hashes, assigned input/output/protected-subject relation, acceptance/step-policy hashes, verdict sources, derived terminal outcome and gate disposition. Include the complete future child cleanup target, current retained account identities and evidence hashes. Canonical content excluding the document's own digest determines the decision digest; it becomes the replay key.

A narrow same-transaction helper is needed for gate validation/insertion. Keep the existing standalone gate contract intact until its callers are deliberately migrated. The new helper validates a controller decision artifact's explicit assigned-input/subject relation; it must not manufacture misleading `reviewed_revision_sha256=input` metadata for a check of different output. The existing `workflow_step_gates.revision_sha256` remains assigned input.

Release seam:

```python
release_decided_child(
    scheduler, child_id, *, expected_generation, decision_sha256, verifier,
) -> ChildReleaseReceipt
```

The verifier remains an exact ownership inspector outside the transaction. It must cover every associated caller/provider/verifier execution, including unknown create intents; current `RoutedChildCleanup.verifier` covers caller/providers, so verifier-runtime cleanup must be included by the controller's combined adapter. No TTL, worker exit code or terminal state substitutes for cleanup proof.

## Fresh decision transaction

Use the existing bounded `_child_control_tx`, never nested Store transactions. In order:

1. Require exact integer generations, correct database, live supported child state and uncommitted decision. Use current `result_authority` before any terminal mutation. Require expected assignment, held root occupancy and controller/account identities to match.
2. Recheck current submit/project authorization, revocation, root/child cancellation, root/ancestor/child generation and deadlines/BudgetAuthority. Preserve durable high-water semantics. Cancellation/revocation/expiry blocks a new successful decision; it does not authorize cleanup or erase evidence.
3. Recheck exact database artifact rows and all scoped IDs/hashes from the reader, current frozen workflow/step policy and acceptance, cleanup membership and verifier receipt bindings. A changed candidate, policy, artifact, caller or input/output relation refuses the transaction. Validate required worker completion status according to the explicit step policy; do not silently turn a failed execution into an approved stage.
4. Derive the terminal outcome and gate from supported policy and validated proof. A protected `needs_review` cannot produce pass. Rejected/failed/incomplete review cannot advance the step. Infrastructure or unresolved cleanup remains non-passing; uncertainty is retained rather than classified as check failure.
5. Store the canonical decision and output relation in `attempts.result` under a reserved controller key, preserving existing result fields. Register the immutable decision artifact, write lifecycle state/outcome and revoke grants in this same transaction. Respect existing transition semantics: when needed, validate and record running→verifying→completed inside the one commit, rather than accepting an otherwise illegal direct jump. Do not call public Store.transition recursively.
6. Insert/validate the input-bound gate and its decision artifact reference when the policy authorizes a definitive pass/reject; otherwise persist the decision without inventing a passing gate. Emit artifact/state/decision/gate events exactly once. A failed event write rolls back all authoritative changes.

Occupancy remains held after commit. The gate may exist while cleanup release is pending, but the next execution cannot claim that occupied root.

## Terminal and release replay

Terminal replay is read-only evidence retrieval, not fresh execution authority. Before requiring a live child, inspect an existing terminal decision for that exact child/generation. Return it only if canonical decision bytes/hash, immutable identity, artifact and expected event rows all match. A differing proposal is a conflict. Do not use terminal replay to grant a start, advance a step, bypass cancellation, recreate an event or alter a newer owner. Parent orchestration separately uses current authority for any subsequent action.

For first release, validate the exact terminal decision and still-held target, inspect outside SQLite, then recheck owner/generation/runtime/root-version/membership in the final bounded transaction. Clear occupancy and write a complete `workflow.child_released` receipt in the same CAS commit. Include decision digest, exact target, cleanup evidence hash, retained account identity/epochs and released root version. On lost response after commit, read this exact receipt and return it without inspector callbacks or account operations—even if a newer child/account owner is now active. Conflicting/multiple/incomplete release events fail closed. A historical release receipt is evidence only, never permission to clean the newer owner. Cancellation must not prevent confirmed physical release of the original held scope.

Crash cases: before decision commit, retry validated evidence and commit once; after decision but before release, load the terminal decision and reconcile its exact cleanup; after release commit, replay the persisted release receipt. Fresh-controller ownership takeover remains separate explicit recovery, never inferred from a matching decision hash.

## Schema and migration boundary

The narrow first unit can use existing reviewed tables: `attempts.result` for the canonical terminal decision/output relation, `artifacts` for its immutable document, `workflow_step_gates` for the input-bound gate, and `events` for full decision/release receipts. Terminal CAS makes a second fresh decision impossible; root occupancy CAS makes a second first release impossible. Exact replay verifies one matching event rather than relying on unindexed arbitrary event existence. These reads must remain bounded by exact child/session scope and fail closed on ambiguous records. No new table or silent schema migration is required for this approach. It does require new transaction-aware helpers and expanded event payloads; old hash-only child-release events must not be presented as new full receipts.

Current trusted prototype primitives can manually set verified completion/gates without the new protected reader. They must not remain an alternate production path for children using the new decision contract. Migrate controller callers deliberately and reject direct verified completion/gate insertion for that contract; preserve historical legacy jobs and existing result selection. If stronger SQL immutability/indexed decision rows are selected instead, that is an explicit schema change requiring pinned DDL verification and migration—not an implicit `CREATE TABLE IF NOT EXISTS` fallback.

Final D6 session promotion is a later explicit schema/API unit: current sessions contain no delivered-revision pointer/version. It must atomically CAS the expected base revision, bind final root result plus all required evidence and promotion event, and leave the last delivered revision intact on failure. Current root_sequence result selection is not promotion. This decision unit must not simulate promotion by merely writing root `outcome='verified'`.

## Next-stage admission and deferred remediation

Extend the existing `admit_request` transaction with a controller-selected prior decision relation. For steps that consume a candidate, require the assigned input hash to equal the approved decision's permitted output (or exact carried-forward input for read-only stages). Require prior committed gate, exact root/turn/generation and frozen step order/profile; never accept a model-chosen hash merely because it is64 hex characters. Existing native broker request idempotency remains the mechanism for creating/linking the next child.

A rejected gate is immutable today. Repair/review-resubmission must create an explicitly allowed new bounded attempt/step revision under the frozen failure rules; this proposal does not add an automatic retry subsystem or reinterpret rejected review as approval. Full workflow activation still needs that lifecycle and the model-review evidence reader.

## Acceptance tests for the next code checkpoint

- Planning/review cannot accidentally receive final global acceptance as their completion contract; unscoped receipts cannot approve those roles. No stage-contract gate alone can complete/promote the root.
- Readonly stage output is bound to unchanged input and exact referenced artifacts; unsupported context_refs remain visibly blocked until a hash-bound loader exists.

- Atomic success yields one terminal decision, artifact, gate and output relation; injected event failure rolls everything back.
- Cancel/revoke/deadline/budget expiry just before commit refuses success; changed account epoch/controller, root/child generation or assignment also refuses.
- Candidate/protected subject substitution, cross-child artifact, wrong criterion description, forged receipt outcome, unsupported stage policy and model prose cannot produce pass.
- Coding input A/output B records both; review must launch on B; supplying A or arbitrary C for the next candidate-consuming step refuses.
- Crash/lost response before and after decision commit has exact replay without running checks/publication again. Conflicting terminal replay refuses.
- Cleanup callback runs outside SQLite; changed target/version while it runs refuses release. Unknown verifier create/material cleanup remains held.
- Crash after release commit replays the exact receipt without invoking callbacks; newer child/account owner remains untouched.
- Historical hash-only release events are explicitly unsupported for the new replay contract, not silently upgraded.
- No passing gate/next execution after rejected or incomplete review. No session promotion or root-result replacement by child generation.

Minimal implementation ownership: parent protected reader/controller composition; scheduler owner transaction-aware decision/gate/release helpers and narrow admission relation validation; focused decision/release regression tests. Store helper extraction only if required to share lifecycle validation without nested transactions. No unrelated runtime, provider, legacy runner or result-selection refactor.
