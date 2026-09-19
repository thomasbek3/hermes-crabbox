# Watch a worker's desktop

[Home](../README.md) · [Quickstart](QUICKSTART.md) · [Evidence](PR-EVIDENCE.md)

The desktop profile includes Chromium, XFCE, TigerVNC/noVNC, and the tools used
for browser evidence. The browser and desktop share the task's resource budget.
Ask the worker to use visible Chromium when you want to watch its interaction.

## Open the viewer

On a configured Mac caller with the full `omarchy-cloud` client and SSH viewer
helper installed:

```sh
~/.local/bin/omarchy-cloud desktop SESSION_ID
```

Use an active session ID from the task service. The viewer helper authorizes the
task through the API, retrieves a private VNC handoff over SSH, and opens a local
browser viewer through loopback tunnels. Keep that command running while viewing.
Ctrl+C ends viewing without cancelling the worker. Closing the browser alone does
not necessarily close the tunnel.

This requires the dedicated SSH/viewer setup in addition to ordinary MCP access.
The portable delegation skill does not install that host setup, and MCP does not
provide a one-click desktop URL. For the implementation and deployment details,
see [the full caller skill](../integrations/omarchy-cloud/SKILL.md).

## When does the desktop disappear?

The desktop exists inside the task container. It ends when that attempt finishes,
fails, is cancelled, or hits its configured limit. Opening the viewer does not
extend the task's lifetime. Save source and screenshots/recordings in the task
workspace before completion; the parent retrieves exported results afterward.

A retained workspace/session is different from a running desktop. A follow-up
can continue saved work in another attempt; it is not an always-on VM.
