# Private MCP service and caller access

[Agent setup](../../docs/AGENT-SETUP.md) · [Host installation](../../docs/HOST-INSTALL.md) · [Caller setup](../../docs/QUICKSTART.md)

Each installation has its own private Tailscale origin. There is no shared
maintainer endpoint. The MCP URL is `<ORIGIN>/mcp`; HTTP uses `<ORIGIN>`;
the authenticated generic skill bundle is `<ORIGIN>/mcp/skill.zip`.

## Connect an agent

The tool-execution host needs Tailscale reachability and a dedicated service
credential. Configure Streamable HTTP with an Authorization header from the
client's secret facility. MCP does not create network access or automatically
install a persistent skill. The host does not implement browser OAuth login.

Use the [caller installer](../../docs/QUICKSTART.md) to save the chosen origin,
project and environment and generate Cursor configuration. Other clients may
use the standalone HTTP fallback. After MCP connection, call
`get_delegation_guide`: it returns the deployment's connection settings as well
as the complete delegation instructions. A generic ZIP download requires those
settings or explicit `OMARCHY_CLOUD_*` variables before its clients can run.

## Tools

`get_delegation_guide`, `submit_task`, `get_task`, `list_tasks`, `get_events`,
`get_results`, `read_artifact`, `follow_up`, `cancel_task`, `prepare_input`.

Tasks run independently of the MCP connection. Save session and attempt IDs.
Mutations need caller-supplied idempotency keys; reuse the original key after an
uncertain retry. Events are finite batches with cursors. Transfer large files
through HTTP, not base64 in model context. A completed process is not proof
that the task's acceptance criteria passed.

Private source is uploaded using the parent's existing local/GitHub access.
The service does not borrow caller filesystem paths or GitHub credentials.
PR publishing remains parent-side; these tools do not push, merge or deploy.
Optional VNC requires its separate SSH viewer setup.

## Operator provisioning

On the worker host:

```sh
sudo /opt/cloud-workbench/.venv/bin/python \
  /opt/cloud-workbench/scripts/provision-delegation-client.py laptop-agent
```

Use a different caller name for each agent. Optional `--config`,
`--credential-dir`, and `--project` select non-default installations/projects.
The result prints the client ID, scopes and protected credential-file path;
never its contents. Transfer the file through the owner's approved secret
mechanism. Repeating the command preserves a matching valid credential.

Credentials cover submit/observe/retrieve/cancel for the selected project.
Tasks and inputs remain owner-isolated. A new credential does not automatically
inherit another caller's existing tasks. Provider/Tailscale tokens do not work
as task API credentials.

## Service configuration

The host installer writes `/etc/cloud-workbench/mcp.env`:

| Variable | Meaning |
| --- | --- |
| `HERMES_CRABBOX_ORIGIN` | Required private `https://HOST.TAILNET.ts.net` origin |
| `HERMES_CRABBOX_UPSTREAM` | Loopback HTTP API origin, default `http://127.0.0.1:7780` |
| `HERMES_CRABBOX_MCP_PORT` | Loopback MCP port, default `7781` |
| `HERMES_CRABBOX_PROJECT` | Project identifier, default `hermes-tasks` |
| `HERMES_CRABBOX_ENVIRONMENT` | Default worker environment version |
| `HERMES_CRABBOX_MODEL` | Model identifier accepted by the configured worker project |
| `HERMES_CRABBOX_ROUTED_ENVIRONMENT` | Optional pstack environment; unset disables routed submissions |

The MCP service uses its own pinned Python dependencies from `requirements.lock`
and an unprivileged dynamic systemd user. It has no database, Docker socket,
provider login or shared master credential. It forwards each caller's service
credential to the API, where scope, project and ownership are independently
checked. Host/Origin validation, request bounds, no-redirect backend requests,
and loopback-only backend configuration remain enabled. Tailscale Serve exposes
private routes; the installer does not enable Funnel.

## Verification

Protocol tests use the real API/store with no worker/provider execution. For an
installed server, `scripts/check-private-mcp.py --server YOUR_ORIGIN` checks MCP
discovery, guide retrieval, task listing and the generic skill download. It reads
an existing credential privately and performs no task mutations or model calls.
Use a separate small delegated task to prove actual worker operation.
