# Set this up with your agent

[Home](../README.md) · [Host installation](HOST-INSTALL.md) · [Connect an agent](QUICKSTART.md)

Give your agent this instruction:

> Read https://github.com/thomasbek3/hermes-crabbox/blob/main/AGENTS.md and follow
> its setup guide. Help me install Hermes Crabbox on my chosen worker computer,
> then connect the agent I'm using now. Inspect both machines first, preserve
> existing installations, and ask only for missing host/access choices or login
> actions. Keep secrets out of chat. Report what actually works and what remains.

## Two roles, two setup paths

| Role | Where it runs | What gets installed |
| --- | --- | --- |
| Worker host | A supported Linux machine or Linux VM | Docker/Crabbox task runtime, Hermes image, task API, MCP service, dedicated model access |
| Calling agent | The machine that executes the agent's tools | A small Python skill/HTTP client and, when supported, an MCP connection |

The machine running the chat UI may not be the tool-execution machine. Establish
that distinction first. An agent running remotely needs its own Tailscale access.

## Agent workflow

1. **Inspect before changing.** Identify OS/architecture, available Python, tools,
   existing install paths, and whether an existing worker host should be reused.
2. **Set up or reuse the host.** Follow [HOST-INSTALL.md](HOST-INSTALL.md). Record
   the resulting HTTPS origin, project and environment version. They belong to
   the owner's deployment; this repository supplies no shared public worker.
3. **Provision this caller.** The host operator issues a scoped service credential.
   Transfer it through the owner's approved secret mechanism, never through chat.
4. **Install on the caller.** Follow [QUICKSTART.md](QUICKSTART.md). Pass `--server`
   to the skill installer; include the host's project/environment if customized.
   Use `--json` for an installation receipt and preserve edited existing skills.
5. **Check the connection.** Run the installed `scripts/check_connection.py`.
   It reports JSON with `status`, `code`, and an actionable `next` step. It does
   not submit work, print task content, or call a model. A failing check exits 1.
6. **Connect MCP if supported.** Register the same origin plus `/mcp`, supply the
   service credential through the client's secret facility, and load
   `get_delegation_guide`. HTTP fallback works without an MCP-capable client.
7. **Prove the requested outcome.** When authorized to use model quota, delegate
   a small task and retrieve the result. Record its IDs and the observed outcome.
   Desktop viewing and optional multi-model routing are additional capabilities,
   not implied by a successful connection check.

## What still needs the owner

An agent can inspect, install files, generate configuration and diagnose failures.
The owner still chooses the execution computer, grants network/caller access, and
completes required provider/Tailscale login. No installer can manufacture those
permissions. Reuse authorization already given; do not ask again for routine
steps within the assignment.

## Finish with a useful handoff

Report the host and caller platform, endpoint, installed skill path, enabled
capabilities, checks performed, and exact remaining blockers. Name private token
**paths** only when useful; never print their contents. Keep 'installed',
'connected', and 'a real task completed' as separate claims.
