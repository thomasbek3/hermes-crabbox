> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# P15 API-only rollout

Prepared after the successful auth/image/retention deployment and all three real Claude qualifications. No live P15 changes have been performed.

Publish exactly these five frozen host files: `api.py`, `cli.py`, `result_bundle.py`, `dashboard.py`, `static/dashboard.html`. Restart only `cloud-workbench-api`. Preserve worker PID/source, both existing images, configuration bytes, registered environments, client grants, legacy service PID, and all data. No resource sampler, preview module, Runner, Store, image build, or provider job is included.

The source snapshot is `evidence/p15-api-source-snapshot`, layered on the exact deployed auth snapshot. Candidate API/CLI/bundle/dashboard/compatibility/qualifier tests passed 125 tests. Three deployment scope/hash/symlink tests pass separately. Fable's bundle REVISE and independent disposition remain in `reviews/result-bundle-disposition.md`; no replacement PASS is claimed. The parent separately verified browser download/default selection locally.

Operator script `scripts/deploy-p15-api-checkpoint.py` defaults to validation. It validates the prior successful deployment receipt by explicit SHA, all source hashes, the exact five-file publication scope, all live base-source/configuration hashes, and idle SQLite/runtime state. Execution requires root-owned staged source, writes a root-private backup (including a record of previously absent files), stops only API admission, rechecks idle, publishes atomically, starts API, and verifies authenticated readiness. Final readback proves exact published hashes, unchanged other sources/configuration and worker/legacy PIDs. Failures retain stage and observed service state and never automatically roll back.

Before execution: upload the frozen candidate to a new private stage; compare every hash; exact-scan against the existing dedicated provider and named-client credentials with only boolean/filename output; run validation and review its receipt. Existing auth deployment receipt is the rollback baseline. API restart invalidates browser cookies held in memory; a fresh dashboard sign-in is expected.

After execution: use the existing completed repository session `95eea001-1c8f-427b-97f2-061b95546818` to run the HTTPS qualifier. Download ZIP through both raw API and CLI into private local files, check deterministic bytes, ETag, count/size limits, all artifact hashes and snapshot identity, and prove CLI no-clobber behavior. No new LLM task is needed. If no existing suitable private denial credentials exist, create two named TEST principals with only observe/retrieve scopes, one admitted to sample-repo and one excluded from it; keep tokens private, request denial, then revoke the exact two generated IDs in a finally block. Existing grants stay byte-for-byte unchanged. These requests prove denial for other owners and excluded-project principals; local tests isolate each authorization condition.

Verify authenticated dashboard configuration defaults to the active admitted v2 versions and the downloaded page contains the bundle action; take a live browser screenshot and inspect the result. Bind final receipts to host, source snapshot, prior deployment receipt, configuration hashes, and actual downloaded ZIP hash. Record that measured resource usage remains explicitly unavailable; this checkpoint does not deploy resource sampling or preview.
