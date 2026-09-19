# Omarchy Crabbox desktop delegation — deployed 2026-09-18

Current target: `omarchy`, the separate Intel MacBook Pro, `thomas@100.83.74.92`.
API remains `https://omarchy.tail0d5eb6.ts.net`. New submissions from both Mac minis
now default to `hermes-tasks` / `hermes-tasks-desktop-v1`.

## Current desktop deployment

- Image `sha256:33e12202540a57f9e024bae3bdfa861fd40524ab9c0f9b19da838e8208e31bb0` adds XFCE, TigerVNC/noVNC and graphical Chromium to the existing native Hermes/Grok4.6-xhigh Crabbox worker. SSH host keys are generated at container startup by `crabbox-init`; they are not baked into the image.
- Each task still uses one container with 1 CPU, 4 GiB RAM and 512 PIDs. Desktop processes share those limits. Workspace/native state remains an 8 GiB bounded volume. Up to eight tasks run, subject to RAM/disk admission; other jobs queue. Budgets remain 1,000 steps/two hours, capture7,320seconds, supervisor7,500seconds.
- Current and AI Minis have the official Crabbox0.61.0 Mac runtime plus the updated canonical `omarchy-cloud` skill, CLI and desktop helper. Existing aliases and gateways were preserved.
- Use `~/.local/bin/omarchy-cloud desktop SESSION_ID` while a desktop task is running. It authorizes the session through the API, retrieves a private native VNC handoff over SSH, forwards loopback ports and opens the local browser viewer. Passwords stay in private pipes. Keep the terminal command running; Ctrl+C closes viewing without cancelling the task. Closing the browser alone leaves the tunnel running. Task completion ends viewing and destroys the container; save deliverables in `/workspace`.
- The server helper checks exact latest task/attempt/generation, journal identity, desktop flag and live container; it ends viewing if the task ends or changes. No public VNC port, personal login profile, refresh token or host Docker socket is exposed. The dedicated cloud access token is present in the guest, as in the previous Crabbox design.
- Old sessions stay pinned. The unused headless `hermes-tasks-crabbox-v1` profile was removed from new submission allowlists after the desktop check revealed it lacked SSH host-key generation. Its registry/history remains. Older web-v2 sessions are unchanged.
- Jev/pstack routing, resource-chart sampling and automatic parent check-ins are not added by this change.

## Evidence and limits

A desktop-only container using the final image started in 11.842seconds. Its XFCE desktop and visible Chromium were inspected through the saved screenshot `evidence/crabbox-desktop/desktop.png`. The first image failed due to missing SSH host keys; the initializer fixed startup. The Mac viewer initially exited; a later transport-only retry remained up for30seconds. Browser URL policy blocked inspection of the local handoff page; no alternate access was used. Thomas subsequently confirmed the temporary desktop is visible in Google Chrome after an explicit Chrome opener was used. Keyboard/mouse interaction is not yet separately confirmed. The temporary desktop-check container was deleted through Crabbox afterward.

The initial desktop deployment did not run a model task. Subsequently, the authorized first real job `138d622c-6319-4f7a-9283-d9a9b766c4a0` completed through the desktop profile: native Hermes/Grok produced five queue-report utility files, reported23 passing tests, and returned its results through the API. The Mac downloaded the bundle, matched all five artifact hashes/lengths, and independently reran23 tests successfully. Omarchy journal phase stopped, zero remaining Docker containers and removed per-task model-key confirm cleanup. Evidence: `evidence/crabbox-first-job/receipt.json`, result.zip, status.json and local-checks.txt. No follow-up, cancellation, eight-task load test, Mac-viewer check or external model review was performed during this job. Environment remains operator approved with `qualified=false`, manifest `9e253f559209a79b57e2d9c2fd4caa1dd96d67ea793bf3a963a0f992b32af212`.

Source syntax, deployed source hashes, caller command loading and API/worker capacities were checked. API and worker services are active. Activation first stopped at a setgid directory guard before source/config publication; services were restored, the guard was corrected to accept owned mode2700, and a fresh registry/backup path was used. Failed-attempt evidence remains intact.

