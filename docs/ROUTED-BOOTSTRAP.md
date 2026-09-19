# Routed task bootstrap provisioning

`routed_bootstrap.py` is a root-only filesystem primitive for one admitted child attempt. It has no listener, sudo wrapper, process launcher, provider credentials, or installed privilege service. Root-controlled code supplies the destination, the exact seven-file source manifest, and both authorization callbacks. A request cannot select paths, Unix identities, source bytes, or helper commands.

## Shared planning and integration

`plan_child_stage(...)` returns a frozen `PlannedStage`: revision consumer binding, assigned role, workflow and role-request identifiers, profile and input-revision digests, readonly policy, and `RoutedLaunchPlan`. It performs the same current scheduler/profile/instruction checks as preparation, without copying files or changing scheduler state.

The intended caller sequence is:

1. Call `plan_child_stage` with the admitted child, exact generation/revision/profile and trusted instruction root.
2. Construct `BootstrapRequest(plan.consumer.attempt_id, plan.consumer.session_id, plan.consumer.generation, plan.profile_digest, plan.launch)`.
3. A root-controlled integration independently authorizes that exact request and calls `RoutedBootstrap.provision`.
4. As worker UID959, call `prepare_child_stage` with the same inputs, `expected_plan=plan`, destination `material.materialization_parent / 'child'`, and `required_tool_gid=1000`. It recomputes the plan and refuses any difference before copying.
5. Start the existing worker socket service in `material.worker_socket_dir`, then prepare the caller runtime. Runtime create/start still requires fresh scheduler authority.

`authorize(request)` must independently verify the exact current scheduler attempt, generation, profile and launch binding. It is invoked before writing and again before publication. It is not a caller-supplied approval flag. No root IPC policy adapter is installed by this module.

## Files and identities

The configured destination root must be root:GID960 mode0750 (setgid is also permitted), with trusted root-owned ancestors. Worker959 can traverse through its primary group960. The source root is a separate trusted root-owned directory; copied source removes any need for worker959 to traverse the original source path.

Each published directory is named with the first24 hexadecimal characters of the attempt SHA256 plus generation. Full identity remains in the receipt; collisions or prior claims refuse provisioning. The constructor reserves a maximum103-byte encoded socket path, including the largest allowed generation and `/worker/socket`.

| Relative path | Owner:group | Mode | Purpose |
| --- | --- | --- | --- |
| attempt root | 0:960 | 0750 | Private worker traversal |
| `receipt.json` | 0:960 | 0440 | Full binding, inode inventory and content hashes |
| `task` | 959:1000 | 0751 | Mount source, tool-private files below |
| `task/{prompt.txt,config.yaml,launch.json,http-capability}` | 959:1000 | 0440 | Frozen launch material and HTTP capability |
| `task/source/cloudworkbench` and source parent | 959:1000 | 0555 | Exact pinned bootstrap source |
| Seven Python source files | 959:1000 | 0444 | Readable by both isolated processes |
| `worker` | 959:1001 | 2750 | Relay client and future inherited-group socket |
| `worker/client.json` | 959:1001 | 0440 | Exact binding and two distinct capabilities |
| `stages` | 959:1000 | 2750 | Empty materialization parent |

The source closure is `__init__.py`, `routed_caller.py`, `hermes_adapter.py`, `pstack_routing.py`, `inference_relay.py`, `inference_service.py`, and `adapters.py`. Each is a bounded regular one-link nofollow file with an exact manifest hash. Capabilities are separately generated; returned material and receipts expose their hashes, not their values. Only port9876 is supported by this bootstrap contract.

Owner959 access lets the worker read its generated files despite not belonging to groups1000/1001. Actual Linux qualification now proves that the stage and socket inherit the required groups without changing host memberships; see the receipt below. Local simulated-DAC tests alone do not establish this behavior.

## Publication, rollback and cleanup boundary

Unpublished material lives in a fresh root-owned0700 pending directory. Files are exclusive, nofollow, bounded and fsynced. Publication creates a permanent exclusive root0600 attempt claim, renames the directory, exposes0750 traversal, and fsyncs the parent. The claim prevents replay after successful discard or interrupted publication. Claims are not automatically deleted.

