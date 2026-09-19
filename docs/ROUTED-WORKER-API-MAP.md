# Routed worker integration handoff

Historical map from the earlier driver checkpoint. For the current implemented WorkerService, root provisioning primitive and combined cleanup APIs, use ROUTED-WORKER-INTEGRATION.md, ROUTED-BOOTSTRAP.md, WORKER-SERVICE-LIFECYCLE.md and ROUTED-CHILD-CLEANUP.md. Statements below that those components do not exist describe the earlier source snapshot.

Current driver checkpoint consumes an explicitly provisioned `CallerSpec`. It
binds the scheduler's immutable caller identity, records/rechecks start authority,
starts the exact caller and relay/Hermes processes, collects worker observations,
fences child grants, and stops/removes the exact caller. Its result remains
unverified. Child/root occupancy remains held; it does not claim provider cleanup,
release_child, verification, or production worker provisioning.

## Existing composition

- `routed_stage.prepare_child_stage` binds admitted child/generation, frozen profile,
  trusted instruction hashes and revision; returns materialization and launch plan.
  Preparation alone is not start authority (`routed_stage.py:32,52`).
- `routed_driver.drive_prepared_child` uses scheduler bind/begin/check/confirm/fence
  operations and the runtime's exact CallerSpec. The driver requires the worker
  socket service and material to exist before invocation (`routed_driver.py:113`).
- Provider side: `ProviderExecutor` wrapped by `SupervisedProviderExecutor`, with
  the adapter cancellation callback wired to `wrapper.cancel_check`. Pass
  `authorize=wrapper.authorize`, `execute_request=wrapper` into `WorkerDispatcher`
  (`provider_executor.py:26`; `supervised_executor.py:25,38,41`;
  `inference_relay.py:309`). Snapshot and dispatch must share the same leases object.
- Bind separate HTTP and worker capabilities to `AttemptBinding(attempt_id,
  generation, profile_digest)`. `WorkerSocketServer` runs on a retained non-daemon
  thread. `routed_caller.run_relay` builds the loopback InferenceService inside the
  caller container and forwards to that host UDS (`inference_relay.py:44,402,429`;
  `routed_caller.py:204`). Never put provider credentials in this relay.
- `RoutedRuntime.prepare_caller/create_caller/start_caller/start_caller_process`
  own the stopped-create and exact process launch journals. Launch relay first,
  require exact ready binding/UID1001/path, then launch Hermes UID1000. Exec launch
  is not readiness (`routed_runtime.py:177,232,306,321`).
- Fixed-name `read_caller_file` collects result/events/readiness while the outer
  caller remains running. The typed collector returns worker-reported observations,
  not gate/cleanup authority (`routed_runtime.py:394`; `routed_collection.py`).

## Provisioning boundary

Current actual worker identity is UID959, primary GID960 with supplementary GID959
and Docker group966. It does not have GID1000 or1001. Do not infer that the existing
worker can create the required caller files or chown/chgrp them. A separately
reviewed trusted provisioning seam is still required; changing worker groups or
service permissions is not part of this driver checkpoint.

The existing synthetic caller harness runs a root controller and creates task
material root:GID1000 (private files0440, task directory0751), readable bootstrap
source0444/0555, and worker socket directory root:GID1001 mode0750. Its client.json
is0440/GID1001; socket0660/GID1001. Workspace/scratch must meet the materializer's
GID1000 access contract. `routed_runtime.py:145,177,200` enforce these boundaries;
`scripts/qualify-routed-caller-linux.py:230-256` provides the fixture example.
A root-preprovisioned proof cannot establish worker959 provisioning readiness.

## Required future stop/release composition

1. Durably revoke exact child provider grants before caller stop. Worker authorize
   and cancellation supervision must observe that fence (`provider_leases.py:397`;
   `provider_executor.py:35`; scheduler.fence_child_execution).
2. Stop/remove exact caller through RoutedRuntime; its receipt explicitly says
   provider_cleanup_qualified=False (`routed_runtime.py:352,358,375`).
3. Drain the host UDS callback; perform bounded shutdown and join of the retained
   server thread, then server_close and dispatcher.close. Socket disconnect feeds
   cancellation. Never close the worker journal while callbacks remain active
   (`inference_relay.py:374,377,402,421`). There is no combined lifecycle handle yet.
4. Require observer shutdown receipt.stopped before recovery. Exact quarantined
   request recovery checks reservation/grant/attempt/generation/profile and settles
   unknown operations without provider restart (`supervised_executor.py:117`;
   `provider_recovery.py:15`). Stuck observers retain fences and occupancy.
5. Verify every provider request for that child has durable physical cleanup and
   released request lease. Retry dispatch.cleanup for residual snapshot hooks even
   after durable physical cleanup. Preserve unknown/tampered material explicitly;
   do not claim all material clean from container absence alone.
6. Only the dedicated combined verifier may issue ChildCleanupReceipt and call
   scheduler.release_child. It must bind exact caller cleanup plus all child
   provider requests. The root account reservation stays held between children;
   cleanup_owner is not a child-release shortcut (`scheduler.py:408`).

The next qualification is one isolated synthetic root-controller fixture, using
real scheduler/revision/driver/Hermes with synthetic Responses. It retains child
and root occupancy and does not grant a verification gate or claim provider auth.
