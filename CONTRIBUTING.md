# Contributing

Thanks for helping improve Hermes Crabbox. Start with the [architecture](docs/ARCHITECTURE.md)
and [current limits](docs/STATUS.md), then choose a focused change.

## Local development

Use Python 3.11+ and an isolated environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest tests/test_skill_installer.py -q
```

Choose checks relevant to your change. MCP protocol tests use their own environment:

```sh
python3 -m venv .venv-mcp
.venv-mcp/bin/pip install -r integrations/omarchy-mcp/requirements.lock
.venv-mcp/bin/pip install -e '.[test]' pytest-asyncio
.venv-mcp/bin/python -m pytest tests/mcp -q
```

Some integration/qualification scripts require Linux, Docker, private fixtures,
prepared images, or live provider authorization. Read their scope first. Do not
run deployment or provider qualification scripts merely to validate a doc edit.

## Before a pull request

- Describe the problem, resulting behavior, and relevant checks you actually ran.
- Keep credentials, live task data, and private recordings out of the diff.
- For visible UI changes, include sanitized screenshots or recordings and explain
  what they demonstrate. Report any unverified behavior plainly.
- Keep setup examples, source pins, and licensing notices consistent with changes.
- Preserve upstream notices. Mark changes to copied Apache-licensed files and
  record provenance for newly copied or adapted material.

Original contributions are provided under this repository's MIT license.
Third-party contributions must retain their applicable terms and attribution.
Do not describe research notes or a passing local check as a production guarantee.

Use the issue templates for bugs or proposals. Follow [SECURITY.md](SECURITY.md)
for security-sensitive findings; do not put secrets in an issue or PR.
