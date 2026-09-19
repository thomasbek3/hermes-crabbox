> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

> 2026-09-18 job budget update: Omarchy native Hermes now allows two hours / 1,000 model steps per submitted job or follow-up; capture7,320s, Hermes supervisor7,500s. No automatic parent checks or test jobs. See BASIC-DELEGATION-STATUS.md and evidence/job-budget-deployment-20260918.json.

# Current build status

Latest web update, 2026-09-18: Internet + Chromium/Playwright profile `hermes-tasks-web-v2` is deployed and both Mac callers default to it. Workspace8GiB; tool memory3GiB; max3 tasks. It is explicitly operator approved but unqualified; no new test/browser job ran. See `BASIC-DELEGATION-STATUS.md` for current handoff and remaining original-spec scope.

Latest update, 2026-09-18: the original operator resumed the simplified basic-delegation build.
Native Hermes is now enabled in the persistent Omarchy API/worker; the generic
`hermes-tasks/hermes-tasks-v1` project and three-task queue configuration are
deployed. Both Mac minis have the new `omarchy-cloud` caller tool/skill and private
caller tokens; Muse has a profile skill alias. No new test jobs or reviews were
run. The full original spec and multi-model workflow remain incomplete. See
`BASIC-DELEGATION-STATUS.md` for current scope, deployment receipts and limits.
Statements about undeployed native Hermes below are historical snapshots.

the original operator stopped further verification and checkpoint-review work on 2026-09-18.
Implementation remains authorized; unfinished checks stay explicitly unverified.
Do not launch the prepared deployment acceptance or provider qualification runs.

Updated 2026-09-18. A real Hermes+pstack job and exact-session follow-up now pass on Omarchy. The private cloud-agent platform remains in development: the new routed controller is not deployed, and the complete multi-model workflow has not passed acceptance.

## Normal Hermes API integration — isolated live proof passed

The goal remains active. The controller now gates native Hermes submission on
explicit enablement and its qualified Grok model, preserves the exact successful
native session for follow-up, and refuses an implicit fresh session after an
incomplete prior attempt. Runner uses a dedicated Hermes coordinator facade while
keeping verification on the ordinary offline, read-only runtime. These source
changes have not been deployed to the existing services.

The isolated HTTP API on Omarchy completed a real job and follow-up, both with
protected verification, artifact download/hash checks and independent offline
checks (14 first-turn cases, 28 follow-up cases). Both turns used native session
`20260918_151018_79c714`. Three running tool containers were observed with the
expected offline mounts, identities and resource limits. No containers or active
attempts remained; the temporary API exited. Evidence and downloaded output are
in `evidence/hermes-api-live-passed/`, bound to
`evidence/hermes-api-live-source-v2.json`. Existing API/worker/cloudd PIDs remained
1240083/1222709/974255. The deployed API has not yet been enabled for Hermes.

The affected parent suites passed 239 tests plus 52 existing API/auth/event tests.
Subsequent environment-policy and resource-limit changes passed their focused
suites. The final Docker metadata correction passed 67 facade tests and the real
API proof above. The initial live check stopped on capability-name normalization
and Hermes' private read-only skills mount; its failed receipt is retained rather
than relabeled as passing. Exact stopped containers were removed and its attempt
was cancelled after inspection.

Grok 4.6/xhigh returned a scoped PASS on the corrected review packet
(`reviews/hermes-api-grok-corrective-report.md`). The earlier truncated review is
retained as partial. The small later CAP_ / skills metadata correction has separate
tests and actual live proof; it was not in that Grok snapshot. Fable was attempted
again for this checkpoint and returned an account-limit error with no verdict
(`reviews/hermes-api-fable-review.stderr`). the original operator subsequently replaced Fable
checkpoint reviews with the Grok skill, using Grok 4.6/xhigh. Fable is no longer a
checkpoint gate. The copied environment registry passed both offline qualification
probes, preserving existing rows and active versions. The frozen activation script
passed 22 focused tests and live validate-only on Omarchy (stage=validated;
passed=false because execution has not occurred). Its Grok activation review is
running. Next: resolve its concrete findings, then enable Hermes alongside Claude
with the prepared backups and idle checks. Existing services remain unchanged.

## First real Hermes job and follow-up — passed

Hermes 0.21.3 with `pstack:tdd`, init-reported Grok 4.6 and configured xhigh created
`status_summary.py`, tests and a README on Omarchy. The first job passed 16
model-run tests and 13 independent cases in offline read-only Docker. Its follow-up
resumed the exact native session `20260918_143228_4e6beb`, added `--strict`, and
passed 32 model-run tests plus 23 parent-run independent cases. These are separate
first/follow-up checks, not 36 unique acceptance cases.

