# Hermes screenshots and video for PRs

Deployed to the separate Omarchy laptop on 2026-09-19 UTC. New submissions from both Mac minis default to `hermes-tasks-desktop-soul-v1`. Existing sessions retain their pinned environment. Opt-in routing uses `hermes-tasks-pstack-evidence-v1`; Fable's account quota still blocks full routed execution.

Each task container includes native agent-browser 0.38.1, Chromium, FFmpeg, the upstream browser skill, and the shared `cloud-evidence:pr-evidence` skill. The working Hermes session captures media itself. Temporary pstack role sessions share the same container/workspace and discover the same skill; existing read-only planning/review tool permissions remain unchanged.

Ask Hermes to capture the relevant before/after UI and interaction video. Files and a schema-version-1 manifest go in `/workspace/pr-evidence/` and are exported through the existing task artifact API. No GitHub login enters the container.

On either delegating Mini:

```sh
~/.local/bin/omarchy-cloud-evidence SESSION_ID --output-dir ./pr-evidence-downloads
```

Inspect the downloaded media and generated comment. When PR publication is authorized, repeat with the exact target:

```sh
~/.local/bin/omarchy-cloud-evidence SESSION_ID --output-dir ./pr-evidence-downloads --pr https://github.com/OWNER/REPO/pull/NUMBER --publish
```

The helper uses the parent's GitHub login and native `gh pr comment --attach`. Both Minis now have GitHub CLI 2.101.0; Omarchy already had it. Publication uses a receipt to prevent blind duplicate retries. Preparing media never contacts GitHub.

## Evidence and limits

Actual native Hermes/Grok job `063c4634-5977-4e3d-ac66-b4b63b210d43`, attempt `31c68208-2ba6-492b-9792-225dea670ddb`, completed with exit 0. It created a small browser counter and captured two PNGs, a 2.1-second H.264 MP4 and a contact sheet. Parent API downloads matched SHA256/length metadata. Visual inspection of screenshots and sampled video frames confirmed 0 -> 1 -> 0. The worker's sparse contact sheet omitted the intermediate 1; the full video contains it.

The task container stopped and was removed; per-task credential/request files were removed. API, worker and cloudd remained active. Receipts and downloaded media are under `evidence/pr-evidence/`. The four focused helper/skill/guest/child test files passed 89 tests. Installed parser readbacks on both Minis select the new single-model environment.

Actual attachment publication to a real PR has not been exercised because no target PR was supplied. Broad environment qualification remains false; this result establishes this capture/download path, not full routed execution or concurrency qualification.

## Pinned dependencies

agent-browser v0.38.1 upstream source: `aff6125c023b810ea3f2e5deec5379e9a4270bdc`; Linux x64 binary SHA256 `5100149a1903211c889de4e545bf36d90803740cea4f99aa22651649f9205ea1`. Upstream core skill and Apache license are retained in `integrations/hermes-pr-evidence/skills/agent-browser/`.

Image digests, environment manifests and operator backup path are recorded in `evidence/pr-evidence/activation.json`. Activation leaves existing versions available. Resource limits and the maximum of eight concurrent tasks are unchanged.

Shared worker identity: new SOUL-enabled profiles install the complete cloud worker identity and repository PR rules at each private Hermes home. See `WORKER-SOUL.md` for the current images and loading proof.
