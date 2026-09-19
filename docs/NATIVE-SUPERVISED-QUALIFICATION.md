# Native Hermes through supervised Docker — isolated qualification

Canonical actual execution: `evidence/native-supervised-linux-20260918T012005Z.json`. The matching `.source-snapshot.json` stores every executed controller source and the caller helper; `.harness.py` is the exact pre-execution harness. Thirty local validation tests load those actual artifacts, hash every source/helper/harness and reject altered cleanup, identity, accounting, journal, process and tool-result evidence.

The archived harness is the exact execution version (`f1995b542c28a32f4e1a5efe52999fbbc031767780499359429356149317e128`). The validator was strengthened **after** execution (`90022edb02ce0a050c5ff94b0ea6404149ced8b8a66a3ace3226158a41dc19da`); these thirty tests apply the stronger rules to the already collected receipt. The `remote_main` collector body is byte-identical, and the caller helper is unchanged. See `evidence/native-supervised-source-comparison.json` and `evidence/native-supervised-harness-validation.diff`. The original receipt, source snapshot and execution harness remain unchanged.

A subsequent read-only SSH check on Omarchy, `evidence/native-supervised-remote-readback.json`, independently reread the retained host receipt and all nineteen source files: their hashes match the local record. It also correlates both budget request rows with the native attempt/generation/profile/nonce and normalized dispatch hashes and cleanup request IDs. This corroborates the saved host record; it is not signed execution attestation or a reconstruction of historical Docker events.

On verified Omarchy, actual native Hermes in the pinned caller image completed this path:

```text
Hermes UID1000 → localhost HTTP/SSE relay UID1001
→ authenticated mounted Unix socket → host WorkerDispatcher.execute_request
→ SupervisedProviderExecutor + CancellationSupervisor + BudgetAuthority
→ ProviderExecutor / ProviderDispatch / ProviderLeases / InferenceBudget / Store
→ fresh provider container running the synthetic CLI
→ exact physical cleanup + supervisor stop → structured response
→ actual Hermes read_file → second inference with the file result
→ second fresh provider scope + cleanup → native terminal exit0
```

The host worker alone had Docker access, provider configuration, synthetic token and the isolated Store. Caller init used UID1002; the relay used UID1001 and Hermes/tools UID1000. Caller network was none, root/source read-only, all capabilities dropped and no-new-privileges set. The only persistent caller mounts were read-only source and the dedicated worker UDS/config directory. No Docker socket, controller database or provider credential was mounted into it.

The native event stream contains `tool_use(read_file)`, successful `tool_result` with `SYNTHETIC_NATIVE_SUPERVISED_FILE`, then the final `SYNTHETIC_NATIVE_SUPERVISED_OK` and exit0. The second actual inference request contained that file result. Both responses were HTTP/SSE status200 and used distinct relay nonces, provider/gateway containers and networks under the same attempt/generation/profile and logical account.

| Request | Exact cleanup observed | Supervised callback returned | Supervisor |
| --- | --- | --- | --- |
| File-tool decision | monotonic80224.427954111 | 80224.431887823 | stopped, not cancelled |
| Final decision using file result | monotonic80234.624730515 | 80234.631762139 | stopped, not cancelled |

Each cleanup included a second Docker inventory check proving the four immutable resource IDs absent, before returning the decision. The receipt has all eight distinct full IDs and verifier digests. Root usage=2, attempt usage=2, budget request rows=2 and durable dispatch rows=2. Relay journal rows match worker nonce/payload digests exactly; relay states are `http_write_completed`, worker states `cached`. A completed socket write alone does not prove client receipt, but this run independently observed the resulting native tool execution and terminal output.

The wrapper snapshot serializes execution and reads `last_receipt` immediately after each callback returns; its context manager closes the supervisor before returning. This is source-backed sequential correlation, not a separately timestamped shutdown attestation: `ShutdownReceipt` has no nonce or `stopped_at`. Each physical cleanup record carries its own dispatch request ID. `durable_revocation_confirmed=false` here means no cancellation/revocation was required on this happy path.

