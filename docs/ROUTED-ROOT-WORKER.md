> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Routed root worker — local controller checkpoint

Status: implemented local composition; production integration and review approval remain open. This checkpoint does not claim the full platform is complete or deployed. The earlier app snapshot reported BLOCKED, without a status update by this checkpoint. A subsequent fresh `get_goal` read now reports ACTIVE (`updatedAt=1789740863`); the historical blocked snapshot is superseded.

## Implemented contract

`RoutedRootWorker(scheduler, *, stage_factory, cleanup_verifier, delivery_root, forbidden_values).run_root(root_id, *, expected_generation, initial_revision, external_running=0)` consumes only a fresh qualified queued root. `RoleScheduler.admit_root` atomically changes queued to preparing while reserving the frozen accounts and budget; this is the exclusive claim, without an additional root file lock. Atomic admission now rechecks submit scope, session ownership/archive status and current authority after preflight. Two scope/archive races were reproduced before that correction and pass afterward.

The worker requires exact root input scope, frozen catalog/progression/contracts and the existing generation-bound delivery base. It creates a private controller broker capability limited to the frozen roles and root deadline. Each deterministic step obtains exact prior context and carried revision, admits and claims one child, and invokes the trusted stage factory. The factory owns any partial handles before returning; the worker retains returned handles on failure. Exceptions do not reset state, free unknown resources or retry inference. The exact broker grant is revoked on normal completion and attempted on exceptional exit, without replacing the original error.

`run_prepared_stage(...) -> CompletedStage(decision, carried_revision, release_receipt_json)` composes the existing driver, persisted observations, result publication/export, stage decision or qualified protected verification, and exact child release. Coding carries the authenticated candidate; readonly stages carry their input. The root worker independently loads historical decision and release evidence before advancing. A genuine nonpassing decision closes the authenticated stopped prefix; all passing stages reach frozen-base root delivery. Model prose cannot supply protected success. Infrastructure or unknown-cleanup errors leave explicit reconciliation work and held ownership.

Terminal replay authenticates saved delivery/stop evidence without provisioning or current account-owner lookup. Already-started roots are refused; this is not a recovery or controller-takeover loop. Stage task text currently has a 32 KiB broker preflight bound, smaller than the general request bound; oversized goals are refused before admission, never truncated.

## Evidence and limits

- `evidence/routed-root-worker-tests-binding.json`: 16 root-worker cases passed; real SQLite/controller composition with synthetic Docker/provider boundaries.
- `evidence/routed-stage-runner-binding.json`: six stage-runner cases passed, including actual local protected scripts and fixed six-stage composition. These component results overlap broader runs and are not summed.
- `evidence/routed-root-worker-integrated-tests.xml`: 82 cases, 81 passed and one retained SQLite 503 failure in remediation stale-base write validation. Both new admission race cases passed. `routed-root-worker-authority-race-red-tests.xml` preserves the two original failing regressions. No full-suite-green claim.
- `evidence/remediation-sqlite-diagnosis.md` and diagnostic receipts preserve the distinction between observed and inferred causes: an injected 60 ms delay produced SQLITE_INTERRUPT (9); 19 natural diagnostic cases passed; naturally observed SQLITE_BUSY (261) occurred only in concurrency coverage. The original suppressed SQLite errors remain unknown. No global timeout or transaction replay was added to conceal the failures.
- `evidence/root-worker-fable-source-binding.json` binds the submitted review source. `evidence/root-worker-fable-execution.json` records exit 1 after approximately 2.65 seconds; `reviews/root-worker-fable-receipt.txt` explicitly reports the unchanged account limit. The review produced no verdict or approval. Earlier limit evidence remains retained; no immediate repeat was attempted.

The parent performed a fresh read-only Omarchy check: hostname omarchy, SSH UID 1000, both v2 services active, with no service changes. That read-only service check alone does not qualify the new modules; the subsequent isolated Linux proof is recorded below. No real-provider execution, production migration, service restart or deployment occurred in this checkpoint.

## Remaining production work