Successful receipt: `evidence/crabbox-desktop/activation.json`. Registry: `/var/lib/cloud-workbench/environments/hermes-tasks-desktop-v1-attempt2.db`. Backup: `/var/lib/cloud-workbench/operator-backups/crabbox-desktop-20260918-attempt2`. Protected stage: `/var/lib/cloud-workbench/qualifications/crabbox-desktop-source-20260918`. Do not blindly rerun activation or roll back task state after new submissions. Legacy cloudd7777 was unchanged.

```sh
~/.local/bin/omarchy-cloud submit --github OWNER/REPO --ref main --goal 'The actual task; use visible Chromium when I need to watch.'
~/.local/bin/omarchy-cloud status SESSION_ID
~/.local/bin/omarchy-cloud desktop SESSION_ID
~/.local/bin/omarchy-cloud results SESSION_ID
~/.local/bin/omarchy-cloud follow-up SESSION_ID --message 'The next instruction'
```

The following sections retain the previous deployment details for old sessions.

## Concurrency update — 2026-09-18

Thomas requested a maximum of eight simultaneous tasks. API/worker capacity and Hermes capacity are now8; the scheduler validation ceiling was raised from6 to8. Per-task4GiB RAM, existing RAM/disk admission guards and other limits remain unchanged. Applied while idle with private backups, source/config readback and active services; no test jobs were run. Receipt: `evidence/crabbox-deployment/concurrency8.json`. Both Minis' caller skills reflect the new maximum.

## Previous web-v2 deployment (historical)

Target: `omarchy`, the separate Intel MacBook Pro, SSH `thomas@100.83.74.92`. Private HTTPS API: `https://omarchy.tail0d5eb6.ts.net`, Tailscale Serve to loopback7780. Legacy cloudd on7777 was neither restarted nor replaced.

Default caller project/environment: `hermes-tasks` / `hermes-tasks-web-v2`. A fresh task starts one native Hermes0.21.3 worker using Grok4.6/xhigh and an independent workspace/profile. Up to three independent tasks run simultaneously; excess tasks queue. Explicit follow-up retains its original session/workspace. Results are exported and containers removed by the existing lifecycle. Task history and native continuation state remain on disk; this is not automatic deletion of saved workspaces.

The image `sha256:588168c9c911a5367997e4679776d333dc97d1caed20750767ac1254ac894934` contains Playwright1.63.0 and Chromium153.0.8010.12. Browser files are under `/opt/playwright`; worker instructions are `/opt/cloud-tools/BROWSER.md`. Browser automation runs through terminal Python `/opt/hermes/venv/bin/python` inside tool containers. Provider credentials and the host Docker socket stay outside those tool containers. Headless browsing is available; no remote graphical desktop or streamed browser UI was added.

Tools use bridge internet for web access and workspace-local dependency downloads. Limits: workspace8GiB disk, coordinator1GiB RAM/128PIDs, each tool container3GiB RAM/256PIDs, each1CPU. Host admission reserves at least8GiB and requires4GiB available above that for new jobs. These are ceilings and admission settings, not measured per-job usage. Updated at Thomas's request: each submitted job/follow-up now permits1,000model steps and7,200seconds (two hours), whichever comes first. Capture timeout is7,320seconds; the Hermes-only host supervisor timeout is7,500seconds. Other agent supervisor budgets remain unchanged. No automatic parent check-ins were added.

The new environment is explicitly operator approved by manifest digest `e6ed9b6fbc9e0e8ffd6e7faed9c7629ee713b8edb703aeafff6df4e3310b7e59`, with `qualified=false` and no fabricated qualification receipt. New successful task outcomes remain `unverified` because the project has no protected checks. Existing offline versions and old sessions remain registered with their original limits; the new default applies to new caller submissions.

Scheduler schema1 now exempts Hermes from the per-agent single-credential uniqueness index, retaining session uniqueness, provider-owner guards and serialization for other adapters. Schema2/3 routed workflows keep their existing serialization. This does not activate the unfinished routed controller.

## Caller installation and use

Both Mac minis have `~/.local/bin/omarchy-cloud`, using their installed Hermes Python. Canonical skill: `~/.agents/skills/omarchy-cloud`, with Codex and Hermes aliases. AI Mini Muse additionally has `~/.hermes/profiles/muse/skills/omarchy-cloud`. Existing gateways were not restarted and no prompt/message was sent to those agents. Ask an agent explicitly to use this skill; automatic discovery in already-running chats is not established.

