> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Hermes-first runtime: source audit and minimal contract

2026-09-17 read-only audit. This supersedes the direct-CLI runtime direction for **new development**, not the immutable identity of existing jobs. No provider calls, credentials/configuration values, legacy workspace contents, or personal auth stores were read. No runtime, image, service, or personal profile changed. Existing direct-Claude jobs and their verified artifacts remain historical direct-Claude jobs.

## Verified installation and evidence

Omarchy `/home/operator/.hermes/hermes-agent` and this Mac's `/Users/operator/.hermes/hermes-agent` are at commit `3b0e392e5a6922034feccac5771041ac78467757`, package version `0.21.3`. Omarchy's tracked checkout is clean. `/home/operator/.local/bin/hermes` is a shell launcher which clears PYTHONPATH/PYTHONHOME and executes the checkout's `venv/bin/python` plus `hermes` entrypoint. The source fingerprints are recorded in `evidence/hermes-runtime-source-audit.json` and `evidence/hermes-omarchy-install-audit.json`; common inspected source files match across hosts.

The legacy Dockerfile installs `hermes-agent[all]` without a package version, then includes a third-party `claude-cli` plugin and Go `claude-bridge`. Its source is **not** an immutable package/version qualification for the existing image. This source audit did not start an image process. The parent subsequently ran an isolated metadata-only image probe: `evidence/hermes-image-audit.json` reports Hermes distribution0.19.0 and Python3.13.15 at `/opt/uv/tools/hermes-agent/bin/python` in f1b15, with no network/mounts/credentials/inference and no labeled leftovers. Host0.21.3 source must not be treated as the installed image version. Our current hardened image Dockerfile explicitly removes `/usr/local/bin/claude-bridge`; therefore this prior bridge deployment cannot be assumed available in the new image. Existing image IDs remain untouched.

## Native execution protocol

The checked-in parser and runtime support this invocation, supplied as an argv array, never an interpolated shell command:

```text
hermes chat
  --query-file /run/task/prompt.txt
  --format stream-json
  --oneshot
  --provider <resolved-native-provider>
  --model <qualified-native-model-id>
  --reasoning <qualified-effort>
  --in /workspace
  --toolsets <trusted-allowlist>
  --max-turns <bounded-count>
  --run-budget <bounded-seconds>
```

This is a **source-verified command contract**, not an executed or provider-qualified Hermes run. `--format stream-json` implies quiet mode and requires a query or query file; TUI is incompatible. Avoid top-level `-z`, which follows a different final-text-only path. The job environment must be built from a strict allowlist with isolated HOME, HERMES_HOME, CODEX_HOME, CLAUDE_CONFIG_DIR and XDG paths. No interactive startup, `--accept-hooks`, personal profile, personal CLI-auth path, host home, Docker socket, or arbitrary project plugin installation is permitted.

`hermes_cli/stream_json.py` emits JSONL `system/init`, `text`, `tool_use`, `tool_result`, and terminal `result` records. Init contains model/session ID; result contains exit code, final text, token counts, duration, optional raw error and session ID. Tool output is capped at 5,000 characters, but tool arguments, text, errors and total stream are not bounded/redacted by this emitter. It omits reasoning-progress events. Use the existing controller's bounded spool, line/total caps, known-secret redaction and safe error normalization for every event; do not expose raw stderr/errors or claim provider/effort attestation from the init model string. Result exit0 still requires the existing independent verifier.

Follow-ups may use `--resume <exact-native-session-id> --no-restore-cwd --in /workspace` only when the same session's pinned Hermes image/config/route and native state are retained. Never use `latest` or adopt a foreign native session implicitly. Cold-context continuation must be labeled separately. Dedicated auth state must not be included in a native-state export or result bundle.

## Provider capability and authentication boundaries

| Requested capability | Checked-in Hermes path | Qualification boundary |
| --- | --- | --- |
| Anthropic API | `anthropic`, native Messages transport; ANTHROPIC_API_KEY or dedicated OAuth-token resolution | Actual account/model/effort and provider acceptance not tested in this audit. |
| `claude-code` built-in alias | Normalizes to `anthropic` in auth.py:1169 | **Not a command to run Claude Code.** It uses native Anthropic inference and Hermes tools. Do not promise subscription base-allowance billing. |
| Claude CLI subscription bridge | Separate legacy `claude-cli` model-provider plugin, local OpenAI-compatible bridge at localhost:9180/v1 | Third-party plugin claims subscription usage, but billing, tool round-trip, transport, model aliases and lifecycle are unverified. Plugin aliases collide with built-in names; pin explicit provider identity. Bridge binary source/build provenance and safe command/tool settings require inspection. Current hardened image removes its binary. |
| ChatGPT/Codex subscription | `openai-codex`, native `codex_responses`, default endpoint `https://chatgpt.com/backend-api/codex` | Uses Hermes-owned auth.json and rotating OAuth grants. Has fallback import from CODEX_HOME/auth.json on missing/invalid auth; isolate CODEX_HOME even though normal storage is separate. No account was inspected or authenticated. |
| Optional Codex app-server | Config `model.openai_runtime: codex_app_server` for openai/openai-codex | Hands the turn/tool loop to Codex's runtime. Exclude from initial Hermes-tools contract; native Responses retains Hermes as agent/tool runtime. |
| Grok API | `xai`, XAI_API_KEY; resolver selects `codex_responses` (Responses wire protocol), not a Codex agent | Actual native model IDs and supported effort must be separately resolved/qualified. |
| Grok subscription/OAuth | Separate `xai-oauth` provider and Hermes auth store, device-code discovery/refresh | Source support is present; account entitlement, available models, refresh and streaming are unverified. |

