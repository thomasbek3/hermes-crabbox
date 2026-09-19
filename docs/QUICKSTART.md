# Connect your agent

[Home](../README.md) · [Agent setup](AGENT-SETUP.md) · [Install a worker host](HOST-INSTALL.md)

Run these steps on the machine that executes your agent's tools. Use an existing
worker host or install one first. The repository does not provide a shared hosted
service, a model subscription, or access to the maintainer's computers.

## What you need

- Python 3.10+ and Git on the caller. macOS/Linux support private credential files;
  native Windows callers use `OMARCHY_CLOUD_TOKEN` from their secret environment.
- Tailscale reachability to the chosen worker from this tool-execution machine.
- The host's HTTPS origin, project, and environment version from its setup receipt.
- A dedicated service-issued credential with the needed task scopes. A GitHub,
  Tailscale, or model-provider token does not authenticate this service.

Store credentials through the agent host's secret facility. The examples use
`OMARCHY_CLOUD_TOKEN`; it must be available to the actual agent process. A terminal
export does not necessarily reach a GUI app. Do not paste tokens into chat.

## Install the caller skill

Clone the public repository, then use your actual host origin in place of the
example. Do not include `/mcp` in `--server`.

```sh
git clone https://github.com/thomasbek3/hermes-crabbox.git
cd hermes-crabbox
python3 scripts/install-delegation-skill.py --agent hermes \
  --server https://worker.example.ts.net --json
```

| Agent | Installer destination |
| --- | --- |
| Hermes | `--agent hermes` → `~/.hermes/skills/omarchy-cloud-delegate` |
| Codex | `--agent codex` → `~/.agents/skills/omarchy-cloud-delegate` |
| Claude Code | `--agent claude` → `~/.claude/skills/omarchy-cloud-delegate` |
| Cursor | `--agent cursor` → `~/.cursor/skills/omarchy-cloud-delegate` |
| Another harness or a named profile | `--skills-dir /actual/supported/skills/directory` |

On Windows use `py -3` if `python3` is not on PATH. For PowerShell, the same
command on one line is:

```powershell
py -3 scripts/install-delegation-skill.py --agent codex --server https://worker.example.ts.net --json
```

Pick the actual profile's skill
directory instead of assuming that every harness uses the same location.

Add `--project YOUR_PROJECT --environment-version YOUR_ENVIRONMENT` when the
host uses non-default identifiers. The installer saves non-secret
`scripts/connection.json`, an agent-readable `CONNECTION.md`, and `cursor.mcp.json`.
The HTTP client and evidence publisher both use the saved host. Explicit
`OMARCHY_CLOUD_SERVER`, `OMARCHY_CLOUD_PROJECT`, and `OMARCHY_CLOUD_ENVIRONMENT`
variables override these defaults. No personal server is used automatically.

Installation is offline and uses Python's standard library. An identical repeat
is a no-op. A differing existing skill is preserved: back it up and choose a new
installation directory, then deliberately switch the agent to that directory.
The command never changes provider credentials, existing MCP settings, or jobs.
Reload the agent's skills or start a new session when its harness requires it.

## Check access without starting a task

Run from the installed skill directory printed in the receipt:

```sh
python3 scripts/check_connection.py
```

The JSON result distinguishes configuration, network, authorization, redirect,
and response-format failures. Exit 0 proves HTTP task-list access only. It does
not print existing tasks or test provider billing, worker execution, or VNC.
On macOS/Linux, an alternative to the token environment variable is an owner-only
`~/.config/omarchy-cloud/token` file (mode 600); use `OMARCHY_CLOUD_TOKEN_FILE`
for another path. On native Windows use the token environment variable.

## Add MCP, or keep using HTTP

The endpoint is your host origin plus `/mcp`, using Streamable HTTP with a
service-issued Bearer credential. Browser OAuth is not implemented.

**Codex:** with the credential available to the Codex process:

```sh
codex mcp add hermes-crabbox \
  --url https://worker.example.ts.net/mcp \
  --bearer-token-env-var OMARCHY_CLOUD_TOKEN
```

**Cursor:** open the **Add this host to Cursor** link in the installed
`CONNECTION.md`, or merge the installed `cursor.mcp.json` into the selected profile's MCP
configuration, preserving existing servers. Its Authorization value references
`${env:OMARCHY_CLOUD_TOKEN}`; it contains no token. The generic
[example](../examples/cursor.mcp.json) uses a placeholder host.

**Other MCP clients:** register the same endpoint and supply the Authorization
header using that client's secret facility. Check the client's support for
Streamable HTTP and custom headers. Do not assume universal configuration syntax.

Then reconnect MCP, call `get_delegation_guide`, and call `list_tasks`. An empty
list is normal for a new caller; credentials only see their own jobs. For a
scripted protocol check, install the pinned MCP requirements in a separate venv
and run `scripts/check-private-mcp.py --server YOUR_ORIGIN` from the repository.

**HTTP fallback:** the installed skill includes the complete standalone client:

```sh
python3 scripts/omarchy_cloud.py list
python3 scripts/omarchy_cloud.py submit --github OWNER/REPO --ref COMMIT_SHA \
  --goal-file task.md --idempotency-key first-task-001
```

The first command is read-only. The second starts work and may use model quota.
Write [a useful assignment](../examples/task.md) first. GitHub snapshots use the
caller's existing `gh` authorization; the GitHub token does not enter the worker.
Save returned session/attempt IDs and inspect results before claiming completion.
Use the same idempotency key after an uncertain retry, a new one for new work.

## Optional desktop viewing

[Desktop viewing](DESKTOP.md) needs separate SSH access and a local viewer. It is
not required for worker browser automation or screenshots. A working MCP
connection does not automatically grant SSH or keep task containers running.
