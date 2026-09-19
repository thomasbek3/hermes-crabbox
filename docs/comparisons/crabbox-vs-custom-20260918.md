# Crabbox versus the deployed Omarchy delegation service

2026-09-18. Read-only source comparison; no installation, migration, model calls, tests or browser launches.

## Decision

Keep the existing task API/queue and Hermes integration for the immediate goal: Mac agents submit independent jobs to the Omarchy laptop and retrieve results. Crabbox is a credible candidate for replacing selected workspace/runtime operations, particularly if visible desktops, richer repository synchronization, checkpoints or additional hosts become priorities. It is not a drop-in replacement for the whole service. Do not add Crabbox around the existing nested containers without removing a corresponding lifecycle owner; that would add complexity.

This recommendation reflects architectural fit, not measured reliability or performance. Neither Crabbox+Hermes on Omarchy nor the latest custom browser/concurrency configuration has been demonstrated end to end in this comparison.

## Scope and evidence

- Crabbox source: `openclaw/crabbox`, commit `a9fd4b5e950e408a0bd1e10710e625632c9937a8`, shallow read-only checkout `/tmp/crabbox-comparison-20260918`. Latest release observed via GitHub API: v0.61.0. HEAD is not assumed identical to that release.
- Existing system: `cloud-workbench`, deployed native Hermes lane only. Do not count the unfinished routed workflow, browser portal or historical experimental modules as deployed capabilities.
- Fresh readback from host `omarchy` confirmed API, worker and credential timer active. Capacity=3, Hermes capacity=3, Hermes supervisor=7500 seconds. The deployed runner, entrypoint and runtime hashes match the recorded source versions. This is deployment evidence, not workload verification.
- Current native run limits: 1000 model steps / 7200 seconds; capture 7320 seconds. No automatic parent polling.

## Comparison

| Concern | Current custom service | Crabbox | Assessment |
|---|---|---|---|
| Agent submits a goal and disconnects | Authenticated submit API, durable SQLite attempts, independent systemd worker | CLI command execution and workspace lifecycle APIs; a retained lease is not a submitted Hermes task | Keep our task interface and supervisor |
| Three running, seven queued | Global/Hermes capacity and session exclusion in transactional claim | Local container acquisition starts containers directly; lifecycle-operation concurrency is distinct from job admission | Keep queue and RAM/disk admission |
| Hermes/model setup | Prepared native Hermes profile, Grok/xhigh, curated plugin, isolated task state | Runs supplied commands/harness; no first-class Hermes login/session adapter found on inspected paths | Keep or port Hermes launcher |
| Model credentials | Dedicated Grok credential and refresh helper; key remains outside tool containers | Harness owns model credentials, not Crabbox | Switching does not remove OAuth work |
| Containers and workspace lifecycle | Custom Docker create/inspect/stop, strict labels/generation/mount checks, bounded workspace | Maintained Docker/Podman adapter, reusable leases, labeled cleanup, runtime identity, checkpoint paths | Crabbox offers broader reusable machinery |
| Browser/desktop | Headless Chromium and Playwright prepared; no streamed desktop | Local-container browser/desktop setup plus VNC/screenshots/input commands | Crabbox is materially richer here |
| Source transfer | Clean committed snapshots, GitHub immutable archive, explicit uploads | Working-tree/diff sync, reusable workspaces and caches | Crabbox better matches interactive local edits |
| Outputs | Persisted API events, protected exports, artifacts and bundles keyed by task | Command streams, downloads, captures and optional coordinator history | Both useful; retain task-to-result mapping |
| Follow-up | Private native Hermes session after successful previous run | Warm workspace reuse, filesystem checkpoints/forks | Filesystem retention does not itself resume an agent conversation |
| Interrupted run | Partial output/workspace can remain, but native continuation rejects failed/cancelled/interrupted predecessor | Lifecycle recovery/checkpoints exist; not automatic recovery of Hermes's model conversation | Neither is demonstrated as seamless agent recovery |
| Isolation | Trusted credential-bearing coordinator has host Docker socket; model tools use separate containers | Provider-dependent developer environments; optional Docker socket access; trusted operator/config model | No automatic security upgrade; choose boundary deliberately |
| RAM | Configured 1GiB coordinator and 3GiB tool limits; these are caps, not measurements | Configurable container CPU/RAM; browser, desktop and Hermes workload dominate | No evidence that switching reduces actual RAM |
| Operations | Existing Python/SQLite service and worker, two-Mini clients | CLI can run without coordinator; optional portal/coordinator adds Cloudflare or Node/Postgres | Avoid full coordinator unless its features are needed |
| Additional hosts and cloud accounts | Current native lane is configured for the Omarchy Docker host; no general cloud provisioning interface | Existing SSH hosts plus provider adapters for cloud accounts and managed sandboxes | Crabbox is the stronger foundation if delegation should expand beyond this laptop |
| Maintenance | Bespoke service must be maintained locally; relevant files roughly 4000 lines including shared/unactivated branches | External upstream and releases; larger multi-provider dependency surface | Reuse only where it deletes meaningful custom responsibility |

