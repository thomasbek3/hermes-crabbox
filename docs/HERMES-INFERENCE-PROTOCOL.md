# Native Hermes inference codec checkpoint

`hermes_inference_protocol.py` is a pure bounded codec. It does not listen, execute tools, select credentials, grant reuse, or authorize provider work. The controller must bind a fresh one-shot inference transport to a current authorization/lease and complete outer container cleanup before another credential use. Buffered SSE is emitted only after a validated CLI result; it is not a live token stream.

## Interfaces

`admit_request(raw: bytes | dict, profile: PinnedCLI, *, allowed_tool_names=FILE_TOOLS) -> AdmittedRequest` validates the pinned model and any explicitly provided effort, serializes a bounded immutable transcript, and retains streaming preferences. `.transport_request` returns a fresh dictionary. The default capability permits read_file, write_file, patch and search_files. A trusted caller may supply a different frozen set of qualified registrations; the wire cannot change this capability. process_manage and terminal are not qualified by this default.

`encode_response(admitted, result: InferenceResult, *, completion_id, created) -> EncodedResponse` returns status_code, content_type, body bytes, and immutable metadata_json (`.metadata` returns a fresh dictionary). `ProtocolError(code, status_code)` carries a fixed safe error code. Provider error results produce non-2xx JSON even for a streaming request. Successful tool results retain function names, argument objects encoded as JSON strings, stable IDs and finish_reason. The response does not claim independently verified model identity, billed cost or credential reuse.

Unknown usage is omitted from OpenAI `usage` and explained in `x_cloudworkbench.usage_status`. Partial counts are preserved only in explicit metadata. Complete Claude usage sums input plus cache creation/read categories into prompt tokens, with output tokens separate. This is worker-reported accounting, not verified billing. No missing count is assumed to be zero.

## Native request boundary

Pinned Hermes 0.21.3 custom chat_completions builds model/messages and nonempty tools; default max_tokens=None adds no limit, and timeout is a client-only SDK option. The direct builder fixture omits reasoning parameters; the actual CLI custom-provider path emits matching reasoning_effort=high, which the codec accepts. Streaming adds stream=true and stream_options.include_usage=true. The actual SDK-serialized bodies are recorded in the native proof; assistant content=None survives with correlated tool calls, and Hermes removes tool-result persistence `name` before serialization.

The codec also accepts explicit n=1, tool_choice=auto, matching reasoning_effort, or matching reasoning {enabled:true, effort}. It rejects unknown top-level parameters rather than discarding requested semantics. Sampling controls, token caps, multimodal data, arbitrary response_format, alternate tool_choice, parallel_tool_calls and auxiliary provider-specific extensions are unqualified. Initial Hermes integration must disable/gate auxiliary requests that require these semantics; this codec does not silently reroute them.

## Evidence and limits

- `evidence/hermes-inference-protocol-tests.xml`: focused protocol tests.
- `evidence/hermes-inference-codec-native-20260917T234512Z.json` and `.stdout.json`: four real pinned Hermes/OpenAI SDK serialization and normalization checks in image sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693 on Omarchy. MockTransport never opens a socket. Nonroot, network none, read-only root, no host mounts, private tmpfs, 2 CPU/512 MiB/64 PID limits. Exact source hashes and successful owned-container cleanup are in the receipt.
- `evidence/hermes-native-inference-loop-20260917T235216Z.json` and `.stdout.json`: actual native Hermes CLI file-only tool loop through private service/codec/fresh synthetic inference CLI on each of two turns. Five measured checks pass. Native read_file executes, actual fixture contents reach the second inference transcript, final result exits zero. Both process groups stop and the isolated outer container is removed. Source hashes, exact config and native requests are recorded. Network none, no host mounts/credentials/provider calls. The initial fixture directory-mode failure is preserved separately (234647Z) and was corrected without changing the codec or service.
- Hermes native terminal token counters default to zero when usage is absent. These are not measured usage. Controller integration must preserve codec metadata's unknown state rather than deriving reported counts from those counters.
- This proves synthetic native tool-loop compatibility. It does not qualify real provider authentication, credential leases/reuse, production routing, release readiness or live activation. The whole synthetic container is cleaned up after the two fake turns, not between real credential uses.

The actual native startup also probes GET /api/v1/models, /api/tags, /v1/props, /props, /version, /v1/models, /models, /v1/models/claude-opus-4-6 and POST /api/show (some repeated). The service refuses them; pinned Hermes then reaches chat completions successfully. The exact sequence is recorded in http_requests. Keep these startup probes separate from inference error accounting.
