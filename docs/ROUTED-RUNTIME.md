# Routed caller runtime seam

Local implementation and fake-Docker tests only. No image, remote host, service,
credential, native provider or live schema changed. `runtime.py` behavior is unchanged.
`RoutedRuntime` composes its command runner, immutable image, configured resource
limits, approved mount roots and owner namespace.

## Controller API

```python
routed = RoutedRuntime(runtime, journal_root=private_controller_directory)
spec = routed.prepare_caller(attempt_id, session_id, generation=generation,
    plan=routed_launch_plan, workspace=materialization.source,
    scratch=materialization.scratch, task_dir=protected_task_directory,
    worker_socket_dir=private_worker_socket_directory)
runtime_id = routed.create_caller(spec)  # stopped container only
scheduler.bind_runtime(attempt_id, expected_generation=generation, runtime_id=runtime_id)
routed.start_caller(spec, runtime_id)  # UID1002 init only
routed.start_caller_process(spec, runtime_id, role='relay')  # UID1001
# Controller checks relay-ready binding + request_path before starting Hermes.
routed.start_caller_process(spec, runtime_id, role='hermes')  # UID1000
```

`CallerSpec` freezes attempt/session/generation, image, owner, exact workspace,
scratch/task/socket paths, workspace readonly policy, public task hashes and resource
limits. Materialization is per child; no writable path is derived from session ID.
Hard storage quota is not inferred from a path: the controller must provision the
bounded stage filesystem using existing Runtime/workspace operations before revision
materialization. `test_path_workspace` still has no live storage qualification.

The task directory contains exactly prompt.txt, config.yaml, launch.json,
http-capability, and source/cloudworkbench/{__init__,routed_caller,hermes_adapter,
pstack_routing,inference_relay,inference_service,adapters}.py. Files/directories are
controller-owned, not group/world writable, and readable/traversable by UID1000.
Prompt/config/launch must match RoutedLaunchPlan, and public bytes are hash-bound.
HTTP capability metadata is checked; its bytes are never read/hashed by this module
or included in argv, journal or receipts. Provider/control credentials are not inputs.

Worker socket directory must be controller-owned0750/GID1001 and contain exactly
client.json0440/GID1001 plus socket0660/GID1001. Only metadata is inspected. The
controller must separately bind the worker configuration/capability to the attempt;
this runtime does not parse it or issue authorization.

Both source/task and worker socket directories mount readonly. The exact workspace
readonly flag and separate writable /scratch are inspected. /run/relay has a UID1001
private tmpfs; /run/tool and nested /run/tool/hermes have UID1000 private tmpfs mounts.
The nested mount lets Docker overlay readonly config.yaml without making Hermes's
native database parent root-owned/unwritable. Network is none, no host listener or
Docker socket, readonly image, no added capabilities, no-new-privileges and bounded
CPU/memory/PIDs. Inspector verifies identity, labels, argv, UID, mounts, tmpfs, logging,
network attachment, limits and other host-exposure flags before starting any process.

Only fixed bootstrap commands are accepted:
`/opt/hermes/venv/bin/python -m cloudworkbench.routed_caller relay|hermes`, with
PYTHONPATH=/run/task/source and PYTHONDONTWRITEBYTECODE=1. There is no model-provided
argv/env interface. Bootstrap loads the protected launch plan. A process launch
receipt means only the Docker exec RPC returned, not readiness, completion or success.

## Crash and collection boundaries

Private fsynced journals precede create/start/exec. Per-attempt/generation flocks
serialize all mutations. A lost create response may be reconciled with
`reconcile_create(spec)`: exactly one fully matching stopped object can be bound;
absence returns None and never authorizes another create. Ambiguous identities or
an unexpected running object fail closed. Start acknowledgment loss can confirm an
already running exact container without issuing another start. An attempted process
role is never executed again automatically, even when its exec acknowledgment was
lost. Unknown outcomes need controller reconciliation/cleanup, not retries with new IDs.

