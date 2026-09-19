> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Bounded child launch control transactions

The six child-control APIs (`bind_child_caller`, `begin_child_start`, `check_child_start_authority`, `confirm_child_started`, `read_child_launch`, `fence_child_execution`) use a scheduler-private SQLite context. Existing Store connections and transactions remain unchanged at their historical 30-second busy timeout.

The private context opens the existing DB with `mode=rw`, enables foreign keys, refuses lock contention immediately (`busy_timeout=0`), and installs a 50 ms monotonic execution deadline with a VM progress check every 100 instructions. It checks the deadline before BEGIN, before COMMIT and after COMMIT. Readback uses a deferred readonly operation; authority checks remain write transactions because successful clock observations must retain the durable budget high-water. The update is conditional on advancement and never charges an inference request.

Lock/VM errors return fixed 503 errors without SQLite internals. Any commit exception or acknowledgement after the deadline is reported as `Child control commit outcome uncertain`, never as a confirmed rollback and never with launch permission. Reconciliation reads the persisted start intent; only a proven `bound` row can ever transition to a first intent. A preserved `start_intent` returns `may_start=False`, even if the original response was lost. Transactions that fail before commit roll back. Cleanup detaches the expired progress handler for rollback/close, preserves the original failure, and does not sleep/retry.

These bounds cover SQLite lock waits and interruptible VM execution, not uninterruptible operating-system disk I/O or fsync. Fail-fast writer contention can conservatively stop a stage; the driver must treat it as authority unavailable, attempt physical stop independently, retain capacity, and report unconfirmed durable fencing where appropriate. It must not infer permission from a prior successful check.

`workflow_child_launch` also has a pinned BEFORE DELETE guard. Deleting the launch record cannot erase start intent and permit a fresh start. Future retention requires an explicit reviewed migration/procedure; no current retention path is silently exempted. The table and triggers remain part of the undeployed candidate schema 2 migration definition. Prior candidate DBs with different definitions fail closed; legacy schema 1 behavior is unchanged. No live DB migration occurred.

Review disposition for `reviews/routed-driver-fable-review.txt`: P2-2's 30-second contention concern is confirmed and corrected for these six APIs. Its suggestion to drop the clock update was declined because it would weaken restart/clock-rollback fencing. P2-3's missing DELETE guard is confirmed and corrected. Other driver/collection/runtime findings belong to their respective owners and are not claimed resolved by this document.

Verification: `evidence/child-control-correction-tests.xml/log`, 117 passing scheduler/control/root cleanup/workflow submission tests. Tests use a real held SQLite write transaction, a long interrupted recursive SQL query, precommit rollback, commit-success with lost or delayed acknowledgement, an uncertain fence commit, concurrent start attempts, and deletion replay refusal. The initial coordinated run's evolving driver expectation failure is preserved in `evidence/child-control-interleaved-driver-tests.xml/log`; parent owns that correction and the final combined run. No external models, provider calls or remote operations were performed.
