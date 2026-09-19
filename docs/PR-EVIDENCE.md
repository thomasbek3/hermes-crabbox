# Screenshots and video for pull requests

The desktop worker image includes Chromium, FFmpeg, agent-browser, its attributed
upstream skill, and the `cloud-evidence:pr-evidence` skill. The working Hermes
session captures evidence in its own task workspace. Supported temporary pstack
role sessions share that workspace; their existing tool permissions still apply.

Ask for relevant before/after screenshots and a short interaction recording.
Save them with a schema-version-1 manifest under `/workspace/pr-evidence/` so
the task artifact API can export them. Workers prepare evidence and PR notes;
GitHub publication happens on the calling agent's computer.

## Retrieve and inspect

From the installed caller skill directory:

```sh
python3 scripts/pr_evidence.py SESSION_ID --output-dir ./pr-evidence-downloads
```

The helper uses the same configured server as the HTTP client, validates artifact
hashes/lengths, and prepares a local comment with the selected media. Inspect the
actual files and generated comment. Preparation does not contact GitHub.

## Publish to an authorized PR

Read that repository's contribution instructions and PR template first. When
publication to a specific PR is authorized:

```sh
python3 scripts/pr_evidence.py SESSION_ID --output-dir ./pr-evidence-downloads \
  --pr https://github.com/OWNER/REPO/pull/NUMBER --publish
```

This requires the caller's GitHub login and a GitHub CLI with native
`gh pr comment --attach` support. Credentials are not sent to the worker.
Retain the publication receipt; after an uncertain response, inspect the PR before
retrying. Returning a prepared comment does not mean an upload happened.

## Evidence and limitations

The initial reference deployment completed a synthetic counter task and returned
two PNGs, a short MP4 and a contact sheet. Parent-side retrieval and visual
inspection confirmed the interaction. One selected screenshot appears in the
README. That recorded result does not establish the same behavior for a new
host image, full pstack execution, concurrent load, or attachment publication to
a real PR. Validate those separately when they are part of the assignment.

## Upstream components

The agent-browser core skill and its Apache license are retained in
`integrations/hermes-pr-evidence/skills/agent-browser/`. See
[the attribution inventory](../THIRD_PARTY_NOTICES.md) and [build inputs](../BUILDING.md)
for exact revisions and binary hashes. Original integration code is MIT licensed.

[Worker instructions](WORKER-SOUL.md) · [Caller setup](QUICKSTART.md)
