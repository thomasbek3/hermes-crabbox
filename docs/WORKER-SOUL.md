# Shared Hermes cloud worker identity

Installed on the separate Omarchy laptop, 2026-09-19 UTC. Canonical text: `integrations/hermes-pr-evidence/SOUL.md`. Thomas approved the full cloud worker identity and added the requirement to read and follow each repository's PR contribution instructions.

The image holds the template at `/opt/hermes/trusted-plugins/cloud-evidence/SOUL.md`. Before launching Hermes, the main worker and each temporary pstack role session copy it into their private `$HERMES_HOME/SOUL.md`. Missing/empty/oversized source fails startup. An existing differing identity or symlink is rejected rather than overwritten. Matching files are retained on follow-up.

Removed `--ignore-rules` from the main launcher and set the child CLI's `ignore_rules=False`: otherwise native Hermes would skip SOUL.md. This also enables Hermes's ordinary repository context loading. Personal memory remains disabled, project plugins remain disabled, and provider/model/tool permissions remain fixed. The parent caller skill now carries the repository PR rules too.

New environment versions:
- Default on both Minis: `hermes-tasks-desktop-soul-v1`.
- Opt-in routed: `hermes-tasks-pstack-soul-v1`.

Existing sessions retain their pinned environment; this does not retrofit older saved sessions. No task containers were running during activation. No personal Mini/Muse identity files were replaced.

## Evidence

65 focused guest/child/skill tests passed. `scripts/check-worker-soul.py` ran inside the built worker image with networking disabled and synthetic credentials. It used real Hermes AIAgent/system-prompt assembly for main, feature and code-review profiles. The full SOUL text appeared initially and after `invalidate_system_prompt` plus rebuild, the boundary Hermes uses after compaction. This checks native loading/reloading; it does not claim a live provider response or full summarizer execution.

All three profiles loaded SHA256 `4746f2ce2c7b283ffbed05fa8c220e42ec82a0ad1ce3c26afd4229cf6a67ebf0`. Receipts: `evidence/worker-soul/prompt-check.json`, `activation.json`, `caller-readback.json`. API/worker/cloudd were active after rollout and no check containers remained. No model calls, provider changes, PR publication or review agents.

Images and manifests are recorded in activation.json. Backup: `/var/lib/cloud-workbench/operator-backups/worker-soul-20260919T025459Z`. Both old environment versions remain available. Fable's quota limitation and qualified=false remain unchanged.
