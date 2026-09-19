# Third-party notices and provenance

Hermes Crabbox combines original integration code with upstream software. Names
and links identify their respective projects; no upstream endorsement is claimed.
Full license texts are retained verbatim in `LICENSES/`. Source URLs and SHA-256
digests of those texts are in [sources.json](LICENSES/sources.json).

## Crabbox

- Source: [openclaw/crabbox](https://github.com/openclaw/crabbox).
- Version: `v0.61.0`, commit `e4d7cc6361f827f3802cf05bbc6ad5fcdac2cdc5`.
- Copyright: **2026 openclaw**, as stated in the upstream license.
- License: [MIT](LICENSES/crabbox-MIT.txt).
- Use: external runtime/CLI for task container lifecycle and desktop transport.
  `src/cloudworkbench/crabbox_*.py` are this project's integration modules, not a
  renamed copy of the Crabbox repository. The desktop package selection in
  `deploy/Dockerfile.hermes-crabbox-desktop` follows Crabbox's default desktop
  environment and is adapted to the existing Hermes image, prepared Chromium,
  worker helpers, and container-start SSH key generation. This derived recipe is
  covered by the retained Crabbox notice; upstream's full source is not vendored.

## Hermes Agent

- Source: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
- Version: `0.21.3`, commit `3b0e392e5a6922034feccac5771041ac78467757`.
- Copyright: **2025 Nous Research**.
- License: [MIT](LICENSES/hermes-agent-MIT.txt).
- Use: agent harness, included from a pinned tracked-source archive when building
  the worker image. The full source is not vendored in this repository.
- Derived material here: `tests/fixtures/hermes/base-tool-schemas.json` extracts
  descriptions and schemas from Hermes tool source;
  `patches/hermes/native-role-call-identity.patch` contains upstream context and
  this project's call-identity changes; `scripts/patch-hermes-pstack.py` contains
  upstream matching text and adds iteration accounting to JSONL completion events.
  The patch manifest and script preserve source hashes. These are local changes,
  not claimed upstream behavior. Retain the Hermes license with this material and
  the complete source inside images.

## pstack and its Hermes port

- Original author: **Lauren Tan (`@poteto`)**.
- Original source: [cursor/plugins, pstack subtree](https://github.com/cursor/plugins/tree/6fecddba65801f9b9c08b8b328d998ee5b09d290/pstack),
  version `0.14.5`, commit `6fecddba65801f9b9c08b8b328d998ee5b09d290`.
- Hermes adaptation: [jmporchet/pstack-hermes](https://github.com/jmporchet/pstack-hermes),
  commit `204e77a7a011c4613dc9c4913a481d77cc0ebe54`.
- Copyright in the port's retained license: **2026 Lauren Tan**.
- Licenses: [original MIT notice](LICENSES/pstack-original-MIT.txt) and
  [Hermes port MIT notice](LICENSES/pstack-hermes-MIT.txt).
- Use: externally sourced, pinned workflows and principles copied into the worker
  build context by `scripts/prepare-hermes-image-context.py`. Full upstream skills
  are not vendored here. Keep the port's `LICENSE`, `UPSTREAM.lock.json`, and
  `PORT-MAP.json` with that source. This project's `hermes-cloud-pstack` plugin and
  role policy connect those workflows to cloud workers; they do not make us the
  author of pstack. The port itself is an independent community adaptation.

## agent-browser

- Source: [vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser).
- Version: `v0.38.1`, commit `aff6125c023b810ea3f2e5deec5379e9a4270bdc`.
- Copyright: **2025 Vercel Inc.**
- License: [Apache License 2.0](LICENSES/agent-browser-Apache-2.0.txt), also retained
  alongside the copied skill at `integrations/hermes-pr-evidence/skills/agent-browser/LICENSE`.
- Vendored material: upstream `skill-data/core/` is copied to
  `integrations/hermes-pr-evidence/skills/agent-browser/`. The content is unmodified;
  only its containing directory changes. Every copied file, original path, and
  SHA-256 is recorded in [agent-browser-files.json](LICENSES/agent-browser-files.json).
  There is no separate upstream NOTICE file at this pin.
- The native executable is installed separately into worker images and is not
  committed here. The wrapper and PR-evidence skill are project integration code.
  Future modifications to copied files must be prominently marked and retain
  upstream notices. Binary dependencies have their own distribution requirements.

## Jev router reference and TypeSafe

- Design reference: [ussyverse/hermes-jev-router](https://github.com/ussyverse/hermes-jev-router),
  commit `8d8ded23927e2eadc534591df6ba77357ec385a3`.
- Copyright: **2026 Hermes Jev Router contributors**.
- License: [MIT](LICENSES/hermes-jev-router-MIT.txt), retained for the referenced
  implementation and any adapted design material.
- The reference informed the separation between Jev decisions and deterministic
  policy. Its plugin is not installed or vendored by this repository. Our
  `jev_client.py`, `workflow_routing.py`, and `agent_pstack.py` implement the
  project-specific transport and workflow policy. Credit for this reference is
  distinct from a claim that its model-selection algorithm is being used.
- TypeSafe/Jev is an external API. Its service access and provider terms are
  separate from the open-source router's license. No TypeSafe API key, model
  weights, or provider account is included. The TypeSafe skill used during
  development is not distributed in this repository.

## Other runtime dependencies and researched projects

Python dependencies are declared in `pyproject.toml`, `uv.lock`, and the MCP
requirements files; they are installed as packages rather than copied source.
The MCP gateway uses the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
Container recipes install system packages including Chromium, Playwright,
XFCE, TigerVNC, noVNC, and FFmpeg. Their individual licenses remain applicable;
this repository does not claim ownership of them or redistribute their binaries.

Coder, OpenHands, agenticlinux, and other alternatives appear in research notes.
A comparison or link is not a claim of code reuse. The current runtime uses
Crabbox with Hermes; it does not embed those alternative agent harnesses.
