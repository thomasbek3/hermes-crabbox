# Omarchy Cloud: private MCP and portable delegation

MCP endpoint: **https://omarchy.tail0d5eb6.ts.net/mcp**
HTTP API origin: **https://omarchy.tail0d5eb6.ts.net**
Skill bundle: **https://omarchy.tail0d5eb6.ts.net/mcp/skill.zip**

All three require Tailscale connectivity. MCP and the bundle require a service-issued `Authorization: Bearer …` credential; no secrets belong in URLs. An MCP caller can also read the full skill immediately using `get_delegation_guide`. This returns instructions; an MCP server does not automatically install a persistent skill in the caller.

## Connect an agent

1. Ensure the machine/container executing the agent's tools can reach the Omarchy tailnet address. Adding an MCP URL does not create a network path for a third-party SaaS host.
2. Have the operator provision a dedicated caller credential using the script below. Store it in that agent host's secret manager. The task service owns these credentials; do not use a model-provider or Tailscale API key.
3. Add a remote **Streamable HTTP** MCP server at the endpoint above and configure its Authorization header through the client's secret facility. Field names and environment substitution vary by client; the following is a conceptual connection record, not a universal configuration format:

```json
{
  "name": "omarchy-cloud",
  "transport": "streamable-http",
  "url": "https://omarchy.tail0d5eb6.ts.net/mcp",
  "headers": {"Authorization": "Bearer <secret-manager supplied service credential>"}
}
```

4. Load `get_delegation_guide`, or download the authenticated skill ZIP and install its `omarchy-cloud-delegate` folder in the caller's supported skills directory. The ZIP includes the Python HTTP caller and parent-side PR-evidence publisher.
5. Submit an assignment. Save its IDs, check status later, retrieve artifacts, and publish only within the user's authorized PR scope.

For clients without arbitrary-header remote MCP support, use the included Python HTTP client. This release does not implement OAuth discovery/login. The API and MCP are first-party interfaces of the same task service and use the same issued credential, scopes, ownership and revocation rules. The adapter never holds a shared master credential or accepts a provider credential for downstream authentication.

## Tools

`get_delegation_guide`, `submit_task`, `get_task`, `list_tasks`, `get_events`, `get_results`, `read_artifact`, `follow_up`, `cancel_task`, `prepare_input`.

Submissions return immediately. The worker is independent of MCP connection lifetime. Events are finite batches with cursors. Large source uploads/media downloads use existing authenticated HTTP endpoints rather than base64 in model context. Every mutation requires a caller-supplied idempotency key. Queued and completed are task states, not acceptance-verification guarantees.

Public repository details can be part of the assignment; the worker has internet access. Private/local source is uploaded by the caller using its own GitHub/local access. MCP does not borrow caller filesystem paths or mount caller credentials. PR publication remains a parent-side operation; this service does not add push/merge/deploy tools. Remote VNC viewing still uses the separate SSH viewer helper.

## Operator provisioning

Run on Omarchy with operator privileges:

```sh
sudo /opt/cloud-workbench/.venv/bin/python /opt/omarchy-mcp/provision-delegation-client.py cloud-muse
```

Use a different name such as `cloud-grokbot` for another caller. The command prints only the client ID, scopes and protected credential-file path. Transfer that file to the intended host's secret store over an authenticated channel; never paste it in agent instructions. Repeating the command preserves an existing valid credential. Credentials are scoped to submit/observe/retrieve/cancel for project hermes-tasks and tasks are owner-isolated. Creating a new client does not give it access to a previous client's jobs.

No cloud Muse/Grokbot connection or credentials are created implicitly by deploying this endpoint. Their exact host/secret configuration still needs to be supplied when onboarding them.

## Deployment

Official MCP Python SDK1.30.0 (maintained v1 line), pinned dependencies in requirements.lock. Isolated runtime `/opt/omarchy-mcp/.venv`; existing API/worker Python environments are unchanged. `cloud-workbench-mcp.service` runs as a dynamic unprivileged user, binds only127.0.0.1:7781 and has loopback-only network access. Tailscale Serve forwards `/mcp` to it while `/` continues to the existing API on7780. No Funnel/public exposure.

The gateway validates each credential against the API, then each tool operation is independently checked by the API for its scope, ownership and project. Host/Origin checks, request/response bounds and no-redirect backend calls are enabled. No request bodies or credentials are logged. Skill package downloads also require authentication.

Targeted integration checks use the real API/store with no worker/provider execution. The live smoke script uses the official MCP client for discovery, skill retrieval, task listing and optional access to an existing task; it performs no mutations or model calls. Connection evidence is under `evidence/private-mcp/`.
