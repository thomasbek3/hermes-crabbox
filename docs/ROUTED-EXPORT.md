# Stopped-workspace export

`routed_export.py` creates a separate Docker collector to read selected deliverables from an exactly bound stopped caller workspace as UID1000. This enables reading caller-owned0600 output without giving worker959 host root or mounting task, scratch, sockets, provider state or credentials. It is a controller primitive, not a running worker service or a verification gate.

## API

```python
exported = export_stopped_workspace(
    routed_runtime, spec, caller_runtime_id, caller_cleanup,
    selected_paths=('src', 'README.md'),
    expected_workspace_identity=prepared.materialization.source_identity,
    authorize=trusted_authorize,
    limits=ExportLimits(),
    forbidden_values=known_secret_bytes,
)
validate_workspace_export(routed_runtime, spec, exported, authorize=trusted_authorize)
```

`expected_workspace_identity` is a required `(device, inode)` tuple of positive exact integers, captured from the original materialization before caller start. CallerSpec and its historical cleanup journal bind the workspace path but do not store that inode. Capturing a fresh identity only at export admission would not detect replacement between execution and export.

The frozen `WorkspaceExport` contains `spec_digest`, original caller `runtime_id`, `workspace_identity`, normalized `selected_paths`, validated `tree: ExportTree`, `collector_id`, `caller_cleanup_sha256`, `stream_sha256` and `receipt_sha256`. The controller must call `validate_workspace_export` before revision capture and recheck current scheduler result-publication authority. Validation compares the returned tree's file bytes and metadata against a digest in the private runtime export journal, checks the receipt binding, checks workspace identity, proves collector absence and rechecks caller cleanup. An object or receipt supplied without that journal is insufficient.

`authorize(action)` is a mandatory trusted controller callback returning exactly `True`. It must independently bind the current scheduler owner, child/root attempt and generation, cancellation policy, admitted profile and materialization. It is not model-supplied or an external approval boolean. Actions are `prepare`, `inspect`, `create`, `start`, `publish`, `validate`, `load`, `status`, `reconcile`, `cleanup_inspect`, `stop`, and `remove`. Every Docker RPC rechecks authority. Policies must distinguish execution/publication from exact-object cleanup: a cancelled child can deny new work while allowing `reconcile`, `cleanup_inspect`, `stop` and `remove`. Cleanup never waives object identity/security inspection. The controller remains responsible for exclusive workspace ownership against other host/controller writers; stopping this caller cannot prove the absence of arbitrary external processes.

## Docker and output boundary

The collector uses the same immutable image bound by the approved CallerSpec, but an independent exact command: `/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /opt/hermes/venv/bin/python -I -S -c <trusted collector program>`. The program comes only from `routed_export_protocol.collector_program`, with trusted selected paths and limits embedded as data. No model command is executed.

The collector has numeric UID:GID1000:1000, network none, localhost-only DNS, read-only root, one read-only workspace bind, drop-all capabilities, no-new-privileges, private IPC, no host PID namespace, no devices/extra volumes/ports/tmpfs, no restart and logging disabled. Fixed limits are0.5 CPU,256MiB memory/swap and32 PIDs. Exact name, immutable image, command, labels, UID, mounts, security settings and resource limits are read back before start and cleanup. No original caller process is reused.

All Docker calls use the configured Runtime Docker executable with the same minimal PATH/LANG environment, no shell, no ambient Docker settings and no raw stderr propagation. Metadata output is capped at256KiB; stderr at64KiB. Metadata/lifecycle calls have five-second deadlines. Attached collector output is written directly to the private spool with the protocol-derived cumulative byte cap and `limits.max_seconds + 5` deadline; it is not accumulated by the subprocess runner. The bounded subprocess runner kills an unreaped CLI process group on failure, never signals an already-reaped successful PID, and bounds reaping. Killing Docker CLI does not prove daemon operation cancellation. A subsequent identity-verified reconciliation must establish cleanup; undecidable effects stay held.

