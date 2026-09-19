# Build inputs and deployment boundaries

The repository includes project source and recipes. It excludes machine state,
credentials, third-party source archives, and built images. Existing worker
images on Omarchy are deployment artifacts, not files stored in GitHub.

## Set up a worker computer

This is the operator path for hosting workers on your own machine. If a worker
host is already available, use [Connect an agent](docs/QUICKSTART.md) instead.

Fresh-host setup is currently manual. The deployment recipes target the
configured Linux/Omarchy host; they are not a cross-platform installer.

1. Check the [hardware and resource limits](docs/STATUS.md#resources).
2. Prepare the pinned sources and image layers described below.
3. Provision the task service, runtime configuration, and provider credentials
   using the [architecture guide](docs/ARCHITECTURE.md) and referenced deployment docs.
4. Configure private Tailscale access and the
   [MCP service and caller credentials](integrations/omarchy-mcp/README.md).
5. [Connect your calling agent](docs/QUICKSTART.md). Add
   [desktop viewing](docs/DESKTOP.md) if you need to watch tasks.

## Pinned upstream source

| Component | Repository | Revision |
| --- | --- | --- |
| Crabbox CLI | `openclaw/crabbox` | `v0.61.0` / `e4d7cc6361f827f3802cf05bbc6ad5fcdac2cdc5` |
| Hermes | `NousResearch/hermes-agent` | `3b0e392e5a6922034feccac5771041ac78467757` |
| pstack Hermes port | `jmporchet/pstack-hermes` | `204e77a7a011c4613dc9c4913a481d77cc0ebe54` |
| agent-browser | `vercel-labs/agent-browser` | `v0.38.1` / `aff6125c023b810ea3f2e5deec5379e9a4270bdc` |

Clone Hermes and the pstack port from those repositories, check out the exact
revisions, then use `scripts/prepare-hermes-image-context.py --hermes PATH
--pstack PATH --destination NEW_DIRECTORY`. The tool uses tracked archives,
refuses dirty/wrong revisions, preserves upstream license files, and creates a
source hash manifest. It must never archive a personal Hermes home or login.

Dockerfiles describe the successive layers: Hermes, browser, Crabbox SSH
integration, desktop, optional pstack routing, evidence tooling, and worker SOUL.
They expect named input files in the prepared build context. Some scripts use
the original local `../work/` context layout and existing base-image IDs; read
the script and deployment guide before choosing the appropriate build path.
Do not assume a fresh clone alone can recreate the full live machine.

The copied agent-browser core skill is already included with its Apache license.
Acquire the native binary separately from the matching upstream release when
building the evidence image; do not substitute an unreviewed latest version.
Keep upstream licenses in any resulting image containing upstream work.

## Private configuration

Provision service principals, provider access, Tailscale, environment registry,
and runtime images separately. Never commit auth files, OAuth responses,
1Password exports, client tokens, live databases, or task output. Host-specific
paths in operator scripts are deployment defaults, not embedded credentials.

The MCP environment uses its own pinned requirements. Generate its skill ZIP
with `python3 integrations/omarchy-mcp/build_package.py` before deployment.
See [the MCP guide](integrations/omarchy-mcp/README.md) for the current deployment
and caller provisioning procedure. Publication of the repo does not change
running services or onboard a new caller.