Before publication, failures roll back only the exact recorded created inodes. Failures after publication and process crashes may retain material or private pending directories for reconciliation. There is no broad cleanup sweep.

`discard_unlaunched(material)` is only for a never-launched, writer-free attempt. Root-controlled `authorize_discard` must independently establish that condition; this module cannot inspect Docker or scheduler state. It validates full receipt/identity, exact path inventory, modes, ownership, hashes and inode identities, then rechecks authorization before removal. Added sockets, stages, altered material or symlinks cause refusal. It leaves claims and other generations intact. It is not used-runtime cleanup, provider cleanup, seat release or verified task completion.

## Checkpoint evidence and remaining work

`evidence/routed-bootstrap-stage-tests.xml` and `.log`: 63 tests passed, covering bootstrap, shared stage planning and existing driver behavior. Bootstrap tests use real local bytes, inodes, concurrent operations and rollback, but simulate privileged UID/GID/mode metadata on this Mac. `evidence/routed-bootstrap-binding.json` binds the exact files.

Remaining gates: root-owned authorization/IPC integration, exact used-attempt cleanup orchestration, and end-to-end live worker activation. This checkpoint changes no host groups, live services or provider authentication. Remote changes were limited to the removed synthetic qualification fixtures.

## Review corrections

The corrective checkpoint has 68 passing bootstrap/stage/driver tests in `evidence/routed-bootstrap-corrective-tests.xml` and `.log`; `evidence/routed-bootstrap-corrective-binding.json` binds the changed source. The earlier63-test binding remains historical.

Discard scans only receipt-declared directories with nofollow descriptors and exact inode checks, stops on the first unexpected entry, and caps the inventory at64 entries with bounded path depth/length. It never recursively walks an unknown subtree. It removes child entries before the receipt. A partial removal retains the receipt and returns a fixed retained/reconciliation error; missing entries are not silently accepted on retry. If the final root removal fails after receipt unlink, it attempts to restore the identical receipt exclusively into the same retained root. Failure of that restoration remains a manual reconciliation condition; no cleanup success is claimed.

An OS failure after publication raises `BootstrapError('bootstrap_publication_retained')` carrying `.material`, the exact `BootstrapMaterial` handle whose receipt is already published. This includes failure of the final traversal chmod. The handle permits explicit root reconciliation or authorized unlaunched discard; the error does not claim provisioning success. Moving chmod before publication was avoided because that would expose worker-owned pending children before their publication fence.

If authority changes after stage materialization and discard also fails, preparation re-raises the original authority exception with fixed `.cleanup_failure == 'unlaunched_materialization_retained'` and a safe reconciliation note. Raw cleanup exception details are not added. The copied material remains for exact recovery.

The relay currently receives plaintext HTTP capability because the existing `routed_caller` reader requires that exact client schema and hashes the capability when constructing its loopback service. The service only needs a digest; this is a compatibility dependency, not a claim that plaintext is intrinsically necessary. Reducing relay exposure requires a coordinated reader/schema change and remains a separate least-privilege improvement. This correction deliberately preserves the existing client contract.

## Actual Linux permission proof

`evidence/routed-worker-bootstrap-linux-v2.json` passed on Omarchy, run `4e6f5e4d6995e71a`, with a frozen31-module source bundle. The root helper provisioned synthetic material; actual workerUID959, primaryGID960 and supplementary959/966 read it, materialized a readonly revision under GID1000, and created a GID1001 mode0660 socket. Actual UID1001 completed one synthetic UDS request. Actual UID1000 read task/source files, could not read worker/client.json or write the readonly source, and could write scratch. WorkerService closed its listener, thread and journal with zero active callbacks. All child process groups were absent after reaping, the exact fixture was removed, and the three service PIDs and live worker groups were unchanged.

The role probes entered only explicitly passed directory file descriptors because the private host ancestors are deliberately inaccessible to tool/relay UIDs. This qualifies subtree permissions, not an actual Docker bind mount, full child driver invocation, real provider authentication, or production root authorization. The transfer directory was also removed; its receipt is `evidence/routed-worker-bootstrap-linux-v2.transfer.json`. The original preparation omitted two data-declared bootstrap source files; the parent detected that before remote execution. That unexecuted bundle is preserved; v2 includes all seven required payload modules and their transitive imports.