Each Mac has its own client principal scoped to hermes-tasks with submit/observe/retrieve/cancel. Tokens are in owner-only `~/.config/omarchy-cloud/token`; they were transferred without exposing values. Both agents on one host share that host's caller identity.

```sh
~/.local/bin/omarchy-cloud submit --github OWNER/REPO --ref main --goal 'The actual task'
~/.local/bin/omarchy-cloud status SESSION_ID
~/.local/bin/omarchy-cloud events SESSION_ID
~/.local/bin/omarchy-cloud results SESSION_ID
~/.local/bin/omarchy-cloud bundle SESSION_ID ./results.zip
~/.local/bin/omarchy-cloud follow-up SESSION_ID --message 'The next instruction'
~/.local/bin/omarchy-cloud cancel ATTEMPT_ID
```

`--github` uses the submitting Mac's existing gh authorization, resolves an immutable commit, downloads its archive and uploads it. GitHub credentials do not enter the worker. Omitted --ref means the repo default branch. Private repository access must already be authorized on that caller; no new GitHub login/access was established or tested. The client also accepts a clean local `--repo-dir` or explicitly uploaded files. Archives lack full Git history/submodule contents, and LFS handling depends on the archive. Worker network access permits public cloning, but private GitHub credentials are not injected.

No automatic PR publishing, browser-account login, secrets broker integration or full Jev/pstack multi-model orchestration was added. These remain distinct from this usable task interface.

## Auth maintenance

Dedicated cloud Grok OAuth renewal timer runs every5minutes with a140-minute refresh threshold. The helper uses the CLI lock and supported OIDC grant, with private atomic credential replacement and fixed safe errors. Initial activation rejected the CLI's0644 lock; corrected to permit a lock not writable by others and narrow it to0600 under flock. Service group is cloud-workbench/gid960. The corrected service returned auth_current. Actual renewal has not been demonstrated by this work. New jobs require at least123minutes of credential lifetime to cover the extended run and shutdown. Credential metadata showed more than3hours remaining when the budget update was installed; no token was exposed or model call made. An ambiguous refresh outcome stops automatic replay and requires recovery rather than repeatedly consuming a rotating token.

## Evidence and recovery

Deployment-only receipts:
- evidence/job-budget-deployment-20260918.json (two-hour/1,000-step update, matching source/config readback and active services; no test job)
- evidence/hermes-native-deployed-20260918.json
- evidence/basic-delegation-deployment-20260918.json
- evidence/basic-delegation-caller-install-20260918.json
- evidence/basic-delegation-github-install-20260918.json
- evidence/basic-delegation-web-caller-install-20260918.json
- evidence/hermes-browser-image/ (image installation log/receipt; no browser run)
- evidence/web-profile-source-binding-20260918.json
- evidence/web-delegation-deployment-20260918.json
- evidence/web-delegation-service-state-20260918.txt

API/worker are active according to deployment/service state. No new end-to-end task, browser behavior or parallel-concurrency proof is claimed. No owned temporary execution/review job remains running; the intended service and auth timer remain enabled.

Backups: `/var/lib/cloud-workbench/operator-backups/hermes-native-20260918`, `/var/lib/cloud-workbench/operator-backups/basic-delegation-20260918`, `/var/lib/cloud-workbench/operator-backups/web-delegation-20260918`. Current registry: `/var/lib/cloud-workbench/environments/hermes-tasks-web-v2.db`, with adjacent approval JSON. Stage: `/var/lib/cloud-workbench/qualifications/web-profile-source-20260918`. Do not restore an old state.db after new work has arrived; inspect exact live state and preserve jobs. Do not restart legacy cloudd as part of this lane.

## Job budget update — 2026-09-18

Raised native Hermes limits from20steps/420seconds to1,000steps/7,200seconds. Aligned capture, the Hermes-only supervisor and credential lifetime admission/renewal thresholds. Deployed on idle Omarchy with source SHA binding, syntax checks and service/config readback. No new tests, model calls, browser launches or parent polling were added. Rollback files: `/var/lib/cloud-workbench/operator-backups/job-budget-20260918`; restore source/config only while idle, never restore an old task database. Caller skill documentation is updated on both Mac minis.