The budget schema has no reserved/settled state. Its transaction increments root/attempt usage and inserts an immutable request record with `charged_us`; a replay does not increment it again. This run demonstrates two request rows and used=2, not monetary settlement. The supplementary readback retains raw-request versus normalized-request digest domains explicitly; those hashes should differ.

Eight UID1000 probes passed: worker capability read, direct worker UDS connect, relay journal read and relay proc environment read were EACCES; SIGTERM to relay and init was EPERM; source write in a 0777 directory was EROFS; `/var/run/docker.sock` was absent. During native execution `cgroup.procs` included relay PID7 and Hermes PID25 alongside init/probe. Live cgroup limits were 1.4CPU/3008MiB/352PIDs. This is shared caller accounting, not protection from caller resource exhaustion.

The positive worker path used the configured capability. The UID1000 EACCES probe establishes filesystem isolation, not rejection of an incorrect capability from an authorized UID. Negative capability authentication remains separate unit coverage; this run did not exercise it. The fixture loopback HTTP authorize callback was permissive within this isolated caller.

All provider scopes used normal controller cleanup. No provider fallback removal was required. Finally the exact caller, source volume and temporary synthetic image were removed; no owned containers, networks or volumes remained. The synthetic provider token was removed. The three named live service units were active before and after. Private nonsensitive source/Store/journals remain as evidence. Service snapshots do not rule out unrelated transient host changes.

## Source and claim boundary

- BudgetAuthority: `9054be7ec6f07c68749ffb85f045309372f56f3b46f5c6e63abfe0fb8ca5bb10`.
- ProviderDocker: `af7e9a07a3cb6ea2f145cd8224fda2360c9d0534a398e15de0857fa634d8b243`.
- Supervised wrapper: `aaf5e82db78bdf790920d55ab31dc7dd59f149c30ca335f0b92ef2c261ce8ea0`.
- Recovery helper: `e62b7917a5f05dd0c61da16d1fbd07b5da485245387f86d0d0dcb59960d1e837`.
- Dispatch: `5ad605feef7a7652179875b46eee581e995c3a6d8294980da25a80f245a70977`, before the subsequent `provider_operation_resolved` event-history delta.

This happy path includes mandatory BudgetAuthority and the wrapper recovery source, but does not fault-test recovery or qualify the later dispatch-event delta. Cancellation/reconciliation is separately evidenced by the prior supervised provider proof and unit tests. No real provider/authentication or production activation occurred; responses came from a synthetic CLI image. No provider usage was returned. Native terminal token counters show default zeros, which are not measured usage or billing evidence.

Hermes plugins, tirith, compression and title generation were disabled in this fixture. Native retry behavior after HTTP loss was not retested or reclassified; prior D9 evidence still requires controller interruption on unknown delivery. Persistent production UID/group provisioning, controller restart recovery, actual lost Docker RPCs, real provider credentials, network policy stress and resource-exhaustion resilience remain separate qualifications. The 22.8-second native fixture duration is not a provider latency or production performance claim.

The embedded candidate record in the source snapshot matches `evidence/provider-image-candidate.json`: base `sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693`, provider candidate `sha256:1e1948adf1632b9b1cf6c5356bca083d9465986a09ee93b266fc75020a5e1db6`, tag `cwb-provider-candidate:003f6c85a34b`. It includes layer-prefix/source binding and references `reviews/provider-main-fable-corrective.md` and `reviews/provider-main-disposition.md`; those establish the candidate provenance, not real provider qualification.

At the supplementary comparison timestamp, these snapshotted modules differed from the working tree: `provider_dispatch.py`, `supervised_executor.py`, `provider_recovery.py`. Their exact old/current hashes are in the source-comparison record; the native result does not qualify those later revisions.