A trusted production stage-runtime factory must compose root-only `RoutedBootstrap` provisioning, approved source/image/profile bindings, dedicated credential snapshots, attempt-scoped provider services and exact cleanup. The privileged adapter and its authorization/partial-provisioning recovery are not yet a production entry point. Existing daemon/API/readiness wiring must then consume qualified roots while preserving legacy jobs. New-controller recovery, target Linux/Docker/SQLite and exact provider/model/effort proof, review disposition, activation and the remaining SPEC acceptance gates are still required. Synthetic factory tests do not establish those capabilities.


Target Linux proof completed with retained bundle correction: on Omarchy as UID 1000,
Python 3.14.7 ran 82 cases; 81 passed and one failed solely because the uploaded
bundle omitted the historical migration fixture
`evidence/role-child-scheduler-review-snapshot/src/cloudworkbench/store.py`.
The exact fixture (SHA256
`db4241f96afa08cb9ec7628d07c6f2d9b7a51c3491cf1a82cb165a759dcc3022`)
was uploaded and that migration case passed 1/1 in 0.22 seconds. Thus all 82
distinct cases have passing receipts across the initial run and one corrective
run; there was no single green 82-case invocation. The original failure remains
retained, and the separate local integrated SQLite503 failure is not relabeled.

`evidence/routed-root-worker-linux-binding.json` and
`evidence/routed-root-worker-linux-receipt.json` bind 248 source files verified
before/after, host identity and unchanged active service PIDs: API 1240083,
worker 1222709, legacy cloudd 974255. Initial results are in
`evidence/routed-root-worker-linux-tests.xml` and `.log`; the correction is in
`evidence/routed-root-worker-linux-fixture-correction.json` and
`evidence/routed-root-worker-linux-migration-tests.xml` / `.log`.
The isolated directory `/tmp/cwb-root-worker-proof-7ZXXdq15` remains retained.
This is actual Linux SQLite and local protected-script composition with synthetic
Docker/provider boundaries, not actual Docker/provider execution or worker UID
959 qualification. No services, credentials or deployed application were changed.
Fable approval remains pending its unchanged account limit. The earlier BLOCKED app snapshot is superseded by a fresh ACTIVE `get_goal`
read (`updatedAt=1789740863`); no status update was called by this checkpoint.


Post-proof correction completed: a typed `StageExecution` is now retained
immediately after its type check, before validating options/services. Thus a
malformed trusted factory result cannot lose the returned cleanup handles. The
two new regressions first failed in
`evidence/routed-root-worker-handle-red-tests.xml` / `.log`.

The corrected local combined 26-case run recorded 22 passes and four stage-runner
failures (deadline/unavailable/caller_cleanup_unconfirmed); its root-worker subset
passed 20/20. The filename `evidence/routed-root-worker-handle-green-tests.xml`
(and `.log`) is historical naming, not an all-green assertion. Those failures
remain retained, and this checkpoint does not establish their original causes.

A fresh 248-file frozen bundle including the correction passed 26/26 on Omarchy,
UID 1000, Python 3.14.7. `evidence/routed-root-worker-handle-linux-binding.json`
and `evidence/routed-root-worker-handle-linux-receipt.json` bind the new source,
verified before/after, and unchanged service PIDs API 1240083, worker 1222709 and
legacy 974255. Exact results are in
`evidence/routed-root-worker-handle-linux-tests.xml` / `.log`; isolated directory
`/tmp/cwb-root-worker-proof-W3O0kaYH` is retained. This newer proof covers the
correction; it does not rewrite the earlier 82-case bundle or local failure
receipts. Linux SQLite and protected scripts were real; Docker/provider transport
remained synthetic. Worker UID 959, real providers, daemon integration and live
activation are not proven. The build remains incomplete, goal status ACTIVE,
with no deployment and no Fable verdict. Final parent checkpoint binding remains
separate from these already-existing source/test receipts.


Final checkpoint status: independent correction review is complete in
`reviews/routed-root-worker-independent-review.md`. This is not a Fable verdict.
The latest fresh goal read is ACTIVE (`updatedAt=1789741200`). Documentation is
frozen for the parent checkpoint binding; no further implementation is included.
