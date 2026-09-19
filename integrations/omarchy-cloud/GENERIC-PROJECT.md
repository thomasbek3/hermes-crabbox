# Generic Hermes project

This initial offline profile was deployed on2026-09-18 without new tests or reviews. The default caller subsequently moved to the requested internet/Chromium profile `hermes-tasks-web-v2`; see `docs/BASIC-DELEGATION-STATUS.md`. This document preserves the initial offline profile's preparation contract.

`hermes-tasks` uses `hermes-tasks-v1`, Hermes with `grok-4.6`, the existing pinned image `sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693`, and the existing 1 CPU / 1024 MiB / 128 PID / 256 MiB workspace contract. Prepared API and worker capacity is 3; worker `hermes_capacity=3` and `host_memory_reserve_mib` is at least 8192 (a higher existing reserve is preserved). This changes scheduling limits, not the machine's physical capacity. Concurrent Hermes execution requires the parent's accompanying bounded Store/Runner concurrency change; other agents retain their existing serialization.

The project has neither a repository nor a template, so a new session starts with the existing runtime's fresh empty workspace. It does not inherit booking files, scripts, or checks from `sample-web`. Follow-ups use the same workspace and native Hermes session under the existing runtime. Upload files through the existing `/v1/inputs` reservation/content API and put their IDs in `input_ids` on session creation. Inputs remain owner-scoped and are exposed through the existing task input mechanism. This does not add arbitrary repository URLs, host paths, network access, or dependency installation.

There are no configured protected checks. Existing `Runner.start_verification` completes a successful execution with `outcome=unverified` without starting a verifier container. A completed task means execution finished; it does not certify requested acceptance criteria. Model-written tests or claims do not change that outcome.

## Administrative preparation

Use the existing deployed Python so the script can import `cloudworkbench.environments`:

```sh
sudo -n /opt/cloud-workbench/.venv/bin/python register-generic-project.py \
  --output /var/lib/cloud-workbench/qualifications/hermes-tasks-prepared-UNIQUE \
  --registry-destination /var/lib/cloud-workbench/environments/hermes-tasks-v1.db \
  --reuse-image-readiness
```

The script reads the active API/worker configurations, copies their registry into the fresh private output, and writes candidate `api.json`, `worker.json`, `environments.db`, `environment.json`, `image-readiness-provenance.json`, and `preparation.json`. It never publishes those files, changes a service, reads provider credential values, alters client grants, or launches Docker/providers. It refuses an existing output directory or existing destination registry; partially written output is retained on failure and is not silently reused.

The environment registry requires nonempty readiness definitions and a qualification record. We retain the original image-only readiness definition and explicitly reuse the already recorded Docker image evidence from `sample-web/hermes-grok-v1` (manifest `552804d2c3873487b5cd7dd399f833a59f5fcd2f9c672876f63f2b5c6ba546bf`). The new record uses the existing `trusted_builder` contract. No probe or test is rerun, and no new output is invented. The sidecar records `probes_run=0`, the original manifest digest, qualification ID/time, original results and probe definitions, and every changed manifest field. Only project/version/checks/template marker/expected deliverables may differ; image, runtime resources, network/secrets, CLI versions, and readiness definitions remain identical. This is inherited image readiness, not qualification of the new project workflow.

Only the new project's registry default is set; existing projects and active versions are preserved. The parent operator must publish the prepared registry/configs with normal service-readable ownership using the existing service procedure, preserving the original configs/registry as a rollback point. The delegating client's authorized projects must explicitly include `hermes-tasks`; the preparation script deliberately does not change principal scopes. The generic project requires no schema change. The preparation script changes no application source; publish the parent's separate Store/Runner concurrency update before relying on three simultaneous Hermes tasks.

New requests use:

```json
{"project_id":"hermes-tasks","agent":"hermes","model":"grok-4.6","environment_version":"hermes-tasks-v1","goal":"The actual task","acceptance":[],"input_ids":[]}
```

Follow-ups remain `POST /v1/sessions/{session_id}/messages` with `{"message":"The next instruction"}`. Native session recovery remains the existing fail-closed policy; the project does not reset or replace old sessions.
