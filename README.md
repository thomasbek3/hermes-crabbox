# Hermes Crabbox

Self-hosted Hermes coding workers in Crabbox containers, with a private HTTP API,
MCP server, desktop viewer, and skills for delegating agents.

**Independent integration maintained for Thomas Bekkers.** This is not an
official Crabbox, OpenClaw, Nous Research, Cursor, or Vercel release. The name
describes the two upstream components it integrates; it does not claim ownership
of them or endorsement by their maintainers.

## What it does

A parent agent such as Muse, Grokbot, or another Hermes submits an assignment
over the private API or MCP. The Omarchy host queues it, creates a Crabbox
container, and starts Hermes inside that container. Hermes has a workspace,
Chromium, and an XFCE desktop. The parent retrieves status, results, and evidence.
It can send a follow-up or cancel the task. Containers are released after the
attempt; saved workspace/session state supports later follow-ups.

The optional pstack profile starts Hermes first. That running agent can ask Jev
to select an allowed workflow and invoke roles with the predefined model policy.
Jev does not choose arbitrary models or run before the worker exists.

## Start here

- **Connect another agent:** [MCP/API onboarding](integrations/omarchy-mcp/README.md).
- **Portable caller skill:** [omarchy-cloud-delegate](integrations/omarchy-mcp/skill/SKILL.md).
- **Worker identity and PR rules:** [SOUL.md](integrations/hermes-pr-evidence/SOUL.md).
- **Screenshots, recordings, and PR handoff:** [PR evidence](docs/PR-EVIDENCE.md).
- **Optional in-agent routing:** [Jev and pstack](docs/CRABBOX-PSTACK.md).
- **Upstream ownership and licenses:** [third-party notices](THIRD_PARTY_NOTICES.md).

The configured deployment is the separate Omarchy Intel MacBook Pro. Caller
hosts need Tailscale connectivity and their own service-issued credential.
Cloning this private repository does not grant access to the running service,
provider accounts, or another caller's tasks. Muse/Grokbot cloud hosts still
need individual onboarding. GitHub authorization is separate from Tailscale.

## Repository contents

| Path | Contents |
| --- | --- |
| `src/cloudworkbench/` | Task API, scheduler, persistence, runtime adapters, result handling, routing |
| `integrations/omarchy-mcp/` | Authenticated MCP server, dependency lock, skill packager, portable caller |
| `integrations/omarchy-cloud/` | HTTP client, desktop viewer helper, credential refresh integration |
| `integrations/hermes-cloud-pstack/` | Hermes tools for agent-invoked workflow routing and roles |
| `integrations/hermes-pr-evidence/` | Worker SOUL, evidence skills, attributed upstream browser skill |
| `deploy/` | Container recipes, service units, browser launch wrapper |
| `patches/`, `plugins/` | Hermes compatibility patch and role plugin |
| `scripts/`, `tests/` | Operator tooling, qualification helpers, automated tests |
| `docs/` | Current guides plus historical architecture and deployment records |
| `LICENSES/` | Unmodified upstream notices and provenance manifests |

Internal package, CLI, and service names still use `cloudworkbench` /
`cloud-workbench`; the GitHub name does not change deployed paths or services.
Earlier runtime/controller implementations remain in source. Historical specs
and checkpoint documents are not promises that every described path is enabled.
Start with the linked current guides above.

## Development and packaging

Python 3.11+ is required for the control service. Use separate environments for
the API and the MCP server, matching the deployment guides:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'

python3 -m venv .venv-mcp
.venv-mcp/bin/pip install -r integrations/omarchy-mcp/requirements.lock
python3 integrations/omarchy-mcp/build_package.py
```

The last command generates the distributable caller skill ZIP. It contains
instructions and client code, not credentials.

This is a source repository for the existing deployment, not a turnkey fresh-host
installer. Docker recipes depend on prepared base images and pinned upstream
source contexts. Several operator scripts intentionally contain Omarchy-specific
paths, checks, or image IDs. Read them before running; do not run all scripts as
a setup sequence. [BUILDING.md](BUILDING.md) records the source pins and boundaries.

## Publication scope and status

The private repository includes the implementation, MCP, skills, tests, and
documentation. It excludes credentials, provider logins, databases, virtual
environments, generated images/archives, recordings, raw reviews, and task output.
Historical documentation refers to local `evidence/` and `reviews/` files that are
deliberately not published; those references are historical receipts, not bundled
verification artifacts.

Publishing this source does not redeploy the host or establish fresh live-provider
verification. The optional complete multi-model workflow has a recorded Fable
quota limitation; see its guide for the precise evidence and limits. GitHub
access for other agents requires credentials authorized to this private repo.

See [LICENSING.md](LICENSING.md) for the scope of upstream licenses. No public
open-source license has been selected for the original integration code.