The default protocol limit is32MiB file bytes,2000 entries,depth32 and30 seconds. Protocol permits larger explicit limits, but whole-file base64/JSON memory overhead can exceed the fixed256MiB collector budget; OOM/nonzero exit is failure, never a partial export. No large-output throughput or memory capacity is claimed by local tests. The controller independently decodes every framed record, verifies hashes/selectors/private-path policy, and scans explicitly supplied `forbidden_values`. Wiring the complete known-secret set is the caller's responsibility; the exporter never reads provider credentials to discover it.

## One-shot lifecycle and recovery

The export shares RoutedRuntime's existing exact-attempt flock. `workspace-export.json` lives beside the caller's private journal, not in the workspace. A durable exclusive `create_intent` precedes Docker create. Later states are `created`, `start_intent`, `exited`, `remove_intent`, and `cleaned`; exact collector identity is persisted as soon as returned. Atomic journal replacements are fsynced. Interrupted initial journal writes remain explicit recovery failures and cannot trigger a blind launch retry.

Repeated export calls refuse once an attempt journal exists. A failed create/start/remove RPC never becomes absence-as-success and never launches a replacement. Explicit `reconcile_workspace_export(runtime, spec, authorize=...)` may discover the exact labelled/name-bound stopped object after unknown create, or inspect/stop/remove a known collector. Empty discovery after unknown create remains unresolved. Absence confirms cleanup only after a durable remove intent or prior cleanup proof. Reconciliation returns physical cleanup metadata and whether retained bytes passed recovery checks, never the bytes themselves or permission to repeat inference/export.

Every owned export failure, including an uncertain mutating RPC, attempts one bounded best-effort reconciliation through a private helper while retaining the existing attempt flock. There is no nested public-method lock acquisition. The helper can resolve a lost create acknowledgment only through exact labels/name/image/policy inspection, and can stop/remove an exactly identified running collector. Unexplained absence still cannot establish cleanup or successful exit. An unresolved pass remains for explicit later reconciliation. Missing/replaced workspace paths do not prevent identity-bound collector cleanup; they do prevent new export or validation. Changed labels, image, mounts or security policy cause refusal to mutate the object.

Before start, version2 journals durably register a newly created exclusive0600 `workspace-export.stdout` file with its inode/device, worker UID and byte limit. The private spool is not mounted in the collector or exposed as an artifact. Capture streams directly to that FD and fsyncs it. Exact collector exited-zero observation plus stream byte count/hash are persisted independently of cleanup state before removal. Successful cleanup precedes result delivery. Malformed/secret-bearing output, nonzero exit, changed workspace, authority loss or unconfirmed cleanup produces no WorkspaceExport.

`load_workspace_export(runtime, spec, authorize=..., forbidden_values=())` reconstructs the exact result after a restart without any create/start action. It requires durable collection proof and cleanup, checks current authority and original workspace identity, rereads the exact regular single-link controller-owned0600 spool with bounded size and stable identity/metadata, verifies the stored full hash/count, decodes the full protocol, scans the current forbidden-secret set, and checks program/selector/limit bindings. It reproduces or validates the same receipt hash. A previously published receipt is not rewritten on an identical load. The returned bytes remain unverified candidate material.

If a crash leaves a complete spool but no collection marker, explicit reconciliation may adopt it only after full protocol/secret validation and an exact collector inspection proving exited-zero. It fsyncs the exact spool descriptor before persisting that proof or removing the collector. Partial, corrupt, secret-bearing or inaccessible bytes do not obstruct authorized physical cleanup, but cannot become a result. A complete stream plus missing container without prior exited-zero/removal proof is still unresolved; absence does not establish success. Unknown start outcomes are never automatically replayed.

