# Legacy workspace import and opt-in compatibility

This is a migration plan, not an executed import or cutover. The inventory was taken on the Omarchy laptop on 2026-09-17. The original service and files remain untouched.

## Current inventory

`evidence/legacy-workspace-inventory.json` binds the read-only driver and actual `cloudd.py` source SHA. Five retained workspaces contain three regular files totaling 268 bytes. No symlinks, multiply linked files, special files or remaining `cloudd=1` containers were observed. No Git directory was found at the expected `work/repo/.git` path; other Git layouts were not checked. File contents, prompts, logs, credentials and `HANDOFF.md` were not read. Every disk-only job has **unknown historical state**; the existence of a log is not success evidence.

| Legacy job | Regular files | Bytes | State evidence |
| --- | ---: | ---: | --- |
| 0917-015824-4707 | 1 | 15 | No container; historical state unknown |
| 0917-015825-a699 | 0 | 0 | No container; historical state unknown |
| 0917-021645-f6a5 | 1 | 16 | No container; historical state unknown |
| 0917-023143-9265 | 1 | 237 | No container; historical state unknown |
| 0917-130430-da69 | 0 | 0 | No container; historical state unknown |

Source defaults are six concurrent jobs, 4 GiB memory and two CPUs per job. The source does not set a PID limit or a workspace disk quota. This is not a claim about effective environment overrides or daemon-wide defaults; no process environment was read and no extant legacy container could supply effective limits.

The v1 API exposes authenticated `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/log`, `GET /jobs/{id}/file?path=...`, `GET /skill.tar`, `POST /jobs` and `DELETE /jobs/{id}`. File download is JSON text with replacement decoding, not binary-preserving. The registry is an in-memory dictionary. Startup removes containers labeled `cloudd=1`; do not restart v1 as a migration technique. Deletion removes the job container and its full job directory.

## Workspace-only import procedure

1. Select exact legacy job IDs and target v2 project/environment explicitly. Do not globally repoint the old `cloud` command, replace port7777, stop v1, or delete old data. Skip empty workspaces unless a user wants a provenance-only record.
2. Recheck each selected workspace for active containers, open writers and source changes. Inventory by root-relative paths with no symlink traversal. Refuse special files, hard links, path escapes and changing files. Do not traverse a job's parent directory or copy `log`, credential staging, personal homes, `server/token`, `HANDOFF.md`, native agent state, `.git`, caches or runtime state.
3. Prepare a bounded manifest with relative path, bytes, SHA-256 and legacy job ID. Treat all selected workspace bytes as untrusted and potentially secret-bearing: workspace-only is not itself a secret guarantee. Require an explicit file allowlist and secret screening/review before uploading. Never print candidate secrets. Reject secret-bearing files rather than silently altering their content. Current inventory intentionally does not certify the three files as credential-free because their bodies were not read.
4. Stage approved regular files into a new private destination, using no-follow reads and exclusive destination creation, bounded by the destination input and disk limits. Do not restore ownership, executable privileges or trusted verification status from v1. Rehash copied bytes and verify the unchanged source snapshot. Preserve relative-path metadata separately from data objects; avoid unchecked archive extraction.
5. Upload the approved files through authenticated v2 inputs and retain the returned input IDs plus a manifest receipt. There is no production migration command implemented by this document. `cloud-compat` is a verb adapter and does not import legacy files. A separate explicit v2 request may reference approved input IDs and select the qualified project/environment. Do not automatically launch a task or replay an old prompt as part of copying files.
6. Verify v2 authorization, input hashes, session provenance, protected checks and artifact download hashes. A v1 `done` label is not v2 verification. Record the legacy-job-to-v2-session/input mapping without implying native conversation continuity. Keep original files intact until any later retention/deletion decision is authorized separately.

Rollback for this plan is to keep using v1 and discard only a separately authorized new import, never to delete or overwrite the original workspace. Credentials must be provisioned through the dedicated v2 path; no legacy credential migration is allowed.

## Opt-in CLI behavior

The separate `cloud-compat` command uses a named v2 client/config and preserves the common `run`, `ls`, `status`, `wait`, `log`, `file`, `rm` verbs. It is not wire-compatible with the v1 API. V2 IDs are new; old job IDs do not become session IDs automatically. Repositories and refs require registered routes, unsupported provider/name options fail explicitly, files come from exported artifacts, and `rm --confirm` means v2 purge rather than cancel. A user must opt in per client; this checkpoint does not redirect existing clients or enable additional providers.
