> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Hermes / Claude CLI bridge: read-only source audit

Audited2026-09-17. No install, build, model request, auth read/change, credential copy, service mutation or remote deployment occurred. Downloaded source was inspected as data only. Exact file hashes are in `evidence/hermes-bridge-source-audit/binding.json`.

## Verdict

The stock bridge cannot satisfy the intended Hermes-owned coding/tool loop. A separately implemented and qualified inference-only adapter is plausible; this audit does not prove it works. Keep Hermes as the runtime and tool executor. Claude CLI would only provide a model decision through its published interface.

## Pinned source findings

[claude-bridge85facf2c](https://github.com/niski84/claude-bridge/blob/85facf2c0d77f854bdc7ef04c0be13cee524e24c/cmd/claude-bridge/main.go) has these material properties:

- Incoming tools are discarded; responses contain text without tool_calls. The CLI's built-in tools remain available.
- The server binds all interfaces through `Addr: ":" + port`, without authentication.
- Full Sonnet/Opus/Haiku names become mutable aliases; effort is not forwarded.
- Prompts enter argv. Output is buffered; stderr is logged and returned.
- Reported usage/cost are zero-initialized rather than parsed; SSE is emitted after completion.
- Request bodies/output lack size caps; child timeout exists, but process-tree cleanup and concurrency admission are not implemented.
- Child environment and working directory inherit; directories may include personal Hermes/home content.
- Safe mode is requested, but there is no explicit empty tool set, structured-output protocol or disabled session persistence.

The [provider plugin66820a9d](https://github.com/niski84/hermes-claude-cli/blob/66820a9dc23945051c140e0e6357800730630782/plugin/claude-cli/__init__.py) registers an endpoint/profile and model aliases; it adds no tool translation. Its subscription billing descriptions are assertions, not billing receipts. The [installer](https://github.com/niski84/hermes-claude-cli/blob/66820a9dc23945051c140e0e6357800730630782/scripts/install.sh) pulls/builds unpinned source and changes user services/configuration. It was not executed.

## Minimal candidate contract — proposed, not implemented

1. Hermes owns the conversation, pstack workflow, tool schemas, tool-call IDs, execution, approval checks, retries and results. The model adapter accepts a bounded structured transcript and returns either final text or a schema-validated tool decision. It never executes a requested tool itself. Convert those decisions to Hermes's actual provider contract; reject unknown tools, mismatched IDs, malformed arguments and unsupported message types. Preserve ordered tool-call/results instead of flattening them into ambiguous prose.
2. Invoke a pinned, unmodified Claude binary with an exact approved model/effort and no automatic model/provider fallback. Use print mode, an empty built-in tool set, no MCP tools, empty strict MCP configuration, disabled persistence and no Chrome integration. Safe mode alone does **not** disable built-in tools. Structured JSON output is a potential decision transport; actual CLI terminal success, structured field shape and compatibility with an empty tool set must be probed at the pinned version. These public controls are documented in the [official CLI reference](https://code.claude.com/docs/en/cli-reference). No candidate argv was executed here.
3. Put prompts on bounded stdin or controller-owned files, never argv/logs. A separate per-attempt provider component receives only the already-authorized dedicated account capsule through the approved CLI auth flow; Hermes tools cannot read that mount, process environment or provider filesystem. Use a minimal environment and exclude API-key/cloud-provider/custom-endpoint fallbacks. Do not copy a personal credentials directory or give the original bridge broad home access.
4. Authenticate the private Hermes-to-provider channel, bind it to exact attempt/generation, and allow no host/public listening port. Keep numeric/private internal networking and existing public-domain egress policy; cap request/output bytes, CPU/memory/PIDs, calls, concurrency, wall time and native scratch state. Cancel the whole provider process group/container on timeout or task termination and confirm cleanup before releasing reservations.
5. Treat usage as unknown unless the actual CLI reports it. Preserve model/effort, terminal outcome, auth/rate-limit classification and source trust labels. A CLI API-equivalent cost is not proof of subscription billing, remaining allowance, or absence of extra-usage charges. Do not infer zero cost from the old bridge's zero fields.

## Authentication and spending boundary

Current [official legal/compliance documentation](https://code.claude.com/docs/en/legal-and-compliance) distinguishes an end user signing into an unmodified hosted Claude Code binary with their own subscription from third-party applications routing subscription credentials or handling login themselves. It also describes conditions for hosting Claude Code and preserves applicable agreements. That distinction matters here; neither a blanket ban on all personal CLI orchestration nor a blanket approval of every bridge follows from it. Preserve the actual Anthropic sign-in flow and unmodified binary. The repository's promised allowance/billing behavior is not authoritative evidence.

The intended path uses the operator's existing subscription, not a silently substituted API key. Even correctly selected OAuth authentication does not establish remaining plan allowance or account extra-usage settings. The first actual qualification must identify the selected auth method without printing secrets and preserve an unknown billing status unless authoritative account evidence resolves it. No provider call or spending occurred in this audit.

## Smallest useful next qualification

First use a fake CLI to test exact argv/environment, bounded framing, malformed/error output, cancellation descendants, tool-ID round trips and empty-tool enforcement. Then, only within the parent's approved real-provider scope, run one minimal Hermes task whose model requests a deterministic read/test tool, Hermes executes it, and the next model turn uses that result. Demonstrate a separate configured review model through the same Hermes runtime with read-only tool authority, capture a specific review finding, make the correction, and rerun independent tests. Bind actual Hermes/provider/image versions and receipts. No new gateway or dashboard expansion is needed to test that path.