After cleanup, a crash before receipt publication can be repaired by loading the durable spool and reconstructing the receipt. After receipt publication, the same bytes can be loaded for idempotent candidate registration. Journal version1 remains cleanup-compatible but has no durable-byte proof and cannot be silently upgraded to a successful loaded result. Retention must preserve the spool through candidate registration/reconciliation; deletion policy is not installed here. Unvalidated spools may contain sensitive output, remain private, and must never be published or logged directly.

## Failure outcomes and orchestration

The automatic failure reconciliation pass and explicit reconciliation share a15-second remaining budget across Docker RPCs; each individual RPC also retains its five-second maximum. Trusted callbacks, filesystem operations and bounded protocol decoding are cooperative and cannot promise a hard wall-clock deadline across blocked kernel I/O. No cleanup RPC is started after that budget expires. Authority, inspection or policy failure leaves ownership unresolved. The original exception is re-raised; ordinary exceptions gain fixed `cleanup_status='confirmed'` plus `export_outcome`, or `cleanup_status='unresolved'` plus a safe reconciliation note. Fixed failure codes are retained without raw stderr or exception details.

`workspace_export_status(runtime, spec, authorize=...)` inspects the private journal under the same attempt lock. It returns `{state, outcome, collector_id, failure_code}`. Only a genuinely absent journal yields `state='not_started'`; a partial, linked or malformed journal fails closed. This is durable-state introspection, not a fresh physical cleanup attestation.

Once physical cleanup is confirmed, the journal records one of:

- `aborted`: no valid result remains under the enforced policy. This export is terminal, cannot reload under a relaxed secret policy and cannot be reset or relaunched.
- `recoverable`: valid bytes and exited-zero proof remain; fresh-authority `load_workspace_export` can construct the result without execution.
- `ready`: a validated result receipt is committed and can be loaded identically.

Unresolved ownership has no terminal outcome and status reports `held`. An aborted export is not successful task completion, workflow failure classification, or permission to release a child/root; future orchestration must make those transitions using its own exact cleanup and scheduler authority.

`reconcile_workspace_export` preserves `collector_id`, `collector_removed`, `spec_digest` and `bytes_recoverable`, and adds `outcome`, `terminal_aborted`, and `failure_code`. Orchestration should inspect status under its own result-driver lock: execute only when not started; otherwise reconcile, load only when bytes are recoverable, and preserve an aborted/unresolved outcome without creating another collector. Reconciliation never removes journals or resets an export slot. Process death still requires a mandatory restart reconciliation path in the controller; an in-process exception handler cannot run after process death.

## Evidence and gates

`evidence/routed-export-failure-reconcile-tests.xml` and `.log` contain the current exporter/protocol/runtime suite. The202-test durable-spool receipt and earlier179-test receipt remain historical. Exporter regressions exercise real subprocess bounds and fake Docker argv/readbacks, concurrent one-shot admission, unknown mutation recovery, revoked execution with separate cleanup authority, identity/policy drift, post-stop workspace replacement, malformed/secret output and forged returned-tree rejection. Protocol tests execute the actual standalone collector locally. Current source hashes are in `evidence/routed-export-failure-reconcile-binding.json`. Failure-reconciliation regressions additionally cover an actually running simulated collector at start timeout, automatic stop/remove, lost-create acknowledgment discovery, zero discovered IDs retaining ownership, cleanup-only cancellation authority, permanent aborted outcomes, safe diagnostic replay and cleanup deadline expiry. New tests inject controller interruption after capture/before marker, before cleanup and during receipt publication; cover uncertain removal, partial streams, missing exit proof, damaged/linked/replaced spools, current secret policies, stale identities, exact stable restart replay, and fsync ordering. These are local fault-injection tests, not a host power-loss qualification.

Actual Docker UID1000 mode0600 access, real read-only/network isolation and physical resource cleanup still require the separate Linux qualification. No remote commands, credentials, provider inference, live service changes, scheduler migrations or deployment were performed here. This module does not capture a revision, release child/root occupancy, or assert verification success.