Safe transcripts, output files and receipts are retained under
`evidence/first-hermes-live/first/` and `evidence/first-hermes-live/followup/`.
Both CLI/coordinator exits were zero; the final receipt reports no remaining
coordinator/tool containers and unchanged API/worker/legacy service PIDs. The
workspace remains at `/var/lib/cloud-workbench/first-hermes-live/job-039d591ebbda`.

This proves real native Hermes+pstack execution and continuation with one model.
It is separate from the routed root-worker's synthetic provider tests, and does
not prove the six-stage/multiple-model pipeline or deployment. The separate normal
API proof is recorded above; deployed UI/API acceptance remains pending.
The tested foundation is reusable, but too much controller machinery preceded
this basic journey. Next work should connect the proven journey, not expand
abstractions. Full scope remains incomplete and active; checkpoint reviews now
use Grok 4.6/xhigh under the operator's explicit replacement instruction.

## Locked workflow

Fable max plans → Astra high reviews the plan adversarially → Fable max finalizes it → Grok 4.6 xhigh implements → Astra high reviews correctness and edge cases → Sol max verifies acceptance. Jev selects a predefined Pstack workflow; it does not select arbitrary models, effort or authority. Explicit workflow selection bypasses Jev. The separate Mini Codex Sol/high lane is unchanged.

## Verified

- Jev client, twelve initial workflow selections, probability/confidence records, abstention and frozen role/effort policy are implemented. Three corrected live synthetic TypeSafe requests selected feature, plan review and code review. The earlier five attempts failed. Both test allowances are exhausted; no further Jev requests are authorized.
- Scheduler root/child admission, owner fencing, stage order, immutable input revisions and aggregate cleanup have local regression coverage. Grok 4.6/xhigh reviewed the scheduler/account-cleanup subset and returned BLOCK. Confirmed findings were corrected: account fences now release atomically with root capacity, and late runtime binding cannot change frozen cleanup identity. The original partial review is not a full integration approval.
- Native Responses adapters and the pinned Hermes CLI completed actual file-tool round trips using synthetic responses for Astra/high, Sol/max and Grok/xhigh. On Omarchy, three isolated routed caller containers made six synthetic requests. UID separation, filesystem/process restrictions, resource settings and exact stop/remove cleanup passed. Existing service process IDs were unchanged; test containers and temporary caller directories were removed. Evidence: `evidence/routed-caller-linux-v5.json`.
- Dedicated Codex and Grok cloud CLI logins exist on Omarchy. Personal credentials were not copied. Login success does not establish inference entitlement or provider model support.
- Per-request credential snapshots compose with ProviderDispatch and ProviderDocker, including exact lease/request binding, recovery without a credential file, and retryable cleanup after durable physical release. Fable returned REVISE; independently confirmed directory-open cleanup, group precondition and owner-cleanup reporting issues were corrected. Unknown or replaced material is retained for recovery.
- The previous credential checkpoint had **606 passing tests** (`evidence/routed-auth-current-tests.xml`). The subsequent corrected driver checkpoint has **459 passing coordinated tests** across its affected scheduler, driver, collector, runtime, revision, dispatch and lifecycle suites (`evidence/routed-driver-corrected-integration-tests.xml`). These counts describe distinct source checkpoints, not one combined current full-suite result. No external re-review PASS is claimed.

## Single-child driver checkpoint — passed in an isolated fixture

The trusted driver now durably binds the exact child and runtime before start, checks authority throughout execution, collects bounded observations and fences inference before caller removal. Fable returned REVISE; independently confirmed issues were corrected, including durable observation storage, shorter output deadlines, input revalidation, bounded database waits and real reader integration tests. The review and corrective disposition remain in `reviews/routed-driver-fable-review.txt` and `reviews/routed-driver-fable-disposition.md`.

That checkpoint’s frozen source passed actual Omarchy Docker execution with the real scheduler, driver, Hermes file tool and collector. Two synthetic Responses calls completed; result/event hashes still matched after the caller was removed. The exact fixture directory was then removed and existing service PIDs remained unchanged. Evidence: `evidence/routed-driver-linux-20260918-v2.json` and `evidence/routed-driver-linux-temp-cleanup.json`.

This used a root-provisioned fixture for one Astra/high profile. It does not qualify production worker 959 provisioning or real model inference. Provider cleanup qualification, protected verification and seat release remained false; the isolated scheduler retained root/child occupancy. No production activation occurred.

