# Routed caller Linux qualification harness

Status: prepared, **not executed by this checkpoint**. The 22 local tests validate the archive, synthetic native Responses frames and receipt rejection rules. Actual Linux results must be recorded separately.

The frozen v3 bundle is `../work/routed-caller-qualification-20260917-v3/bundle.json`, SHA256 `279e9d422b56dd14fd6e7907faec53a5e6b612d182dcb78c2183fbc730b0c162`, run `routed-df0a326cb165`. Use its accompanying frozen `harness.py`; earlier v1/v2 bundles are superseded and preserved. Full hashes are in `evidence/routed-caller-harness-preparation.json`.

## Operator execution

Copy only `bundle.json` and `harness.py` to a fresh private root-owned directory on Omarchy, compare their SHA256 values with the preparation receipt, then invoke:

```sh
sudo -n /usr/bin/python3 /tmp/<private-root-stage>/harness.py --remote /tmp/<private-root-stage>/bundle.json --receipt /tmp/<private-root-stage>/receipt.json
```

The host must identify as `omarchy`, UID must be root, and receipt path must not already exist. The image must already exist and inspect to `sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693`. There is no pull/build/service restart. Each Docker command times out within 30 seconds; the case has a 12-second relay-ready deadline and a 110-second result deadline. The three cases run serially.

Fetch the receipt privately and validate from the repository:

```sh
.venv/bin/python ../work/routed-caller-qualification-20260917-v3/harness.py --validate <receipt.json> --bundle ../work/routed-caller-qualification-20260917-v3/bundle.json
```

A failed run still saves its partial receipt. Inspect that receipt and actual owned inventory before deciding any recovery; do not rerun against an existing receipt or automatically delete unknown objects.

## What is exercised

Actual Hermes and relay execute under separate UIDs inside the inspected network-none caller: init1002, relay1001, Hermes1000. All three native profiles use synthetic host responses (Astra/high, Sol/max and Grok/xhigh). Each must produce exactly two requests: `read_file` of a synthetic fixture, then a final answer after the file result appears in the next request. The frozen launch receipt, request model/effort/profile binding, public `tool.started` and final event, bounded result/event files, and result launch hash must agree. Tool output is not added to the public spool.

Readonly review workspace and writable scratch are distinct mounts. The workspace directory has write DAC permission for the tool group before the readonly bind; the actual UID1000 write must fail EROFS. Probes also check denied worker config/socket and relay state access, init signaling, source modification, absent Docker socket, process UID/capabilities/no-new-privileges and cgroup resource limits. Exact mounts and process configuration are inspected by RoutedRuntime before process launch.

The harness records stopped create, a fsynced qualification-local binding, then starts init/relay/Hermes. This does not qualify RoleScheduler integration, revision capture/quiescence, durable workflow recovery, provider/account supervision or filesystem quotas.

## Ownership and cleanup

Every container uses unique owner `routed-df0a326cb165` and a per-case attempt label. Before cleanup, every discovered container must match both owner and the current attempt. Cleanup stops the exact ID, inspects stopped state, removes that exact ID, and verifies absence. Unknown mismatched objects are refused. Final inventory must be empty. No networks, images, volumes, live services or provider containers are created or removed. Private temporary fixture/evidence files remain for inspection.

All three live service PIDs/states and host headroom are recorded before and after; the validator requires services unchanged and active. Synthetic capabilities exist only in the private fixture and are not printed. No personal/provider credentials are read. Docker stdout/stderr are limited after capture, so this harness does not claim a streaming peak-allocation bound for Docker command output.
