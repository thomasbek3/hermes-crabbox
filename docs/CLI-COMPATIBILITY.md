> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Opt-in v1 command adapter

`cloud-compat` accepts the familiar `run/status/log/file/ls/rm/wait` verbs and talks to the authenticated v2 API. It does not replace `cloud.sh`, change port7777, import old jobs, or copy legacy credentials. IDs are v2 session IDs; old IDs remain on the legacy service until an explicit import/mapping exists.

Create a non-secret configuration pointing to the existing private **named v2 client** credential:

```json
{
  "server": "https://worker.example.ts.net",
  "token_file": "~/.config/cloud2/token",
  "default": {"project_id": "sample-repo", "environment_version": "repo-v1"},
  "repositories": {
    "REGISTERED_REPOSITORY_URL": {
      "main": {"project_id": "sample-repo", "environment_version": "repo-v1"}
    }
  }
}
```

Each repository/ref pair maps to an operator-qualified immutable environment. A branch name is a configured alias for its pinned commit, not permission to fetch a changing remote ref. Unknown repositories/refs fail; no ad-hoc clone or extra scopes. `--repo` without `--branch` requires an explicit empty-string default-ref entry. `--provider` and `--name` currently return unsupported errors. Agent/model are submitted without silent substitution; server policy remains authoritative.

```sh
cloud-compat --config ~/.config/cloud2/compat.json run claude "Fix the registered sample"
cloud-compat --config ~/.config/cloud2/compat.json wait SESSION_ID
cloud-compat --config ~/.config/cloud2/compat.json status SESSION_ID
cloud-compat --config ~/.config/cloud2/compat.json log SESSION_ID
cloud-compat --config ~/.config/cloud2/compat.json file SESSION_ID booking.py
```

`run` prints only the new session ID. `status` and `ls` emit JSON; `log` emits structured public events rather than raw private provider transcripts. `file` writes hash-verified immutable artifact bytes for the latest attempt, including binary bytes. An unfinished follow-up cannot silently return the previous attempt's file. This preserves the command workflow, not byte-for-byte legacy response formatting.

`wait` has a deadline and returns nonzero on failed/cancelled/interrupted/paused/rejected/unknown states, protocol errors or timeout. Completed `unverified` or `needs_review` returns zero for execution completion; the printed outcome explicitly does not claim verification. Disconnect does not cancel work. `rm` requires `--confirm`, calls the distinct purge endpoint, and currently returns its explicit unsupported error; it never silently cancels or archives.

Qualification:21 focused HTTP tests cover mapped submit, literal prompt, idempotency, denied options, wait/error handling, pagination, credential redaction and binary latest-attempt download. `evidence/compat-live-corrected.json` proves status/list/log/wait/file against the real verified repository session on Omarchy, with file SHA matching the API artifact. Corrected qualification used HTTPS and the the original operator cloud2 named principal; before/after session/attempt/event/artifact counts and legacy PID matched, including confirmed purge returning its unsupported error. This is bounded observational evidence, not a guarantee against unrelated concurrent operations. No token migration occurred. A full legacy principal/import/cutover rehearsal remains open.
