# Status and known limits

[Home](../README.md) · [Architecture](ARCHITECTURE.md) · [Build inputs](../BUILDING.md)

This is a **preview of the configured Omarchy deployment**, not a general hosted
service or a fully automated fresh-host installer. The records below summarize
existing evidence; the documentation release did not rerun live model tasks.

| Capability | Recorded status |
| --- | --- |
| Basic Hermes coding task | A task completed, exported results, and cleaned up its container |
| Desktop viewing | Desktop shown through the private viewer and confirmed by the operator |
| Browser evidence | A task produced before/after screenshots and a recording; parent retrieval/hash checks passed |
| Private MCP | Protocol integration tests and read-only live discovery/result/skill checks passed |
| Native worker SOUL | Main and role profiles loaded it, including after prompt rebuild |
| Full optional Jev/pstack workflow | Routing was observed; complete provider/stage execution remains partially qualified |
| PR evidence upload to a real PR | Not established by the initial evidence task |
| Eight simultaneous tasks | Configured ceiling; an eight-task load test is not claimed |
| Cloud Muse/Grokbot onboarding | Requires individual caller/network/credential setup |
| Fresh machine installation | Operator work; recipes still depend on prepared images and host configuration |

Detailed records: [MCP](../integrations/omarchy-mcp/README.md),
[PR evidence](PR-EVIDENCE.md), [worker SOUL](WORKER-SOUL.md),
[routing](CRABBOX-PSTACK.md). Some historical evidence files stay on the operator's
machine and are deliberately not included in this repository.

## Resources

The configured deployment permits up to **eight concurrent tasks**, subject to
memory/disk admission. Each task container has a **4 GiB memory ceiling**,
**1 CPU**, and **512 PIDs**; browser/desktop processes share those limits.
Workspace/native state has an **8 GiB bound**.

A run currently permits **two hours and 1,000 nominal model steps**, whichever
limit is reached first, with separate capture/supervisor grace periods. The
routed profile divides the step budget between primary and role sessions.
These are configured bounds, not measured typical RAM consumption, a billing
cap, or an assurance that eight jobs fit on any computer. Leave capacity for
the OS, services, and other applications; admission can queue extra work.

## Model availability

The initial routed task encountered a Fable quota limit. Its expected reset date
was an account-specific historical observation, not a current availability check.
Do not infer that every provider works from a successful Grok task or a working
MCP endpoint. The policy does not silently substitute a different model.

## Local compute versus model inference

The task containers run on your computer. The configured models use external
provider services; availability, subscriptions, usage charges, and credentials
still apply. A fully offline local-model configuration is not supplied here.
