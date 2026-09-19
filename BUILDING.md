# Build inputs and installation

For a fresh worker host, follow [host installation](docs/HOST-INSTALL.md).
For the machine running your existing agent, follow [caller setup](docs/QUICKSTART.md).
The agent entry point is [AGENTS.md](AGENTS.md).

## Set up a worker computer

`scripts/install-host.py --plan` reports prerequisites without making changes.
The explicit `--apply` path builds a new Linux host installation, with dedicated
service accounts, private Tailscale routing, caller authentication and an immutable
environment manifest. It refuses existing unmanaged deployments. See the host
guide for required arguments, provider login and current verification limits.

`deploy/Dockerfile.portable` starts from a public pinned base and builds the
Hermes/browser/Crabbox/desktop/evidence/SOUL image without a pre-existing local
image or private sibling checkout. Operator credentials are not build inputs.
The install source manifest uses tracked allowlisted files, not the whole home
or an arbitrary Docker context.

## Pinned upstream source

| Component | Repository | Revision |
| --- | --- | --- |
| Crabbox CLI | `openclaw/crabbox` | `v0.61.0` / `e4d7cc6361f827f3802cf05bbc6ad5fcdac2cdc5` |
| Hermes | `NousResearch/hermes-agent` | `3b0e392e5a6922034feccac5771041ac78467757` |
| pstack Hermes port | `jmporchet/pstack-hermes` | `204e77a7a011c4613dc9c4913a481d77cc0ebe54` |
| agent-browser | `vercel-labs/agent-browser` | `v0.38.1` / `aff6125c023b810ea3f2e5deec5379e9a4270bdc` |

Binary release hashes and public base pins live in the installer and portable
Dockerfile. Source revisions and licenses are recorded in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Keep these licenses in derived
images and retain distribution notices for their other dependencies.

`scripts/prepare-hermes-image-context.py` remains a developer utility for making
a pinned source context from explicit `--hermes`, `--pstack`, and `--destination`
paths. The older layered Dockerfiles and archived migrations document the initial
implementation; the portable installer does not replay those migrations.

## Caller package

```sh
python3 integrations/omarchy-mcp/build_package.py
```

The portable skill ZIP contains instructions and Python clients, not an endpoint
credential. Prefer `scripts/install-delegation-skill.py --server YOUR_ORIGIN` to
install the skill with the correct host settings. A generic downloaded bundle
requires explicit `OMARCHY_CLOUD_SERVER` configuration before use.

## Development checks

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

MCP checks additionally need `integrations/omarchy-mcp/requirements.lock` in the
test environment. Optional historical/provider tests require explicitly supplied
external fixtures; missing private rollout files are skips, not installation
failures or evidence that those integrations passed.

Never commit auth files, OAuth responses, secret-manager exports, client tokens,
live databases or private task output. Source publication does not deploy the
runtime or grant anyone access to a running host.
