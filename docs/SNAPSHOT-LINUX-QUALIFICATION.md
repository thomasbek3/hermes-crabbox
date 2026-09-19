# Synthetic Linux snapshot qualification — passed

Parent execution of frozen v5 passed on Omarchy: `evidence/snapshot-linux-v5-20260918.json`.
The receipt validator passed, all source hashes match the frozen bundle, four synthetic
requests passed the UID/read-only mount and lifecycle checks, and the three named live
service process IDs were unchanged. Exact temporary resources and all three fixture
directories were removed; the original base image/tag were preserved. Directory cleanup
receipt: `evidence/snapshot-linux-temp-cleanup.json`.

No real credentials or provider requests were used. Refresh, entitlement, root-orchestrator
wiring and end-to-end workflow acceptance remain outside this proof. The historical
preparation and failed-attempt details below remain useful provenance.


This v5 checkpoint is prepared offline. The parent's v4-final Linux attempt passed
image build, base-layer/alias checks and direct-source DAC, then failed in the
worker harness. Its receipt remains `evidence/snapshot-linux-final-20260918.json`;
cleanup completed and service PIDs were unchanged. Exact read-only inspection of
the isolated fixture DB showed a collected synthetic result. The harness had passed
collect()'s boolean acknowledgement into json.loads instead of reading its durable
response. This revision fixes only that fixture mistake and adds fixed subphase /
exception-type diagnostics. Production auth/runtime sources are unchanged.

## What the fixture will establish

The harness requires the exact `omarchy` hostname and root supervisor, plus the
existing `cloud-worker` UID959, primary GID960, supplementary GID959 and Docker
socket access. It creates a fresh `cwb2-snapshotqual-*` temporary directory,
private isolated controller DB, dedicated **synthetic** source, snapshot root and
Docker journal root. It never reads a live worker config, environment, controller
DB, canonical token, or dedicated-login home.

It freshly inspects exact base image
`sha256:1e1948adf1632b9b1cf6c5356bca083d9465986a09ee93b266fc75020a5e1db6`
before building. This is an existing immutable provider candidate, not a claim
that all current application code is baked into it. A unique temporary derived
image replaces only the provider entrypoint with the embedded synthetic fixture,
removes its inherited Python bytecode cache, and adds a unique ownership label.
The build uses `--network none --pull=false --progress plain` with private HOME
and Docker config. The base must already have an existing tag so removing the temporary alias cannot
remove its sole reference. A new qualification-owned base alias is created only after an
absence check; its tag and pinned image ID are rechecked immediately before FROM
uses it. Derived RootFS layers must contain the exact base-layer prefix. Cleanup
removes only that exact temporary alias after ID readback and confirms the pinned
base still exists. Existing images and production tags are unchanged. Controller source comes from the reviewed prepared artifact.

A network-none container under UID958:GID959 must fail to open a readonly direct
mount of the synthetic source file (UID959:GID960,0600) with EACCES. The worker
then uses the real AuthSnapshots, SnapshotProviderDocker and ProviderDispatch to
stage that source as UID959:GID959,0440. The actual request container runs under
UID958:GID959, checks that it can read its exact snapshot, attempts write/chmod and
records denial, and reports only synthetic metadata/hash. The gateway has no
credential mount. The request fixture performs no HTTP calls; its allowlist is
only `snapshot.invalid`. Four short synthetic request instances exercise:

1. Request release removes its snapshot after actual container/network cleanup.
2. Removing the staged credential after execution does not prevent inspection,
   release and cleanup using the retained private binding receipt.
3. An old owner can be durably cleaned while its snapshot awaits the explicit
   owner-material hook.
4. A newer owner/request retains its snapshot and exact two containers/two
   networks while the old owner's material hook runs, then cleans normally.

The source bytes, owner/group and0600 mode must remain unchanged until temporary
fixture cleanup. These are Unix DAC/mount/lifecycle assertions. A synthetic
entrypoint result is **not** real provider authentication, entitlement, model
support, inference, refresh, or production activation evidence.

## Bounds and cleanup

The script requires2GiB available memory and Docker filesystem free space. It uses
one sequential request pair at a time, the production adapter's memory/PID/CPU and
bounded local-log settings, plus one earlier64MiB network-none DAC container.
The base image already exists; no installation or host configuration is performed.
Build deadline120s, worker deadline150s, outer SSH deadline360s. Payloads and source
files are tiny; this is not a workspace disk-quota proof.

Each admitted request's exact scope is persisted before Docker RPCs. Fallback listings filter both reservation and request labels, so an older request cannot select a newer request under the same reservation. Root fallback
cleanup checks the full runtime IDs, reservation/request/epoch/launch labels,
expected names and exact derived image before removing only fixture objects.
Temporary image removal checks its unique label and exact image ID. There is no
broad prune. Unknown build/worker outcomes remain unconfirmed even if a later
listing is empty; the receipt cannot report a pass or complete cleanup in that
case. The isolated directory and receipts remain for inspection. Normal final
cleanup removes only synthetic credential files from that newly created tree.
Named service ActiveState and nonzero MainPID maps are captured before/after and must be identical; the harness contains no restart/stop or
production migration command.

## Prepared artifact and parent execution

Prepared offline artifact: `evidence/snapshot-linux-prepared-candidate-v5.json`.
Local guards: `tests/test_snapshot_linux_qualification.py` (57 passing tests).
Bindings: `evidence/snapshot-linux-harness-v5-binding.json`.

To make another fresh offline candidate after reviewed source changes:

```sh
.venv/bin/python scripts/qualify-snapshot-docker-linux.py --prepare \
  --output evidence/snapshot-linux-prepared-NEW.json
```

Only after parent inspection and explicit decision to execute the isolated proof:

```sh
.venv/bin/python scripts/qualify-snapshot-docker-linux.py --remote \
  --prepared evidence/snapshot-linux-prepared-candidate-v5.json
```

The remote branch uses only `thomas@100.83.74.92` and
`sudo -n /opt/cloud-workbench/.venv/bin/python`. `--prepare` never invokes SSH.
No mode defaults to execution. Existing artifact paths are not overwritten. The
parent should inspect any failed receipt and its exact fixture namespace before
retrying; the harness does not automatically run a second qualification.

Failures include fixed root/cleanup phase names and a bounded worker phase/error-code record. Exception text and raw worker stderr are not returned; unavailable or malformed diagnostic data becomes a fixed unknown diagnostic. Worker success requires the complete phase and exit0.

The fixed synthetic build, which runs before any credential fixture is created,
records at most65536 raw bytes combined stdout/stderr, exit code and fixed timeout/
output-limit status. Output-limit and timeout kill the CLI process group and retain
unknown daemon effects; they do not prove BuildKit stopped. Generic Docker runtime
and worker stderr remain suppressed. Successful receipts require build exit0,
no build error/truncation, verified base layers and confirmed base-alias removal.

The synthetic evidence helper requires collect() to return exactly True, then reads
only the exact request row from the private fixture DB, requiring collected state,
1..4096 response bytes, matching stored SHA256 and the exact synthetic metadata
shape. This is controller qualification evidence, not production deliver() or a
response-authorization shortcut. Provider cleanup/release assertions remain intact.
`evidence/snapshot-linux-v4-diagnosis.json` records the secret-free read-only finding.
