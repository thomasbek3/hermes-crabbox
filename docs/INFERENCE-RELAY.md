> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# D9 inference relay plumbing

`inference_relay.py` adds a per-attempt loopback-HTTP callback and a private authenticated Unix-socket worker endpoint. It does not activate provider containers, mount credentials, implement ProviderLeases or grant authority. All authorization/execution callbacks are trusted controller code.

## Integration

- Freeze `AttemptBinding(attempt_id, generation, profile_digest)` and a scoped 64-hex capability. Only its SHA256 is configured at the worker; capability bytes occur in bounded Unix frames, never journals or logs.
- `InferenceRelay(socket_path=..., journal_path=..., binding=..., capability=..., fence=..., deadline_seconds=180)` supplies `forward(body,cancel)->ServiceResponse` and `delivery_result(response, response_sent:bool)` to `InferenceService`. `ServiceResponse.delivery_token` binds the callback to the exact pending nonce; uncorrelated/rejected responses cannot clear it. Prefer `make_loopback_service`, which fixes those callbacks and validates190s>180s defaults. HTTP service execution deadline must exceed the relay deadline and remain within the attempt/root cleanup budget.
- `WorkerDispatcher(journal_path=..., binding=..., capability_sha256=..., authorize=..., execute=...)` checks current authorization before admission, cached-response return and post-execution success. `execute(body,cancel)` returns `DispatchResult(response, outer_cleanup_confirmed=True)` only after trusted outer cleanup. A false/absent receipt produces fixed failure, never success. Caller must implement bounded execution/cancellation and real grant/lease/account budgets before activation.
- `WorkerSocketServer(path, dispatcher, socket_gid=None)` serves serially with backlog8 and one cancellation watcher. No published TCP port. Default socket0600; explicit authorized group0660. Controller precreates a trusted parent with no group/other write. Existing sockets are never unlinked at startup; close unlinks only the bound inode.

The relay commits a cryptographic random nonce and payload digest in SQLite FULL synchronous mode before connecting. Lost UDS responses retry at most twice with .25s/1s backoff inside one absolute deadline, retaining the nonce. Cancellation before admission creates no nonce; proven never-transmitted requests become not_sent. Any possible partial write remains uncertain. Worker admission is committed before callback execution; exact retries return the cached bounded response. Pending outcome after crash is uncertain and never re-executes. Same nonce/different payload conflicts. A genuinely new identical prompt receives a new nonce. Per-attempt journals cap32 journaled new requests (including failed/not_sent attempts); nonce retries do not consume new calls; root128 and rate60/minute burst4 remain required in the worker's real admission callback, not claimed implemented by this plumbing.

Request JSON is bounded256KiB, response1MiB; four-byte length framing and total read/write deadlines bound socket input/output. Databases use private0700 parents, nofollow single-link0600 files, immutable identity binding and an exclusive per-relay flock. Worker journal response retention is bounded by request cap. Journals never store capabilities.

## Delivery and trust boundary

`delivery_result(exact_response, True)` means complete local HTTP socket write observed, not proof that the native client consumed it. Its durable state is `http_write_completed`. A failed/cancelled write or an unfinished journal on restart fences `response_delivery_unknown`; new HTTP requests receive409 and do not regenerate inference. The trusted fence callback must interrupt the actual attempt. A disconnect after a successful socket write remains inherently uncertain without native acknowledgment; no native response replay/resume is claimed.

Production must run relay under a controller-provisioned dedicated UID distinct from Hermes tools, with private journal mount inaccessible to those tools, no CAP_SETUID/CAP_SYS_PTRACE and no-new-privileges. Same-UID synthetic proof does not qualify that protection. No host UID/group provisioning or production mount is performed here.

## Evidence so far

- `inference-relay-focused-tests.xml`:85 passing relay/service/codec tests (20 relay cases).
- `inference-relay-integrated-tests.xml`:158 passed, three unchanged transport timing fixtures failed because .25/.3-second deadlines fired before startup PID files existed. The three failed again in isolated `inference-relay-transport-timing-recheck.xml`. Transport source unchanged; not hidden as green, no unrelated fixture edits.
- `inference-relay-native-uds_drop_once-20260918T000200Z`: actual pinned Hermes0.21.3 file loop through HTTP/UDS with first worker reply lost. Six checks pass: dispatch nonce sequence N,N,M causes only two fake CLI executions, exact schemas/model/effort and tool results preserved. Networknone, no real credentials/providers, no host mounts; outer container removed, zero own leftovers.
- `inference-relay-native-http_drop_once-20260918T000229Z`: HTTP loss caused Hermes to retry once; relay durable409 fence prevented a second inference. Three of four checks pass, native one-HTTP condition fails.
- `inference-relay-native-http_drop_once-20260918T000434Z`: same failure despite supported HERMES_STREAM_RETRIES=0. Source analysis locates independent recovery in Hermes turn_api_error.py:343 and agent_runtime_helpers.py:941, which rebuilds a custom-provider client once after configured retries exhaust. Do not spoof aggregator identity to skip it. Intrinsic no-retry activation gate remains unmet pending explicit controller interruption proof or reviewed native boundary correction.

Final review revisions:97 relay/service/codec tests passed in inference-relay-revised-final-tests.xml. Response-aware delivery correlation, bounded backoff, not_sent classification, strict binding checks and safe deadline composition are covered. The valid Fable REVISE is independently dispositioned in reviews/inference-relay-disposition.md; no PASS verdict claimed. Native controller interruption uses a separate trusted callback to terminate the real Hermes process group before its unconditional primary recovery; this is required behavior, not intrinsic no-retry configuration.

## Worker integration context

Use execute_request(context,payload,cancel) for durable provider dispatch. The frozen DispatchContext contains authenticated AttemptBinding, original64hex request_nonce and payload_digest after envelope validation. Pass that exact nonce to ProviderDispatch.admit; do not regenerate/truncate it or inject authority fields into model payload. Constructor requires exactly one of this callback or legacy execute(payload,cancel). Cached same-nonce responses bypass callback execution. Root/account grants and cleanup remain callback responsibilities.
