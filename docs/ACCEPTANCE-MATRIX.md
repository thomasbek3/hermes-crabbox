> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Acceptance evidence and remaining gates

Historical infrastructure audit after the 2026-09-17 auth/retention/image deployment. Current correction: no direct-Claude receipt below satisfies Hermes H01–H08; the normative full-spec contract is SPEC-FIRST-CONTRACT.md. Requirements are SPEC.md sections16–17. The frozen candidate passed541 tests; subsequent bounded build/resume and Linux private-directory corrections passed14 driver and101 affected tests. Auth rollout and three real provider attempts passed with parent readback. P15 bundle/API/default UI was subsequently deployed (p15-api-live-binding.json, p15-live-browser.json); dashboard input UI and resource integration remain undeployed. Historical receipts retain their exact source/image bindings.

| Gate | Evidence inspected | Remaining acceptance work |
|---|---|---|
| T01 admission/concurrency | Store tests, `cp2-final-lifecycle-bound.json`; unique live session/account constraints and capacity logic | Include candidate's remote suite and confirm no reservations after its qualification. No new claim about multi-worker fencing. |
| T02 HTTP retry/idempotency | API/Store tests, `cp2-final-http-bound.json`, compatibility literal/idempotency tests | Preserve these checks in the deployed candidate; arbitrary caller-generated new keys are not retries. |
| T03 crash boundaries | `cp2-final-lifecycle-bound.json` create/start/lost-response/restart; `cp3-corrective-live-recovery.json` after export and verification intent | Terminal-commit gap reproduced and corrected locally; 65 affected tests and actual Omarchy process-exit probes before/after commit pass in `completion-recovery-live.json`. Fable REVISE findings corrected, rollout completed; `completion-recovery-deployed-delta.diff` records the only post-crash-proof changes (private-directory mode acceptance). |
| T04 injection | Immutable Git snapshots, registered repo/ref mapping, literal prompt/model validation tests; compatibility unknown repo rejection | General remote cloning is unsupported. Keep it disabled; no ad-hoc repository path or shell interpolation. |
| T05 auth contention | Real dedicated Claude inference, corrected auth unit container proof, runner auth tests | Deployed and integrated proof passed: `auth-live-qualification-binding.json`. Actual CLI401 capture/classifier replay passed. Old pinned408eb lacks new structured classification; no automatic refresh claim. |
| T06 continuation/input/permissions | Real workspace follow-ups, `permission_unsupported` runner test, adapter capabilities | Native conversation resume and interactive input callbacks unsupported. Verify live old/new image follow-ups; don't claim native memory restoration or an approval executor. |
| T07 resource exhaustion | `cp2-final-runtime-qualification.json` hard ENOSPC; `release-fault-qualification.json` memory/PID/log bound and sibling checks | Physical Docker rotation footprint not measured; resource sampler is separate work. Do not convert observed maxima into true peak claims. |
| T08 isolation | Runtime qualification no-network/gateway/private-address checks; fault matrix other-session denial | Exact positive allowlist is scoped; other-session IPv6 not tested in current IPv4 network profile. General browser/preview networks remain disabled. |
| T09 credential isolation | `claude-native-secret-scan.json`, synthetic canary receipt, two-session native isolation, corrected auth unit | Three actual jobs ended with zero staged token files (`auth-live-parent-check.json`); private quarantine/capsules remain excluded from exports/backup. |
| T10 artifact safety | Artifact/API tests for links, traversal, bounds, binary and attachment handling; actual SHA-matched downloads | P15 live API/CLI/browser ZIP proof is recorded in p15-api-live-binding.json; Hermes results must repeat applicable export/provenance gates. |
| T11 cancellation | CP2 cancel; CP3 `cancel_partial`; verifier process-group timeout tests | Explicit clone/test/tool descendant phase mapping remains to be completed. No live network clone capability to claim. |
| T12 takeover | State enum only; no implemented write lease/barrier | ReleaseB implementation and race/disconnect/stale-token proofs required. |
| T13 independent verification | Real known-broken booking task fixed/verified; protected verifier rejection/infrastructure tests | Preserve verifier image pinning and script hashes in next rollout. A model's report is not verification. |
| T14 preview isolation | No preview routing yet | ReleaseB per-session hostnames, HTTP/WS auth and revocation, cross-session replay tests required. |
| T15 approvals/PR | Reserved unsupported endpoints | ReleaseB digest-bound executor, expiry/single-use/remote reconciliation and actual authorized draft PR required. |
| T16 recovery/restore | Supervisor restart; real SSE reconnect; encrypted quiesced off-host restore; synthetic true volume remount | Physical reboot, AC/lid/sleep and host network outage remain. LUKS physical unlock must be arranged before reboot. Replacement-host activation is not proven by isolated file restore. |
| T17 headless | `repository-provider-smoke.json`: real fix, protected checks, patch apply/hash equality, follow-up and SSE | Corrected candidate live proof passed, including patch hash/mode application and old/new image follow-ups. Full browser benchmark and owner preview belong to B and remain open. |
| T18 supplied-input document | `claude-document-smoke.json` + independent content rubric | Bounded supplied-input case proven. Service's unverified software outcome and independent document content review are distinct. |
| T18R research | No research profile | Later optional expansion; disabled. No open-web sourcing claim. |
| T19 isolated reaper failures | Runner tests, CP2 failed create/start/inspect paths and stopped-worker readiness | Carry exact candidate evidence forward; whole-host outage remains T16. |
| T20 missing auth/staging | Corrected missing/invalid auth, private-state fail-closed and staged-token integration tests | Candidate deployed, private state initialized once, authenticated readiness true and real jobs verified. No automatic recreation of lost quarantine state. |
| T21 egress escapes | Runtime/gateway tests and positive TLS receipt; declared provider allowlist | No new destinations/protocols enabled by candidate. Research/browser profiles need their own qualification. |
| A-QUOTA | Actual fixed-size ext4 volume ENOSPC and bounded log read/retention config | Hard quota is proven for recorded runtime. Do not infer Docker daemon rotation footprint or hostile-tenant isolation. |
| P11/P22 retention |79 local tests and reviewed dry-run/keep/deadline controls; backup exclusions27 tests | Controls deployed/read back, reversible keep/idempotency/stale409 proof passed. Scheduled deletion policy not adopted; no purge executor or artifact deletion performed. |
| P14 environment lifecycle | Immutable manifests, candidate qualification/CAS activation, pinned agent/verifier images | Qualifiedv2 versions live; new sessions and their follow-ups usef1b15, historical session retains408eb. General install/build/startup-command service remains unsupported. |
| P15 result delivery | Patches/files and verification records exist; bundle module11 tests | Bundle API/CLI and live download/provenance passed p15-api-live-binding.json. Resource integration has local/disposable Docker proof, not a production rollout. |
| P12/section17 legacy migration | Opt-in CLI21 tests + real HTTPS reads; read-only legacy inventory/plan | Actual legacy record/file import, principal/token mapping, deliberate client cutover and rollback remain unexecuted. Never restart v1 as an import method. |
| Release C | Claude only qualified | Other adapters, scoped MCP/notifications need separate auth and repeated applicable gates. No inherited Claude pass. |

