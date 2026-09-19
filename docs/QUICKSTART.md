# Connect your agent

[Home](../README.md) · [Documentation](README.md) · [Access and security](../SECURITY.md)

This guide connects a caller to the **existing Omarchy deployment**. For a new
execution host, see [BUILDING.md](../BUILDING.md); fresh-host setup remains manual.

## Before you start

1. Join the calling machine to the approved Tailscale network. The process running
   the tools must have network access; a remote SaaS agent does not inherit your
   laptop's VPN connection.
2. Ask the operator for a dedicated service credential with the required scopes.
   [Operator provisioning](../integrations/omarchy-mcp/README.md#operator-provisioning)
   prints a protected credential-file path, not a token to paste into a chat.
3. Supply that credential through your agent host's secret manager. The examples
   use the environment variable `OMARCHY_CLOUD_TOKEN`. Make it available to the
   actual agent process; a shell export does not necessarily reach a GUI app.

| Setting | Existing deployment |
| --- | --- |
| MCP transport | Streamable HTTP |
| MCP URL | `https://omarchy.tail0d5eb6.ts.net/mcp` |
| HTTP origin | `https://omarchy.tail0d5eb6.ts.net` |
| Authentication | Service-issued Bearer credential |
| Caller scopes | `submit`, `observe`, `retrieve`, `cancel` |
| Project | `hermes-tasks` |

Do not run an OAuth login for this server: it does not implement browser OAuth.
GitHub repository access, network connectivity, and task credentials are separate.

## Cursor

Use **Add to Cursor** on the [repository front page](../README.md#connect-your-agent).
It opens Cursor's installation confirmation with the URL and an environment
variable reference. It does not contain or create a credential.

Alternatively, merge [examples/cursor.mcp.json](../examples/cursor.mcp.json) into
`~/.cursor/mcp.json`, keeping any existing servers. The Authorization value is
`Bearer ${env:OMARCHY_CLOUD_TOKEN}`. Restart/reconnect the client after making the
credential available to its process, then load `get_delegation_guide`.

The link/config follow Cursor's official [install-link format](https://cursor.com/docs/mcp/install-links),
[HTTPS deep-link format](https://cursor.com/docs/reference/deeplinks), and
[environment interpolation](https://cursor.com/docs/mcp). The configuration was
checked against that format; an interactive Cursor login/install is not part of
this release's verification.

## Codex

With `OMARCHY_CLOUD_TOKEN` available to Codex:

```sh
codex mcp add hermes-crabbox \
  --url https://omarchy.tail0d5eb6.ts.net/mcp \
  --bearer-token-env-var OMARCHY_CLOUD_TOKEN
```

Start a fresh session or reconnect MCP, then ask it to read `get_delegation_guide`.
The command registers the server; it neither provisions access nor starts a task.
The flags were checked against the installed Codex CLI's `mcp add --help`.

## Hermes and portable skills

From a clone of this repo:

```sh
python3 scripts/install-delegation-skill.py --agent hermes
```

This installs `omarchy-cloud-delegate` under `~/.hermes/skills/`. Use `--agent codex`
for `~/.agents/skills/`. For a named Hermes profile or another agent, specify its
actual skills directory with `--skills-dir /path/to/skills`. The destination is
always a child folder named `omarchy-cloud-delegate`.

The installer is offline, uses Python's standard library, and refuses to replace
a modified installation. Repeating it for identical content is a no-op. It
installs no provider login and changes no MCP settings. A new agent session may
be needed for discovery.

Without cloning, download the [preview skill bundle](https://github.com/thomasbek3/hermes-crabbox/releases/tag/v0.1.0-preview.1),
verify it against the release's `SHA256SUMS`, and extract the contained
`omarchy-cloud-delegate/` directory into your agent's skills directory. Keep its
MIT license with it. Downloading a bundle does not itself install or enable it.

## Other MCP clients

Configure the endpoint above with an Authorization header supplied from that
client's secret facility. The client must support Streamable HTTP and custom
headers. [The operator guide](../integrations/omarchy-mcp/README.md) includes a
conceptual configuration and all ten tool names.

Call `get_delegation_guide` first. For a read-only connection check, call
`list_tasks`; a new caller may correctly see an empty list. Each credential sees
its own jobs, not another caller's history. Grokbot, Claude, or another harness
can use this interface when its actual tool host supports these requirements.

## Use HTTP instead

Python 3.10+ on macOS/Linux, with a credential from the secret manager or an
owner-only `~/.config/omarchy-cloud/token` file:

```sh
python3 integrations/omarchy-mcp/skill/scripts/omarchy_cloud.py list
python3 integrations/omarchy-mcp/skill/scripts/omarchy_cloud.py submit \
  --github OWNER/REPO --ref COMMIT_SHA \
  --goal-file examples/task.md --idempotency-key my-task-001
```

Edit the assignment before submitting. GitHub source snapshots use the parent's
existing `gh` authorization; its GitHub token is not sent to the worker.
The first command is read-only. The second starts work and may use model quota.
Use a new idempotency key for a genuinely new assignment.

## Follow a task

Save the returned session and attempt IDs. Ask for status/events, then retrieve
results when it finishes. A submission returning an ID means queued/accepted,
not completed. A task's result still needs the requested acceptance review.

Follow-ups use the existing session ID. Cancellation uses the exact active
attempt ID. Large source/media transfers use HTTP; avoid putting their bytes in
MCP messages. [The skill](../integrations/omarchy-mcp/skill/SKILL.md) explains the
complete sequence and PR-evidence handoff.

## If connection fails

| Symptom | Check |
| --- | --- |
| Host not found / connection timeout | Tailscale is running on the tool-execution host and can reach Omarchy |
| HTTP 401 | Credential is issued by this service, unexpired/unrevoked, and available to the client process |
| HTTP 403 or inaccessible task | Caller scope/project/ownership; another caller's task may be intentionally inaccessible |
| Cursor link does not open | Use the manual JSON configuration and a client version supporting MCP install links |
| GUI client cannot find token | Its environment differs from the terminal; supply the variable through its launch environment |
| No tasks returned | A new caller normally has no jobs; verify which identity is configured |
| Browser skill present but no service connection | Skill installation and API authentication are separate steps |

Do not paste tokens, auth files, raw headers, or provider responses into issues.
