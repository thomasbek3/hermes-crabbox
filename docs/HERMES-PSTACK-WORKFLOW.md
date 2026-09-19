> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# First Hermes + pstack workflow

This is the first integration target after the full-spec checkpoint closes, not a deployed capability. See [Full-spec baseline](SPEC-FIRST-CONTRACT.md). It preserves the full platform spec; it does not replace Release A/B/C with a routing demonstration.

## Acceptance

A private cloud task runs a Hermes coordinator with pinned pstack skills. A pstack workflow requests an implementation seat and a review seat by role. The controller resolves distinct qualified model routes, runs real Hermes processes inside bounded cloud containers, and records each child's requested role, effective provider/model/effort, route/image/config/skill hashes and outcome. The reviewer receives the delivered revision under a genuine read-only workspace policy. Independent checks decide verification separately from model review. A follow-up retains the right workspace and route provenance. Parent cancellation fences queued child launches and stops owned running children; no duplicate child after a lost request/response or controller restart.

Two distinct actual models are required for the first proof. Two labels pointing at the same effective model, static skill files, fake HTTP responses, direct Claude Code jobs or a manually described role map do not close this gate. A synthetic transport rehearsal is useful startup/tool/protocol evidence only. Cross-provider Grok/Claude/other routes then receive their own credential/egress and capability qualification; the intended configurable backend support remains required.

## Trusted boundaries

The existing pstack port provides workflows but no per-seat routing implementation. Native Hermes delegate_task is homogeneous under the audited source. Do not pretend passing unknown model fields works, or change a shared delegation config while children run. Distinct routes use isolated supervised Hermes processes, with each profile fixed before launch and no ambient fallback. Same-route native delegation may remain available only when its additional processes/resources/cancellation behavior have been qualified.

A workflow-facing routing call selects a named role and bounded task text. It cannot supply credentials, an endpoint URL, a model not in the configured policy, a Docker target, a host path or arbitrary runtime options. The parent identity/session/generation and allowed role map come from its controller-issued scope, not the model's text. Each request has a durable idempotency key and one immutable child/seat mapping. New calls recheck parent cancellation and credential readiness before reservation/launch. A guessed child/session ID grants no access.

All child containers count against worker and parent workflow limits, including preparing and verifying. The coding writer has exclusive ownership of its branch/workspace; reviewers consume an immutable snapshot read-only. Shared artifacts are explicit references with hashes, not broad host mounts. Provider refresh ownership is serialized per dedicated credential family; do not copy rotating refresh tokens into concurrent writable homes. No role may acquire unrelated personal credentials.

The trusted process plan records requested configuration. The actual Hermes runtime must additionally emit a controller-consumed safe effective-route receipt after resolution, with provider/model/effort/transport only, never credentials or raw client objects. Model-reported JSONL fields are separately labeled. Native schema and source pin are validated before route activation.

## Requested role policy

the operator's Cursor pstack sheet provides the desired division: Grok for implementation/exploration, Fable for judgment/prose, and mixed Fable/Grok/Opus panels with a different-family cross-judge where possible. Cursor model slugs are desired aliases only. Resolve native model IDs, supported effort and authorized account transport before producing active Hermes profiles. `auto`/`inherit-parent` resolves the exact frozen parent route. The separate Mini Codex gpt-5.6-sol/high lane remains unchanged.

## Current evidence and next steps

- Pinned pstack plugin doctor and50 namespaced skill resolutions pass in an isolated real Hermes plugin runtime: registration receipt (private historical record, not distributed) and doctor receipt (private historical record, not distributed). No global install or cloud activation.
- Local RoleRouter preserves panel cardinality, records effective profile provenance and refuses unresolved/unready routes. Synthetic tests are not model availability evidence.
- Hermes LaunchPlan/parser foundation is under Fable review. Its initial safe-mode configuration intentionally disables plugin loading, so it is not yet the trusted pstack profile.
- An unactivated candidate image was built from pinned Hermes0.21.3 source and pstack source; its synthetic tool roundtrip is protocol evidence only. The prior cloud image contains Hermes0.19.0 and cannot stand in for this source pin.
- Required next integration work is a reviewed pstack-only plugin policy and routing call, durable parent/child mapping and cancellation fences, dedicated backend auth transport, safe effective-route evidence, and the real two-model task proof. Keep native continuation unsupported until separately proven.
