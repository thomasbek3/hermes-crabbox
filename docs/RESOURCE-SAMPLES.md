> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Bounded resource observations

Status: sampler and coordinator are now integrated in local Runner/Store/result-bundle source. This integration has been qualified with a disposable real-Docker Runner on Omarchy, but **has not been deployed to installed services**. The earlier sampler proof remains separately scoped. Historical attempts without controller observations remain explicitly unknown.

```python
sample = sample_runtime(runtime, runtime_id, expected_generation=generation,
                        timeout_seconds=3)
window = SampleWindow(max_samples=60)  # allowed 1–120
window.add(sample)
summary = window.summary()
```

`runtime` is the trusted per-attempt Runtime, including its image, owner, UID/GID and root configuration. `runtime_id` must be its exact 64-character Docker id, taken from the controller attempt, with positive integer generation. Do not accept an arbitrary runtime id from a guest request. The observer snapshots already-validated Runtime fields and checks exact ownership/generation/running status with a compact targeted inspect both before and after a targeted `docker stats --no-stream --no-trunc` call. It never changes the caller Runtime, container state, Docker configuration or credentials.

Each call shares one monotonic deadline (default and maximum three seconds, configurable down to 0.1 seconds) and one aggregate 4,096-byte stdout budget across both ownership inspections and stats. Stderr is discarded. Three Docker commands are used in total, avoiding inherited OCI labels and health-check logs. Inspection overflow is labeled separately from stats overflow; missing executables and malformed inspection JSON have precise unavailable/ownership reasons. Each observation command is an argument-vector subprocess with a fixed environment and its own process group; timeout or output overflow kills that client process group, not the observed container. A 25 ms cleanup reserve is held inside each remaining deadline. Cleanup uses the remaining deadline rather than adding a fresh wait; a pathological pending OS kill can still defer reaping. A real-child regression checks that normal timeout cleanup kills and reaps the direct observer. Docker is a trusted configured binary; this is not containment for arbitrary executables that escape the process group. As with any user-space timeout, process scheduling can add small overhead; this is not a hard real-time OS guarantee. Hostile CLI tests cover output flooding, hung inspection and a descendant retaining stdout.

A sample contains UTC time, exact runtime id/generation/owner, a source ownership-policy SHA256 (owner/image/UID/GID/root/executable), elapsed milliseconds, status/reason and four values: `cpu_percent`, `memory_bytes_approx`, `memory_limit_bytes_approx`, `pids`. Invalid ownership, stale generation, nonrunning/missing runtime, malformed/incomplete metrics, output overflow, unavailable Docker or elapsed deadline produces **unknown with null metrics**, never a fabricated zero. Invalid identity/configuration can lack policy fields and is returned to the caller but cannot be mixed into a window that requires proven identity; record its reason as an observation failure separately. Legitimate idle CPU 0% is accepted.

