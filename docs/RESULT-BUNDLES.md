> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Result bundles

This is a next-checkpoint source feature. It is not part of the frozen auth/retention rollout snapshot and has not been deployed by this unit.

`GET /v1/sessions/{id}/bundle` requires **observe and retrieve**, plus the existing owner/project authorization. The endpoint snapshots the latest attempt, that attempt's registered artifacts, and its adapter provenance event in one SQLite read transaction. The latest attempt must be terminal; a newer queued or running attempt causes 409 instead of silently returning an older result. A follow-up arriving after the snapshot does not change the pinned response attempt. `X-Cloud-Session-Id` and `X-Cloud-Attempt-Id` identify the snapshot.

The response is `application/zip`, an attachment, with exact Content-Length and a SHA256 ETag. All registered file bytes are opened descriptor-relatively with nofollow checks, checked for regular/single-link identity, stable stat metadata, exact bytes and SHA256 **before any archive is returned**. A missing, changed, hardlinked, symlinked, special or out-of-root file rejects the entire bundle. No partial ZIP is sent. Metadata and names are validated; duplicate or file/directory-conflicting archive names are refused.

Limits are 16 MiB for the complete ZIP, including generated metadata and ZIP headers, and 1,000 total files including the four generated documents. ZIP_STORED avoids compression work and expansion ambiguity. Each archive entry has regular-file mode 0600; executable intent can be present in the artifact manifest but the archive never sets executable bits. Larger results remain available through individual authenticated artifact downloads; the bundle does not silently omit oversized files.

The archive contains:

- `result.json`: authoritative session/attempt/generation/state/outcome/reason, public summary, verification, delivery/environment/image identity, continuation mode, unresolved criteria/issues, timing, resource limits and explicit usage/cost/provenance availability.
- `summary.md`: state/outcome first, then the public summary and unresolved issues. This is untrusted downloaded content, never HTML rendered under the dashboard origin.
- `artifact-manifest.json`: registered artifact ids, relative names, bytes, SHA256, media types and executable intent. No controller storage paths.
- `verification.json`: controller-recorded check receipts, attempt/generation/outcome and the bundled artifact-manifest hash. This receipt indexes existing verification evidence; it does not run checks again or manufacture an exact-target proof absent from the original check/delivery records.
- `files/<original artifact path>`: exact registered immutable bytes for this attempt, including `files/@delivery/changes.patch` when that artifact was actually produced. No replacement patch is synthesized during download.

Controller outcome always takes precedence over a contradictory result-body claim. Failed, cancelled, interrupted, paused and unverified attempts can produce a truthful diagnostic bundle if their latest attempt is terminal. Missing verification receipts and incomplete text patches remain visible; no download turns them into verified work.

Requested model, provider-reported resolved model, observed CLI version and environment-declared expected CLI version are separate values with explicit unknown reasons. Adapter event provenance is labeled worker-reported. Usage is `reported` only when a nonempty usage object exists; otherwise it is null/unknown with a reason. Configured limits remain separate from measurements. Local integration now reads reserved controller resource events from the terminal snapshot: retained observed execution/verifier windows, explicit counts and approximate memory semantics. No events means measured usage is null with a reason. Sidecars, true lifetime peaks and billing are not inferred; this later integration is not live-qualified. Actual billed cost stays null with its reason; a provider-reported USD estimate, if present, is separately labeled as unverified billing. The attempt `started_at` is the controller preparing/claim transition, not execution start. Queue, preparation, running-to-terminal, execution and verification durations use their respective controller transition timestamps (the running event is snapshotted with the result) and are derived only from available valid timestamps; missing/invalid timestamps produce null and a reason.

Only already-exported artifacts and public controller result metadata enter the archive. Raw logs, supplied inputs, unexported workspace files, native state, credential snapshots and provider auth roots are never traversed. Sensitive metadata fields such as storage paths, script-source paths, token hashes, secret references and credential fingerprints are omitted. This is not a new general-purpose unknown-secret detector; upstream public-event redaction and immutable export validation remain required.