The historical direct-Claude auth/retention/image rollout and bounded provider qualification were completed for that runtime. Result bundles and dashboard active-version defaults were subsequently deployed. Candidate Hermes/pstack implementation has resumed after the specification work; resource, preview and new provider-controller components remain separate from the live runtime. Physical host recovery, actual legacy migration/cutover and the remaining B/C requirements cannot be replaced by additional unit-test counts.

## Hermes/pstack acceptance correction

H01–H08 are required in addition to the infrastructure rows above. Pinned image and synthetic tool-loop evidence exist, but actual Hermes backend authentication, two-model routed execution, durable child lifecycle and the complete acceptance regression are not qualified. Cross-provider requested roles remain C requirements. No A/B/C release is complete. See SPEC-FIRST-CONTRACT.md for the exact gates and spec-first order.


## Current candidate integration evidence

These rows do not supersede H01–H08 or confer historical direct-Claude acceptance on Hermes.

| Component | Evidence | Remaining requirement |
| --- | --- | --- |
| Separate provider runtime | `evidence/provider-docker-corrected-binding.json`; actual Omarchy `provider-docker-linux-20260918T010855Z.json` | Real authentication/model qualification; unanswered-RPC and complete crash lifecycle |
| Supervised execution and budget | `evidence/supervised-provider-linux-20260918T010306Z.json` proves synthetic response after physical cleanup, replay charge invariance and cancellation quarantine/reconciliation | Earlier snapshot excludes active BudgetAuthority and automatic wrapper recovery; new full native proof pending |
| Active root/ancestor authority | `evidence/budget-authority-final-binding.json`; local supervisor integration logs | Actual full-path qualification and production scheduler registration |
| Same-controller recovery | `evidence/provider-recovery-revised-binding.json`; concurrency regression log | Automatic wrapper integration, actual adapter invocation and restart-owner recovery |
| Native Hermes caller and exported results | `evidence/routed-results-linux-final-binding.json`: actual caller/file tool/export, durable observation reload, exact cleanup, two simulated provider requests | Real provider authentication/model/effort and complete workflow remain unqualified; synthetic transport is not provider inference |
| Routed child lifecycle | Candidate scheduler/root ownership, assignment, budget and cleanup implementation; `docs/ROLE-SCHEDULER-CHECKPOINT.md` and `docs/ROOT-CLEANUP-CHECKPOINT.md` | Protected decisions, stage contracts, revision advancement, new-controller recovery and production composition still require their own acceptance evidence |
| Isolated protected-check runtime | `evidence/routed-verifier-linux-v4.json`: eight actual Omarchy cases, including failure, timeout, cancellation, secret filtering and interrupted-observation refusal | Root-controller fixture; retained Docker logs are not full-lifetime log completeness; unknown execution is held, never relaunched by resetting its journal |
| Caller/export/protected-check composition | `evidence/routed-verification-linux-passed.json`, `routed-verification-linux-rejected.json`, `routed-verification-linux-empty.json`, each with exact cleanup receipt | Correct pass/reject/needs_review, immutable candidate and replay without create/start. No workflow decision, seat release, qualified environment binding, real provider or production activation is exercised |
| Durable task-acceptance decision | `evidence/routed-decision-binding.json`: local atomic outcome/gate, durable worker completion, cleanup release and inert replay tests | Only acceptance_verification/verify is supported. Needs_review has no gate. Qualified environment binding, other stage contracts, Linux decision proof, next-stage output relation and root/session promotion remain |