Hermes profile auth uses `<HERMES_HOME>/auth.json`; named profiles do not inherit the root store (auth.py:671-679). This alone is insufficient isolation: Anthropic can read CLAUDE_CONFIG_DIR/.credentials.json or macOS Keychain; even an explicit token path can consult refreshable Claude credentials. Codex can import CODEX_HOME/auth.json. Startup loads Hermes dotenv and a source-tree project dotenv fallback. Build the image from a clean pinned source archive with no `.env`, personal config, auth, plugin cache or credentials; use container isolation and empty dedicated fallback directories, not only a changed HERMES_HOME.

Refreshable OAuth stores require one explicit owner and serialized refresh per credential identity. Do not clone a single-use refresh-token family into concurrent independent homes. Choose and qualify either one dedicated per-credential persistent store with controlled serialized access or a controller-owned refresh/broker design before enabling concurrent role processes. The current staging capsule/quarantine implementation is direct-Claude-specific and does not establish this Hermes refresh contract. Provider-specific egress must cover only the chosen inference/auth endpoints; current claude-only policy cannot silently become an all-provider policy.

## Pstack roles and delegation

The parent inspected the existing community `jmporchet/pstack-hermes` port at `204e77a7a011c4613dc9c4913a481d77cc0ebe54`: use that candidate for workflow/skill evaluation rather than a wholesale rewrite. Its role-routing limitation still matches the audited Hermes source.

The actual `delegate_task` task-item schema has only `goal`, `context`, `output_schema`, `images`, `group`. `_build_children` uses the same resolved `creds['model']` and provider overrides for all children (`tools/delegate_tool.py:364-390`). Config-wide `delegation.model/provider/base_url/request_overrides` resolves the route; `delegation.reasoning_effort` overrides parent effort. Omitting overrides inherits the parent. No per-child model/provider/reasoning selector exists; unknown model fields must not be treated as honored. An internal Python child-construction argument is not a supported model-facing tool field. Native `role` is legacy/depth-related, not a model-role router.

Smallest trusted implementation:

1. Keep runtime identity `hermes`. Add a versioned controller-owned role map outside the user prompt, e.g. coding/exploration, judgment/prose, and named panel seats. Reuse pstack/poteto workflows as reviewed pinned skills; skills do not grant authority or enforce routing by themselves.
2. Resolve a role to a qualified `(provider, native model ID, supported effort, credential reference, endpoint policy, skill/config digests)` **before launch**. Treat Cursor/Fable/Grok UI slugs as aliases requiring explicit mapping, never as native model IDs. Unsupported/unqualified mappings return a clear refusal; no implicit fallback or guessed substitution. Preserve the separate existing Mini Codex sol/high lane.
3. Launch one Hermes process with an immutable, dedicated generated config per distinct role route. Same-route native subagents may inherit. A mixed Fable/Grok/Opus panel requires independently supervised Hermes processes and recorded result aggregation/cross-judge dependencies. Do not rewrite global `delegation.*` while sibling threads run, and do not mount another personal profile to borrow its provider.
4. For user `inherit-parent`/`auto`, omit explicit model overrides in workflow calls but resolve inheritance from the **parent's frozen effective route**, not ambient provider auto-detection. Materialize the same trusted route in the child's isolated config; keep fallback chain empty unless explicitly qualified. Store both requested inheritance and resolved parent route ID. The current Hermes resolver's `auto` can fall through providers; it is not proof of parent inheritance.
5. Record runtime=Hermes, source commit/image, role-map/config/skill digests, requested alias, resolved provider/model/effort, transport, credential reference (never bytes), parent route/child IDs, and provider-reported fields separately. Add a safe Hermes-side runtime-resolution receipt or instrumented wrapper: current stream init does not prove the provider or applied effort. Test unknown routing refusal, inherited exact route, conflicting delegation config, no personal fallback, cross-provider isolation, unsupported effort, and mixed-panel seat identity before live use.

## Minimum remaining gates

- Build and qualify one immutable Hermes image from the audited source plus bounded trusted dependencies; current image contains older0.19.0, not the audited host0.21.3 source.
- Implement one Hermes entrypoint/stream adapter and pinned `runtime/provider/model/effort` admission; preserve old direct-Claude records/images and no retroactive renaming.
- Select explicit qualified role/provider models and dedicated credential provisioning, including the intended subscription/API distinction. No actual auth stores were read, so current account usability is unknown.
- Qualify JSONL, secret redaction, terminal error classes, independent verifier, interruption/restart/native-resume, capsule cleanup and resource bounds with fixtures, then an authorized real Hermes provider task. Do not reuse direct-Claude provider success as Hermes proof.
- Implement trusted role/process routing for mixed-model pstack panels or explicitly gate them unsupported; native delegation configuration alone cannot satisfy per-seat overrides.

The legacy staging tool remains paused locally at 40 passing tests, unreviewed and undeployed. It has not read/imported any selected legacy file, uploaded an input, or started a session. Its live selection/allowlist/quiescence/review gate remains unresolved independently of the Hermes direction.
