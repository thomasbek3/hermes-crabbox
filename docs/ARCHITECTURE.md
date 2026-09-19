# How Hermes Crabbox works

[Home](../README.md) · [Quickstart](QUICKSTART.md) · [Status](STATUS.md)

## The delegation path

1. **A parent agent submits a task.** Grokbot, Hermes, Codex, Claude, or another
   caller supplies the assignment and authorized source inputs over MCP or HTTP.
2. **The host admits and queues work.** The task service checks credentials,
   ownership, project policy, and available resources.
3. **Crabbox creates the environment.** Each task attempt gets its own container.
   The workspace, tools, browser, and desktop live in that task environment.
4. **Hermes does the work.** It loads the cloud-worker SOUL and applicable repo
   instructions, works on the assignment, and saves deliverables.
5. **The parent collects the result.** It retrieves status, files, and visual
   evidence and handles any authorized GitHub publication with its own credentials.

MCP is a client-facing adapter to the existing API. Both use the same issued
credentials, scope enforcement, ownership checks, and revocation model.

## Optional Jev and pstack roles

The primary Hermes worker starts first. In the routed profile, it can invoke
`cloud_route_task`. Jev selects from allowed workflows; an explicit workflow can
skip Jev. Deterministic policy supplies the predefined model and effort for each
role. The primary worker invokes stages through `cloud_run_pstack_stage`.

Temporary role sessions run in the same task container and workspace, with
separate role contexts. Planning/review roles retain their read-only tool
boundaries. They do not each create another task container. See
[the routing guide](CRABBOX-PSTACK.md) for current policy and qualification limits.

## Lifecycle

Closing the parent connection or viewer does not cancel the job. The worker runs
until completion, failure, cancellation, or its configured limit. On terminal
cleanup, the task container and host-side per-task credential snapshots are
removed. Saved workspace/session state and exported results have a separate
retention lifecycle so an explicit follow-up can continue prior work.

A follow-up is another attempt using the session's saved state and pinned
environment. A fresh assignment is a separate task. There is no automatic parent
check-in service in this release. See [resource limits](STATUS.md#resources).

## Trust and access

Tailscale controls reachability; the API credential controls task access. Neither
a GitHub clone nor a downloaded skill supplies either permission. Each cloud
caller needs a reachable tool-execution host and its own credential.

Containers are operational separation, not a claim of hardened isolation against
all hostile code. Dedicated task model access is available where the worker
needs it. Personal login homes, refresh-token state, parent GitHub credentials,
and the host Docker socket are not intended task mounts. See [SECURITY.md](../SECURITY.md).