## Worker provisioning and combined cleanup checkpoint

The root-only bootstrap primitive and shared stage-planning interface are implemented. Actual Omarchy workerUID959/GID960 with supplementary959/966 successfully read provisioned files, materialized a GID1000 stage and created the GID1001 relay socket. UID1001 completed a synthetic request; UID1000 could read task/source and write scratch but could not read relay credentials or write readonly source. WorkerService shut down its handler/listener/journal, all fixture children exited, and both fixture and transfer directories were removed. Existing group memberships and service PIDs were unchanged. Evidence: `evidence/routed-worker-bootstrap-linux-v2.json` and its transfer receipt.

`RoutedChildCleanup` now combines caller removal, bounded worker drain, supervisor quiescence, exact child grant/request membership, provider physical cleanup and credential-material cleanup. It produces durable atomic evidence without releasing shared root account reservations. Local tests exercise two grants/services, concurrent release and interrupted evidence publication. Root-only authorization/IPC and complete worker orchestration still need integration; the permission proof used scoped directory descriptors, not actual Docker bind mounts.

Fable returned REVISE. Confirmed receipt-publication, discard-recovery, stale-target, material-hook and error-preservation findings were corrected. A real supervisor timeout test confirmed that quarantine retains ownership and prevents unsafe reuse, so that protection remains. Review dispositions and the two cancellation-order test corrections are preserved in `reviews/routed-worker-fable-disposition.md` and `reviews/supervised-recovery-race-disposition.md`. No external re-review PASS is claimed.

Final coordinated verification: **413 passed in 17.40 seconds**, covering the corrected worker/cleanup/bootstrap/stage/driver/provider and qualification suites (`evidence/routed-worker-corrected-integration-tests-v3.xml`). Earlier failing runs are preserved with their deterministic test corrections. Linux atomic evidence publication also passed separately (`evidence/child-cleanup-publication-linux.json`). The current source, reviews and proof receipts are bound in `evidence/routed-worker-checkpoint-final-binding.json`.

## Linux credential checkpoint — passed

The corrected synthetic snapshot qualification passed on actual Omarchy. ProviderUID958 could not read the original UID959,0600 synthetic credential, but could read its exact UID959:GID959,0440 request snapshot through a read-only mount. Writes and chmod were denied. Four requests proved normal release cleanup, recovery with a missing snapshot file, old-owner material cleanup and preservation of the newer owner's request. Original source bytes/permissions were unchanged until fixture removal.

Evidence: `evidence/snapshot-linux-v5-20260918.json`, independently validated against its frozen harness and current source hashes. All test containers, networks, derived image and temporary base alias were removed. The base image and its original tag were preserved. All three existing service process IDs were unchanged. The three exact temporary fixture directories were then removed with evidence in `evidence/snapshot-linux-temp-cleanup.json`.

The earlier build failure and boolean-result harness defect are preserved as failed receipts. The harness-only correction has **57 passing focused tests**; production auth/runtime modules are unchanged from the606-test coordinated run. This establishes synthetic Linux credential/mount/lifecycle behavior, not real provider authentication, inference, refresh or full root-controller integration.

## Result retention and publication checkpoint

The driver now saves exact validated result/event bytes to a private controller snapshot before caller teardown. The snapshot can be reloaded under the same valid controller authority after caller removal. This is durable byte retention, not takeover by a new controller identity. Actual Omarchy scheduler/driver/Hermes execution proved the snapshot existed before stop and matched after removal. The exact synthetic fixture was removed; API/worker/cloudd process IDs remained unchanged. Evidence: `evidence/routed-driver-observation-linux.json` and `evidence/routed-driver-observation-linux-cleanup.json`.

Bounded stopped-workspace export, durable export reload, authorized observation publication and immutable candidate capture are implemented. Publication rechecks current authority and exact combined cleanup in its final transaction. Worker observations and candidates remain explicitly unverified; they cannot pass workflow gates or promote a delivered revision. The coordinated local suite passed **558 tests in 38.88 seconds** (`evidence/routed-result-durable-integration-tests.xml`). This is an affected-suite result, not full-platform acceptance.

