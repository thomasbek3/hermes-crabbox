# Hermes-first runtime correction

Thomas clarified on 2026-09-17 through the coordinating task that the intended architecture has always been Hermes agents with configurable Claude, Codex, Grok or other supported model/provider backends. The prior specification and implementation drifted by treating standalone Claude Code, Codex and Hermes as interchangeable agent products and deferring Hermes to Release C. This document corrects the target; it does not claim that correction is implemented.

## Current verified state

Omarchy runs a v2 control plane and worker with isolated bounded containers, durable attempts, named-client access, independent verification, workspace continuity, immutable artifact/patch delivery, private HTTPS dashboard and result bundles. Actual execution currently invokes Claude Code directly. Those provider jobs passed their recorded checks, but none is an actual Hermes acceptance receipt. Historical source snapshots and evidence retain their original contents.

## Required runtime contract

The user selects the Hermes agent and an allowed backend/model profile. Keep runtime identity, model/provider identity and transport identity separate in immutable attempt/environment provenance. A transport may itself call a supported provider CLI where Hermes implements that behavior; invoking that CLI directly as the agent is not equivalent. Synthetic fixtures remain explicitly test-only.

Pin and inspect the actual Hermes source/version and supported noninteractive protocol. Run Hermes with a fresh per-session home and an explicitly bounded tool/config/skill/MCP set; no personal home, gateway, chat channels, cron jobs, memory or unrelated credentials may accompany cloud jobs. Give each attempt only its dedicated backend credential capsule. The existing Claude login is authorized for that separate cloud identity; a different paid credential or account is not automatically authorized.

Map Hermes launch, stream events, usage, auth/quota errors, cancellation, result completion and reconstructed/native continuation into the existing controller protocol. Preserve existing direct-Claude attempts and pinned images for history; do not rewrite their identities or claim their follow-ups are native Hermes continuity. New Hermes environments must be qualified independently, with backend selection fail-closed until the selected credential and model work.

First prove a disposable isolated Hermes task through one supported backend, then the same supplied-input/repository/follow-up/protected-verification and auth/cancel/restart tests against the actual Hermes process. Additional Codex/Grok/other backends pass their own credential/identity/capability tests. Publish unsupported or untested backends honestly rather than treating a dropdown option as support.

## Work sequencing

Superseded by Thomas's later spec-first request: finish and review the full A/B/C specification before any further implementation, image builds, live qualification or rollout. Preserve existing work and evidence. See [Full-spec baseline](SPEC-FIRST-CONTRACT.md) for the normative contracts and post-spec order.

## Pstack role routing requirement

Thomas explicitly confirmed pstack for Hermes is required because different seats must use different models. The Cursor role sheet is a behavior reference, not a list of valid Hermes API model IDs. Preserve the intended coding/exploration, judgment/prose, panel and cross-judge roles. Mixed panels create one distinct seat per entry, including repeated/inherited entries. The cross-judge prefers a different model family from the parent. `auto` and `inherit-parent` mean the frozen parent route, never ambient provider auto-detection. The separate Mini Codex sol/high lane is unaffected.

The community port `https://github.com/jmporchet/pstack-hermes` is pinned locally at `204e77a7a011c4613dc9c4913a481d77cc0ebe54`. Its 50 namespaced skills resolve in a temporary real Hermes plugin runtime with network disabled, and Plugin Doctor passes. Receipts: `pstack-hermes-registration.json`, `pstack-hermes-doctor.txt`. It has no model-routing tool. Current audited Hermes likewise lacks per-child model selection; mixed routes require separate supervised processes with immutable per-role configs. No personal plugin installation occurred.

`pstack_routing.py` is an initial local resolver with synthetic tests, not execution integration. It binds role, seat, backend profile, effort, inheritance and configuration/qualification hashes; missing routes and unavailable backends refuse instead of silently substituting. Concrete credential/model qualification, routed-process launch, concurrent resource admission and pstack tool integration remain necessary. Neither a hash field nor a readiness callback alone proves backend qualification; the integration must bind them to trusted live evidence.

The earlier integration-first sequence is superseded: full specification and Fable review first, followed by transport feasibility, routed execution, real two-model acceptance, then the remaining A/B/C work. The first workflow never substitutes for the full platform.
