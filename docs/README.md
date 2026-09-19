# Documentation

[Repository home](../README.md) · [Downloads](https://github.com/thomasbek3/hermes-crabbox/releases/tag/v0.1.0-preview.2)

## Choose your path

| I want to… | Start here |
| --- | --- |
| Have an agent set up host and caller | [Agent setup](AGENT-SETUP.md) |
| Install a new worker computer | [Host installation](HOST-INSTALL.md) |
| Connect Grokbot, Hermes, Codex, Claude, or another caller | [Quickstart](QUICKSTART.md) |
| Give a worker a useful coding task | [Assignment template](../examples/task.md) |
| Understand containers, workers, and routing | [Architecture](ARCHITECTURE.md) |
| Watch a worker's desktop | [Desktop access](DESKTOP.md) |
| Collect screenshots/video for a PR | [PR evidence](PR-EVIDENCE.md) |
| Understand what is proven and what is pending | [Status and limits](STATUS.md) |
| Provision a caller or operate MCP | [Operator guide](../integrations/omarchy-mcp/README.md) |
| Build or adapt the host deployment | [Build inputs](../BUILDING.md) |
| Contribute a change | [Contributing](../CONTRIBUTING.md) |
| Check reuse, copyright, or license terms | [Licensing](../LICENSING.md) · [Upstream notices](../THIRD_PARTY_NOTICES.md) |

## Worker behavior

- [Worker SOUL](../integrations/hermes-pr-evidence/SOUL.md): identity, repo rules,
  task boundaries, evidence, and handoff obligations.
- [SOUL loading](WORKER-SOUL.md): how the native Hermes prompt receives it.
- [Delegation skill](../integrations/omarchy-mcp/skill/SKILL.md): instructions for
  the calling agent, including input transfer and result collection.
- [Jev/pstack](CRABBOX-PSTACK.md): optional routing inside an already-running worker.

## Historical engineering records

Other files in this directory retain earlier designs, implementation contracts,
qualification notes, and deployment checkpoints. They are useful for maintenance
but can describe superseded profiles or unshipped paths. Use the guides above
for the current entry points. References to local `evidence/` and `reviews/`
artifacts do not imply those private artifacts are included in GitHub.
