> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Trusted single-child execution driver

`drive_prepared_child` composes the existing RoleScheduler, PreparedStage, CallerSpec, RoutedRuntime and bounded result collector. It is a controller API for trusted, already-provisioned objects. It has no HTTP endpoint and does not accept model-supplied filesystem/runtime identities.

The driver takes an exclusive per-attempt host lock, refuses an existing launch or runtime journal, rechecks preparation authority and reconstructs the exact caller spec from current material and verifies the materialized input publication, metadata and content. Input identity/content are checked again before Hermes starts. It creates a stopped caller, requires stopped inspection, binds the full runtime/spec/profile/input-revision/assignment identity in the scheduler database, and durably records start intent. Only the first bound-to-intent transaction permits a start RPC. An unanswered create/start/exec is never blindly retried.

After observed container start, authority is checked before each process launch and each output RPC. The relay must publish a bounded readiness record with its UID1001, exact attempt/generation/profile and request path before Hermes launches. The collector validates the fixed launch receipt, event schema, terminal consistency and file identity/bytes. Missing initial output means not ready; malformed or changing output cannot become success. The output wait is bounded by the frozen run budget plus30seconds, further limited by the driver deadline. Local deadlines and scheduler budget/deadline/client/account checks remain separate. Child-control SQLite operations fail promptly on contention; OS I/O is not a hard wall guarantee.

Worker records live on the durable scratch bind at `/scratch/.cwb-observations`, retained across caller removal for explicit recovery. Event reads use1MiB chunks with64KiB records; no timeout becomes a blind retry.

Teardown durably fences further provider inference for the exact child before stopping/removing its exact caller. Teardown does not invent user cancellation. Cleanup errors or authority loss suppress the returned observation. The returned QuiescedStage explicitly has verification_pass=False, provider_cleanup_qualified=False and scheduler_seat_released=False. The child/root seat and account reservations stay held for provider recovery and protected verification. Runtime cleanup receipts already explicitly cover only the caller.

## Checkpoint scope

Tests compose real SQLite scheduler/revision/stage preparation with RoutedRuntime and a synthetic Docker transport that serves real base64 read envelopes through the unpatched RoutedRuntime reader. They cover cancellation before start and during readiness/collection, grant-fence ordering, malformed observations, timeout, lost create/start/exec acknowledgments, cleanup failure, duplicate invocation and concurrent-driver exclusion. The corrected checkpoint has 459 passing coordinated tests.

The frozen current driver also passed actual Docker qualification on Omarchy: `evidence/routed-driver-linux-20260918-v2.json`. Real scheduler, materialization, driver, Hermes file tool and runtime collection completed with two synthetic Responses calls. Grant fencing preceded caller removal; the result and events remained readable with matching hashes afterward. Root/child occupancy remained held and no verification gate was created. Exact test resources and the temporary fixture folder were removed, with unchanged service PIDs. This root-provisioned, single Astra/high-profile fixture does not qualify worker959 provisioning, real provider inference, provider cleanup or workflow advancement.

The candidate scheduler schema adds workflow_child_launch and pinned guards. Existing undeployed schema2 files are not silently migrated. Production legacy schema1 remains untouched. Deployment requires an explicit migration/recovery candidate, not dropping a live database.

## Remaining integration

Production bootstrap material and the host worker service lifecycle are still required; see ROUTED-WORKER-INTEGRATION.md. A successful caller observation must be durably collected, its produced workspace revision exported after quiescence, every exact provider request cleaned, and protected verification/outcome gates committed before releasing the child. This API intentionally cannot advance workflow steps, publish verified artifacts, promote a session revision or release capacity by itself. The original full-platform acceptance criteria remain unchanged.

Fable returned REVISE; independent corrective disposition: `reviews/routed-driver-fable-disposition.md`. No re-review PASS is claimed.
