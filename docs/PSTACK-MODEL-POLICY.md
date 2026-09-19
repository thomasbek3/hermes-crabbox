# Agreed Pstack model policy

Status: approved by Thomas on 2026-09-17; implementation resumed by Thomas, not deployed. This document takes precedence over earlier model-role mappings in the project specification and handoffs. It does not change live Cursor configuration or the separate Mini Codex sol/high worker.

Engineering checkpoint reviews for this platform build use the call-grok skill,
Grok 4.6/xhigh, under Thomas's 2026-09-18 instruction replacing the Fable skill.
This reviewer change does not alter the cloud-job role assignments below.

## Fixed role assignments

| Role | Model | Reasoning effort |
|---|---|---|
| Planning, plan revisions, disputed design decisions | Fable | max |
| Exploration, implementation, debugging, tests, refactoring, optimization, fixes | Grok | xhigh |
| Adversarial review of the plan | GPT-6 Astra | high |
| Resulting code review for correctness and edge cases | GPT-6 Astra | high |
| Independent acceptance verification | GPT-5.6 Sol | max |
| Tooling reflection | GPT-5.6 Sol | max |

Grok is the primary coding model. The normal substantial-task flow is Fable plans → Astra challenges → Fable finalizes → Grok builds → Astra reviews → Sol verifies → Grok fixes. Fable resolves disputed findings or changes requiring a revised design. A lighter Grok → verification workflow for small routine fixes remains a future policy option; the implemented bug_fix workflow currently uses the full sequence.

These are requested model identities and effort settings, not claims of qualified Hermes transports or account entitlement. Exact provider-native identifiers and effective effort must be verified before activation. An unavailable assigned model blocks that role; it does not permit silent substitution or effort reduction. Older panel definitions do not override these assignments; additional panel seats need an explicit policy definition before activation.

## Jev workflow selection

Agreed design: Jev classifies the task into an allowed, predefined Pstack workflow. The selected workflow defines its steps, roles, fixed models, and effort settings from the table above. Jev does not choose arbitrary model IDs, override assignments, or grant execution permissions. Explicit user workflow/model instructions take precedence.

The controller validates the selected workflow and enforces execution and credential policy. Unknown or uncertain classifications must not silently select an arbitrary workflow. Classification uncertainty is surfaced or handled by an explicitly configured fallback.

The referenced ussyverse/hermes-jev-router repository is a starting point: its current complexity-to-model recommendation must be adapted to workflow selection. It is not installed or activated by this decision. Three authorized synthetic Jev selections now pass (see JEV-WORKFLOW-ROUTER.md). End-to-end provider/workflow qualification remains incomplete.
