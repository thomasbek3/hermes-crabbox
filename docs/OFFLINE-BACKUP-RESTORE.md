> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Offline v2 backup and restore rehearsal

This tooling creates a consistent, credential-disabled portable copy of the **new v2 workbench**. It does not stop services, reboot Omarchy, migrate legacy `cloudd`, prune files, or promote a restored copy into production.

Current retention behavior: archive hides a session and preserves all data. Cancellation stops work without purging it. Delete/purge remains unavailable in the API until destructive retention is separately implemented and verified. No automatic 30-day cleanup is configured by this work.

## Preconditions and scope

Use a maintenance window in which the operator has stopped the v2 API, worker, and any external writers to the selected workspaces. Resolve queued/live attempts deliberately first. The command requires `--acknowledge-quiesced` and rejects any nonterminal attempt; it never rewrites running/queued work into a convenient terminal state. A SQLite write transaction prevents new DB changes while copying, but cannot prove that an unrelated filesystem process has stopped.

Only the selected v2 worker configuration is read. The configuration is not bundled. The SQLite backup API captures committed WAL data consistently; raw `state.db` copying is not used. Schema version, integrity and foreign keys are checked. Clients retain their IDs and ownership references, but copied credential hashes are replaced with disabled values and every copied client is revoked. The original database and original credentials remain unchanged.

The bundle contains:

- Sanitized standalone SQLite database.
- Configured artifact, input, result and logical workspace trees. Host-owned repository baseline files and readiness markers are included when the configured state root has a `baselines` directory. Workspace `work` and `native` directories are included; `.git` metadata is retained where regular and safe.
- A manifest with every included file's SHA256 and byte count, component policies and visible exclusions.

Raw ext4 `.volumes` images, virtual environments, installed dependencies/caches, and specifically named credential/auth directories and files are excluded. Per-attempt `tasks/.credentials` token capsules and `credential-state`/`credential_state` private quarantine trees are expressly excluded even if nested inside a selected tree. Provider auth roots, API client token files, service configuration, task launch files and raw logs are not selected components. Dependencies must be rebuilt from pinned manifests when promoting a restore. Symlinks, hardlinks and special files in included trees reject the entire operation; trusted dependency/auth exclusions are skipped without following links.

This is a **selected logical-state backup**, not an exact disk image or a claim of complete native conversation restoration. Executable permissions and numeric host ownership are intentionally not reinstated; all restored files are private and read-only. Provisioning a real replacement host must explicitly establish the correct service identities, bounded volumes and permissions before any controlled import.

Known-secret byte scans can reject accidental credential copies using `--scan-secret-file`. Named exclusions cannot prove that arbitrary prompts, files, or third-party native state contain no unknown secrets. Treat the entire bundle as confidential. Conservative exact-name exclusions also omit ordinary code directories called `auth`, `tokens` or `secrets`; the component report lists these omissions. Review that report before considering any promotion, recover required code from its pinned repository, and do not treat the bundle as a complete workspace image.

## Creating a bundle

The following is an operator command template. A canonical quiesced v2 run was completed on Omarchy and restored separately on the local Mac mini; the bound receipts and limits are listed below:

```sh
/opt/cloud-workbench/.venv/bin/python /opt/cloud-workbench/scripts/backup-state.py create \
  --config /etc/cloud-workbench/worker.json \
  --destination /path/on/protected-storage/cwb-backup-YYYYMMDD \
  --acknowledge-quiesced \
  --scan-secret-file /var/lib/cloud-workbench/auth/claude-token
```

Use the actual provisioned Python interpreter; the path above is a template. The destination must not exist and its parent must exist. Use canonical paths: symlinked ancestors (including macOS `/var` aliases) are refused, rather than followed implicitly. Component roots cannot overlap or include the source database/destination. Default operation budgets are 4 GiB, 100,000 entries and 120 seconds, with an additional 1 GiB free-space reserve. Admission requires three times the byte budget plus that reserve, allowing for SQLite journal/VACUUM scratch copies. Set a larger reserve matching the host policy (20 GiB on Omarchy), and sufficient byte/time limits for the measured data. The byte limit includes the manifest; restore reserves space for its receipt and limits SQLite page growth during path rewrites. Referenced artifact/input SHA256 values and byte counts must also agree with their database records. Failure removes only the command's private staging directory; existing destinations are never overwritten.

The output is **not encrypted**. The bounded off-host qualification plan uses the separately tested `scripts/seal-backup.py` AES-256-GCM envelope: stream the quiesced Omarchy bundle over SSH directly into sealing on this Mac, keep the private key at `~/Library/Application Support/CloudWorkbench/backup.key`, and retain the encrypted result in the task's private backup directory. Independently authenticate/decrypt it into an isolated local rehearsal directory and run restore verification. No plaintext transfer archive, schedule, deletion or service cutover is part of that plan. This path was exercised by the canonical quiesced run below; the envelope has separate bounded unit review evidence. Native state needs the same protection. File hashes detect damage but are not a cryptographic signature against an attacker who can rewrite the manifest and files together.

