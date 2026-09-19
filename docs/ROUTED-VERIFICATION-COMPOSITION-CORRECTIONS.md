# Protected verifier composition corrections

These corrections affect the undeployed routed verifier preparation/result composition. They do not authorize terminal decisions, workflow gates, session promotion or live provider work.

## Preparation identity and interrupted publication

Preparation first atomically publishes a private intent directory. Its immutable intent binds the entire plan, provisioned material-parent path/inode and required tool group. A preexisting material destination without this intent is refused. The separately approved `before_publish_material` callback records the exact StageMaterialization (publication ID, parent/source/scratch inodes, metadata/content hashes and scope) after preparation validation and before its atomic destination publication.

Retries reuse published material only after matching that recorded identity and revalidating its contents. Protected-script task trees similarly receive a durable intent containing their staged root and task-directory identities before publication. A retained unpublished directory is recoverable only by its recorded inode under the pinned parent, with bounded inventory and complete validation before publication. Same-content replacement with a different inode is refused. No generic deletion, path-only adoption or recreation of previously pinned material occurs. The atomically published `preparation.json` is the completed-preparation marker.

An ordinary exception in workflow material publication can remove its unpublished temporary tree through the existing helper's finally block. If an intent survives but neither its exact final nor staged inode exists, recovery returns `verification_material_publication_unresolved`. It preserves the intent and does not fabricate a replacement. This explicitly differs from a process crash that retains the pinned unpublished tree, or interruption after successful publication; those exact retained materials can be recovered.

## Durable phase budget

`phase-budget.json` binds plan/preparation hashes, first wall-clock start, fixed deadline, wall-clock high-water, remaining execution seconds and a sticky rollback block. It is controller-private and atomically replaced under the existing exclusive child driver lock. Each authorization debits the greater of wall-clock advance and active monotonic elapsed time. Retries retain the remaining budget and original wall deadline; process restarts/reboots use elapsed wall time since the persisted high-water, rather than comparing unrelated monotonic clocks. Detected wall rollback blocks further execution durably. Active monotonic expiry is persisted and cannot be reset by retrying while wall time is stalled.

This bounds authorization/execution progress; filesystem/Docker operations still have their independent bounded-operation contracts and cannot be made physically interruptible merely by this deadline. Unobserved clock behavior across a process outage cannot be measured by this local journal. Current root/controller/budget authority remains independently required.

When the exact verification artifact is already committed, replay reconciles retained verifier resources and loads immutable check receipts without calling run/start. The final artifact/event comparison and current authority still apply. Such load-only replay is allowed after the verification-phase budget expires; it does not grant a new budget or extend root authority.

## Cleanup and public events

Both cleanup action allowlists include `cleanup_logs`, permitting final diagnostic collection while cancellation prevents new execution/publication. `artifact.created` events exclude the private `storage_path`; authoritative artifact metadata retains that path for controlled retrieval.

Initial corrected composition/policy/materialization suite:158 tests passed in12.92 seconds, recorded in `evidence/routed-verification-corrected-tests.xml` and `.log`. The testing delegate separately owns the latest regression receipt, including the explicit missing-unpublished-tree refusal. No SSH, provider calls or live services were used by this correction.

## Grok findings 3 and 5: fixed identity and explicit actions

The currently supported verifier identity is UID/GID1000:1000. Preparation rejects any `required_tool_gid` except `None` or exact integer1000 before storage/material writes; booleans and coercible strings/floats are refused. Prepared-material validation repeats that restriction and checks task-tree group consistency when1000 was explicitly requested. Runtime specs always use1000:1000. The runtime owner separately enforces effective POSIX read/execute access before create/start; `None` does not bypass runtime access checks or mean arbitrary group support.

Authorization is now an explicit allowlist. Cleanup actions are exactly `reconcile`, `cleanup_inspect`, `cleanup_logs`, `stop`, `remove`, `status`. Full-authority actions are exactly `prepare`, `create`, `start`, `inspect`, `publish`, `load`. Unknown or non-string actions fail before budget/current-result authorization; reconciliation refuses full-authority actions. Cancellation still permits exact owned cleanup while preventing new work.

Final focused composition receipt:54 tests passed in18.78 seconds (`evidence/routed-verification-action-group-corrected-tests.xml`), including20 new identity/action regressions. A separate concurrent composition+policy run recorded113 passes and one fixture setup503 in the prior result-export authority transaction, before verifier composition ran (`evidence/routed-verification-actions-tests.xml`). That failure is retained, not relabeled as a fully green combined suite; no control timeout was relaxed.

## Published receipt reader

`load_published_verification` requires an existing committed artifact before opening the phase budget. It reconciles exact retained verifier resources, then only loads the matching runtime receipts and compares their recomputed assessment with the existing private receipt, artifact and event. It cannot run a check, create a missing receipt file, or reinsert an artifact removed during loading. A cancelled attempt permits reconciliation but refuses the result read. This API therefore has cleanup effects; it is not a pure filesystem read.

The reader still requires current publication authority. Once a child becomes terminal, decision replay must use its separate durable decision record instead of reopening publication. The five focused reader regressions and the existing composition tests passed together:59 tests in18.57 seconds, `evidence/routed-verification-load-combined-tests.xml`. This does not supersede the retained broad-suite setup failures.
