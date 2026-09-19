> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Isolated routed check runtime

`routed_verifier_runtime.py` supplies a local, controller-owned per-check Docker
lifecycle. Each check is a separate container. This module records actual Docker
exit/OOM observations and bounded diagnostics. It does not publish a workflow
gate, release a child/root seat, or interpret stdout as proof that a check passed.
No live Docker qualification or deployment is claimed by its fake-Docker tests.

## Contract

Construct `VerifierRuntime(base: Runtime, journal_root=private_path)`. The base
owner and immutable image must equal the check spec. The controller must provide
an exact `True` returning authorization callback; caller/model booleans are not
an authority source.

Frozen `CheckRuntimeSpec` fields are `owner`, `attempt_id`, `generation`,
`root_id`, `root_generation`, `plan_sha256`, `check_id`, `image`,
`candidate_path`, `task_path`, `candidate_identity`, `task_identity`, `argv`,
`timeout_seconds`, `uid=1000`, `gid=1000`, `cpus=2`, `memory_mib=4096`,
`pids=512`, and `max_log_bytes=65536`. Paths are `Path` objects; identities are
positive `(device, inode)` tuples. The canonical SHA256 digest includes every
field. Check IDs are bounded to 128 ASCII identifier characters. Limits match
the protected policy: 1–600 seconds, 1–2 CPU, 128–4096 MiB, 16–512 PIDs, and
0–1 MiB diagnostics. A zero diagnostic cap permits only empty diagnostics;
nonempty output fails closed instead of being silently discarded.

A command is either a protected `/run/task/<basename>` script or an approved
absolute Python, shell, or Node interpreter, followed by a small explicit flag
set and the protected script. Inline `-c`/eval and workspace/tmp executables are
rejected. The first qualification command can be
`/opt/hermes/venv/bin/python -I -B /run/task/check.py`. The controller policy
must independently pin the entire argv and trusted script bytes. This runtime
also hashes the script and rechecks its bytes and directory identities after
authorization, immediately before create/start. Before either RPC, candidate
root, task root and script must have read/execute POSIX mode permissions for
the exact UID/GID 1000 identity, with no supplementary groups. Owner permission
bits take precedence over group bits, then other bits. A mismatched private
0550 group is an infrastructure refusal before container creation, not a check
rejection caused by exit 126. These are mode-bit checks, not a claim that an
arbitrary host ACL or security module cannot impose additional restrictions.

- `run(spec, authorize=..., forbidden_values=())` creates and starts at most once,
  observes exit, persists its receipt and secret-scanned logs, then removes the
  exact container. It returns `CheckResult` only after acknowledged removal and
  absence verification. A ready prior journal loads the same result.
- `reconcile(...)` never launches. A nonexistent attempt journal returns
  `state=not_started`, `cleanup_confirmed=True`, `result_available=False` and
  creates no attempt directory. An existing exact journal can stop/remove its
  container and returns `state=cleaned`, `runtime_id`, `spec_sha256`,
  `cleanup_confirmed`, `result_available`, and `aborted`.
- `load(...)` rechecks the exact private journal, absent runtime, log file
  identity/hash/permissions, current secret policy, and fresh authorization.
  It returns the same `CheckResult`, including durable diagnostic bytes.

`CheckResult` has `spec_sha256`, `runtime_id`, `exit_code`, `oom`, `timed_out`,
`cleanup_confirmed`, `logs_sha256`, and `logs` (excluded from repr).

Authorization actions are `prepare`, `create`, `start`, `inspect`, `publish`,
`load`, and cleanup actions `reconcile`, `cleanup_inspect`, `cleanup_logs`,
`stop`, `remove`. The composition must permit exact cleanup after cancellation
without granting execution or result publication. Cleanup uses the stored spec
and Docker identity; replaced/missing candidate/task paths do not block it.
Current source bytes, revision and scheduler authority remain mandatory in the
controller callback for execution/publication.

## Isolation and persistence

Containers have a read-only root filesystem, UID/GID 1000, no capabilities,
no-new-privileges, no network, DNS loopback, private IPC, no host PID/UTS sharing,
fixed CPU/memory/swap/PID limits and capped Docker local logs. Only the exact
candidate (`/workspace`) and protected task (`/run/task`) are mounted, both
read-only with private propagation. A 64 MiB noexec/nodev/nosuid `/tmp` is the
only writable tmpfs. There are no credentials, provider endpoints, controller
files, sockets, caller scratch, or agent task mounts. `/usr/bin/env -i` clears
execution environment before supplying fixed PATH/HOME/TMPDIR/LANG and the
protected argv. The pinned image/interpreters and host Docker daemon remain
trusted dependencies.

Private 0700 directories, 0600 single-link files, fsync/atomic rename and an
attempt-generation flock preserve create/start/observation/removal state. Call
authority is thread-local. Create/start intent is durable before each RPC; exact
image/name/labels/mounts/security/resource policy is inspected before start and
cleanup. Active execution has a monotonic deadline. Completion observed after that
deadline is marked timed out. Persisted wall time alone cannot establish
elapsed time across a controller crash and clock rollback. If recovery finds
an exited check without an already durable exit observation, it records
`aborted=completion_timing_unproven`, cleans up the exact container, and returns
no usable result. It does not claim that the check timed out. A valid observation
persisted by the live controller before the crash retains its original timing
classification and can be replayed after exact cleanup.

Failures trigger a separately authorized, bounded 15-second exact reconciliation
while the existing lock is held. The original error is preserved with
`cleanup_status=confirmed|unresolved`. Unknown create with zero matches,
unexplained absence, or a lost removal acknowledgment stays held. A removal
intent alone is never absence proof: `removal_confirmed` is durable only after
Docker acknowledges removal. A crash between removal and that durable marker
can therefore require operator reconciliation; the module never resets or
relaunches the check. Recovery that stops an unfinished check marks it aborted,
rather than treating its stop exit as successful execution.

Diagnostics include all merged stdout/stderr that Docker still retains, read
without a line-tail limit and subject to the configured byte/time cap. Output
above that cap aborts publication rather than silently truncating it. Docker
local rotation can still discard earlier output: its two 1 MiB files include
record framing, so this storage bound does not prove 1 MiB of retained payload.
The log hash and known-secret scan therefore cover retained output, not a
proven full-lifetime transcript. Full-lifetime coverage remains an integration
gate requiring bounded capture from execution start or reliable rotation
detection; this correction does not claim that gate is met. Known secrets are scanned before controller persistence and again on
load, including decoded strings/keys in complete JSON or JSONL diagnostics.
The shared controller policy permits at most 64 values of 4096 bytes each and
65536 total bytes. This is a known-value scan, not a general secret detector or
a decoder for arbitrary encodings. Oversized/unreadable/secret-bearing logs
abort result publication while exact cleanup proceeds. Logs written before a durable observation marker remain private evidence;
a fresh exited state cannot reconstruct the lost monotonic completion bound,
so those logs cannot create a usable result after restart. Partial or replaced
logs likewise never produce a result. These private receipts are controller evidence, not model
attestations or independent protection against a compromised controller UID.

The remaining gate is actual Linux Docker qualification with the pinned image,
protected scripts, real UID1000 access and parent-owned candidate/authority
composition. No production schema, services, credentials or host groups change
in this module.
