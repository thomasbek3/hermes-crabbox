> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Native provider bootstrap integration

The existing provider_main entrypoint now selects a native branch only from a trusted root-owned profile file exactly `{transport: "native-responses-v1", provider, model, effort}`. Historical five-field Claude CLI profiles continue through the existing token/CLI path. Profile identity is NativeProfile.digest; no request-selected provider, endpoint, auth path or CLI command exists.

Native bootstrap opens only `/run/secrets/native-auth.json` by default inside the disposable provider. It checks no-follow parent traversal, regular file, one link, no group write or any other-user access, and64KiB maximum. It never discovers personal auth, refreshes tokens, writes state or falls back to an API key. Expiry within120seconds fails before HTTP; a dedicated serialized refresh or explicit reauthentication remains an integration gate.

Supported saved formats were observed by key/type inspection only on the newly authorized dedicated cloud-worker logins:

- Codex0.154.0: top-level auth_mode(chatgpt), OPENAI_API_KEY(null), tokens(access_token/refresh_token/id_token/account_id), last_refresh. Only access token and validated optional account ID enter NativeResponses. JWT exp is inspected for expiry, not asserted as cryptographically verified; the provider validates authentication. Conflicting stored/claimed account IDs refuse.
- Grok1.0.34: one OIDC record keyed by issuer + `::` + public client ID. Issuer is `https://auth.x.ai`; client ID is `b1a00492-073a-47ea-816f-4c329264a828`. This is an auth-store namespace, not an inference endpoint. Record auth_mode must be oidc; key is the bearer access credential; refresh_token is retained only in the private file; expires_at is a timezone-qualified ISO timestamp. The parser admits exactly the observed metadata fields and rejects another namespace/client/mode. The selected inference endpoint remains fixed `https://api.x.ai/v1/responses`.

The public envelope keeps the existing six top-level fields. Success result is exactly `{profile_digest, response, transport_stopped: true}`. No SSE bytes are serialized; the controller independently validates/re-emits native SSE after physical cleanup. Exact access/refresh/id-token values found in a response suppress the result. This is not protection against arbitrary transformed leaks; isolation and container destruction remain mandatory. A transport that reports incomplete local cleanup never emits a success envelope.

Dedicated saved files are UID959:GID960 mode0600. ProviderUID958:GID959 cannot read them directly. A reviewed canonical credential handoff/staging policy with correct ownership and private parents is still required; this source change does not chmod, copy or activate those credentials. No production image was built or deployed, and no provider request was made.98 local tests cover native+historical bootstrap paths with synthetic tokens. Separate Fable review of this bootstrap delta is pending while the review account is at its reported session limit; the completed native transport review does not cover it.
