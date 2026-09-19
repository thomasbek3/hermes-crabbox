---
name: pr-evidence
description: Capture genuine before/after screenshots and a short interaction video for UI changes in a Hermes cloud task, and prepare evidence files for the parent agent's pull request.
---

# PR evidence in a cloud task

For work that changes a visible UI, capture the relevant before/after states and a short video showing the resulting interaction. Save these with the task results so the parent agent can include them in its pull request. This is part of the assigned implementation or verification work, not a separate test campaign. For tasks with no visible UI change, skip screenshots and video; record that reason in the manifest and PR notes.

The container has native `agent-browser`, Chromium, ffmpeg, and Playwright. Use existing terminal tools; this skill registers no browser tool server. `/workspace` is shared between the task's roles. Read-only planning and review roles may inspect existing evidence and identify gaps; they must not capture, write files, or acquire additional tools. The assigned implementation or verification role creates the media.

For browser work, load `cloud-evidence:agent-browser` when needed. If this role exposes only file/terminal tools, read its installed source at `/opt/hermes/trusted-plugins/cloud-evidence/skills/agent-browser/SKILL.md`. Its command reference supports this capture workflow; it does not add verification work to tasks without UI changes.

## Capture the relevant behavior

Use `/workspace/pr-evidence/` for all publishable evidence. Use synthetic or already authorized demo data. Exclude credentials, session tokens, private account pages and unrelated desktop content from captures.

1. If the prior UI is available, capture its real state before changing it. Use a comparable route, viewport and interaction state for the after image. Never reconstruct or fabricate a before image. If work already changed the UI, explain that a before capture was unavailable and provide the genuine after evidence.
2. Run the project using its normal development command. Open the actual app and exercise the changed behavior. Avoid capturing loading states or blank pages as completion evidence.
3. Capture the after state and one focused interaction video. Use concise filenames that identify the feature. Include only the files that help explain the change.
4. Inspect the screenshots and recorded output. Use an available image/vision tool, or a contact sheet with the available visual inspection surface. If this role has no way to see image pixels, record that limitation and ask the parent to inspect them; file existence or a DOM snapshot alone is not visual confirmation.

Example native browser flow; substitute the real local app URL and fresh element references:

```bash
mkdir -p /workspace/pr-evidence
agent-browser --session pr-evidence open http://127.0.0.1:3000
agent-browser --session pr-evidence snapshot -i
agent-browser --session pr-evidence screenshot /workspace/pr-evidence/before.png
```

When the user needs to see or interact with the browser on the task desktop, start the session with `--headed` and the inherited `DISPLAY=:99`. Otherwise headless capture is appropriate. The image config supplies Chromium through `AGENT_BROWSER_EXECUTABLE_PATH`; do not install a different browser or borrow a user's browser profile.

After implementing the change and reaching the relevant route:

```bash
agent-browser --session pr-evidence screenshot /workspace/pr-evidence/after.png
agent-browser --session pr-evidence record start /workspace/pr-evidence/interaction.mp4 --cursor --contact-sheet
agent-browser --session pr-evidence snapshot -i
# Perform the real interactions using refs from that snapshot.
agent-browser --session pr-evidence click @e1
agent-browser --session pr-evidence snapshot -i
agent-browser --session pr-evidence record stop
```

Replace `@e1` with the observed control; never copy a guessed ref. Re-snapshot after navigation or DOM changes. `record stop` finalizes the MP4; do not export an unfinished recording. `--contact-sheet` also produces a timestamped PNG summary: inspect and include the actual path reported by the command. The cursor overlay is appropriate for the interaction video; take clean before/after screenshots outside the recording if the overlay would distract.

Check the recording's stream and duration without mistaking this metadata check for a visual inspection:

```bash
ffprobe -v error -show_entries stream=codec_name,width,height -show_entries format=duration -of json /workspace/pr-evidence/interaction.mp4
```

If useful, create a contact sheet from an existing short recording:

```bash
ffmpeg -i /workspace/pr-evidence/interaction.mp4 -vf 'fps=1/2,scale=320:-1,tile=4x3' -frames:v 1 /workspace/pr-evidence/contact-sheet.png
```

Choose sampling to cover the relevant recording duration. Contact sheets summarize real frames; they do not replace the video.

## Native desktop or Playwright fallback

For a native application or behavior outside the browser, capture the actual XFCE display. Inspect its size first (`xdpyinfo -display :99`); use that size in the command. Example for a 1920x1080 desktop:

```bash
scrot /workspace/pr-evidence/desktop-after.png
ffmpeg -f x11grab -video_size 1920x1080 -framerate 15 -draw_mouse 1 -i :99 -t 30 -c:v libx264 -pix_fmt yuv420p /workspace/pr-evidence/desktop-interaction.mp4
```

Run the recorder through the terminal's managed process facility while performing the actual interaction, or record a bounded demonstration. Finish with normal recording shutdown so the MP4 is playable; do not leave a recorder running after the role ends. Desktop capture includes every visible window, so close or move unrelated/private content first.

If native `agent-browser` cannot operate the page, existing Playwright is available through `/opt/hermes/venv/bin/python`; see `/opt/cloud-tools/BROWSER.md`. Use its real screenshot/video APIs, write outputs under `/workspace/pr-evidence/`, and close the recording browser context to finalize the video. Convert its WebM to MP4 with ffmpeg if needed. Report the fallback and any remaining capture limitation without claiming a fabricated success.

## Prepare the parent's PR handoff

Write `/workspace/pr-evidence/manifest.json` with this exact top-level schema. Asset paths are relative to `/workspace`, must name real files under `pr-evidence/`, and must not contain secrets or traversal. Include only assets intended for the PR; list a contact sheet only when it exists.

```json
{
  "schema_version": 1,
  "assets": [
    {"path": "pr-evidence/before.png", "label": "Before: existing form layout"},
    {"path": "pr-evidence/after.png", "label": "After: corrected form layout"},
    {"path": "pr-evidence/interaction.mp4", "label": "Submitting the updated form"}
  ],
  "summary": "Describe the visible change, demonstrated behavior and any inspection limitation."
}
```

Write `/workspace/pr-evidence/pr-body.md` in user-facing language: what changed, what before/after shows, and what the video demonstrates. Use workspace-relative references so the parent can replace them with published URLs. Keep the video link on its own paragraph:

```markdown
The form now keeps the submit button visible when validation errors appear.

Before:
![Before the change](pr-evidence/before.png)

After:
![After the change](pr-evidence/after.png)

Interaction recording:

![](pr-evidence/interaction.mp4)
```

If there is no UI, use `"assets": []` and explain why media does not apply. If capture is unavailable, include the useful files you did obtain and describe the specific missing evidence. Do not invent paths or labels claiming an unseen result.

Return the manifest and PR-notes paths in your final task/role result. The parent agent handles GitHub credentials, uploads and PR publication. This container prepares files and does not need GitHub login or publishing permissions.
