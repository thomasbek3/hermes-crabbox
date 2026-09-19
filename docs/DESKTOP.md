# Watch a worker's desktop

[Home](../README.md) · [Caller setup](QUICKSTART.md) · [Evidence](PR-EVIDENCE.md)

The desktop profile includes Chromium, XFCE, TigerVNC/noVNC and browser evidence
tools. Ask the worker to use visible Chromium if you want to watch interaction.
These processes share the task container's resource limits.

## Prepare the viewer computer

The optional viewer helper supports macOS/Linux; use WSL for Windows. Install the
[official Crabbox CLI](https://github.com/openclaw/crabbox/releases/tag/v0.61.0),
OpenSSH, and `lsof`. Authenticate normal SSH access to the chosen worker host and
verify its host key. MCP access alone does not grant SSH permissions.

Set `HERMES_CRABBOX_SSH_HOST` to the authorized `user@host` or SSH config alias.
If Crabbox is not on PATH, set `HERMES_CRABBOX_VIEWER` to its actual executable.
No SSH target is selected automatically.

The host must provide the desktop bridge and a narrow, operator-approved route
to execute it as `cloud-worker`. Caller provisioning does not grant this sudo
permission, and the fresh-host installer does not invent an SSH account. Have
the operator configure it for the intended viewer user.

## Open the viewer

From the installed caller skill directory, using an active desktop task ID:

```sh
python3 scripts/omarchy_cloud.py desktop SESSION_ID
```

The client checks task access through the API, obtains a private VNC handoff
over SSH and opens a loopback viewer. Credentials pass through pipes, not command
arguments. Keep the helper running while viewing. Ctrl+C closes viewing without
cancelling the worker; closing the browser tab alone may leave the tunnel open.
MCP does not currently provide a portable one-click desktop URL.

## When does it disappear?

The desktop lives inside the task container. It ends on completion, failure,
cancellation or an execution limit. Viewing does not extend that lifetime.
Save files and evidence under `/workspace`; the parent retrieves exported results
afterward. A follow-up can reuse retained workspace state in a new attempt, but
it is not an always-on desktop VM.