The verifier's requested Grok4.6/xhigh review returned REVISE. Confirmed findings were corrected and independently tested; this is not a subsequent Grok approval of the fixes. See `reviews/routed-verification-grok-disposition.md`. Fable checkpoint review remains pending on the previously observed account limit. The broad785-case run retained two setup failures; focused passes and the correlated diagnosis in `evidence/routed-fixture-diagnostic-report.md` do not relabel that broad run as green.

Qualified environment and versioned output-contract integration now has local
evidence in `docs/QUALIFIED-WORKFLOW-CHECKPOINT.md` and
`evidence/qualified-workflow-final-binding.json`. Frozen environments are enforced
through legacy verifier entry points too, and acceptance decisions require the
authenticated structured answer. This closes those local implementation gaps;
it does not supply Linux/current-source or production workflow qualification.
Planning/review finalization, stage advancement, revision promotion, root/worker
activation, real provider/model/effort qualification, and the full six-stage run
remain open.

The subsequent stage progression candidate implements durable non-acceptance
stage finalization, full structured plan/review context, registered initial
revisions and exact predecessor-output admission. Local planning completion and
release now reach the next Astra review prompt through these controllers. See
`STAGE-PROGRESSION-CHECKPOINT.md`. This is synthetic runtime/provider evidence;
it neither establishes real Fable/Grok/Astra/Sol execution nor closes final
delivery promotion, remediation, recovery, deployment or release gates.

## Atomic delivery candidate update

The local candidate now implements historical task/context evidence authentication,
complete workflow delivery proofs and atomic session publication with exact cleanup
release. `ROOT-DELIVERY-NEXT.md` and `evidence/root-delivery-final-binding.json`
record168 final delivery/cleanup/schema/Store/report checks across three receipts.
This supersedes earlier statements that root promotion is wholly unimplemented;
production composition, follow-up import, remediation, new-controller recovery,
current-source Linux and real-provider qualification remain open. No A/B/C release
or deployment is approved by this local checkpoint.

## Stopped workflow candidate update

The local controller now closes authenticated nonpassing prefixes with completed
`needs_review` or `rejected` outcomes, retains evidence and session delivery, and
releases capacity only after exact cleanup. `ROUTED-STOP-CHECKPOINT.md` and
`evidence/routed-stop-final-binding.json` record168 focused checks plus successful
delivery regressions. This closes the formerly permanently-held rejected-prefix
case; it does not implement repair submission. Explicit remediation admission,
versioned failure catalog, cross-root rebind/context, bounded lineage and the
remaining production/live acceptance gates are still required.


## Explicit remediation candidate update

Local same-session fresh-root correction is now implemented, superseding the
previous statement that repair submission/catalog/rebind were wholly missing.
One correction preserves exact original request/acceptance, carried content,
fixed model seats, delivery base, original deadline and remaining requests. A
16 KiB authenticated untrusted sidecar carries rejected findings without relaxing
ordinary context scope. Actual local controllers compose three repair stages,
protected acceptance and delivery; inference/runtime boundaries are synthetic.

ROUTED-REMEDIATION-CHECKPOINT.md records overlapping component passes and the
retained parent-final result166 passed/one 503 fixture-preparation failure out
of167. Its isolated retest passed; the original cause is unproven. Grok4.6/xhigh
review is running with no verdict, Fable remains pending, and no production,
real-model, full-suite-green or Release A/B/C acceptance is conferred. Product/API
exposure, production root/worker composition, new-controller recovery, scoped
Linux/provider/model qualification and reviewed activation remain required.


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
