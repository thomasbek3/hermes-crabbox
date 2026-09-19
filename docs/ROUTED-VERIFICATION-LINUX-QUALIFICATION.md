# Protected verification composition qualification

`scripts/qualify-routed-verification-linux.py` is an opt-in wrapper around a byte-for-byte frozen copy of the existing routed driver qualification harness. It does not edit or replace that harness. Preparation freezes the wrapper, driver, caller helper, and transitive application source closure; parent execution remains a separate step.

Each fresh bundle has one mode:

- `passed`: a protected Python script reads the exact candidate file as UID/GID 1000, confirms candidate writes are denied, writes only private `/tmp` scratch, and exits 0.
- `rejected`: the same observations are required, followed by exit 7; the protected outcome must be rejected.
- `empty`: no scripts or verifier containers run; the published outcome must be needs_review.

Each mode uses its own isolated database, account reservation, root/child identities, fixture directory and unique Docker owner label. The original actual native Hermes caller and read-only export collector still execute. Provider inference and physical provider cleanup remain simulated in memory; this qualification does not make a provider, auth or production readiness claim.

The wrapper then uses the real saved `ChildResults`, candidate, policy/materialization, `VerifierRuntime`, and protected receipt publication. It provisions the material parent as root:GID1000 with SGID and passes `required_tool_gid=1000`. Protected check containers use the pinned existing image, UID/GID1000, no network, read-only candidate/task mounts, one CPU, 512 MiB, 64 PIDs and a 15-second check timeout inside a 45-second phase budget.

The proof binds candidate/input/cleanup hashes, frozen driver hash, plan and published receipt hashes, actual per-check runtime/spec/log hashes, exact exit codes and UID/GID log observations. A second composition invocation must return the same receipt with no additional create/start. Candidate bytes remain unchanged; scheduler occupancy, account reservations and budget/request counts remain held, and no acceptance gate or terminal lifecycle result is committed. Public artifact events must omit private storage paths. There are three final artifacts: worker observation, candidate and protected verification receipt.

The worker persists exact verifier specs before any check runtime call. Its `finally` reconciles these checks. Independently, the root cleanup hook validates the persisted specs against the original caller and frozen image/owner, reconciles exact verifier journals and requires zero verifier objects before invoking existing export/caller cleanup. Unknown or mismatched effects fail closed. Existing service before/after and caller cleanup checks remain in force. The root fixture is retained for the parent's exact-directory cleanup procedure; the wrapper never broadly deletes host data or restarts services.

Local tests cover frozen source closure, original driver preservation, standalone package imports, pass/reject/empty receipt rules, proof tampering, numeric identity types, replay, caller-bound cleanup specs and cleanup ordering. These are offline harness tests and do not prove Docker execution.

Prepare locally:

```sh
.venv/bin/python scripts/qualify-routed-verification-linux.py --prepare evidence/routed-verification-linux-passed-source.json --mode passed
.venv/bin/python scripts/qualify-routed-verification-linux.py --prepare evidence/routed-verification-linux-rejected-source.json --mode rejected
.venv/bin/python scripts/qualify-routed-verification-linux.py --prepare evidence/routed-verification-linux-empty-source.json --mode empty
```

After source review/freeze, the parent may execute each separately:

```sh
.venv/bin/python scripts/qualify-routed-verification-linux.py --remote evidence/routed-verification-linux-passed-source.json --receipt evidence/routed-verification-linux-passed.json
```

Use the matching rejected/empty bundle and a fresh receipt filename for the other modes. Do not pass `--mode` to remote execution: the frozen bundle already determines it. Preserve unsuccessful receipts and use the existing exact-fixture cleanup process before another run. No remote execution has been performed by this preparation task.

## Actual Omarchy proof, 2026-09-18

Parent executed all three frozen bundles on Omarchy. Independent local readback validated each receipt against its bundle and confirmed all **52 application hashes matched source at that readback time**. This proof remains bound to the frozen bundle hashes; later application changes require separately scoped evidence. The wrapper and frozen driver hashes also match. No further execution was performed during this readback.

| Mode | Run ID | Protected outcome | Verifier create/start | Replay |
| --- | --- | --- | --- | --- |
| passed | `driverqual-dedcb53392dc4e7d` | passed, exit 0 | 1 / 1 | Same receipt; no additional create/start |
| rejected | `driverqual-ca7458786a2c4ce2` | rejected, exit 7 | 1 / 1 | Same receipt; no additional create/start |
| empty | `driverqual-2dbf5f08f4914e11` | needs_review | 0 / 0 | Same receipt; no additional create/start |

The two executed checks reported UID/GID1000, successful candidate reads, denied candidate writes and writable private scratch. Each scenario retained unchanged candidate bytes, root occupancy/account reservation and no gate or seat release. Each published three artifacts, and the protected artifact event omitted its private storage path.

All exact fixture directories were subsequently removed by the parent's separately bound cleanup procedure. Cleanup receipts confirm zero owned resources and preservation of image `sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693`. The three services remained active with unchanged PIDs throughout execution and cleanup: API **1240083**, worker **1222709**, and `cloudd` **974255**. Completed run timestamps span 08:26:04–08:27:21 UTC.

Receipts are `evidence/routed-verification-linux-{passed,rejected,empty}.json`, with corresponding `-cleanup.json` files. `evidence/routed-verification-linux-final-binding.json` binds source, bundle, execution, cleanup and documentation hashes. The cleanup receipt's remote receipt hash matches the root fixture's pretty-printed JSON bytes reconstructed from the local receipt; the local stdout receipt has a separately recorded raw-file hash.

This proves actual Docker caller/collector/protected-verifier composition for these synthetic fixtures. The controller fixture ran as root; it does not qualify worker959 provisioning. Provider inference and physical provider cleanup were simulated. No workflow decision, acceptance gate, terminal lifecycle outcome, production deployment, live provider/auth use or qualified-environment registry activation is established by these receipts. The in-progress decision module was excluded from all three bundles.

### Subsequent scheduler guard

After the 52-module readback above, a separately approved two-line `scheduler.release_child` guard was added: children carrying a `routed_decision` result must use decision cleanup release. The frozen proof used scheduler SHA `b85fbb95e423077322f7eddbb4cb3650bb81209c8bf4eb4b8e9f7bd5522598e4`; the later file is SHA `e16b1e5160e5e1de6267487dc1c72a66f512a284fdd1f9a949e577463f9be601`. The exact delta is saved in `evidence/routed-verification-linux-postproof-scheduler.diff`. The other 51 modules still match. This guard and its separate offline regressions are outside these Docker receipts; the qualified snapshot is not silently advanced.
