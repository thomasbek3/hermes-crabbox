# Pinned Hermes Responses compatibility — local synthetic proof

The actual pinned Hermes `run_codex_stream` driver, OpenAI SDK, response normalizer and next-turn converter work through the Workbench `/v1/responses` listener for the six tested scenarios. The harness imports the complete pinned modules; it does not extract/reimplement their parser or mock the SDK/socket layer.

## Executed boundary

`Hermes run_codex_stream → OpenAI responses.create(stream=True) → HTTP on127.0.0.1 → InferenceService /v1/responses → synthetic response → actual SDK events → Hermes assembler/normalizer → synthetic tool-role output → Hermes replay converter → second HTTP request`.

Three configured profiles each run twice: `openai-codex/gpt-6-astra/high`, `openai-codex/gpt-5.6-sol/max`, and `xai-oauth/grok-4.6/xhigh`. Profile labels are fixture configuration, not verified provider identities. In the direct mode the listener emits `response.output_item.done` frames followed by `response.completed` with `output:null`. In the adapter mode the callback uses the actual `NativeResponses` module with an in-memory synthetic upstream connection; it consumes fragmented item-done/terminal-null SSE and returns its buffered normalized SSE to the listener.

Each scenario performs exactly two actual localhost HTTP requests. First response contains an encrypted-reasoning fixture and a `read_file` function call. Hermes captures the reasoning ID and opaque blob, function item ID, exact call ID and arguments. Its normalizer yields `tool_calls`. Its replay converter preserves the opaque reasoning blob, call ID and exact Unicode/multiline synthetic tool output. Second response normalizes to a final text with finish reason `stop`. Both complete socket writes are recorded; actual subsequent native processing independently demonstrates content consumption.

Hermes intentionally removes reasoning item `id` and provenance fields during replay, and omits the provider function-item `id` while retaining the correlating `call_id`. The harness asserts this behavior rather than claiming byte-identical replay of provider item IDs. The opaque reasoning string is synthetic; no claim about a provider decrypting it is made.

## Isolation and provenance

The child interpreter receives an explicit small environment with temporary HOME, HERMES_HOME, CODEX_HOME, CLAUDE_CONFIG_DIR and XDG locations. It inherits no API-key environment variables. The only supplied bearer value is a deterministic synthetic64-hex listener capability. No installed credentials are loaded or required. Socket connect/connect_ex and DNS lookup reject non127.0.0.1 targets before Hermes imports; the SDK has retries disabled and ignores proxy environment. Native upstream inference is a byte fixture, not a network provider.

The existing local Hermes dependency interpreter provides OpenAI2.24.0, httpx0.28.1 and pydantic2.13.4. All93 imported Hermes source files are checked against the archive manifest pinned to commit `3b0e392e5a6922034feccac5771041ac78467757`. The harness verifies module import paths and checks Workbench source hashes before imports and again after the proof. This qualifies these exact local source/dependency versions, not every SDK version or the dependency set in a deployed image.

The initial fixture used a nonhex bearer token and correctly received401 from the listener. The fixture was corrected to the listener's existing64-hex capability contract; no production listener/auth change was made. The canonical successful receipt is `evidence/native-responses-hermes-local.json`; the earlier successful receipt is retained as `evidence/native-responses-hermes-local-initial.json`.

After the adapter's independent review corrections, all six scenarios passed again against native source `fed386b943ccf7170c60ea3a0ada0fcef0fc3b0bdafb5bf142b91456cb1fb955`. The current receipt is `evidence/native-responses-hermes-local-corrected.json`, with eight passing tests in `evidence/hermes-responses-compatibility-corrected-tests.xml` and updated binding `evidence/hermes-responses-compatibility-corrected-binding.json`. The in-memory connection fixture gained the adapter's explicit `connect()` interface; it opens no upstream socket. Earlier receipts and their binding remain historical evidence for the prior source.

Every listener is stopped and joined; fixture native transport threads are absent afterward, and the private temporary environment is removed. The parent process imposes a45-second subprocess timeout; all proof paths use synthetic data. These are local process/socket observations, not a Docker/cgroup cleanup qualification.

## Tests and limits

`tests/test_hermes_responses_compatibility.py` contains eight passing cases, with one shared actual isolated run exercising all six profiles/modes and twelve HTTP calls. JUnit: `evidence/hermes-responses-compatibility-tests.xml`. Exact source/evidence hashes: `evidence/hermes-responses-compatibility-binding.json`.

Reproduce with `python3 scripts/qualify-native-responses-local.py --hermes-source <pinned-archive>/hermes --python <existing-Hermes-dependency-interpreter> --output <receipt.json>`. The test suite supports `CWB_HERMES_SOURCE` and `CWB_HERMES_DEPENDENCY_PYTHON`; it skips explicitly if those local prerequisites do not exist, and never installs dependencies.

Not exercised: full Hermes CLI startup, its tool executor, actual file execution, provider credentials/subscriptions, provider-side reasoning decryption, real inference, network isolation in containers, account grants, budgets, cancellation/lost-response faults, or production services. The tool-role output is generated by the fixture; the native parser and replay conversion are real. `NativeResult.container_cleanup_required` stays true and credential reuse stays unauthorized; the test does not replace the controller's required physical cleanup gate.