## Isolated restore

Create an empty directory on protected local storage, distinct from both the backup and any live service path. It must be owned by the invoking account and not group/world writable.

```sh
mkdir -m 700 /path/to/isolated-cwb-rehearsal
python scripts/backup-state.py restore \
  --backup /path/on/protected-storage/cwb-backup-YYYYMMDD \
  --target /path/to/isolated-cwb-rehearsal
```

The tool verifies safe relative paths, bounded content, file hashes/counts, SQLite integrity, reference coverage and closed attempt states before publication. Artifact/input storage references are remapped to the isolated target. All restored clients remain revoked, and no raw credentials or service configurations are installed. No job is launched. A populated target, symlink, unknown schema, missing referenced file, unexpected file or altered hash causes refusal.

`restore-receipt.json` records integrity checks, credential revocation, path remapping, unchanged attempt states and the new database hash. The original manifest remains provenance for the source bundle; the restored database differs because its paths and disabled client identities were transformed. Do not use the restored directory as another backup bundle: it has a restore receipt and transformed database rather than the original checksummed payload.

A real promotion requires a separate review of new credentials/client ownership recovery, platform configuration, encrypted storage, service permissions, workspace volume recreation, adapter state compatibility, provider authentication and a bounded canary. This command intentionally offers no `--start`, `--restore-credentials`, or destructive overwrite option.

## Verification and remaining gates

Tests exercise a populated synthetic v2 database, uncheckpointed WAL, binary artifacts, ready input, archived session, workspace Git metadata and native event data. They verify source credentials stay valid while backup/restore credentials are disabled, hashes survive roundtrip, absolute paths relocate, and missing files/tampering/traversal/links/nonempty targets/live attempts fail closed.

The synthetic local Mac rehearsal is recorded in p22-offline-restore-rehearsal.json (private historical record, not distributed), with 27 current passing tests (including credential capsule exclusions) and unchanged source physical/logical database hashes.

The canonical **actual Omarchy-to-Mac qualification** is the `p22-quiesced-*` receipt set, completed 2026-09-17 at approximately 20:07 UTC:

- Quiesced source backup (private historical record, not distributed) follows qualifier quiescence proof (private historical record, not distributed). The v2 API/worker were restored after the bounded backup window; legacy services were untouched.
- Encrypted SSH stream (private historical record, not distributed) produced an AES-256-GCM envelope on the Mac; authenticated decryption (private historical record, not distributed) preceded isolated restore. An independent source tar digest (private historical record, not distributed) matches the decrypted tar byte-for-byte (1,136,640 bytes). Receipts contain hashes and sizes, never key values.
- Isolated restore (private historical record, not distributed) verified 162 files, remapped storage paths, revoked the copied client and started no services.
- Independent checks (private historical record, not distributed) verified all 36 artifact hashes and 5 input hashes, SQLite integrity, 17 closed attempts (16 completed, 1 failed), and zero active restored clients. The failed attempt was an earlier synthetic preparation interruption, retained truthfully; it was not caused by backup or restore.

The earlier `p22-live-backup-create` attempt overlapped an active qualifier subtree and is **uncertain/superseded**, not canonical restore proof. Use only the quiesced receipt chain above for the off-host qualification claim.

This proves recovery of the selected logical state into an isolated directory. It does not prove a bootable replacement host, service activation, provider authentication/native conversation continuation, power/reboot/sleep/network recovery, or production cutover. Retention duration, keep rules, unattended scheduling and deletion remain separate operational decisions; this work neither chooses nor executes them.


## Envelope assumptions and review

The local seal/open command requires the `backup` dependency extra (`cryptography`, locked to 50.0.1). Its tests import that dependency directly; a missing install is an error rather than skipped encryption coverage. Nine focused tests passed with zero skips, including exact and over-limit inputs, wrong keys, ciphertext/tag corruption, truncated envelope, dangling symlink destination, stdin CLI execution, exact 32-byte key enforcement and no temporary residue after ordinary failures. See test receipt (private historical record, not distributed).

Use operator-owned private directories and a filesystem supporting hard links (such as APFS/ext4); no-clobber publication relies on a same-filesystem hard link. Decryption writes private mode-0600 temporary plaintext before tag verification, then publishes the named destination only after authentication. SIGKILL or power loss can leave a `.sealed-backup-*` private orphan; an orphan is never authenticated restore input and must not be promoted. Crash/power-loss cleanup is not qualified here.

Fable's one bounded envelope review (private historical record, not distributed) returned **REVISE**. The implementation agent independently addressed its minimum changes and recorded disposition (private historical record, not distributed); this is not a replacement Fable PASS or a whole-release verdict.
