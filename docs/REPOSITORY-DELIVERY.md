> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Registered repository delivery

The worker can initialize a coding session from one operator-registered local Git repository at a full immutable commit. The current profile places that repository at the workspace root. Requests cannot supply arbitrary filesystem paths, clone URLs, shell commands, branches or moving refs.

Worker configuration maps a repository ID to an absolute operator-owned repository path and an allowlist of commit IDs. An immutable environment manifest selects `repository_id`, `commit` and destination `.`. A legacy configured project can use the same `repository` object while its environment registry is being imported. Operator configuration must not combine a repository and a template.

The supervisor reads Git metadata and blobs directly, with bounded output, file count, total size and elapsed time. It rejects links and submodules. It does not invoke Git in an agent-writable checkout, run checkout filters/hooks, or expose host credentials. The workspace contains the selected commit's files; it does not include `.git` or permit Git push/PR actions. Those integrations belong to the later release.

A private baseline is saved under the worker's `baselines` directory before workspace initialization. Partial initialization resumes only if existing files match that baseline. A completed initialization marker prevents a follow-up from replacing agent edits. The baseline commit and SHA256 are recorded with the attempt.

After the agent stops, normal immutable export and secret checks run. The platform compares exported bytes with the baseline and publishes two additional artifacts:

- `@delivery/changes.patch`: a unified patch for supported text files.
- `@delivery/manifest.json`: base commit, patch hash, changed-file hashes and explicit omissions.

Binary files, files without final newlines, empty-file additions/deletions, and filenames outside the conservative patch syntax remain downloadable as files and are explicitly omitted from the text patch. A partial text patch is never labeled complete. File modes are not changed through this supplementary text patch. The delivered file hashes are authoritative.

`GET /v1/sessions/{session_id}/diff` returns the latest attempt's delivery metadata and the authenticated patch artifact ID. It requires access to that session and its artifacts. A new unfinished follow-up does not silently return an older attempt's patch. The reserved `@delivery/` namespace cannot be supplied by a job as a platform artifact.

Current verification: immutable commit extraction; symlink/size rejection; actual `git apply --check` and application; baseline integrity; partial initialization; controller integration; follow-up preservation; cross-client delivery denial. Live Omarchy repository task qualification and Fable review are still required before this unit is accepted.

Patch corrections: LF-only diff records preserve embedded text separators; source executable bits are retained in artifact metadata and Git mode headers. A patch-size cap produces an explicit omission rather than aborting file delivery. Baseline dependency paths outside export policy are listed separately rather than reported deleted. Binary/no-final-newline/unsafe-filename changes remain explicit file delivery; never assume a partial patch recreates the entire workspace.
