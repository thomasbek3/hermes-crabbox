> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Prepared real-provider qualification harness

Preparation only. No real token was read, no inference called, no service stopped and no production database migrated. `scripts/qualify-real-provider-guarded.py` is a one-request Fable qualification, not the full Pstack workflow. Hermes native tool-loop evidence remains in the separate synthetic harness.

The fixed role policy requires exact `claude-fable-5-1` with `max`; the harness rejects any other model/effort. The parent verified the official [model configuration documentation](https://code.claude.com/docs/en/model-config), including the minimum CLI2.1.257. Candidate CLI2.1.274 meets that floor. The same page warns that Fable billing depends on plan/seat and noninteractive calls may use credits without prompting. Model support and a Max login therefore do not establish no-incremental-spend entitlement.

## Concrete invocation after the gates are satisfied

Use a frozen, reviewed controller source snapshot with its dependency environment on Omarchy. The manifest is root-owned, not group/world writable, and contains no credential values. The source-only example at `deploy/real-provider-qualification.example.json` intentionally contains null authorization/hashes/evidence and cannot execute.

```text
sudo env -i PATH=/usr/bin:/bin PYTHONPATH=<frozen-source>/src \
  /opt/cloud-workbench/.venv/bin/python <frozen-source>/scripts/qualify-real-provider-guarded.py \
  --manifest <root-owned-approved-manifest>
```

This defaults to preflight and never invokes the provider. The separately authorized invocation adds `--execute-approved`. Do not invoke that option until there is approval for the exact quiescence window, fixed profile/image and account billing basis. This preparation task does not supply such authorization.

Required manifest evidence:

- Current worker config SHA and its exact state root/database paths; existing runner.lock device/inode and worker UID/GID. Observed previously: `/var/lib/cloud-workbench/worker/runner.lock`, device32/inode618220, UID959/GID960,0660. These must be refreshed, not assumed unchanged.
- Root-approved authorization reference,600–3600 seconds of remaining window, and confirmation that other consumers of the same dedicated credential are fenced. No automatic service operation is included.
- Immutable provider image, root-owned readonly five-field profile (binary path/hash/version, exact native model, max effort), raw profile SHA and canonical profile digest linked in the model identifier evidence.
- Model identifier evidence with the official source reference, native ID, profile digest, image and reviewer timestamp. This proves a reviewed identifier, not account entitlement.
- Separate fresh account-specific evidence for logical account `dedicated-cloud-claude`, exact model, canonical credential lstat metadata, source reference and `existing_entitlement_no_incremental_spend`. Generic subscription status is insufficient. The implementation deliberately does not allow a spend-authorized alternative within this no-new-spend task.
- Frozen controller module/harness hashes and an explicit subset of current Anthropic egress domains. The sole credential path is `/var/lib/cloud-workbench/auth/claude-token`; only the provider container reads it.

## Shared lock and cleanup contract

`LegacyRunnerFence` opens the same existing lock as runner.main with O_NOFOLLOW and no O_CREAT/truncate/unlink. It verifies its inode before and after exclusive nonblocking flock, root/config binding and safe ownership. The configured worker service must be loaded/inactive/dead with MainPID0; live or queued attempts in the production database cause refusal. That database is opened only with SQLite mode=ro/query_only, never through Store.

The lock remains held through provider execution and physical cleanup. New Docker create/start commands recheck lock/config identity, credential metadata and worker quiescence. Already-owned inspection and cleanup require the held lock but may proceed when service quiescence changes. The qualification has its own private Store and one-request root/attempt budget. There is no account-release or retry loop. A successful canary without provider-reported exact model is still not a passed model qualification. Only safe status/identity/usage metadata is emitted; raw CLI/error streams and response text are not printed.

The trusted settlement check requires all created qualification dispatches cleaned/delivered without uncertainty, observer stopped, and no labeled owned containers/networks after normal acknowledged cleanup. If that cannot be established, the script writes a safe failure receipt and parks while retaining the flock. It never times out into an unlock or restarts the worker. The proof folder and private ledger are kept under /root for controlled recovery. SIGINT/SIGHUP/SIGTERM request cancellation and do not release an uncertain parked fence.

**Flock is not a crash-persistent account fence.** SIGKILL, process exit or host reboot releases it while daemon containers may survive. The separately authorized quiescence window must therefore keep automatic worker restart/admission disabled until recovery verifies cleanup. This script does not configure that window. A stopped service/empty queue observation alone is not that operational guarantee. Root/operator replacement of the lock is detected but cannot be prevented by a process holding the old inode.

Local tests prove exact-inode interprocess exclusion, busy/missing/replaced/unsafe lock refusal, readonly idle checks, retained uncertain lock, manifest/profile/billing refusal, default no-inference preflight, safe parked failure, and one actual ledger/budget/supervisor composition with an injected fake runtime. No real Docker/provider proof is claimed for this harness.
