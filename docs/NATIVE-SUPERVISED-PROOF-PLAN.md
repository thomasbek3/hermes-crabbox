> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Next bounded native Hermes / supervised provider proof

Preparation only. Do not execute until the runtime adapter corrections and mandatory BudgetAuthority integration have a parent-approved frozen source set. The completed `supervised-provider-linux-20260918T010306Z` execution predates that delta and cannot qualify it.

## Required path

```text
native Hermes 0.21.3, caller UID1000
  → authenticated localhost HTTP/SSE, relay UID1001 in the same caller
  → private read-only mounted UDS directory, relay-group socket access
  → host WorkerDispatcher.execute_request(DispatchContext, payload, cancel)
  → SupervisedProviderExecutor with BudgetAuthority and current controller ID
  → ProviderExecutor / ProviderDispatch / ProviderLeases / InferenceBudget / Store
  → separate disposable provider container + gateway, synthetic CLI only
  → bounded structured decision / exact physical cleanup / joined supervisor
  → UDS response / relay SSE / actual Hermes read_file
  → second inference containing that real tool result
  → second fresh provider scope / cleanup / native terminal result
```

The trusted worker process remains outside the caller container and is the only component with Docker access. No Docker socket, controller database, account locks, provider profile, or synthetic provider token is mounted into the caller. Model request JSON contains no grant/lease/nonce authority: `DispatchContext` carries the exact relay nonce and immutable binding separately.

## Concrete wiring

1. Snapshot the finalized controller dependency set, including `budget_authority.py`, and the minimum relay/service source. Record every hash before transfer. Use the already qualified Hermes image for the caller and the exact provider candidate plus a fresh labelled synthetic CLI layer. Never activate the candidate image or alter live services.
2. Create a new private host folder, Store database, synthetic account/reservation/root budget, one preparing/running attempt, and a scoped grant. Configure the real ProviderDocker with a callback holder initially denying all requests. Bind `runtime.cancel_check` to the active wrapper's `cancel_check`. Bind exact canonical profile digest across the image profile, AttemptBinding and executor.
3. Construct `WorkerDispatcher` with `authorize=wrapped.authorize` and **`execute_request=wrapped`**, not the legacy `execute` callback. Its host journal is private controller-owned state. Construct `WorkerSocketServer(..., socket_gid=1001)` in a controller-root-owned directory chmod0750/gid1001; socket0660. This numeric group is for the disposable proof and does not provision a host user. The host folder's outer0700 boundary limits access outside Docker's bind.
4. Read-only bind only that dedicated UDS/config directory into the caller at `/run/worker-inference`. A synthetic relay capability configuration is root/group1001,0440. UID1000 must fail direct socket/config access. Its HTTP capability is separate and available to Hermes as required; it must not grant direct worker access.
5. Create caller init as UID1002, tool state tmpfs0700/UID1000, relay journal tmpfs0700/UID1001, root/source read-only, network none, ALL capabilities dropped and no-new-privileges. Use trusted Docker exec as UID1001 to start InferenceRelay and `make_loopback_service`; use exec as UID1000 for native Hermes. Both share the caller cgroup; separate provider containers have their own adapter limits. No privilege-changing init is needed.
6. Use ordered deadlines large enough for actual Docker: worker/relay180s, HTTP190s, provider transport120s plus cleanup reserve. Keep the first proof's total harness deadline bounded and return fixed failures. The supervised execution must receive the UDS cancel event. Any unresolved runtime outcome retains the account/quarantine and follows the explicit controller reconcile/cleanup path documented by the prior proof.
7. The synthetic provider CLI parses the real transport's stdin JSON. With no correlated tool result it returns a `read_file` decision for `/run/tool/fixture.txt`; when the actual `role=tool` message contains the fixture marker it returns a final marker. It cannot read the caller file because no caller workspace is mounted into provider containers. Tool-call ID and schema are fixed fixture data; the real codec preserves their correlation.
8. Configure actual Hermes with isolated HOME/HERMES_HOME/CODEX_HOME/CLAUDE_CONFIG_DIR, file tools only, known source-supported auxiliary/title/compression/plugin/tirith disables, custom chat_completions localhost provider and HERMES_STREAM_RETRIES=0. Record those disabled features as fixture limits. Native retry suppression is not intrinsically complete: unknown HTTP delivery requires trusted controller interruption; do not reclassify prior retry failures.
9. Launch native Hermes stream-json/oneshot. Record actual tool_call/tool_result and terminal events, exact accepted HTTP requests, worker DispatchContext nonce/binding digests, durable budget/dispatch rows, immutable provider IDs, cleanup timestamps, and supervisor stopped receipts before UDS success. Never print capabilities or token values, even synthetic values, in the result evidence.
10. Stop/join the HTTP/UDS servers and observers explicitly. Stop/remove only the exact caller, source preparation resources, provider/gateway scopes and networks. Confirm owner labels/full IDs, zero owned containers/networks/volumes, remove the temporary synthetic image and token, and record service snapshots. Preserve source/journals/DB as nonsensitive proof evidence.

## Acceptance gates

- Actual native CLI exits0 with one terminal result, read_file really executes, and the second request contains the actual file contents under the correlated tool-result ID.
- Two distinct worker nonces and two provider runtime scopes, one logical auth owner, same exact attempt/generation/profile; two budget charges and no regeneration on an exact cached replay if included.
- Each worker success follows authoritative physical absence of its exact four provider resources and a stopped supervisor. Native success is not evidence of resource cleanup by itself.
- Runtime cancellation callback is the active wrapper callback. BudgetAuthority is mandatory and source-bound. This first success-path proof does not replace the existing dedicated cancellation/fault tests.
- Caller tool UID cannot read the relay journal, signal init/relay via tested SIGTERM, read worker capability config, connect directly to worker UDS, or access `/var/run/docker.sock`. Relay UID can connect; caller source mount passes behavioral EROFS. Record cgroup membership/limits and process identity while native Hermes is alive.
- No real provider inference/authentication, production service/schema/config change or host user creation. No fake success when a core API or schema differs: preserve exact refusal/evidence and report the dependency before modifying shared code.

## Required controller follow-through

The earlier actual proof established raw cancellation409 with cleanup unconfirmed and `start_outcome_unknown`. Therefore the integrated harness must retain that distinction. A non-success response must never become a credential-reuse signal. On unknown operation evidence, keep quarantine; only authoritative adapter resolution followed by exact physical cleanup completes the controller cleanup obligation. Do not bolt arbitrary account release or unconditional force-removal onto the worker callback to hide this gap.

Potential production requirements remain separate: persistent provisioning of the three caller UIDs and UDS mount group, durable controller recovery, account selection/credential lifecycle, real provider behavior, HTTP lost-response controller termination, and resource exhaustion resilience. This plan qualifies one isolated file-only synthetic native loop.