## CLI

```sh
cloud2 --server https://your-private-workbench --token-file /path/to/private-token \
  bundle SESSION_ID /path/to/new-result.zip
```

The CLI requires ZIP media type, bounded Content-Length and a SHA256 ETag for a bundle, checks the complete download, and publishes a private temporary file atomically without overwriting an existing destination or symlink. It refuses redirects and never prints the bearer value. It does not extract or execute the archive. Successful exit means the transfer and integrity checks passed; inspect `result.json` for the job outcome, just as an artifact download does not declare the job successful. Existing `download ARTIFACT_ID DESTINATION` and compatibility commands retain their behavior.

Verification evidence is in `evidence/result-bundle-integration-tests.xml`. The bounded Fable review and disposition are recorded separately. No UI, resource collection, external publication or live rollout is included in this unit.

## Review disposition and live qualification

Fable returned **REVISE** on the initial source snapshot. Independent validation and fixes are recorded in `reviews/result-bundle-disposition.md`; the original verdict is preserved. Metadata uses explicit projections for repository, environment, delivery, export and check receipts. Repository URLs, commands, environment variables, unknown nested fields and check stdout/stderr are omitted; raw export/verifier exception text is replaced by a generic diagnostic reference. Public summaries and file contents retain their upstream redaction boundary. Portable names are compared after NFC normalization and case-folding, including parent/file conflicts. Requested/reported model differences are flagged as an unverified alias relationship, not automatically a wrong model; CLI version differences are explicit.

Paused is a terminal **attempt**, not a finished user task. The controller refuses any later transition on that attempt, and resume creates a new attempt/generation. The bundle preserves paused state and its unverified diagnostic outcome. Terminal `updated_at` remains unchanged on later artifact registration, event creation or session messages. A snapshot already captured before resume remains pinned to the old attempt; the next request sees the new attempt and refuses while it is nonterminal.

The ETag checks transport consistency with this authenticated server; it is not an independent signature. Bundle transfer rejects weak ETags and non-identity Content-Encoding. Private atomic publication requires a filesystem supporting hard links; exFAT or incompatible network mounts fail without publishing. Use a local APFS/ext4 destination. Per-request bytes/files are bounded, but this unit does not qualify concurrent memory load or add a shared admission limiter/cache.

`./scripts/qualify-result-bundle.py` is **prepared, not run against the live host**. After the parent deploys this checkpoint, select a quiescent, latest-terminal qualification session and three already-configured private credentials: its owner, a different owner with observe/retrieve, and a principal whose allowed projects exclude the selected session (also with observe/retrieve). Independently verify those client records; a 404 alone does not establish why access was denied. The script never creates credentials, sessions or follow-ups, and makes GET requests only. Do not point it at the currently frozen auth rollout before the bundle endpoint is deployed.

```sh
.venv/bin/python scripts/qualify-result-bundle.py \
  --server https://worker.example.ts.net \
  --token-file /private/owner-token \
  --other-owner-token-file /private/other-owner-token \
  --excluded-project-token-file /private/project-excluded-token \
  --session-id TERMINAL_QUALIFICATION_SESSION \
  --output-dir /private/new-bundle-qualification
```

It requires HTTPS and a new output directory; checks attachment, exact length, SHA256 ETag and attempt/session headers; parses all bounded ZIP entries; verifies artifact bytes/hashes and manifest binding; requires both access-denial cases to return 404; compares the actual CLI download byte-for-byte; and confirms a second CLI publication cannot overwrite it or leave staging files. It writes private `result.zip` and `receipt.json` only on success. If another attempt or artifact arrives during its multi-request run it refuses determinism rather than claiming a pinned cross-request result. Concurrent follow-up safety is covered by the local Store/API regression; the live qualifier intentionally does not mutate existing work to reproduce it. No successful live receipt exists for this checkpoint yet.

Resource history query failures return retryable 503 instead of changing a successful terminal ZIP to contain transient unknown metrics. Malformed persisted history remains explicitly unknown for its affected role.