Memory values are approximate because the CLI formats rounded units. Linux CLI memory excludes cache, and Docker PIDs include kernel threads. These semantics are explicitly retained in summaries. See [Docker's stats documentation](https://docs.docker.com/reference/cli/docker/container/stats/). These observations are neither billed usage nor CPU time integrals.

`SampleWindow` is memory-only, identity/policy bound, and retains at most 120 samples. It emits total/retained/dropped counts, observed/unknown counts, safe samples and observed maxima **within the retained window**. `true_peak` is always false. An older high value can be discarded, restart starts a new window, missed intervals remain missed, and execution/verifier/sidecar containers require distinct windows. The module does not claim host pressure monitoring, full-session/lifetime peaks, total reservations or multi-container aggregate measurements.

## Local integration

Runner owns one ResourceMonitor and polls completed work before existing lifecycle processing, then offers one candidate afterward in rotating attempt order. Observations default enabled (`resource_sampling_enabled: false` disables them); `resource_sampling_cadence_seconds` accepts 10–3600 seconds, default 10. There is one process-global outstanding sample, a process-start ten-second cooldown, and no queued backlog. Tick/cancel/close never wait for the sampler future. Completion intent suppresses further samples before the terminal commit; the atomic guard also refuses terminal/cancelled/fenced/replaced bindings.

Store.append_resource_sample checks the exact session/id/generation/runtime/role, live state, cancellation and completion intent inside the same SQLite write transaction as its reserved event. Its busy wait is 20ms with a 50ms query progress budget. Repeated identical sample ids are idempotent; conflicts refuse. The generic append_event refuses the reserved type, and the provider spool allowlist still rejects it. All event payloads are reconstructed from fixed validated fields.

No attempt result or completion sidecar is rewritten to add telemetry. The result bundle reads only controller resource events in the same authorized terminal snapshot. Each execution/verifier role includes at most 120 retained observations for its latest observed runtime, explicit historical/drop/older-runtime counts and identity. Sidecars are excluded and never implicitly summed. If no controller events exist, measured_usage is null with a reason. Malformed retained history stays explicitly unknown per role. Transient SQLite errors or history query deadlines return a retryable bundle 503, so transient load never silently substitutes unknown metrics into a previously observed ZIP. This preserves completed bundle bytes against late samples, supports restart readback from durable events, and retains existing completion-intent recovery semantics.

Candidate queries use a 20ms busy wait and 50ms progress budget and only select running/verifying attempts. Cadence is checked before optional database work; a failed candidate is skipped fairly. Poll, offer and close failures cannot abort lifecycle work. Invalid sampling config disables only the observer. Heartbeats expose fixed statuses/drop counters without exception text or runtime identity.

The integration has local lifecycle/auth/bundle regression evidence under `evidence/resource-integration-tests.xml`. Its own bounded Fable review is separate from the sampler unit review. No API/CLI/dashboard/preview code was changed.

## Integration constraints

1. Run optional observations outside the critical scheduling/control loop; a call can consume its full three-second budget. Use a bounded worker/concurrency/cadence policy. Cancellation and admission must not wait on an unbounded queue of stats calls.
2. Bind the trusted attempt id, current generation, exact runtime id and execution/verifier role alongside each sample; the sampler itself validates full runtime id, owner and generation, not an API-supplied attempt association. Discard stale results if the controller generation/runtime binding changes while sampling.
3. Keep one window per runtime/generation/policy. Do not mix verifier, execution or egress sidecar observations. Scope an initial integration honestly to whichever roles are actually sampled.
4. Persist fixed sample events through the guarded Store path at the global cadence. Restart readback reconstructs windows from those events; it does not guarantee capture before a short task exits.
5. Continue explicit unknown reasons for absent samples. Present retained observed maxima as sampled observations, not actual peaks, exact usage, billed cost or full reservation totals. Bundle metadata now uses the persisted controller format; provider-result telemetry claims are ignored.
6. Qualify recovery, cancellation responsiveness, generation fencing, short/missing samples, scope/aggregation, event bytes and fresh-controller serialization before claiming integration complete. No new host mounts, secret reads or Docker socket exposure to jobs are needed.

## Current proof

`evidence/resource-samples-docker-proof.json` records an actual disposable container on `omarchy` using the existing immutable image `sha256:408eb6e2b5c4cb58747fbb1fbac31031545135005079abf878012c33a6632331`. Source was passed through SSH into a unique private temporary directory and removed afterward; no installed source or services changed. The fixture had no host mounts, no credentials, network none, read-only root, no capabilities, nonroot uid 1000, 0.25 CPU, 128 MiB RAM/swap, 16 PID limit and 16 MiB tmpfs.

The canonical script emits its own source hashes and full receipt; `resource-samples-docker-proof.json` is verbatim stdout. `resource-samples-proof-transport.json` separately binds that stdout and the transferred source. Docker 29.7.2 on systemd/cgroup v2 was observed. Three valid samples used the quarter-core limit; the third overlapped eight targeted inspection reads on the same disposable container. This modest read-load case does not establish throughput under arbitrary host load. Exact values and elapsed times are in the receipt; all were within the three-second limit.

Wrong-generation, wrong-owner and stopped-runtime checks returned unknown with the expected reason. The exact unique-owner container was removed and owner-filtered absence verified. Initial proof before review is retained as `resource-samples-docker-proof-initial.json`; its hashes were appended by the local transport wrapper, and it is superseded by the verbatim canonical receipt. This demonstrates the sampler on a small quota container; it does not demonstrate integrated collection, full-host capacity, true peaks, billing or a production rollout. Observer commands and the production Runtime use the same fixed PATH/LANG-only environment; external DOCKER_HOST/context overrides are not honored by either path.


Tests and one bounded Fable review are recorded under `evidence/resource-samples-local-tests.xml` and `reviews/resource-samples-*`. The parent-side transport is `scripts/run-resource-samples-proof.py`. The disposable proof script is `scripts/qualify-resource-samples.py`; do not run it concurrently with a host operation that requires complete quiescence.


## Disposable full integration qualification

Canonical receipt: `evidence/resource-integration-docker-20260917T223429Z.json`; matching `.transport.json` binds verbatim stdout and every transferred source SHA256. Frozen source is `evidence/resource-integration-qualified-source.zip`. The real host was Omarchy, owner `resint-c2f39852a8d7`, using the existing immutable image. Isolated SQLite/state and synthetic execution/verifier/cancel containers used network none, no host mounts, no provider credentials, nonroot uid 1000, read-only root, 0.25 CPU, 128 MiB, 16 PIDs and 16 MiB tmpfs. The temporary source/state directory was removed; installed source/config/services/images were not changed.

Real Runner ticks published two execution and one verifier observations through the atomic Store path. Reconstructing a fresh Runner and Store against the same isolated DB preserved the events. This is controller-instance restart/readback, not a process-kill/service-restart test. A real pending sample was rejected after terminal commit, and a separately pending sample was rejected after actual cancellation. The completed unverified diagnostic ZIP contained both role windows, exact runtime/generation identities and no cost claim; 8700 bytes, SHA256 `657a0fb16a5af67a5a7ffed252474fa866296f6883f36a804d4ab6f7c140102a`. Its bytes were unchanged after polling the late result. Phase transitions were driven by the test harness; no provider or acceptance success is claimed.

All 266 canonical ticks were below the qualifier's 1.5s limit, maximum 0.880749s; actual cancellation took 0.880754s. This is bounded fixture evidence, not a host-throughput or universal latency guarantee. The first run `resource-integration-docker-20260917T223305Z.json` is retained as a failure: its PID1 Python sleeper ignored SIGTERM, triggering the existing Docker 10s stop grace and an 11.019s cancellation tick. The rerun only added a SIGTERM handler to the synthetic workload; production Runtime.stop behavior was unchanged. That failure remains evidence of the existing graceful-stop bound, not a sampler wait.

Both runs verified zero owned container/network leftovers. An independent fresh SSH readback is `evidence/resource-integration-cleanup-readback.json`. Source hashes in the remote receipt match the transferred local map. No full provider-job, system service restart, long-duration retention, multi-host load or production deployment claim follows from this qualification.


## Review disposition and exposure

The integrated Fable review returned REVISE. Verified fixes and evidence are recorded in `reviews/resource-integration-disposition.md`; no second PASS review is claimed. The ten-thousand-observation regression covers realistic retained-history query cost and retryable query-budget refusal. The observer intentionally spends the cadence interval even on a failed observation, to bound load rather than retry rapidly. Heartbeat counters describe the current controller process and reset after restart; persisted sample events remain the bundle source.

Session observers can read their authorized session's reserved sample events through the existing events API, including full container id, controller owner label and the policy digest. These are diagnostic identities, not credentials or host paths. No access to another owner's session is added. Existing clients must tolerate unknown event types. Provider input still cannot emit this reserved type. No secrets, environment values or Docker errors enter these events.

History counts refer to currently persisted matching controller events, not an independently retained lifetime total. No scheduled event purge exists in this checkpoint. Bundle reconstruction happens on download from a fresh authorized SQLite snapshot; Runner does not load 120 samples into memory on restart. The standalone `history_summary` helper remains available for diagnostics. The process-global executor can delay Python interpreter exit by up to the bounded sampler duration even though tick/cancel/close do not wait; this is not a hard real-time shutdown guarantee.
