# Immutable environment registry (P14 foundation)

`cloudworkbench.environments` is an operator-only registry. It does not accept end-user URLs, commands or host paths. Registering and qualifying a candidate never change the active environment. Activation requires the previously observed active version, and only a qualified candidate can win that compare-and-swap.

The manifest versions Linux architecture, base/runtime image SHA256, package-lockfile hashes, CLI versions, registered repository IDs and exact commits, install/start/readiness argv, protected verification script IDs and hashes, secret references, network-profile ID, resource limits and deliverable paths. Skills/MCP lists must remain empty until their separate capability gate. Manifest data contains references, never secret values. Any content change requires a new version. Repository locations and protected-script paths resolve through separate trusted operator configuration; neither is accepted as a request path.

## Operator commands

Use the installed worker Python and an operator-owned database directory. The API needs read access only once integrated; end users must not receive write access to this database.

```sh
python -m cloudworkbench.environments --registry /path/to/environments.db register /path/to/demo-v2.json
python -m cloudworkbench.environments --registry /path/to/environments.db qualify sample-web demo-v2
python -m cloudworkbench.environments --registry /path/to/environments.db show sample-web demo-v2
python -m cloudworkbench.environments --registry /path/to/environments.db activate sample-web demo-v2 --expected-active demo-v1
python -m cloudworkbench.environments --registry /path/to/environments.db active sample-web
```

Use `--expected-active NONE` only for first activation. A nonzero qualification/activation exit leaves the active version unchanged. Build the image in the trusted operator build boundary first, capture its immutable image ID and base image ID in the candidate manifest, then register/qualify it. A failed image build must never trigger activation. The registry deliberately does not execute install/start commands on the host.

Qualification inspects the exact local image ID, OS and architecture, checks every declared CLI's `--version`, and runs every readiness argv. Each probe uses a separate bounded Docker container: nonroot, no network/host mounts/secrets, read-only root, dropped capabilities, no-new-privileges, CPU/memory/PID bounds, bounded tmpfs and no Docker logs. The image must include Python 3 for the version probes. Timeouts and failed cleanup prevent qualification. Probe output is discarded; safe pass/fail receipts remain in the registry. A killed operator can leave a `running` qualification record and a runtime labeled `io.cloudworkbench.environment-probe=true`; inspect that exact probe before cleanup/retry. It cannot activate a candidate.

This is **prebuilt-image readiness qualification**, not a complete environment build service. Declared installation/startup commands are immutable metadata; the operator build process must execute and attest them in the intended boundary. Readiness probes should check material expected behavior. The current Docker qualifier runs probes independently and cannot qualify network-dependent or persistent-service startup behavior. Those profiles require a separate trusted qualifier and evidence; do not substitute a no-op probe.

## Integration contract

```python
registry = EnvironmentRegistry(database_path)
registered = registry.register(manifest_dict)
qualified = registry.qualify(project_id, version, docker_qualifier)
snapshot = registry.resolve(project_id, explicit_version)
registry.activate(project_id, version, expected_active=previous_version)
```

`resolve()` returns `{manifest, manifest_sha256, qualified, qualification}` for the explicit immutable version. It never substitutes the active version. `qualification` includes image/platform/CLI identity, all probe exits, timestamp and qualification ID. APIs should open `EnvironmentRegistry(path, read_only=True)` and resolve the explicit requested project/version before admission. Read-only construction neither creates a missing database nor changes its permissions. The worker persists this full snapshot on the attempt before launching and retains it through recovery, verification and subsequent turns. It resolves the previous same-session snapshot for follow-ups, so changing the active environment never changes an existing session. Validate protected script bytes against `script_sha256` before copying them. Activating v2 must not change queued/running v1 attempts or checks. Public `SessionRequest` does not accept the internal snapshot; use a trusted store field or persist it through a fenced attempt transition.

`legacy_manifest(project_id, version, project_config, runtime_config, architecture=..., cli_versions=..., readiness_probes=...)` imports the existing trusted demo configuration explicitly. It records `legacy_template_id`, hashes protected scripts and excludes host paths/token settings. This compatibility marker makes no claim of a Git checkout or patch. Import still requires actual qualification before activation. Base image defaults to the existing runtime image in this legacy import; full build provenance requires a fresh manifest with the true base image digest.

