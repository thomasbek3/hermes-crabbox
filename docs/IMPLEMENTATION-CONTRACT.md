> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Shared implementation contracts

Historical initial implementation interfaces below; current normative requirements are SPEC.md and SPEC-FIRST-CONTRACT.md. Full-spec review precedes any further implementation. Earlier ownership and freeze directives below are historical and must be reassigned explicitly for resumed work. Python 3.11+, FastAPI/Pydantic2, standard-library SQLite, pytest. Local source is synchronized to /home/operator/cloud-workbench on Omarchy; do not alter legacy /home/operator/cloud or port7777. Keep auth secrets out of source/logs. This is real implementation with explicit unsupported capabilities, not mock success.

## Ownership
Control delegate owns models.py, store.py, api.py and tests/test_store.py, tests/test_api.py. Runtime delegate owns runtime.py, egress.py, deploy/Dockerfile*, tests/test_runtime.py, tests/test_egress.py and runtime-related scripts only. Parent owns runner.py, adapters.py, artifacts.py, cli.py, deployment integration, evidence/checkpoints. Ask via message before crossing ownership.

## Control-plane interface (freeze now; send any change to parent)
Store(db_path: Path) creates/migrates SQLite WAL. All methods return plain dictionaries/lists. Public errors class StoreError with status_code and detail. API create_app(store, settings=None) where settings dict includes artifact_root, input_root, capabilities, readiness callable if available.
add_client(name, token, scopes:list[str], projects:list[str]) -> principal dict; authenticate(token) -> principal or None. Store hashes tokens. Principal includes id, scopes, projects.
create_session(principal, request:dict, idempotency_key:str) -> dict {session_id,turn_id,attempt_id,state}; validate project belongs to principal. request keys project_id,goal,agent,model (optional),acceptance (list),input_ids (list), environment_version (optional). Store durable original request in turn JSON.
list_sessions(principal,limit=50,offset=0); get_session(principal,session_id) includes turns/attempts; add_message(principal,session_id,message,key); cancel(principal,attempt_id,key); resume(principal,session_id,key); archive/prune explicit separate operations.
claim_next(capacity=2,blocked_agents=None, external_running=0) -> attempt dict or None: atomic queued->preparing, one writer per session, one writer per agent credential identity initially agent name. Returns attempt id,session_id,turn_id,request (full task),generation. Use BEGIN IMMEDIATE. Counts all live states. Disabled agents stay queued with visible reason rather than launching.
transition(attempt_id,state,**fields) -> attempt dict, legality enforced and event in same transaction. Fields exit_code,reason,outcome,runtime_id,result. get_attempt(attempt_id) -> same flat dict; active_attempts() -> list; append_event(attempt_id,type,payload); events(principal,session_id,after=0,limit=1000).
register_artifact(attempt_id,metadata) -> artifact dict; list_artifacts(principal,session_id); get_artifact(principal,artifact_id). metadata id optional,path,sha256,bytes,mime,storage_path; storage_path is trusted internal and never returned in API.
Cancellation of queued is terminal immediately. Cancellation of running sets cancel_requested (does not free leases); runner stops then transitions cancelled. Resume always new attempt, preserving workspace, labeled reconstructed unless native proven. Followup creates new turn and queued attempt. Parent runner handles all Docker operations; API must not call Docker or shell.

## Runtime interface
Runtime(config:dict) config fields image (immutable sha256), root Path (workspace store), cpus=2,memory_mib=4096,pids=512,workspace_mib=20480,network_enabled=False. make_workspace(session_id)->Path. launch(attempt_id,session_id,argv:list[str],env:dict, mounts:list[dict]|None=None)->runtime_id. Additional mounts MUST be controlled config, never request strings. Work path /workspace; job UID1000 initially (distinct controller UID to be provisioned). status(runtime_id)->dict {state:'running'|'exited'|'missing',exit_code,oom}; stop(runtime_id); logs(runtime_id,max_bytes=...)->bytes; list_owned()->list of dicts; cleanup(runtime_id) only owned stopped runtimes. All subprocess argv only. No `shell=True`. Parent may adapt exact contract after delegate evidence.
Runtime does not read personal credentials. Default network none; future egress uses isolated internal per-session network and trusted restricted CONNECT gateway; deny host/private addresses, resolve/pin public address to avoid rebinding; IPv6 parity; fail closed. Root filesystem read-only, dropcaps,no-new-privileges,no sudo. Hard quota via bounded ext4 loop filesystem is preferred over enabling shared Btrfs qgroups. A test mode path-only workspace MUST be explicit and not release ready.

## Review/checkpoints
Checkpoint 1 inventory/auth feasibility + architecture interfaces; checkpoint2 durable service and runtime isolation; checkpoint3 actual adapter/end-to-end/recovery ReleaseA evidence; checkpoint4 browser/UI and ReleaseB. Fable review every checkpoint. User has authorized implementation on laptop, but no personal auth changes, new paid usage, or v1 replacement without gates. False completion forbidden.

## Checkpoint 1 mandatory corrections
- Runtime labels use io.cloudworkbench.*, names cwb2-*. NEVER cloudd=1 or v1 naming. Reconcilers require owned namespace and generation.
- Store transition requires expected_generation and rejects stale updates; runtime stop/cleanup require expected_generation. Runner is single OS-flock holder too.
- Parent runner owns legacy v1 active count and host-memory/disk-reserve measurement; claim passes external_running. API bind is 127.0.0.1:7780 for test service; wildcard forbidden.
- Legacy jobs contain credential copies: never walk/import/backup their claude/,codex/,hermes/,claude.json subtrees. Legacy importer remains disabled until workspace-only allowlist reviewed.
- Source-only backup was restored to isolated restore-proof directory and source hash matched. Fresh offline evidence v1-regressions.json includes timestamp/hash.
- Final identities: cloud-control API without Docker, cloud-worker trusted supervisor with Docker authority; both distinct from personal the original operator and job UID. Prototype launch as the original operator is only explicit test mode, never final boundary.
