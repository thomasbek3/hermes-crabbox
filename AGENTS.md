# Hermes Crabbox: start here

This repository provides Hermes workers in Crabbox containers, on hardware chosen
by the owner. Do not assume the repository maintainer's network or accounts are
available. Read [the agent setup guide](docs/AGENT-SETUP.md) before installation.

## Choose the right machine

- **Caller**: the machine executing the user's agent tools. Install the delegation
  skill there and configure the owner's worker endpoint. A laptop's Tailscale
  connection does not connect a separate SaaS tool host.
- **Worker host**: the Linux machine running Docker, the task service, and workers.
  Follow [host installation](docs/HOST-INSTALL.md). A caller installation does not
  install a host. Both roles may share a machine when its platform supports it.
- Only ask for information that cannot be discovered safely: the intended host,
  the owner-approved endpoint/access path, and provider login where required.
  Never ask the user to paste secret values into chat.

## Supported setup entry points

- `scripts/install-host.py`: host plan/preflight and explicit installation.
- `scripts/install-delegation-skill.py`: idempotent caller skill installation.
- Installed `scripts/check_connection.py`: read-only JSON connection diagnosis.
- `scripts/provision-delegation-client.py`: operator-side caller provisioning.
- `scripts/check-private-mcp.py`: optional real MCP protocol check.

Do not run historical migration/probe scripts as installation steps. They rely on
old deployment state; archived scripts are not a second installer. Do not relax
protection checks to make an installation appear successful.

## Completion means evidence

A copied skill proves installation only. An HTTP listing proves read access only.
For MCP, load `get_delegation_guide` and list tasks. To establish worker operation,
run one owner-authorized small task, retrieve its result, and confirm cleanup.
State which layers passed and which remain unverified. Do not start model calls
solely as an installation side effect. Leave existing jobs and services intact.

Use `CONNECTION.md` from an installed skill and the host installation receipt as
configuration sources. Explicit environment settings override installed caller
settings. Preserve existing edited profiles/configuration; do not overwrite them
or silently substitute a different model/provider. Keep credentials in the
approved secret store or owner-only files, outside prompts, logs and Git.

## Development

Install `.[test]` in a project virtual environment. Run relevant pytest suites,
inspect the final diff, and update user-facing guides when setup behavior changes.
Tests requiring private historical receipts or optional provider source checkouts
must report explicit skips; skipped checks are not portability proof.

Retain MIT and third-party license notices. Do not modify copied upstream browser
skill files without updating the attribution/source inventory.