Actual UID959/UID1000 export qualification now passes: private0600 tool files were exported through the pinned networkless collector, then restored under a fresh runtime view with no create/start calls. Source hashes, exact cleanup and unchanged live service PIDs were verified (`evidence/routed-export-linux-20260918-v3.json` and its transfer receipt). The first two failed runs are preserved: their fixture inherited a 512 MiB virtual-address limit incompatible with the Docker Go CLI. A read-only same-UID probe confirmed the cause; only the harness limit changed to 4 GiB, with53 harness tests passing. No application change was needed. Fable returned its account usage-limit error on the initial review and one bounded retry, so no Fable review verdict exists for this checkpoint. At the operator's request, Grok 4.6/xhigh completed a read-only partial review and returned REVISE. It reported missing packet context, so this is not full-platform sign-off. Independent inspection confirms an activation recovery gap: complete worker orchestration must mandate exact reconciliation after uncertain export operations. Its candidate replay concern was disproved by content-addressed capture and77 passing focused tests. That review also identified the missing controller-known-secret predicate before private snapshot persistence. The following correction checkpoint addresses this and result-phase reconciliation; production root/startup composition remains pending. Resetting uncertain journals or treating unexplained disappearance as successful removal is rejected. Full findings/disposition: reviews/routed-result-grok.md and reviews/routed-result-grok-disposition.md.

## Result recovery and secret-policy correction — local checks passed

The exporter now performs bounded exact reconciliation after an owned failure, retaining uncertain journals and never resetting or relaunching. Its durable status distinguishes aborted, recoverable and ready exports. The driver/snapshot/publication paths share a bounded controller-known-secret policy that scans raw bytes and decoded JSON strings before snapshot creation; recovery rechecks current policy. The policy must be supplied by production orchestration, and stricter recovery does not delete historical snapshots.

`publish_child_results` now connects combined cleanup, pending export reconciliation, disk snapshot reload, unverified observation publication and candidate capture. Cancellation still permits exact physical cleanup. Retries reuse the export and refuse changed selection; no result path changes outcomes, gates or scheduler occupancy. Current coordinated local verification: **507 affected-code tests passed in 21.55 seconds** (`evidence/routed-results-corrected-integration-tests.xml`); source is frozen in `evidence/routed-results-local-binding.json`. The mixed real-Docker/simulated-provider Linux composition proof subsequently passed against the corrected source, as recorded below. Previous Linux receipts remain historical to their source hashes.

The first composed Linux run completed its worker but failed a harness-only observation ID check; that check now accepts the exact prefixed observation ID and enforces its hash mapping. A subsequent run lost authority during a provider request. A separate deterministic reproduction found that a brief concurrent SQLite writer caused immediate control failure. The scheduler now retries only lock acquisition before the transaction body, sharing its original 50 ms deadline. It never replays a body, commit or launch. **562 affected-code tests pass in 28.38 seconds** (`evidence/routed-results-contention-integration-tests.xml`). Both unsuccessful Linux receipts and exact fixture cleanup receipts are preserved. Current-source Linux v3 qualification passed for both composed results and pre-snapshot secret refusal. Real caller/Hermes and export containers were used with simulated provider execution and cleanup. Publication/replay produced two stable artifacts with one export; a stricter recovery policy refused the saved observation. A fresh secret-bearing run created no controller snapshot, artifact or export and removed its caller. Both fixtures were removed after exact receipt-bound readback; service PIDs and the pinned image were unchanged. Evidence: `evidence/routed-results-linux-20260918-v3.json`, `evidence/routed-secret-refusal-linux-20260918-v3.json`, and their cleanup receipts. The final harness has **94 passing tests**. These are root-provisioned component proofs; they do not qualify production worker 959 provisioning, physical provider cleanup, real inference, protected verification or a complete coding job.

Fable's review is still pending because the previous attempt and bounded retry hit the account limit. No substitute approval or activation is claimed. See `ROUTED-RESULTS.md` and `ROUTED-PROTECTED-DECISION-NEXT.md` for the implemented result boundary and next protected-decision work.

## Before a usable multi-model coding job

1. Complete credential refresh/recovery integration. The owner material-cleanup helper still needs root-orchestrator wiring.
2. Compose the implemented planning/bootstrap/driver/WorkerService/combined-cleanup components through production root authorization and private journal placement. Implement protected outcome verification and produced-revision promotion.
3. Compose the qualified tool-created0600 workspace export and durable observation/candidate publication into the production worker. Add protected terminal acceptance, output decisions and promotion; the implemented export/publication components do not supply those decisions.
4. Qualify the exact real provider routes under their account, entitlement and billing permissions; preserve the selected models and effort.
5. Run and verify the full six-stage coding workflow, including its returned diff, evidence and follow-up.
6. Review a concrete migration/deployment candidate that preserves current jobs before activation.

## Remaining product scope