## Important distinctions from the source

1. **Static SSH is not container provisioning.** The static provider uses an existing host. For isolated laptop containers, the local-container CLI should run on Omarchy or be reached through a deliberately configured adapter. Pointing a Mac at the laptop via static SSH does not alone create an isolated worker per task.
2. **Local container is direct, not a cloud job queue.** `internal/providers/localcontainer/provider.go` declares `CoordinatorNever`; acquisition in `backend.go` creates the container. The workspace lifecycle adapter exposes create/read/delete/desktop connection routes (`internal/cli/controller_service.go:552-616`), not arbitrary goal execution. Registered inventory can provide portal visibility without transferring execution ownership to the coordinator.
3. **Normal command execution remains owned by the CLI transport.** Keep/lease handles mean reuse of a workspace, not durable detached task semantics. Our long-lived worker could own that CLI process on Omarchy, allowing Mac callers to disconnect.
4. **Credentials remain our responsibility.** `docs/integrations/agents.md:217-235` explicitly separates remote execution from prompts, harness protocol and model credential delivery. A switch does not create a shared login broker.
5. **Our continuation limitation is real.** `src/cloudworkbench/store.py:443-465` requires the preceding attempt to be completed and its native result valid. Previously saying a stopped task could simply resume was too broad. Fixing this would be separate work, not part of the limit update or this comparison.

## Why integration is not just changing one command

Hermes currently owns its Docker terminal backend. The trusted coordinator receives the model token and host Docker socket; tool containers receive neither. Our runtime verifies and cleans up both roles.

Two possible integrations:

- **Crabbox owns the whole Hermes workspace.** Prepare an image with Hermes and Chromium, then run a job-specific Hermes profile inside it. This is the simplest execution model, but authentication, private profile persistence and tool access to credentials must be deliberately designed. Retaining our current Docker tool backend inside that container would preserve much of the old lifecycle and require Docker access; it would not deliver the hoped-for simplification.
- **Crabbox owns Hermes tool environments.** Keep the trusted coordinator outside, connect tool execution to an existing Crabbox workspace, and let Crabbox own creation and deletion. This better preserves current credential separation, but needs a Hermes backend/adapter integration. At the inspected commit, local-container does not advertise `FeatureClaimExec`; `crabbox exec --id` cannot be assumed to support it. Use an actual supported retained-lease run/SSH path. It is engineering work, not a proven configuration-only swap.

In either case keep: submit/status/cancel/results, task queue/admission, task ownership, private session mapping and model credential management. Replace only the chosen runtime/workspace owner. Do not run two independent cleanup authorities against the same resources.

## Recommended next decision

Thomas specifically highlighted cloud accounts and existing SSH hosts. For that broader direction, prefer Crabbox as the reusable environment layer rather than expanding our custom service into another multi-provider runtime. Preserve the Hermes job-facing contract above it. This is a recommendation for consolidation, not a claim that our present queue/session/auth can be deleted or that provider integrations have been exercised here.


For getting the requested service usable now, retain the deployed path and avoid an immediate rewrite. If visible browser desktops, dirty-worktree sync or multiple execution hosts are the next priorities, evaluate a single separately named Crabbox-backed worker before deciding to migrate. That later evaluation should establish isolated concurrent jobs, preserved credential separation, explicit continuation, cancellation, cleanup and output retrieval. No such evaluation was launched here because Thomas stopped test/verification jobs.

No hour estimate, lower-RAM claim or superior-reliability claim is justified by source inspection alone. The presence of upstream tests and releases is useful maintenance evidence, not proof of our particular deployment.

## Sources

- https://github.com/openclaw/crabbox/tree/a9fd4b5e950e408a0bd1e10710e625632c9937a8
- https://crabbox.sh/providers/local-container.html
- https://crabbox.sh/providers/static-ssh.html
- https://crabbox.sh/integrations/agents.html
- https://crabbox.sh/features/jobs.html
- https://crabbox.sh/features/runtime-adapter-stack.html
- https://crabbox.sh/features/coordinator.html
- https://crabbox.sh/features/portable-coordinator.html
- https://github.com/openclaw/crabbox/blob/a9fd4b5e950e408a0bd1e10710e625632c9937a8/SECURITY.md
- Local: `docs/BASIC-DELEGATION-STATUS.md`, `src/cloudworkbench/{store,runner,api,artifacts,hermes_job_entrypoint,hermes_coordinator_runtime}.py`, `integrations/omarchy-cloud/`.

Browser detail: the provider has real desktop/browser bootstrap code, but its browser flag is not a universal Chromium guarantee; Ubuntu fallback can install Firefox. A Chromium-specific requirement should use a prepared image or explicit setup.
