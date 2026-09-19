> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Default root result selection

Default session ZIP and repository diff now select the latest session root. Schema1 uses generation ordering. After the scheduler migration, only `legacy` and `hermes_root` attempts participate, ordered by `root_sequence`; `hermes_child` attempts are excluded. A newer queued/running root does not fall back to an older terminal bundle. Diff selection requires a terminal selected root with delivery and likewise never borrows a child's patch.

`authorized_snapshot` retains its single authorized SQLite read transaction for root identity, artifacts, provenance and resource evidence. The pure bundle constructor and default diff handler use the same root-order semantics through `latest_root_attempt`. Missing or ambiguous root ordering fails closed. No new attempt-selector endpoint was introduced. Existing owner/project-authorized explicit child artifact downloads remain available.

Changed scope: `src/cloudworkbench/result_bundle.py`, the default diff import/handler in `src/cloudworkbench/api.py`, and `tests/test_root_result_selection.py`. No Store or scheduler code, deployment, real provider inference, credentials or service configuration was changed.

The fourteen focused cases cover legacy ordering; mixed legacy/root/child ordering independent of list position; missing/ambiguous roots; actual scheduler-created child admission; a completed child with an active root; owner denial; explicit child artifact access; a newer queued and then terminal root; a previously authorized bundle snapshot retaining its selected root; malformed projections; delivery metadata on a running root; actual schema1 migration/backfill; and exact root-sequence uniqueness. The later-root history fixture uses migrated SQL identity guards and does not claim to test a future scheduler follow-up admission API.

Exact review inputs are preserved in `evidence/root-result-review-snapshot/`, `evidence/root-result-review-binding.json` and `reviews/root-result-review-packet.md`. Local regression evidence is `evidence/root-result-selection-tests.xml`. Fable's bounded review and independent disposition are recorded separately under `reviews/root-result-selection-*`; they qualify only this patch, not the broader workflow release.

Review outcome: Fable **REVISE**, independently addressed in `reviews/root-result-selection-disposition.md`. The verified running-root diff exposure was reproduced and fixed; malformed helper inputs now yield controlled errors. A genuine scheduler-driven same-session follow-up remains outside the existing admission API and is not claimed by the history fixture. Final local checks:93 combined tests passed, then14 focused tests passed after strengthening the database uniqueness assertion. No subsequent independent PASS is claimed.