## Evidence and limits

Focused tests cover immutable/idempotent registration, detached snapshots, failed candidate preservation, concurrent promotion, inactive-version resolution, mismatched/failed probes, schema/path/resource constraints, pinned CLI failure, Docker timeout/cleanup and safe legacy import. Tests use fake Docker subprocess responses; they do not claim a live Docker qualification. The API/runner integration is opt-in through `environment_registry` in both service configurations. No live registry was deployed as part of this module task.

## Supported initial execution profile

Manifest CPU/memory/PID/workspace limits must equal the worker deployment. The optional worker `qualified_images` list admits at most 32 exact local immutable image IDs (`sha256:...`). Without that list, only the worker's default image is allowed. Registry qualification and project/version admission remain required: this allowlist alone never qualifies a manifest. Remove an image from the list to revoke further execution and verification with it; the worker fails closed instead of substituting a different image.

Both agent and protected verifier use the image from the attempt's persisted manifest. A different admitted image gets a new Runtime object with the deployment's existing UID/GID, mount roots, limits and network restrictions; the shared Runtime is never mutated. The verifier still has no network and a read-only workspace. The egress gateway image stays pinned independently, even when it originally defaulted to the worker image. Status, stop, inventory and cleanup operate on fenced runtime IDs through the common deployment runtime.

To upgrade the execution image, qualify new immutable manifest versions, explicitly allow both old and new image IDs, and deploy the new default image. Keep the old local image available while its sessions can resume. Existing sessions and follow-ups retain their original snapshot, including old image and protected checks; changing the active version or worker default never migrates them. To retire an unsafe old image, revoke it and start a new session with an explicitly chosen new version. Never rewrite historical manifests. Older images without structured authentication-failure events retain their older generic failure classification, though the current worker's credential admission and staging checks still apply.

`environment_network_profile` (default `none`, or `legacy-configured` for network-enabled deployments) and `environment_secret_refs` (default `[]`) must match the manifest. Nonempty startup commands remain unsupported. One root repository is supported through the separately registered local repository/immutable commit configuration; see REPOSITORY-DELIVERY.md. Optional registry omission retains existing demo behavior and does not claim immutable-environment qualification.

Protected check description/argv/timeout and script destination name come from the snapshot. Only the script ID resolves to a trusted configured source path; its regular-file, single-link, no-follow read and SHA256 are verified immediately before each verifier launch, including restart. Changed check bytes fail verification infrastructure rather than silently changing acceptance.

## Qualification diagnostics and promotion semantics

`show` includes `latest_qualification` with status, timestamps and a bounded `error_code`. Categories distinguish unavailable Docker, image inspection/platform failure, CLI mismatch, failed readiness, timeout, cleanup uncertainty, and invalid/mismatched receipts. Raw stderr, command output and exception text are never persisted. `resolve` excludes this mutable diagnostic so a previously pinned snapshot stays stable. This module's `EnvironmentError` is a domain `ValueError`, not Python's historical alias for `OSError`.

`image_digest` and `base_image_digest` currently mean a **local Docker image ID** (`sha256:...`); a distribution manifest digest is not interchangeable. CLI versions are exact tokens: `3.13.2` matches `Python 3.13.2`; `3.13` intentionally does not. Omitted resource limits default to the current Runtime values: 2 CPUs, 4096 MiB RAM, 512 PIDs and 20480 MiB workspace. A regression test compares the manifest/import defaults directly against an actual `Runtime` instance without invoking Docker. The initial manifest profile supports integer CPUs from 1 through 6.

Activation is an operator promotion pointer, **not admission revocation**. Admission requires an explicit qualified version also allowed by project configuration; activating v2 does not automatically forbid a new explicitly allowed v1 session. Remove v1 from new-session admission policy deliberately when appropriate, while preserving its immutable data for recovery. Automatic default-version selection/revocation is outside this initial integration.

Schema version 1 serialization is a compatibility contract: registered manifests already contain all defaulted fields. Changing defaults does not rewrite those explicit stored values. Adding/removing fields or changing serialization must use a versioned decoder/migration before deployment; never reinterpret existing digests using a changed schema. No cross-schema migration is currently implemented.
