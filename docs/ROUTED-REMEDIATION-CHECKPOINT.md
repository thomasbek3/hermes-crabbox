> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Explicit routed remediation checkpoint

Updated 2026-09-18. The local controller now admits one explicitly requested,
same-session correction after authenticated stop and exact resource release.
This is an undeployed implementation checkpoint. The platform goal remains
active and incomplete; no real provider/model execution, current-source Linux
runtime qualification, production migration or activation is established here.

## Implemented behavior

`routed_remediation.enqueue_remediation` freezes the authenticated source stop,
cleanup, blocker, carried revision/content digest, original request/acceptance,
qualified environment, fixed internal workflow and exact delivery base. The new
turn/root, parent relation, root sequence and events commit atomically. An exact
lost-ack retry returns the same root; changed payloads or stale authority fail.
It creates queued work, not a provider call.

`prepare_remediation_input` rebinds exact immutable content to the fresh root and
registers root-input and remediation-input receipts. The scheduler's
`admission_budget` hook requires these and the original admission record before
compute reservation can commit. Only one correction is allowed; a correction
root cannot remediate again. The new budget retains the original admitted-time
deadline and only the unconsumed request allowance. Current-clock high-water
initialization still applies.

Internal catalogs select Fable revision → Astra plan review → the original
coding suffix, or Grok fix → Astra code review → Sol acceptance. Normal workflow
IDs, their policy digest and Jev choices remain unchanged. The internal entries
are not Jev-selectable. Instruction binding and delivery/stop readers resolve
the appropriate catalog digest. Fixed model/effort identities remain intact.
Only definitive rejection is supported; missing/manual evidence without a reject
gate and disputed-design routes are not silently converted to repairs.

The copied revision preserves files, executable bits, directories and selected
paths while changing root/turn binding and manifest hash. The separate canonical
remediation sidecar is bounded to 16 KiB and reauthenticates source reports,
findings and relevant protected checks before prompt rendering. It remains
untrusted data. Ordinary context references stay within the new root; no old
gate is overwritten and no model recommendation becomes protected acceptance.

Local composition covers actual controller repair-code admission, three fresh
stages, protected acceptance and root delivery, plus rejected recheck → stopped
root → second-correction refusal. Model/provider/runtime boundaries are synthetic.
The original rejected root and its events remain unchanged. Failed remediation
does not promote a workspace; successful delivery still uses the existing
protected decision and atomic session-base checks.

## Evidence and failures

These suites overlap. Do not sum them or describe the entire current suite as
green.

| Receipt | Recorded result and scope |
| --- | --- |
| `evidence/routed-remediation-parent-final-tests.xml` | 167 cases: 166 passed, one failed with child-control 503 during scope-case fixture preparation. |
| `evidence/routed-remediation-composed-tests.xml` | Earlier 16 composition cases passed, including full repair/delivery and rejected recheck. |
| `evidence/remediation-revision-final-tests.xml` | 108 revision/rebind cases passed. |
| `evidence/remediation-input-context-corrected-tests.xml` | 89 input/context cases passed. |
| `evidence/remediation-catalog-integration-tests.xml` | 228 passed, including the final 49 catalog cases; 42 setup errors share one existing normal delivery fixture's `authority_unavailable`. |
| `evidence/remediation-delivery-diagnostic-tests.xml` | Diagnostic rerun of the affected normal delivery module: 52 passed. Its 210 authority checks recorded no exceptions; the earlier failure cause remains unproven. |
| `evidence/routed-remediation-scope-isolated-tests.xml` | One fresh isolated scope case passed; this does not relabel the 167-case run. |

The scope failure occurred in `prepare(v, created)` before the test mutated
client scopes: nested brief → stopped-proof final read-only `validate_db` returned
503. The original SQLite code was suppressed, so a timeout is plausible but not
proven. Saved-fixture inspection found unchanged client scopes, a queued correction,
and no new account reservation, budget or remediation-input event. The diagnosis
is retained in `evidence/routed-remediation-scope-diagnosis.json`. No production
deadline was relaxed or uncertain transaction body replayed to make tests pass.

Earlier failed receipts remain retained, including the catalog fixture's wrong
normal instruction binding before correction and the broad authority failure.
Component source bindings exist at `evidence/remediation-catalog-binding.json`,
`evidence/remediation-revision-binding.json` and
`evidence/remediation-input-binding.json`. They describe their own historical
source/test closures. The six-stage fixture was subsequently extended for generic
coding roles and root-scoped counts; the parent composition evidence covers that
dependency. The post-review source and receipts are bound by
`evidence/routed-remediation-final-binding.json`.

## Review and activation boundary

The authorized Grok 4.6/xhigh read-only review completed exit 0/end_turn with
a REVISE verdict. One of five findings was confirmed and fixed: a missing
preparation root now yields controlled 409 instead of TypeError. The other four
were not supported by actual call paths. See
`reviews/remediation-grok-disposition.md` for evidence and review coverage.
The correction suite recorded 17 passes (including the new regression) and two
more read-only source-evidence SQLite503 failures; availability remains unresolved.
Execution metadata is in `evidence/remediation-grok-execution.json`. Independent local source review found
no new supported-input defect in catalog → admission → rebind → context → budget
composition. That observation is not external review approval. Fable review
remains pending its previously observed account limit; no automatic quota retry
or substitute Fable approval is claimed.

Product/API/worker exposure, production root authorization/provisioning,
new-controller recovery, current-source target qualification, real route/account/
model/effort acceptance and reviewed activation remain. General follow-up import,
additional remediation rounds and disputed-design routes are separate work.
Original Release A/B/C scope is not replaced by these local tests. No services,
credentials, live jobs or production databases were changed by this checkpoint.
See [WORKFLOW-REMEDIATION-NEXT.md](WORKFLOW-REMEDIATION-NEXT.md) for API details and
the next product/production gates.
