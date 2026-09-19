> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Routed-driver Linux qualification

## Executed checkpoint

Frozen candidate v2 passed on Omarchy on 2026-09-18: `evidence/routed-driver-linux-20260918-v2.json`, run `driverqual-410adc7c9bf54ab9`. Its 36-module source closure and harness were checked against the local source. The actual Hermes file tool completed with two synthetic Responses calls; terminal records survived caller removal with matching hashes. All lifecycle assertions below passed. Root/child occupancy remained held, with verification, provider cleanup qualification and scheduler seat release all false.

The exact temporary fixture directory was subsequently removed after receipt-hash, resource, mount and filesystem identity checks. `evidence/routed-driver-linux-temp-cleanup.json` records unchanged API/worker/cloudd PIDs and the preserved caller image. Local frozen source, review and test receipts remain available. The root-provisioning and synthetic-provider limitations below still apply.

This is an opt-in, isolated synthetic proof, not production activation. The script
is `scripts/qualify-routed-driver-linux.py`; no remote action occurs in --prepare.
The parent must inspect and explicitly run the frozen artifact. No image build,
real provider credentials, inference, external model review, live schema changes,
service restart, user/group changes, or production task is part of this harness.

## Exact exercised path

The fixture creates a new private controller DB and migrates that DB only. It uses
real Store, ProviderLeases, RoleScheduler, role broker admission, workflow submission,
revision capture, prepare_child_stage and drive_prepared_child. A synthetic root is
explicitly pre-admitted; synthetic instruction text and qualification callback are
clearly fixture inputs, not proof of production model/policy qualification.

The child profile is exactly openai-codex / gpt-6-astra / high. Actual pinned Hermes
runs in the existing caller image, with separate UID1000 Hermes, UID1001 loopback
relay and UID1002 init. The host WorkerSocketServer supplies two synthetic Responses:
a read_file invocation for /workspace/fixture.txt and a fixed final message after
observing that actual tool's output. There is no upstream HTTP request or provider
container. The response builder is embedded from the prior caller harness, with
its own frozen hash. Controller source is a frozen transitive relative-import
closure; every included file and both scripts are hash bound.

The proof checks durable scheduler bind/start intent before Dockerstart; exact
ready binding before Hermes; actual typed output collection; grant fencing before
Dockerstop; exact caller removal; stopped worker thread; two distinct request
nonces; read_file event and final event. Root state remains held with the same child
occupying its seat, child/root cancellation remains false, and no workflow gate is
recorded. The returned observation stays unverified, provider_cleanup_qualified and
scheduler_seat_released remain false. It does not exercise provider cleanup/release.

After caller removal, the harness reads /scratch/.cwb-observations through its exact
host bind path with no-follow opens and bounds. Result/events UID1000, inode, size,
and SHA256 must match the collector evidence. This proves retained scratch records
for this run; it is not a claim that stopped tmpfs survives or that worker records
are authoritative verification evidence.

## Root fixture and limits

This proof intentionally uses a root controller to preprovision task GID1000 and
worker socket GID1001 material, plus the setgid GID1000 stage parent. Actual service
worker959 currently lacks those groups. Successful execution therefore does not
qualify ordinary service-worker provisioning, change groups, or grant root access.

Remote target is only operator@worker.example.ts.net with hostname omarchy and root supervisor.
Each invocation uses a fresh private cwb2-driverqual-* directory and owner label.
It requires 6GiB available memory and 5GiB Docker free disk, verifies the existing
image ID, and refuses an existing owner namespace. One caller uses 1.4CPUs,
3008MiB and352PIDs, networknone, existing tmpfs/log bounds. Controller worker has a
150-second deadline; outer SSH360seconds. No disk-fill or workspace quota proof.
Named API/worker/cloudd active states and nonzero PIDs must be identical before/after.

Fallback cleanup selects only the exact run owner, then requires matching image,
name, child/generation/spec digest and role before stopping/removing a caller. No
broad prune, image removal, or existing path deletion occurs. Unknown controller
outcomes stay unknown even after empty inventory; they cannot produce a pass.
The harness initially retains its fixture DB, scratch observations and receipt for inspection. After the successful v2 receipt was validated and saved locally, the parent removed only that exact temporary fixture directory as recorded above. Failed historical receipts remain preserved locally.

## Commands after parent inspection

Prepare locally (no subprocess or remote commands):

```sh
.venv/bin/python scripts/qualify-routed-driver-linux.py \
  --prepare evidence/routed-driver-prepared-candidate-v2.json \
  --image sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693
```

Then parent may execute the exact frozen artifact once:

```sh
.venv/bin/python scripts/qualify-routed-driver-linux.py \
  --remote evidence/routed-driver-prepared-candidate-v2.json \
  --receipt evidence/routed-driver-linux-FIRST.json
```

Prepared/receipt paths must be fresh. Failures retain an explicit false result,
fixed phase/status and service/cleanup evidence; there is no automatic retry. The
local tests cover offline freezing/closure imports, real scheduler+revision fixture
setup, cleanup identity, durable-file hashes and rejection of incomplete receipts.
Those local tests alone do not establish remote execution; the executed v2 receipt above supplies that evidence for its exact frozen source and fixture.
