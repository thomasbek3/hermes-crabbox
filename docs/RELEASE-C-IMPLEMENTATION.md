> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Release C: provider coverage, integrations and notifications

Normative expansion of SPEC P20–P21 and SPEC-FIRST-CONTRACT. Full scope remains required; account-dependent live gates can block a route without converting it to completed N/A. No installation or live change is performed by this document.

## C1 — Requested providers and pstack workflows

Runtime-integration owner qualifies each requested Claude/Codex/Grok route using the same isolated Hermes runtime, provider-service boundary and exact native identity/effort records. Source-supported login is not account entitlement. For each route produce a matrix of auth ownership/refresh, structured tool roundtrip, usage/error handling, egress, cancellation/restart, continuation and applicable A/B behaviors. Record unsupported capabilities explicitly; unsupported required capabilities remain a gap. Do not pass a provider by launching its standalone agent instead of Hermes.

Complete all requested role mappings and arena/architect/interrogate panels, how/why, reflect and swarm. Each enabled workflow has versioned patched skill references, actual role-tool schema tests and a real outcome receipt. Mixed panels retain ordinal identity, exact revision, all outcomes and a qualified cross-judge. Panel synthesis is independently verified. Readiness reports unavailable models before execution, without silently replacing them. Native effort incompatibility or changed model availability invalidates affected profiles, not historical records. Preserve the separate Mini Codex lane.

Acceptance: H01–H08 per provider, actual Grok/Fable/Opus requested panel and family-preference receipt, unknown route/effort/alias refusal, same-account contention and mixed-provider cleanup, a fail-then-correct coding/review task and applicable visual benchmark. Grok 4.6/xhigh through call-grok reviews this checkpoint; activation is explicit scoped CAS with rollback to the prior qualified policy, never prompt-driven.

## C2 — Project-scoped skills and MCP

Integration owner adds a versioned tool catalog. Each entry records immutable source/package digest, tool schemas, execution location, network destinations, credential reference, allowed project/principal, read/write effect category, limits and qualification receipt. Built-in pinned pstack is already an A dependency; this unit adds project-selectable extensions. No ambient personal-home plugin/MCP import, dynamic installation from model text or arbitrary host shell command.

Project environment versions select exact catalog entries. Read-only tools run inside the bounded job or a scoped broker. Consequential external writes go through the existing controlled executor with task authority and exact-action digest; an MCP tool's description cannot bypass approvals. Integration credentials remain in the service boundary, not general job/browser/verifier mounts. Tool responses are untrusted data with byte/time limits and provenance; they cannot change role policy or authorize additional grants.

API: authorized catalog/readiness read and administer-scoped versioned project selections. Manifest activation follows environment CAS/rollback. Revocation fences new calls and handles pending/unknown external effects through IntegrationExecution reconciliation. Existing attempts retain pinned tool schemas but revoked credentials cannot be revived by old manifests. Tool-schema drift fails readiness.

Acceptance: one selected read-only fixture integration and one explicitly authorized controlled write fixture; denied unselected tool, cross-project token, schema drift, malicious response instructions, arbitrary endpoint, credential export and expired/replayed action. Real external tool qualification requires an already authorized exact identity/target; an absent grant remains a visible execution gate. Grok 4.6/xhigh checkpoint review through call-grok and source-bound receipts precede activation.

## C3 — Opted-in terminal-state notifications

Integration owner uses a durable notification outbox keyed by session/attempt, terminal-or-action-required event sequence, channel and configured destination. Events: completion, failure, quota/auth block and required owner input. Default disabled and quiet on unchanged state; no periodic progress messages. Opt-in configuration states exact connector/destination and allowed event kinds; possession of a Slack/email credential alone is not permission to send.

Outbox states pending, sending, confirmed, retryable_failure, delivery_unknown, permanently_failed. Revalidate destination authorization/revocation before send. Deduplicate on stable provider idempotency key where supported; after ambiguous send without provider dedupe/readback, mark delivery_unknown rather than automatically send a duplicate. Retry transient confirmed-unsent failures with bounded exponential delay: at most 5 attempts over 24 hours, then show undelivered status. No notification failure changes a verified task's execution result.

Payload includes safe task title, outcome/required action and private authenticated UI link; no credentials, raw transcripts, hidden reasoning, approval bearer links or sensitive artifact attachments by default. Link access still requires application auth. Follow-up turns create distinct event identities; a page reconnect cannot re-send old events. Revocation cancels queued messages, preserves audit metadata and never leaks connector secrets to the job.

Acceptance: opt-out sends nothing; repeated terminal events produce one outbox item; restart during send reconciles; unknown delivery is not labeled delivered; destination change/revocation and cross-owner access denied; auth failure visibly undelivered. A real delivery/readback is required only after the original operator authorizes its exact destination; a mock receipt is not an external send. Grok 4.6/xhigh checkpoint review through call-grok precedes enabling the opted-in connector.

## C closeout

Map every requested profile, workflow, tool and adopted notification destination to its immutable qualification and actual outcome. Distinguish implemented/fixture-tested/live-qualified/activated. Remaining owner-specific prerequisites stay named and prevent their requirement's completion. A working subset is useful progress, not completion of the full platform goal.