The browser/dashboard/preview/takeover/PR workbench, remaining provider/MCP/notification routes, and applicable recovery/backup acceptance gates remain in `SPEC.md` and `ACCEPTANCE-MATRIX.md`. Existing direct Claude jobs are separate from acceptance of the new multi-model workflow.

No production service restart, migration or routed-controller activation occurred during this checkpoint. Historical receipts and unsuccessful review/test results remain preserved.

## Atomic root delivery local checkpoint

Final local delivery/controller checks pass:154 integrated plus10 historical
cleanup and4 report-only publication cases. Root publication and resource release
now commit together; cancellation publishes nothing; saved success/cancellation
replay authenticates the original cleanup proof without invoking runtime work.
Historical task/context evidence readers and full delivery proof validation were
also strengthened after the requested Grok review. See ROOT-DELIVERY-NEXT.md and
CHECKPOINTS.md for exact receipts, retained failures, and current source binding.
This advances the candidate only. Production root-worker wiring, follow-up import,
remediation, new-controller recovery and current-source Linux/real-model acceptance
remain unfinished. No deployment or Fable approval is claimed.

## Stopped workflows local checkpoint

Qualified rejected reviews and failed/missing acceptance now have a durable stop
result plus authenticated cleanup/replay.168 focused checks pass; current delivery
regressions also pass. No workspace is promoted by this path. See
ROUTED-STOP-CHECKPOINT.md for the tested boundary and WORKFLOW-REMEDIATION-NEXT.md
for the subsequently implemented local correction boundary described below. No remote
activation, real-provider qualification or Fable approval is claimed.


## Explicit remediation local candidate — review pending

The formerly unimplemented correction composition now admits one explicit fresh
root in the same session, preserving original scope/acceptance and delivery base.
Fixed internal repair catalogs, exact revision rebinding, a bounded16 KiB findings
sidecar and inherited deadline/remaining requests are connected through compute
admission. Local synthetic-runtime composition reaches repair → independent
review → protected acceptance → delivery, or a new stopped result without a
second correction. Historical gates and rejected evidence remain intact.

Current parent-final run:166 passed, one fixture-preparation child-control503
failure out of167; one isolated scope retest passed without establishing the
original cause. Earlier16 composition,108 revision and89 input/context suites
passed; catalog228 passed/42 shared-fixture errors plus52 diagnostic passes are
preserved. Counts overlap; the current broad suite is not entirely green.
Grok4.6/xhigh review is running without a verdict, and Fable remains pending its
account limit. No production or real-model acceptance is claimed. See
ROUTED-REMEDIATION-CHECKPOINT.md and WORKFLOW-REMEDIATION-NEXT.md for implemented
APIs, evidence and remaining product/worker/recovery/activation work.


Post-review update: Grok4.6/xhigh completed with REVISE. Independent source
checks confirmed one missing-root error-handling defect, now fixed; four findings
were unsupported by the actual call paths. The correction run recorded17 passes
and two retained SQLite503 failures during source preparation. The new regression
passed; the suite is not globally green. See
`reviews/remediation-grok-disposition.md` and
`evidence/routed-remediation-final-binding.json`. No deployment or Fable approval.


## 2026-09-18 routed root worker — local composition, review pending

New `routed_root_worker.py` and `routed_stage_runner.py` connect fresh queued
qualified roots to fixed child stages, driver/results, protected decisions,
independent historical release checks, and stop or atomic delivery. Queued
admission is the exclusive database claim; already-started roots require explicit
reconciliation. Partial handles and unknown cleanup remain held, with no inference
retry or automatic remediation. Terminal replay performs no current-owner lookup.

Component receipts record 16 root-worker and 6 stage-runner passes. The latest
integrated 82 cases recorded 81 passes and one retained SQLite 503 remediation
stale-base write-validation failure; two newly reproduced scope/archive admission
races pass after the atomic guard correction. Counts overlap. Original SQLite
failure causes remain unproven; injected interruption and successful diagnostic
runs do not relabel the failing suite. See `ROUTED-ROOT-WORKER.md` for exact receipts.

The new Fable checkpoint attempt ended at the unchanged account limit after
approximately 2.65 seconds, exit 1, with no verdict. Review remains pending; no
approval, full-suite-green or deployment claim. A fresh read-only Omarchy check
reported hostname omarchy / UID 1000 and both v2 services active without changes.
The earlier app goal snapshot was BLOCKED, not set by this checkpoint. A fresh
`get_goal` read now reports ACTIVE (`updatedAt=1789740863`); work continues. Production factory
and privileged adapter, daemon/API wiring, recovery, target real-provider proof,
activation and full SPEC acceptance remain open.


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
