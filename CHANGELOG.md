# Changelog

## 0.1.0-preview.2

Portable host and caller setup, with an agent-readable installation path.

- Separate Linux worker-host and macOS/Linux/Windows caller instructions.
- Host plan/apply installer, public pinned image build, dynamic identities, and private Tailscale configuration.
- Caller installers for Hermes, Codex, Claude Code and Cursor; custom skills directories remain supported.
- Saved host settings, generated Cursor MCP link, JSON receipts and read-only connection diagnostics.
- Removed maintainer-specific defaults and archived historical deployment scripts behind execution guards.
- Cross-platform caller CI and explicit skips for optional historical test inputs.

The public-source image and offline non-root Chromium smoke passed. A complete
fresh-machine systemd install and provider-backed task remain unqualified.

## 0.1.0-preview.1

Initial packaged preview of the existing Omarchy integration, plus repository
onboarding and distribution improvements.

- Private HTTP/MCP delegation to Hermes workers in Crabbox containers.
- Worker SOUL, browser evidence skills, desktop helper, and optional Jev/pstack routing.
- Downloadable MIT-licensed caller skill and non-destructive offline installer.
- Cursor MCP configuration link, Codex registration command, and connection guide.
- MIT license for original code, with separate preserved upstream licenses.
- Documentation index, architecture, status, contribution, and security guides.

This release does not redeploy the host or claim a complete fresh-host installer,
full multi-model qualification, public endpoint, or automatic caller provisioning.
