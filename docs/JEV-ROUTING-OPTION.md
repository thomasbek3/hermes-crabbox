> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Jev semantic routing option

Status: proposed design, not enabled or a release dependency. the original operator suggested TypeSafe Jev as the router during the full-spec discussion. No API request, credential access, installation or model-route change is implied by this note.

## Placement

Explicit pstack role → deterministic RolePolicy resolution → qualified Hermes worker.

Ambiguous task → optional Jev workflow/role recommendation → existing policy validation → qualified Hermes worker.

Jev supplies a bounded semantic judgment; the trusted controller still selects actual configured profiles, checks authority/readiness, reserves resources and launches Hermes. It cannot invent model IDs, endpoints, credentials, permissions or approvals. Explicit user model selection and explicit workflow roles take precedence. Do not add another inference request to every deterministic role lookup.

## Inputs and outputs

Supply only the authorized task text, minimal relevant context, current workflow stage, and plain definitions of the allowed workflow/role choices. Include an insufficient-context/no-match option. Do not send credentials, raw personal state, repository files or transcripts merely because the caller can access them. External submission of project content requires the selected project data policy; omit/redact disallowed context and abstain if the remainder is insufficient.

Use Choice for mutually exclusive workflow selection; independent boolean judgments can identify multiple needs where appropriate. Record exact question/model/schema versions, an access-controlled input reference/hash, allowed candidates, returned probabilities/confidence, proposed role and final policy decision. Probabilities compare supplied choices; confidence summarizes distribution concentration, not factual correctness, permission or proof that a model is best for this workload.

Keep intended workflow selection distinct from empirical model-performance prediction. Existing role-to-model preferences come from the operator's policy. Changing those preferences would require an explicitly enabled, evaluated adaptive policy; Jev's knowledge alone does not establish relative model performance.

## Evaluation and failure behavior

Start with an offline representative task set and explicit expected/acceptable workflow labels. Compare deterministic rules, coordinator selection and Jev on routing errors, abstention, task outcomes, added end-to-end latency and observed usage. Include ambiguous, multi-step, adversarial and insufficient-context cases; do not tune thresholds only on the evaluation set. A minimum useful sample size and success thresholds must be set before claiming an improvement.

If authorized later, shadow mode records recommendations but does not affect execution. Shadow requests still consume a provider service and disclose their input; existing installation or possession of an API key is not spending/data-sharing authorization. Do not activate real calls as part of writing this spec.

On timeout, service failure, stale state, invalid output, unapproved data or insufficient confidence, retain the ordinary coordinator/deterministic path and record why Jev was not used. Explicit choices are never silently replaced. A recommendation is bound to a workflow-state version; changed state invalidates it. Thresholds are selected from held-out outcomes, not copied blindly from a cookbook.

## Sources inspected

- TypeSafe function-calling cookbook: https://docs.typesafe.ai/cookbooks/function_calling.md
- TypeSafe confidence semantics: https://docs.typesafe.ai/confidence.md
- Installed typesafe-ai skill, which directs using semantic judgments alongside ordinary deterministic code.

Live cookbook and confidence pages were read on 2026-09-17. No latency, cost, reliability or model-performance claim has been measured for this platform.
