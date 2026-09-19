> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Qualified workflow integration checkpoint

This candidate connects explicit qualified environments, frozen final-answer
contracts, and bounded prior-stage context to routed child preparation. It is
local implementation, not a deployment or a completed six-stage workflow.

`enqueue_qualified_workflow` reads a successful durable qualification and freezes
the normalized manifest, qualification receipt, and contract version with the
root. It refuses unsupported network, credential, and startup-command profiles.
There is no active-version fallback. Submission retries currently revalidate the
registry and protected scripts before reaching scheduler idempotency, so registry
availability is still required for that admission API's retries.

Stage preparation reconstructs the expected contract from the frozen role,
assigned revision, and ordered artifact references. Context must come from
finalized earlier producers and pass exact canonical-data, digest, rendering,
and reference validation. The prompt marks model-written context as untrusted
evidence. The caller rejects oversized structured final answers instead of
silently clipping them. The launch receipt binds the environment image/resources;
runtime preparation refuses mismatched configured allocations before creating
material or containers. This resource comparison does not independently prove
filesystem quota enforcement.

`prepare_qualified_verification` loads the frozen manifest and exact protected
script bytes without consulting current registry defaults. Protected execution
remains separate from model-reported conclusions. Empty protected check sets
remain empty and cannot fabricate acceptance.

The historical verification entry points now also enforce the frozen normalized
manifest at preparation and the exact manifest digest, image, and ordered checks
at execution, load, and final publication. Calling an older helper cannot bypass
the qualified policy. Existing owned runtime cleanup precedes refusal, so policy
errors do not disable reconciliation.

For versioned acceptance stages, the decision controller reconstructs the
expected contract from the hash-bound root and assignment, authenticates the
saved launch and final observation, and parses the exact structured answer.
Malformed, mismatched, or `needs_review` answers refuse completion and retain
occupancy. A valid `complete` report cannot override failed protected checks.
Committed decisions retain contract and normalized-output hashes; historical
replay stays inert. Automatic handling of incomplete reports remains part of
the unfinished orchestration work.

The context loader currently supports finalized task-acceptance decisions and
their linked worker observations. It deliberately does not accept a speculative
planning/review artifact format. Durable planning/review finalization, subsequent
stage admission, revision promotion, production root/worker integration and the
full six-stage Fable/Astra/Fable/Grok/Astra/Sol flow remain unfinished. All
original Release A/B/C acceptance gates remain in scope.

The requested Grok4.6/xhigh review examined the earlier protected-verifier
snapshot and returned REVISE. Its confirmed findings have independent fixes and
tests; it has not reviewed this newer integration. Fable remains pending after
the previously observed account limit. No quota retry, replacement approval,
Jev request, provider inference, credential change, remote operation, service
restart or deployment occurred in this integration unit.

Local evidence:

- `qualified-workflow-integration-tests.xml`: 168 passing admission, adapter,
  caller, stage, and runtime cases.
- `qualified-workflow-components-tests.xml`: 225 passing contract, environment,
  and context cases.
- `qualified-verification-binding-tests.xml`: 88 passing new binding and existing
  verification/decision cases.
- `qualified-decision-contract-final-tests.xml`: 35 passing contract and legacy
  decision cases. The earlier run had 34 passes and one existing SQLite deadline
  failure before decision execution; its receipt and isolated diagnostic pass
  are retained. The deadline was not increased and operations were not retried.
- `qualified-workflow-final-composition-tests.xml`: 18 passing tests, including
  three combined qualified-environment/structured-answer/protected-decision
  scenarios, pass/reject replay, cleanup replay, and malformed-answer refusal.

These suites overlap; their counts are not a unique-test total. Independent
read-only review found no additional concrete defect in the new answer and
environment gates. Final source/test/report hashes are in
`evidence/qualified-workflow-final-binding.json`. Historical Linux verifier proofs
remain evidence for their frozen snapshots, not these changed source files. The
older broad-suite setup failures remain recorded separately.
