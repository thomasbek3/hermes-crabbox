> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Native Responses provider boundary

`native_responses.py` performs one bounded HTTP inference request. Hermes generates native Responses input and executes returned function tools. This module never invokes an agent CLI, discovers credentials, refreshes OAuth, retries, starts containers or changes existing deployments.

`NativeProfile(provider, model, effort)` freezes exactly three policy pairs: `openai-codex/gpt-6-astra/high`, `openai-codex/gpt-5.6-sol/max`, `xai-oauth/grok-4.6/xhigh`. Its SHA binds `transport=native-responses-v1`, provider, model, effort and exact HTTPS endpoint. Endpoints are `https://chatgpt.com/backend-api/codex/responses` and `https://api.x.ai/v1/responses`. No API-key billing fallback or request-selected hostname is supported.

`NativeResponses(profile, timeout=120, proxy=None).execute(request, credential=..., account_id=None, cancel=Event)` accepts bounded native JSON and an explicitly injected dedicated bearer credential. The optional account ID is trusted Codex metadata, not request content. The HTTP implementation uses stdlib verified HTTPS, no ambient proxy, no redirects, no compressed responses, one request, and optional numeric RFC1918 CONNECT proxy. Only function tools are admitted; server-side search/code/browser tools are refused. The pinned required Hermes tool schemas are covered, including annotations and bounded anyOf.

The current contract supports text messages, correlated function calls/outputs and encrypted reasoning replay. It refuses image/audio inputs, opaque extra_body/headers, native compaction, service-tier upgrades and unsupported schema fields rather than dropping them. Exact effort is required; provider-reported exact model is required for success. Effort acceptance is not independently attested by the response. Identity trust remains `worker_reported`.

Native SSE is parsed under a2MiB total/8192-frame bound; request JSON is capped256KiB. `output_item.done` is assembled even when completed.output is null/empty/string, matching the pinned Hermes event assembler contract. Duplicate/unknown tool calls, invalid arguments, incomplete streams, disagreeing output and mismatched model refuse without partial delivery. Encrypted reasoning items are retained unchanged. Unknown token usage stays unknown. Safe401/403 and429 classification does not inspect free-form provider error text.

The returned `NativeResult` contains native response JSON and buffered SSE output_item.done/completed events. The caller MUST deliver those bytes only after existing durable dispatch cleanup proves the disposable provider and gateway terminated. `container_cleanup_required=True` and `credential_reuse_authorized=False` are always returned. A local network thread blocked in DNS/connect may survive cancellation; `transport_stopped=False` exposes this, and only outer container/cgroup cleanup can authorize credential reuse. Even `transport_stopped=True` never releases account ownership. No module success constitutes controller cleanup proof.

Source support is based on the checked-in pinned Hermes context: agent/codex_responses_adapter.py, agent/codex_runtime.py, agent/codex_headers.py and hermes_cli/auth_constants.py. Local tests use injected fake HTTP connections and never contact providers. The separate artifacts lane owns actual pinned Hermes parser fixtures. Container entrypoint credential loading/refresh ownership, egress, native dispatch envelope integration and real account/model entitlement remain explicit integration gates. Dedicated device login alone does not establish model entitlement or no-incremental-spend billing.

Review correction: socket ownership survives Connection:close detachment, and cancellation after a blocked connect is checked before POST. An actual stdlib HTTPConnection loopback fixture covers hanging response cancellation; this is not a TLS/provider proof. Output refusal parts replay as assistant content. Completed events end the stream; trailing keepalives are ignored. Terminal metadata is reduced to id/object/status/model/output/usage.
