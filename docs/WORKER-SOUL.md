# Shared Hermes cloud worker identity

The canonical worker instructions are
[integrations/hermes-pr-evidence/SOUL.md](../integrations/hermes-pr-evidence/SOUL.md).
They describe the worker's role, workspace, completion requirements and duty to
follow the assigned repository's contribution and PR rules. They do not identify
a particular owner, computer, account, or personal assistant.

The image stores the template at
`/opt/hermes/trusted-plugins/cloud-evidence/SOUL.md`. Before launching Hermes,
the main worker and supported temporary pstack role sessions copy it into their
private `$HERMES_HOME/SOUL.md`. Missing, empty or oversized source blocks startup.
A differing identity or symlink is rejected; matching files survive follow-up.

Hermes loads this through its native SOUL mechanism. Repository context loading
is enabled, personal memory remains disabled, and the instructions do not expand
model/tool permissions. Existing sessions retain their pinned environment;
updating the repository alone does not replace already-running images or homes.

`scripts/check-worker-soul.py` exercises real Hermes prompt assembly inside a
prepared worker image with synthetic credentials and networking disabled. It
checks the full text initially and after prompt invalidation/rebuild. That is
loading evidence, not a live model response or proof of an entire multi-model
workflow. Fresh images need their own check; old image hashes are not proof for
new builds.
