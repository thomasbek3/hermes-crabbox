# Status and known limits

[Home](../README.md) · [Agent setup](AGENT-SETUP.md) · [Host installation](HOST-INSTALL.md)

This is a **self-hosted preview**, not a shared hosted service. The portability
release adds separate agent/caller and Linux host installation paths. Login,
network access and model entitlement remain the owner's responsibility.

## Portability scope

| Component | Scope |
| --- | --- |
| Caller skill / HTTP client | Python 3.10+ on macOS/Linux; native Windows uses a token environment variable |
| MCP service | Configurable private Tailscale origin, project, environment and model |
| Worker installer | Linux x86_64, systemd, rootful Docker Engine, Tailscale; plan before explicit apply |
| Worker image | Public pinned base/source inputs, configurable guest identities, browser/desktop/evidence/SOUL |
| Other host platforms | A suitable Linux x86_64 VM is required; native macOS/Windows/ARM hosts are not supplied |
| Initial provider | Dedicated Grok CLI OIDC login and configured Grok model; arbitrary provider API keys are not accepted by this installer |
| Multi-model pstack | Optional separate profile; not provisioned by the initial host installer |
| Desktop viewer | macOS/Linux or WSL helper, with separate SSH/bridge permissions and local Crabbox CLI |

## What the checks establish

Caller tests cover selected-server configuration, idempotent installation,
preservation of edited skills, read-only connection checks and error redaction.
Host tests cover configuration, dynamic identities, manifest resolution and API
submission without running a provider. MCP checks cover protocol/authentication
and ownership boundaries. Historical optional-source/receipt tests explicitly
skip when their required inputs are not supplied.

A disposable image built successfully from the pinned public sources. An offline,
read-only-root smoke check at UID/GID 200000 loaded Hermes, found the worker skills,
copied SOUL into private state, and captured a real Chromium screenshot. It made
no provider calls and did not modify the existing deployment.

A full fresh-machine systemd installation, login and delegated task has **not**
been qualified end to end by these tests. Do not infer that proof from an image
build, unit-test result, service heartbeat or successful HTTP connection.
Cross-platform caller checks are defined in
[the CI workflow](../.github/workflows/portability.yml); use its result for the
specific commit, rather than assuming every platform ran locally.

## Reference deployment history

The earlier reference deployment completed a coding task, desktop viewing,
browser screenshot/video capture, result retrieval and cleanup. Native worker
SOUL loading was checked after prompt rebuild. These recorded results concern
that deployment, not every new computer or rebuilt image.

Complete optional multi-model execution, real-PR media attachment publication,
and eight simultaneous tasks remain unproven by those reference checks. Private
rollout receipts are not distributed, and archived migration scripts are not
supported install or upgrade commands.

## Resources

The host installer defaults to **two concurrent tasks** and accepts a ceiling of
up to eight, subject to memory/disk admission. Default per-task limits are
**4 GiB RAM**, **1 CPU**, **512 PIDs**, and a **20 GiB workspace/state volume**.
Browser and desktop share those resources. Check the generated host configuration
for execution limits; operator overrides and older installations may differ.

These are limits, not measured idle consumption or a billing cap. Leave capacity
for the OS, services, source/build caches and exported artifacts. Increasing the
configured concurrency does not prove that the machine can run that many jobs.

## Model access

Task environments run on the owner's hardware; model inference still uses the
configured external provider. Subscription access, quotas and charges apply.
A working API connection or login does not prove model entitlement. The optional
role policy does not silently substitute providers when a model is unavailable.
A fully offline local-model installation is not supplied here.
