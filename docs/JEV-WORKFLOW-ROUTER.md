# Jev workflow router

Implemented locally, not activated in the Omarchy service. The fixed role policy is `PSTACK-MODEL-POLICY.md`.

`workflow_routing.py` defines 12 initial workflows: feature, bug_fix, refactoring, perf_issue, hillclimb, investigation, why, planning, plan_review, code_review, verification, tooling_reflection. Other installed Pstack skills remain outside automatic routing until their workflow definitions are added. No classifier can invoke shipping, deletion, deployment, or arbitrary skills through this catalogue.

`select_workflow` accepts an operator-approved task summary and allowed workflow list. An explicit workflow bypasses Jev. Otherwise external classification must be enabled. One TypeSafe Choice question selects among the allowed workflows plus no_match. The result validates exact question/options, probability values/sum/winner, confidence, reported model and usage. Missing/invalid outputs fail closed. Both reported confidence and the winning option probability must meet the recorded threshold. Low confidence or no_match requests clarification; transport failure returns an unavailable result. The initial 0.8 confidence threshold is an uncalibrated configurable starting value, not a correctness guarantee.

`resolve_workflow` binds the selection to its input and current workflow policy hash, expands fixed steps, and checks the configured profile alias, exact native model identity, provider family and effort for every role. Blocked profiles remain visible; it never silently substitutes another model. Model identifiers, actual provider identity and capabilities still require backend qualification. This resolver is not a scheduler or permission grant. Admission must recheck readiness and authoritative access limits.

Normal coding sequence: Fable max plan → Astra high challenge → Fable max finalize → Grok xhigh implement → Astra high code review → Sol max acceptance verification. Failed review/verification requires Grok fixes and rechecking, not a success declaration; the durable stage runner remains an integration task. The five coding workflows conservatively use full planning; a future short-fix workflow needs explicit definition before automatic selection.

## CLI

From cloud-workbench, with an ordinary text file containing the approved summary:

```sh
.venv/bin/python scripts/route-workflow.py --summary-file /absolute/path/summary.txt
.venv/bin/python scripts/route-workflow.py --summary-file /absolute/path/summary.txt --workflow feature
```

The first command previews the outbound payload; the second resolves an explicit workflow offline. `--live` sends the summary to TypeSafe using server-side `TYPESAFE_API_KEY`. Never put the key in command arguments or source. Preview output contains the supplied summary; receipts contain its hash instead. This CLI does not launch workers.

The key reference verified for this task is 1Password item `TypeSafe API — Codex AI & Automations`, vault `Automation & AI Secrets and Passwords`, field `credential`. No key is stored in repository files. The router service requires its own approved secret injection path before unattended activation; a local unlocked 1Password desktop session is not a durable service credential.

## Evidence and limits

158 local transport, routing-policy, workflow, CLI and scheduler-submission tests passed after review corrections (evidence/jev-workflow-corrected-tests.log). The five explicitly authorized synthetic live requests are recorded in `evidence/jev-workflow-live-authorized-five.jsonl`; all returned classifier_unavailable. This is a failed qualification, not proof of classification accuracy, billing, latency, or successful API authentication. No additional calls are authorized by that five-request batch. A real local TLS reproduction confirmed a socket-lifecycle bug: complete Content-Length replies caused a subsequent settimeout on a closed socket. The client now stops reading closed responses; eight real TLS regressions pass. The original batch lacks successful response bodies; this local fix does not retroactively qualify those requests. No model execution or service rollout occurred.

Live sources read 2026-09-17: https://docs.typesafe.ai/api.md, https://docs.typesafe.ai/primitives/choice.md, https://docs.typesafe.ai/cookbooks/function_calling.md. The linked ussyverse/hermes-jev-router repository informed the distinction between semantic classification and deterministic policy; this implementation is adapted to the existing workbench and does not claim the upstream plugin executes workflows.

Fable returned REVISE; independently reproduced findings and dispositions are in reviews/jev-workflow-disposition.md. Exact requested identities currently bind Fable to claude-fable-5-1, Grok to grok-4.6, Astra to gpt-6-astra, Sol to gpt-5.6-sol. This is desired policy, not backend qualification. Failure/recheck rules are included in the policy digest; automatic retries are disabled until controller outcome gates and budgets authorize them.

## Corrected live qualification

Thomas authorized three additional short synthetic requests. The initial retry stopped before any API call because 1Password was locked; its receipt is retained as jev-workflow-three-preauth-block.jsonl. After op signin, exactly three requests were sent, with no retries. All selected the expected workflow: feature, plan_review, code_review. Provider-reported model jev-1.13.0; measured wall durations 0.510/0.402/0.477 seconds. These three synthetic requests prove key access and basic API/schema routing; they are not a held-out accuracy evaluation, a latency benchmark, or cloud job execution proof. No raw key or repository/personal content was sent or saved.

Canonical receipt: evidence/jev-workflow-live-authorized-three.jsonl. It includes the validated probability distributions, confidence, reported token usage, and exact source hashes. A replay test validates those actual replies without network access. 159 focused tests pass in evidence/jev-workflow-final-tests.log. Both approved API batches are exhausted (five original attempts plus three corrected attempts); further paid tests require an additional allowance. No unattended 1Password service setup is claimed.

## Pstack stage instructions

workflow_instructions.py binds each selected stage to an actual pinned Pstack playbook or role reference. All twelve workflow mappings were checked against the candidate image source tree (13 tests, including an escaping symlink rejection). workflow_submission.py freezes those reference SHA256 values and an explicit stage boundary with the job; a runtime must verify the materialized reference bytes before use. These file bindings do not claim the stage runner is deployed.

## Native Responses listener seam

InferenceService now accepts one trusted, fixed request_path per instance: existing /v1/chat/completions by default, or /v1/responses for a native Responses profile. It never treats them as aliases and passes native JSON unchanged to the existing authorized callback. 64 listener/relay tests passed, including a real loopback HTTP synthetic function-call/output roundtrip and rejection of the other path. This is a transport seam, not an actual Hermes/real-provider execution proof.


Runtime instruction binding: `load_stage_instructions` validates the complete frozen workflow reference list, chooses only the assigned step, and reads/hash-checks the exact Pstack bytes before returning them with the controller stage boundary. Changed bytes, escaping paths, missing or special files, malformed metadata, oversized or invalid UTF-8 material are refused. 27 instruction/submission tests pass. The trusted Pstack tree still requires a controller-owned read-only mount; this helper does not create mounts or authorize execution. The runtime caller must use it when assembling the stage. Fable checkpoint review is in progress.
