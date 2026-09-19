> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Installed route inventory — read-only

Fresh SSH metadata is recorded in `evidence/pstack-installed-routes-readonly.json`. No inference or token values were read/printed. CLI login status concerns the host user; it is not permission to mount those personal credentials into cloud jobs.

| Required role route | Omarchy | AI Mac Mini | Practical activation gap |
| --- | --- | --- | --- |
| GPT-6 Astra / high | Codex0.154.0; cached exact model advertises high; ChatGPT login reported | PATH Codex0.144.6 is old; app-bundled0.153.4 exists; exact cached model advertises high; ChatGPT login reported | Dedicated cloud Codex credential reference absent in inspected deployment. Native Responses isolation/refresh/egress integration and actual exact-route qualification missing. Cache is not an executed inference. |
| GPT-5.6 Sol / max | Same installed Codex route; exact cache advertises max | Same app-bundled route; exact cache advertises max | Same cloud adapter/account gates. Do not change the separate Mini sol/high worker to satisfy this platform role. |
| Grok4.6 / xhigh | Grok1.0.34 installed; models listing includes grok-4.6 but reports login required | Grok1.0.4 at ~/.grok/bin/grok, absent from login PATH; models listing includes grok-4.6 but reports login required | No dedicated Grok reference in live worker/api or auth directory. A stored 1Password item may exist elsewhere; this inspection did not inspect vault items. Do not equate personal CLI login-required with proof that every cloud auth route is unavailable. |
| Fable5.1 / max | Host Claude2.1.273 advertises max; personal Max login. Candidate contains qualified2.1.274 binary | Host Claude2.1.187 advertises max but predates documented Fable5.1 minimum2.1.257 | Dedicated cloud Claude token exists; exact model/account no-incremental-spend evidence and authorized shared-account quiescence window still needed. Pinned candidate execution, not the older Mini binary, is the prepared route. |

The deployed cloud worker/API expose only `claude_enabled` and the canonical `/var/lib/cloud-workbench/auth/claude-token`; that auth directory contains only this file (UID959/GID959,0640). No dedicated Codex/Grok token or provider reference was found in those scoped locations. No personal auth file was parsed; presence/mode and CLI status were the only auth observations.

## Actual adapter gap rather than missing model vocabulary

The pinned Hermes source already includes `openai-codex` → native `codex_responses`, and `xai`/`xai-oauth` → Responses. Its `agent/reasoning_effort.py` explicitly preserves Astra/high, GPT5.6/max and Grok4.6/xhigh. The source has a generic weaker-effort clamp, so the controller must check requested==effective for the exact pinned model rather than treating arbitrary future models as supported.

The cloud boundary currently implements a Claude CLI provider: ProviderExecutor requires PinnedCLI, normalizes into its inference-only transcript, and ProviderDocker launches provider_main with `/run/secrets/claude-token`. Its cleanup, quarantine and approved egress have only been composed with that provider. Existing host CLI logins do not fill this gap.

The shortest next implementation is a dedicated native Responses provider adapter for the already-supported Hermes wire protocol, preserving Hermes tools, the relay/controller account reservation, one serialized refresh owner per OAuth identity, exact effort and model policy, provider-specific egress and result/cleanup evidence. Codex app-server mode hands the tool loop to Codex and does not satisfy the selected Hermes-runtime requirement. Copying a host refresh token family into multiple job homes is also not a substitute.

Before the first full workflow: locate/provision approved dedicated Codex and Grok account references, establish their billing/entitlement basis, implement/qualify those cloud adapters, qualify Fable/max under the guarded window, then exercise the fixed multi-model workflow. No silent model replacement, lowered effort, personal credential fallback or API spending is authorized by this inventory.