`read_caller_file(spec, runtime_id, name=..., offset=0, max_bytes=65536)` accepts only
names events, result, relay-ready and relay-fenced. Fixed isolated Python reads as the
owning role UID with O_NOFOLLOW, regular/singlelink/owner checks and a16MiB file cap.
It returns absence explicitly or bounded bytes with inode/offset/size metadata.
The parent must maintain durable cursor/inode checks and interpret/redact public
progress. Results remain worker_reported; marker existence is neither physical cleanup
proof nor a passing verification gate. Container logs are not used for detached exec.

Existing Runtime.stop/cleanup address exact owned IDs/generations. The controller
must stop the outer caller, verify absence/stopped state and settle creator intents
before supplying scheduler cleanup receipts. This module does not automatically remove
existing state, replace stale sockets, approve gates or release account reservations.

## Evidence and remaining gates

`evidence/routed-runtime-tests.xml/log` and source binding record141 passing combined
tests, including lost create/start acknowledgments, uncertain exec, concurrent retries,
reconstructed-controller replay, identity/mount/network/resource drift, changed source,
fixed UID commands and actual local nofollow/hardlink refusal for the collection program.
Linux UID/group/socket metadata are synthetic in these local tests. No claim of actual
Docker or numeric-UID filesystem qualification is made.

Parent owns stage orchestration, routed_caller supervision and the checkpoint review.
Remaining integration proof: source-bound Linux caller launch; real mount/group access
and filesystem quota; relay readiness matching its controller grant/profile; protected
native/session broker binding; complete stage event collection; actual caller cleanup;
root/child orchestration and protected outcome gates. No provider-model call is needed
to first prove this path with a synthetic supervised provider.

## Cancellation and cleanup correction

`stop_caller(spec, full_runtime_id)` persists a cleanup fence before stopping, verifies physical stopped state, and returns a caller-only observation. `remove_caller(spec, full_runtime_id)` requires that durable stop proof, removes the exact ID, verifies absence, and persists a removal tombstone. Repeated calls after controller restart reconcile the same ID, including lost stop/remove acknowledgements. Create, reconcile-create, start and process launch refuse once cleanup begins. No operation grants account/provider cleanup authority or scheduler release by itself.

Cleanup inspection queries the full ID without label filters before checking the exact caller policy: a changed label cannot masquerade as absence. An unconfirmed create with no durable runtime ID remains uncertain; absent discovery does not prove that an unanswered create has settled. Discovery of all jobs remains the controller's responsibility; historical `Runtime.list_owned()` still lists legacy role `job` only. `Runtime.stop()` and `cleanup()` themselves already accept routed IDs, so the correction wraps existing actions with routed identity and durable state rather than replacing the runtime.

Existing private version1 journals without a cleanup field are read as cleanup-not-started. Subsequent writes add the field. These journals have not been deployed as live workflow state; no production migration was performed.

The container's init and relay working directory is `/`, so neither depends on workspace DAC permission. Hermes still receives explicit `--in /workspace`. Only inert inherited `io.cloudworkbench.candidate=true` and bounded `io.cloudworkbench.build-owner` metadata may accompany exact authority labels. Both writable tool tmpfs mounts are now noexec; Python stays at the pinned `/opt/hermes/venv/bin/python` image path.

## Relay bootstrap permissions (Linux v4 correction)

The relay is UID/GID1001 and must import the same public bootstrap package before it can read its own private client config. Task-root0750/GID1000 blocked that import. The controller now provisions task-root0751 (caller group can list; relay can only traverse), public `source` directories0555 and fixed source files0444. Prompt, launch/config and HTTP capability remain0440/GID1000; relay client config remains0440/GID1001 in its separate0750/GID1001 mount. Public code contains no credential values.

`prepare_caller` and subsequent file revalidation require UID1001 task-root traversal and public source read/traversal, and reject private task files readable by the relay. This models fixed numeric identities without supplementary groups. Ownership, no-write, no-symlink, fixed closure and file hash checks remain enforced. These checks do not grant UID1000 access to the relay client config/socket.

Frozen v5 includes parent atomic result/ready publication and the permission correction. The unit checkpoint has148 passing tests (`evidence/routed-relay-permission-tests.xml`). Actual Linux execution remains parent-owned. The prior failed v4 evidence is preserved; a ready-file timeout by itself is not a successful relay launch.
