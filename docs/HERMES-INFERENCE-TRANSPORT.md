# Hermes inference transport foundation

This is a local, one-request subprocess primitive, not an activated provider or service. `InferenceTransport(PinnedCLI(...), home=private_home, scratch=private_scratch).run(request, cancel=event)` returns `InferenceResult`. It never executes the returned tool calls. Hermes retains the tool loop, transcript and tool-result correlation. The caller selects a preapproved immutable profile; this module neither chooses a model nor implements fallback, retry, billing or auth.

The command uses the exact native model and effort, a pinned binary SHA256, print/text-input/JSON-output, an empty built-in tool list and strict empty MCP configuration, safe mode, no settings sources, no session persistence, no Chrome and one CLI turn. Request transcripts and tool definitions are JSON on stdin; only fixed instructions/schema appear in argv. The child environment is constructed from an allowlist. Optional proxy configuration accepts only numeric RFC1918/ULA HTTP destinations, without credentials. Auth is not an argument or environment input to this module.

## Required outer boundary

D1 requires execution inside a separate trusted provider container, with the sole persistent auth capsule owner and exclusive request/refresh lease. The provider container must not mount the Hermes workspace, Docker socket or control-plane credentials. Private HOME and scratch directory checks do not prove container separation. D2 resource accounting, CPU/RAM/PID/disk limits, container/cgroup termination, refresh ownership and lease release are caller responsibilities. No such integration is implemented here. The existing deployment uses a dedicated setup-token file, not a qualified CLI auth-home layout; this module does not read that file or prove reuse of its identity. Credential provisioning remains a service integration gate.

Each instance admits one subprocess request. Subsequent requests fail `outer_cleanup_required`. Cancellation, timeout, output overflow and setup failure terminate the child process group, escalating TERM to KILL within bounded grace periods. `cleanup.process_group_stopped` reports only the observed process group. A child can escape a group by starting a new session; zombies may keep a group observable. Every result sets `container_cleanup_required=true` and `credential_reuse_authorized=false`. Never reuse credentials or release service capacity on process-group proof alone. Immutable read-only binary and trusted parent directories are mandatory: digest verification followed by path execution is not a same-UID replacement-race defense. The declared CLI version is controller-pinned metadata, not a live version probe.

Default request/output/stderr caps are 256 KiB / 2 MiB / 64 KiB; wall time is 120 seconds plus bounded cleanup (1 second TERM, 3 seconds final cleanup). Stderr is counted and discarded; errors use fixed codes. Selector-based IO avoids pipe deadlock and polls cancellation even after all pipes close. Limits bound this primitive; they do not establish service-level concurrency/resource limits.

## Transcript and schema contract

Requests contain exactly `messages` and `tools`. Text-only system/user/assistant messages, OpenAI-style assistant function calls and matched tool messages are admitted. System messages precede other roles; IDs cannot repeat and all calls must have results before another conversational role. At most 256 messages, 32 tools and eight calls per assistant decision are accepted. Multimodal data and other provider-specific message extensions fail explicitly.

Tool parameter schemas support object/array/string/integer/number/boolean/null, properties/required, closed objects, homogeneous items, enum, numeric and size bounds, description/default annotations, and `anyOf` (up to four branches). Depth is at most eight. Unknown/inapplicable keywords fail. Defaults are checked but never inserted; Hermes owns default application. Omitted additionalProperties is deliberately treated as closed: this is a stricter admission policy, not full JSON Schema semantics. Dynamic open dictionaries and unknown extensions require explicit qualification rather than silent lossy translation. Text/tool-output caps remain in effect even when schema maxima are larger.

The pinned base `read_file`, `write_file`, `patch`, `search_files` and `terminal` schemas are tested from `tests/fixtures/hermes/base-tool-schemas.json`; extraction provenance is in `evidence/hermes-base-tool-schemas.json`. This proves these data shapes are accepted. It does not prove dynamic Hermes schema overrides or actual provider behavior.

CLI structured output must be exactly either:

```json
{"kind":"final","text":"answer","tool_calls":[]}
```

or:

```json
{"kind":"tool_calls","text":null,"tool_calls":[{"id":"new_call","name":"read_file","arguments":{"path":"file.txt"}}]}
```

Calls must name an admitted tool, carry valid arguments and use fresh IDs. The transport returns these decisions; a future Hermes adapter must translate them without executing anything inside Claude CLI. Serializing the conversation into one text input preserves the ordered data, but does not prove native role semantics or effective tool reasoning.

## Result and remaining gates

Output requires one JSON result object with a successful exit, success subtype and validated structured output. Duplicate JSON keys, noise, malformed decisions, permission denials, reported model/effort mismatches and unexpected tool names fail closed. Only explicit structured result error status 401 or 429 maps to auth rejection or rate limiting; free text is never sufficient. Unknown/missing usage is explicit. Reported cost is labeled an API-equivalent estimate, never actual subscription billing. Requested identity is `controller_pinned`; reported identity, usage and decisions remain `worker_reported`. Missing reported model/effort does not become verified identity.

Tests use only synthetic local executables. Required actual qualification remains: exact immutable unmodified CLI flags and JSON protocol; `--json-schema` behavior with empty tools and one turn; exact requested/reported native model and effort; Hermes full tool-result roundtrip and schema overrides; subscription authorization/support and no unexpected paid usage; sole capsule refresh/cancel/recovery; outer container/cgroup/resource/egress cleanup. No provider calls, credentials, service changes or deployment were made for this foundation. The stock legacy bridge is not used.

## Checkpoint evidence

The final local suite has 76 passing tests on macOS. Linux selector/PID/reaper behavior has not been qualified. Synthetic TERM-ignoring leader/descendant tests exercise KILL escalation and keep cleanup unknown when an orphan zombie remains observable. No outer cgroup cleanup claim follows. A pre-signal group existence check reduces unnecessary signaling but cannot eliminate PID/PGID reuse races; the eventual provider PID namespace/container is required to isolate unrelated processes.

The pinned path must be the qualified real executable, not a convenience symlink. The primitive validates a regular executable's bytes, not its interpreter or full dependency closure; the immutable provider image must qualify that closure. Fake test executables are Python scripts by design and do not prove native CLI packaging. `--max-turns` does not appear in the captured 2.1.274 help; exact argv acceptance, especially its combination with empty tools and structured output, remains open.

Fable returned REVISE against the frozen initial review pack. Reproducible local issues were corrected and the affected suite rerun; there was no repeated review to seek PASS. See `evidence/inference-transport-review-disposition.md` and exact hashes in `evidence/inference-transport-binding.json`.

Parent independently captured candidate CLI metadata/help without networking, mounts or auth in `evidence/hermes-candidate-claude-cli-metadata.json`: CLI2.1.274 at `/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe`, SHA256 `15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07`. This improves binary/help provenance but does not prove exact inference argv acceptance. Omission of max-turns from help is not evidence the flag is unsupported.
