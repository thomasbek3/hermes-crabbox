> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Personal Cloud Agent Platform — implementation specification

Approved model and Jev workflow policy: [PSTACK-MODEL-POLICY.md](PSTACK-MODEL-POLICY.md). It supersedes older role mappings below. the original operator has resumed implementation; the policy remains unactivated pending qualification.

Status: full-spec revision after Fable REVISE and documented corrections; release acceptance remains incomplete. Owner: the original operator. Date: 2026-09-17. Architecture correction: Hermes is the required agent runtime; the earlier revision 4 described separate agent products and did not preserve this requirement. Existing direct Claude Code execution is an infrastructure qualification, not Hermes acceptance. See [Hermes runtime correction](HERMES-RUNTIME-CORRECTION.md). Historical frozen evidence remains unchanged.

Normative sequencing and Hermes/pstack contract: [Full-spec baseline](SPEC-FIRST-CONTRACT.md). It defines required multi-model routing, release additions H01–H08, and the current spec-first checkpoint. Older status/sequencing statements do not authorize continuing builds before that checkpoint closes.

## 1. Outcome and scope

Evolve the existing Omarchy `cloudd` service into a private workbench where the original operator or an authorized agent can submit work, observe progress, intervene, resume, and receive tested deliverables. Target workloads: coding, browser-based verification, research, and file/report production. Initial release supports coding and file/report jobs from supplied inputs; unrestricted browsing/research is gated on the credential-isolating gateway profile. Use Hermes as the agent runtime. Configure Claude, Codex, Grok and other supported model/provider backends behind Hermes; they are not separate cloud-agent products. Qualify each exact Hermes backend, transport, model identity and credential mode before enabling it. Do not build a new reasoning model.

Target the practical Devin/Cursor experience: prepared environments, continuing conversations, live progress, terminal/browser access, evidence, and clean handoff. This specification is not a claim of product parity, task success rate, or VM-grade container isolation.

Initial deployment: one owner, one Linux worker on the existing 32 GB Intel Omarchy laptop. No Kubernetes, shared cluster, payment system, public signup, or public inbound service. Native macOS/iOS and Windows tasks require later OS-specific workers. The laptop remains a daily-use machine; cloud jobs must not exhaust its resources. A sleeping/offline laptop cannot execute tasks. Remote workers are a later extension of the same protocol.

This document authorizes no production migration, credential rotation, purchase, or deployment itself. Implementers must use the operator's task authorization and preserve existing jobs and unrelated services.

## 2. Historical v1 baseline versus intended platform

Verified 2026-09-17 via SSH as `operator@worker.example.ts.net`, hostname `omarchy`:

| Area | Observed implementation | Required change |
|---|---|---|
| Runtime | Ubuntu image; Claude/Codex/Hermes launch paths | Pinned Hermes runtime with capability-tested backend transports and images |
| Limits | `docker run --memory 4g --cpus 2`; configured maximum six | Atomic admission including preparation, queue, disk/PID/log/time limits |
| API | Shared bearer token, tailnet-bound port 7777 | Named client identities, scopes, versioned API and safe errors |
| State | In-memory `jobs` dictionary | Transactional durable sessions, turns, attempts and events |
| Recovery | Adopts labeled containers as agent `?`, prompt `(adopted)` | Restore original provenance, reconcile uncertain executions |
| Credentials | Copies provider files; Hermes broadly copies its home tree; mtime-based copyback | Explicit credential inventory, dedicated service-owned auth state, no personal-home copyback |
| Repo launch | Concatenated Bash clone command; Windows `list2cmdline` for Bash | Argv-safe clone and launch, strict inputs |
| Result | Exit zero sets `done`; text logs and workspace file retrieval | Execution outcome distinct from verified task outcome; binary artifacts and manifest |
| Conversation | One process/prompt per job | Follow-up turns, native resume or clearly labeled checkpoint fallback |
| Hardware snapshot | 31 GiB RAM reported, 23 GiB available; 876 GiB root free | Runtime admission uses current measurements, not this snapshot |

One real Claude job (`0917-130430-da69`) returned `HELLO FROM CLOUD`. No provider/isolation/recovery promises follow from that alone. Source inspection is based on copies of `cloud/server/cloudd.py` and `cloud/image/Dockerfile`, not an exhaustive host audit. Service is a system unit at `/etc/systemd/system/cloudd.service`, observed active. Additional host read at 2026-09-17T17:51:29Z: service User=thomas; jobs filesystem Btrfs; Docker reports default seccomp/cgroup namespaces and no rootless security option. Existing image ID `sha256:b08595b4ff44def60a66aa0bbd5614cdee71f2852088ed27f774af7f7d94e1f6`. These are v1 facts, not the desired v2 account/isolation configuration. Source SHA256: `0d60f0099a9d25b5f12413236a71cd9c2dad198065b9b778920951d8858ec70c`; Dockerfile SHA256: `d1f1e71261b21027c930c261371db3f4673ff929df81998e7a567de6eeff7fa1`. Local review copies matched host hashes.

Two offline source reproductions (no live jobs launched): eight concurrent requests passed a six-job admission cap; a literal `$(printf EXPANDED)` in a prompt was expanded by the Bash construction used on the repo path. The latter executes within the job container, not directly on the host, but changes task semantics and can expose credentials available there.

## 3. Product contract

Every submitted task declares goal, project or input files, requested agent/model, acceptance criteria, access profile, and limits. Defaults must be visible before submission. The owner can start, inspect, send a follow-up, cancel, resume from a supported checkpoint, retrieve results, and archive. A dashboard and CLI are clients of the same API.

Success means the declared mandatory checks passed against the delivered revision/artifacts. `exit_code=0` is only an execution fact. A missing test, human acceptance, or provider receipt produces `unverified` or `needs_review`, never fabricated success. Research/document jobs use named evidence and content checks rather than irrelevant code tests.

Do not promise arbitrary pause/resume. Native conversation continuation, filesystem recovery, and suspension of a running process are three different capabilities. Surface which one a given adapter supports.

## 4. Architecture decisions

Use one Python control service (FastAPI or equivalent), SQLite in WAL mode on local disk, a single scheduler, a local worker supervisor, and a managed filesystem store. Use SSE for incremental events, HTTP for mutations/downloads, and a narrowly scoped WebSocket terminal/desktop gateway later. No external queue/database dependency in the first release.

Logical components:

1. **API/auth:** validates requests, resolves client/project scopes and idempotency; never invokes untrusted shell text.
2. **Controller/database:** authoritative session and attempt records, event append, policies, approvals and retention.
3. **Scheduler:** durable queue, atomic worker and credential reservations, admission budgets and retry policy.
4. **Worker supervisor:** creates only approved runtime configurations, manages processes/networks/volumes, spools events, checkpoints and exports files. Only this trusted service talks to Docker.
5. **Agent adapter:** version-specific launch/event/resume/cancel/usage interpretation. Agent text is untrusted output.
6. **Credential manager:** per-identity grants and exclusive leases, dedicated auth storage and revocation; no arbitrary host path selection.
7. **Artifact/preview gateway:** authenticated downloads and session-scoped application previews.
8. **Dashboard/CLI:** task interaction and visibility; losing a client connection never cancels a job.
9. **Egress gateway (runtime owner):** enforce destination profiles with provider-specific positive connectivity tests as well as escape-denial tests.

Initially these may be modules in one service, with explicit interfaces and a separate privileged worker boundary where practical. Only add a network worker protocol when a second worker is needed. A job must never receive the control service's bearer credentials or Docker socket. Run the controller under a dedicated `cloud-control` service identity distinct from the original operator and the mapped job UID; credential/artifact roots mode 0700. A narrowly exposed supervisor owns Docker privileges. P00 records rootful/rootless Docker and numeric UID mappings; do not give the controller unrestricted personal-home access. Any same-process prototype with Docker authority is explicitly trusted host administration, not the final privilege boundary.

## 5. Records and persistence

Use opaque UUIDs. UTC timestamps. Schema migrations are versioned and backed up before upgrade. Foreign keys on; sessions/turns/attempts are not interchangeable.

| Record | Required fields |
|---|---|
| ClientPrincipal | id, name, hashed credential, scopes, allowed_project_ids, created/revoked timestamps |
| Project | id, owner, display name, repository definitions, environment version, policy/secret references, test profile |
| EnvironmentVersion | id, manifest hash, image digest, OS/architecture, runtime/CLI versions, dependency build receipt, readiness |
| Session | id, owner, project, goal, workspace id, active turn, retention deadline/keep flag, access policy version |
| Turn | id, session id, ordered message id, prompt/input refs, requested adapter/model, acceptance criteria, outcome |
| Attempt | id, turn id, worker, runtime id, fencing generation, state, native conversation id, resolved model, starting commit, environment digest, timestamps, exit/reason, resource limits |
| Event | session id, monotonic sequence, attempt id, type, timestamp, redacted payload, schema version |
| Artifact | id, session/attempt, safe relative path, media type, bytes, SHA256, creation time, validation state, retention |
| Checkpoint | id, attempt, workspace snapshot reference, Git revision/patch, native state refs, summary, environment digest, consistency status |
| CredentialGrant | topology, persistent owner id, execution grant scope, inference/refresh lease references, identity id, project/adapter scope, mode, lease owner/generation/expiry, secret reference, readiness; never secret plaintext in DB events |
| Approval | id, exact proposed action digest, principal, allowed scope, expiry, used/revoked status, execution receipt |
| Verification | check id/version, target revision/artifact hash, command or rubric, mandatory flag, exit/result/evidence refs |

All state changes and associated events commit in one DB transaction. Unique constraints enforce one active attempt per turn and one active writer per session. Idempotency key plus principal plus request hash deduplicates retries; same key with another payload returns 409. Clients can reconnect from last event sequence. Raw stdout is chunked, bounded and separately retained; event history is not an unbounded stdout buffer.

These are logical contracts, not eleven independent microservices or mandatory tables on day one. Initial schema centers on Session, Turn, Attempt, Event and Artifact; single-owner/project/environment configuration may use validated immutable documents. Credential leases and idempotency reservations must still be durable and transactional. Add separate approval/worker administration when those capabilities are introduced.

## 6. Lifecycle, scheduling, and recovery

Attempt states: `queued`, `preparing`, `running`, `waiting_input`, `awaiting_approval`, `checkpointing`, `paused`, `held`, `verifying`, `completed`, `failed`, `cancelled`, `interrupted`. Completion has separate outcome: `verified`, `needs_review`, `unverified`, or `rejected`. `failed` records setup/auth/provider/timeout/OOM/verifier_infrastructure/approval_expired/permission_unsupported/internal categories. A mandatory acceptance-check failure produces `completed` with outcome `rejected`; `failed(verifier_infrastructure)` means verification could not execute reliably, not that a test assertion failed.

Legal transitions:
- queued -> preparing/cancelled; preparing -> running/failed/cancelled.
- running -> waiting_input/awaiting_approval/checkpointing/verifying/failed/cancelled/interrupted.
- waiting_input -> running/checkpointing/cancelled/interrupted; timeout follows the table below.
- awaiting_approval -> running/checkpointing/cancelled/interrupted/failed; approval expiry is failed(approval_expired).
- running/waiting_input/awaiting_approval -> held after a takeover barrier is established; held -> previous live state only after explicit release, or checkpointing/cancelled/interrupted.
- checkpointing -> paused/running/failed/interrupted; paused is a closed, checkpointed attempt with runtime/resources released; resume creates a NEW continuation attempt, never revives the old id.
- verifying -> completed/failed/cancelled/interrupted.
- terminal attempts never change back to running. A follow-up is a new Turn; retry/continuation is a new Attempt with parent reference.

| Waiting state | Timeout source | Required outcome |
|---|---|---|
| waiting_input | Configured input wait budget | checkpointing if supported, then paused only after a durable checkpoint; otherwise interrupted with workspace retained |
| awaiting_approval | Exact action approval expiry | failed(approval_expired); do not execute or automatically replay the action |

Routed Hermes admission and root budgets follow SPEC-FIRST-CONTRACT.md D2 (one root reserves coordinator plus child at two slots). Initial per-attempt defaults, tunable after measurement: two concurrent compute attempts; 2 CPUs, 4 GiB RAM, 512 PIDs, 20 GiB workspace, 100 MiB logs, 2 GiB exported artifacts per session; one-hour execution budget, ten-minute preparation budget, 15-minute input wait before checkpoint/release when supported. These are proposed limits, not measured capacity. Reserve at least 8 GiB RAM and 20 GiB disk for the host; measure pressure and stop admitting work before it is exhausted. Six is a configured ceiling, not a promise of six runnable jobs.

Preparing, running, held, checkpointing, verifying and live waiting attempts all hold resource reservations. Native-unsupported input-wait expiry becomes interrupted with workspace preserved, not a fictional checkpoint. Approval expiry follows the separate table above. Environment image builds are separate build jobs with their own budgets; attempt preparation starts only from a ready image and does not include image compilation. Credential lease may constrain parallelism below compute capacity. Reserve in a single transaction before Docker launch; name/label the container with attempt id, generation and image digest. Keep a durable launch intent before external creation. The supervisor enforces one writer; reconcile creation timeout by runtime identity instead of blindly creating again.

On restart: discover labeled runtimes, compare to DB, recover original adapter and workspace identity, reconnect only when generation/lease match. Mark missing runtime interrupted and preserve artifacts. Quarantine unknown orphan runtimes for owner inspection; do not silently adopt as a successful known job or delete. An uncertain external action never receives an automatic retry. Pure setup/retrieval steps may retry with bounded backoff. Provider 429/quota exhaustion releases compute when possible. Automatic retry is allowed only before tool side effects, or with an adapter-proven continuation of the exact interrupted call. If earlier tool effects are uncertain, mark interrupted and require an explicit resume decision; do not replay the turn.

Reconciliation handles each attempt independently: one malformed runtime response or missing directory cannot kill the reaper. Catch and persist stage/IO/container failures, release reservations, and clean up only owned staging data. Expose reaper heartbeat and stale-attempt age in readiness; an API that answers while its supervisor is dead is degraded, not healthy. Limit request bodies (initially 1 MiB) and stream logs/artifacts rather than materializing them fully in controller memory. Secrets and prompts should use protected files/stdin when the adapter supports it, rather than Docker command-line arguments.

Cancel: reject new tool actions, terminate the attempt process group/container, wait at most 10 seconds then force stop; revoke preview/terminal leases and credential grant; persist partial result. Cancel is not purge. A held process/container retains RAM and all leases for brief human takeover. A paused attempt has a durable checkpoint and no live runtime. Held timeout triggers checkpointing if supported, otherwise interrupted with workspace retained; it never silently resumes. Freeze/stop adapter tools and session side effects, drain or account for in-flight actions before issuing the takeover lease. If this barrier cannot be established, reject takeover with a reason.

## 7. Environments, workspaces, and Git

Project manifest must version: OS/architecture, base image digest, package lockfiles, CLIs, repository URLs and initial refs, trusted install scripts, startup commands, readiness probes, secret refs, network profile, selected skills/MCP, verification commands, resources and expected deliverables. Manifest changes create a new immutable environment version. Failed builds never replace the last passing version. Record exact version on every attempt.

Repositories are cloned with subprocess argv, an option terminator where supported, explicit destination and validation of allowed URL schemes/hosts/refs. Reject credential-bearing URLs, local/file protocols, leading option values and unsupported transports. Never insert API prompt/repo/branch/model values into Bash. Trusted project-defined shell scripts remain possible but execute only within their explicit build/job boundary.

One workspace writer per session, one branch per coding session. Record base commit and resulting commit/diff. Multi-repo manifests declare repo names, paths and base refs; delivery lists each changed repo separately. Follow-up while running queues a new message; steering is adapter-specific and must be acknowledged, not assumed. A native session id is scoped to adapter version and environment; fallback uses a curated checkpoint and states that model conversation memory was reconstructed.

Persist native conversation state in a per-session directory, mounted separately from provider-auth files. Keep a stable in-container workspace path and native session identity across continuation. Never mount a whole shared provider home as the auth capsule. For CLIs mixing auth and transcripts, adapter setup maps/redirects specific paths and probes the actual layout; if it cannot isolate them, disable native resume or that auth mode. Native state is part of encrypted session checkpoints, never an ordinary exported artifact or input to another session.

Snapshot only after quiescing writers and accounting for sidecar state. A file archive is not a complete database snapshot. For database tests prefer disposable seeded services; if database state must survive, use an explicit versioned dump/restore hook. Exclude provider secrets, sockets and caches from general workspace/artifact snapshots.

## 8. Provider credentials and supported concurrency

Do not inherit the current broad Hermes home copy or copy any job-modified `.env`, `config.yaml`, `.credentials.json`, or `auth.json` into the operator's personal home. Inventory required auth/config fields without printing their values. Unrelated email, browser, vault, deployment and messaging credentials must not accompany a general job.

**Historical direct-Claude profile; routed Hermes uses SPEC-FIRST-CONTRACT.md D1 instead:** a dedicated cloud credential capsule per provider account, created through an explicit supported login path, stored under the service's protected credential root. One exclusive writer/execution lease per capsule. It must not share auth files or concurrent refresh ownership with a personal desktop CLI. Same-account jobs queue until the adapter proves a safe supported concurrency mechanism. Different dedicated identities may run concurrently within worker limits.

The capsule may contain provider-native writable refresh state if the CLI requires it. Mount only the matching capsule and minimal allowlisted provider config to the authorized runtime; no personal home mount and no host-home sync-back. Agent code in that runtime can potentially read or alter those credentials: container separation and log redaction DO NOT hide credentials from arbitrary code inside the container. Therefore this initial profile is only for the operator's trusted workloads with explicit provider-account exposure accepted. An auth error or unexpected auth/config mutation quarantines the capsule for reauthentication; never trust file mtime as validity. The adapter allowlist explicitly defines credential filenames, schema and permitted refresh-field changes. Files outside that list, invalid schema, unexpected executable/config fields, or unrelated identity change are unexpected; normal allowlisted token refresh is not. Validate without logging values.

Gate each adapter on live tests of initial login, token refresh, expired auth, cancellation during refresh, restart and queued reuse. If the provider's supported CLI/account behavior cannot meet this ownership scheme, mark that adapter unavailable and offer explicit alternatives: manual reauthentication for limited runs, a supported credential broker, or an API-key mode requiring separate spending approval. Never pretend a generic OAuth broker works for every subscription. New accounts/subscriptions are an owner decision, not an implementation assumption.

Provider probe deliverable is a table recording: CLI/version, native login method, official credential path override, observed access/refresh expiry behavior, writable-state needs, refresh atomicity, same-account simultaneous logins, native transcript path isolation, resume identity requirements, proxy/base-URL behavior, normal auth/refresh through enforced egress, auto-update disablement, structured output, termination behavior, and provider-supported use. Do not inspect or print token values; use redacted event outcomes and auth success/failure. Tests run only on a designated disposable/test login, not by deliberately expiring the operator's active personal credentials. A supported long-lived automation token that avoids copying personal login state is preferable when available and verified.

Hermes is required in the initial release. A copied `claude-bridge` binary is not evidence of a supported backend: inspect the pinned Hermes source and qualify its actual backend transport, auth ownership, event/usage behavior and cancellation semantics. Do not assume a Claude-compatible OAuth proxy inherits official support. Reuse the already authorized dedicated Claude subscription login only through an established supported path; if that path is unavailable, surface the exact remaining backend/auth decision rather than silently substituting standalone Claude Code, a new API key or paid account. Existing direct Claude Code receipts remain historical infrastructure evidence. Reference for the separately evaluated Claude auth boundary: https://code.claude.com/docs/en/legal-and-compliance .

For stronger isolation, use provider-supported short-lived grants or a trusted model gateway with a narrow protocol and no tool execution in the credential-bearing gateway. Implement only after protocol and terms are checked for that adapter. For routed Hermes, the credential-free provider-service topology is now an explicit initial dependency under SPEC-FIRST-CONTRACT.md D1; broad-research qualification remains later. Capsule leakage grants provider-account access; preserve that fact in the UI and threat model.

Application/integration secrets are separate named grants per project/attempt. Prefer dedicated test/staging identities. Inject only required secrets, mask known values in logs/events/exports, and scan result artifacts before release. Redaction is best-effort and cannot prevent deliberate encoding/exfiltration by job code. Enforce limits through network and credential scope. Owner handles new MFA/login prompts. No vault-wide 1Password token inside jobs.

## 9. Runtime and network boundary

Default runtime: non-root UID, dropped capabilities, no-new-privileges, normal seccomp confinement, no privileged mode, no host PID/IPC/network, no Docker socket, no host home. Read-only base image where tooling permits with writable workspace and explicit tmpfs/cache directories. Dependencies installed at environment build time. The current image's passwordless sudo is incompatible with this default and must be removed from the runtime image. A development/build profile with extra privileges requires a separate reviewed boundary; it is not the standard agent job.

Dedicated network per session; only its declared sidecars share it. Block job access to host/control API, LAN/tailnet, link-local metadata and other sessions by default; allow specific integrations through a trusted gateway or explicit destination rules. Private-network grants are per project, time bounded, and audited. Policy enforced by worker/network layer, not just agent instructions. Test IPv4 and IPv6 and DNS/redirect paths. Broad web-research access requires a credential-isolating gateway profile: provider refresh/account credentials stay outside the research runtime, which receives a revocable task-scoped gateway capability. Such a capability can still be stolen and abused within its limits, so cap requests/lifetime/model scope and revoke at exit. Defer unrestricted research until this supported gateway mode is proven; do not combine the native credential capsule with broad browsing and describe it as secret-free.

Default coding profile is deny-by-default outbound with explicit provider, source-control and necessary package-registry domains through an egress gateway. Broad public browsing is a separately selected, later gateway-backed research profile. Neither permits direct private-address access, DNS-rebinding to private addresses, or arbitrary UDP/QUIC bypass. Readiness fails if the gateway/filtering is absent; never silently fall back to unrestricted Docker bridge. Registry access is primarily a build-time capability. A dedicated provider endpoint is an explicit network exception, not unrestricted access to the host. P08 must demonstrate provider login/refresh and authorized Git fetch succeed through permitted egress while direct bridge-gateway/host/private-network access is blocked. A local egress proxy may be the single explicitly permitted gateway listener; every other port on that address stays denied. Disable CLI self-updaters via supported settings and verify pre/post versions; if disablement is unsupported, preserve digest immutability and refuse drifted attempts. Do not publish job ports directly; proxy backends bind only to loopback or a private per-session network.

Docker containers share the host kernel. Initial release is single-owner trusted personal work, not a hostile multi-tenant sandbox. Untrusted downloaded code can still attack the boundary. Add dedicated VMs/microVMs or a separate expendable worker before offering arbitrary hostile-code execution or multiple mutually untrusted users.

Actual disk quota must be enforced, not a field in the DB. Phase 1 selects a supported bounded workspace store (for example a sized filesystem/volume) and reserves capacity before use. If quota cannot be enforced on current Btrfs/Docker configuration, lower feature scope to monitored soft limits explicitly and block the hard-quota release gate. Log rotation and artifact byte limits are independently enforced.

## 10. Adapter interface

`probe()`, `prepare()`, `start(turn, context, policy)`, `events(cursor)`, `deliver(message_or_input)`, `decide(approval_id, decision)`, `request_stop()`, `checkpoint()`, `resume(checkpoint, turn)`, `collect_result()`, `cleanup()`.

`deliver` and `decide` return acknowledged, queued or unsupported with a durable correlation id. A response to an input request references its id; POST messages either satisfies that request or creates a queued follow-up turn. Platform approvals are consumed by the trusted integration executor; they are not arbitrary model statements. For unsupported live delivery, keep the message queued until the next turn or checkpoint-assisted continuation and tell the caller. Never report an unsupported steering request as delivered.

Probe response contains exact CLI version, available auth mode, resolved model identity, supported native resume, structured events, input/approval callbacks, usage metrics and cancellation semantics. Each capability is supported/unsupported/untested. Never claim a feature from a CLI name alone.

Canonical event types: attempt.state, assistant.delta/message, tool.started/finished, command.started/finished, input.requested, approval.requested, artifact.created, verification.result, usage.updated, resource.sample, error, checkpoint.created. Use structured native output when available; plain-text fallback is labeled and must not fabricate tool traces or billable token counts. No raw secrets in events. Model output cannot directly authorize an approval or emit trusted verification receipts.

Each adapter has a versioned fixture suite and live smoke suite. Provider/model override must be honored or explicitly rejected. Native CLI permission mechanisms are not the platform's only authority boundary; externally consequential capabilities must use a controlled integration path.

## 11. API contract and clients

Version prefix `/v1`. Every mutation uses an idempotency key; invalid inputs -> 422, denied scope -> 403, unknown resource -> 404 without cross-owner disclosure, state conflict -> 409, unavailable capacity/provider -> queued state or 503 with retry information. Never return a job id as success after failed setup. Apply request size, rate, concurrency and response limits.

| Endpoint | Contract |
|---|---|
| POST /sessions | Create with project/env, goal, adapter/model, criteria and access/limit profile; 201 with session/turn/attempt ids |
| GET /sessions | Paginated, filterable summaries authorized for caller |
| GET /sessions/{id}/workflow; GET /attempts/{id}/route; GET /role-policies | Authorized paginated child graph and safe route/policy provenance per SPEC-FIRST-CONTRACT.md D4 |
| GET /sessions/{id} | State, active attempt, capability set, outcome, timing and next required action |
| POST /sessions/{id}/messages | Ordered follow-up; explicit queued/accepted steering receipt |
| POST /attempts/{id}/cancel | Idempotent stop; return cancellation state, preserve files |
| POST /sessions/{id}/resume | New attempt referencing supported checkpoint, or explicit fallback mode |
| GET /sessions/{id}/events | SSE with Last-Event-ID and backpressure/catch-up behavior |
| POST /inputs; PUT /inputs/{id}/content | Reserve an owner-scoped upload, stream up to configured byte limit (default 100 MiB/file), hash/validate then finalize; incomplete uploads unavailable to jobs |
| GET /sessions/{id}/artifacts | Manifest; authenticated binary download endpoint by artifact id |
| GET /sessions/{id}/diff | Versioned patch/commit view; bounded size |
| POST /sessions/{id}/takeover | Exclusive expiring lease for supported terminal/browser, pauses tool writes |
| POST /sessions/{id}/release | Revoke human write capability and resume only after explicit policy |
| POST /approvals/{id}/decision | Owner decision bound to exact action digest, expiry and single use |
| POST /sessions/{id}/archive | Hide from active view, preserve data |
| DELETE /sessions/{id} | Separate explicit purge, refuse running attempts until stopped, record tombstone |
| GET /capabilities, /health, /ready | Minimal public liveness; detailed authenticated adapter/worker readiness |

Input uploads use a separate streaming byte limit from 1 MiB JSON requests. Input ids must belong to the caller and be finalized; mount immutable copies read-only in authorized jobs. Scan archives for traversal/expansion limits before extraction.

Dashboard authentication uses a short-lived Secure, HttpOnly, SameSite=Strict session cookie issued by a one-time owner login flow over HTTPS, with Origin checks and CSRF tokens on mutations. SSE uses that same-origin cookie; no bearer tokens in event-stream URLs. CLI/API clients retain Authorization headers. Preview origins do not receive dashboard cookies; use an audience-limited exchange into a per-preview scoped session cookie, with WebSocket Origin and authorization checks.

Preview and terminal endpoints are short-lived session capabilities, never arbitrary host/port proxies. Named client keys stored hashed server-side; scopes such as submit, observe, retrieve, cancel, approve, administer. Tailnet membership alone is not application authorization. Owner setup grants intended clients; bootstrap shared token rotated only with coordinated client cutover. Keep secrets out of URLs and process arguments where possible. `/capabilities` returns effective profile limits, default settings, adapter support, and reserved/unavailable endpoint features. Through Release A, multi-repo UI/administration, takeover, approval management, notifications, and approve/administer client grants are RESERVED and return explicit unsupported errors. Legacy token maps to a named migration principal against a single default project/repo allowlist and only existing verb scopes; no automatic new approval/admin rights. Unknown ad-hoc repos are rejected or require explicit project registration. Release A uses a probed noninteractive adapter policy for already-authorized local actions. Any unexpected native permission prompt is immediately denied and visibly ends the attempt as failed(permission_unsupported), preserving the workspace; it never enters awaiting_approval. Release B enables awaiting_approval only when the controlled executor and owner decision path are ready.

CLI v2 supports submit, list, show, follow, message, cancel, resume, artifacts, download, archive, delete. Return nonzero on HTTP/protocol/job failure; bound network timeouts; never wait forever on null/unknown state. Preserve old `run/status/log/file/ls/rm/wait` via a documented compatibility adapter until existing clients migrate; old `rm` requires explicit destructive intent and must not silently mean cancel.

## 12. Browser, preview, and human interaction

Phase 2 adds a pinned browser and automation interface within session boundary. User-visible preview can be served without a full desktop. Phase 3 adds an isolated desktop and authenticated interactive terminal. Do not remotely control the operator's active Moonlight desktop to operate a cloud job.

One browser profile per session. Persistence across sessions is opt-in and scoped to a project/test identity; cookies are credentials, not ordinary artifacts. Human login assistance must stop agent interaction and use a takeover lease. Lease disconnect leaves the session held and visibly awaiting a decision; held timeout leads to checkpointing or interrupted under section 6, never paused without a durable checkpoint;  it must not silently resume actions while a human may still be typing. Explicit release invalidates browser and terminal write tokens before agent resumes. Read-only observers may watch concurrently.

Preview router maps authorized session id to a registered service endpoint on that session network. No URL-supplied arbitrary upstream, no host root exposure, no public default. Use separate origin isolation per session, authenticate HTTP and WebSocket upgrade, expire routing after stop/retention. Render artifact HTML/PDF and agent markdown as untrusted content with sandboxing and escaped text.

## 13. Verification, results and approvals

Acceptance criteria are versioned at task creation; changes require a user-visible revision. Run mandatory checks in a separate verifier execution against exact delivered commit/artifact hashes. Stop/quiesce the agent runtime before starting verifier compute; both share one reservation sequentially. Store each verification runtime id and generation under its attempt. If sidecars remain, their resources are included in that reservation; no unaccounted double allocation. A worker can alter repository tests, so include protected external smoke checks for material behaviors. Record failures, skipped checks, changed tests, logs and screenshots. A screenshot demonstrates appearance, not full correctness.

Result bundle: `result.json`, concise `summary.md`, patch or repo/branch/commit refs, artifact manifest with hashes, verification receipts, unresolved issues, environment/CLI/model provenance, elapsed/runtime resource data and usage if available. Research results include sources and retrieval date; document results include file and content/render checks. Unknown cost/usage is null with reason, not zero.

Artifact export uses a quiesced workspace and descriptor-relative, no-follow file access or a platform-equivalent race-resistant mechanism; a string `realpath` prefix check alone is insufficient while a worker can swap paths. Reject symlinks/hardlinks outside policy, device nodes, sockets and pipes; cap bytes/file count/time. Export copies live in controller-owned immutable storage, not the worker-writable tree. Scan and hash those copies. Run verifier commands in a fresh bounded runtime with protected criteria and no provider credentials, never execute repo commands on the personal host as part of verification.

Release A delivers patches/files only. PR creation in Release B uses an explicitly granted repo-scoped integration; branch push/PR status is distinct from merge/deploy. Merging, deployments, account changes, external messages, spend, and deleting existing user data need task-scoped authority. Bind approvals to exact targets, content/diff hash, principal, expiry and invocation count. Changed action invalidates approval. Shell access plus unrestricted credentials cannot enforce these boundaries: do not provide such credentials to an agent and rely on text instructions to compensate.

Notifications: completion, failure, quota/auth block, or required human input only. Delivery through an opted-in connector and destination, with retries/deduplication. No periodic noise by default.

## 14. Dashboard and operations

UI pages: active/queued/recent sessions; per-session chat/timeline; files/diff/results; tests/evidence; preview/desktop; project environment builder; adapter/auth readiness; worker/resources; approvals. Show blocked reasons and available actions in plain language. Show native vs reconstructed continuation. Do not expose secret values. API works before UI exists.

Metrics: queue delay, preparation/execution/verification duration, adapter failures, auth failures, cancellation latency, restart recovery, memory/disk pressure, result validation, unknown usage, job count by state. Never call an agent transcript hidden chain-of-thought; record available messages/tool events and observable decisions. Log redaction tested across stdout/stderr/errors/download names.

Deploy service enabled at boot with explicit dependencies, health check and logs; distinguish running process from ready API. Detect worker offline/host reboot and preserve pending work. Optional independent observer on AI Mac mini can report laptop outage once authorized. No always-on promise until AC/lid/sleep/network/reboot behavior is tested.

Backups: consistent SQLite backup plus workspace/artifact manifests and needed files, encrypted off-host destination selected by owner. Exercise restore to a separate directory/host; never test restore over production. Initial retention: 30 days after last activity, configurable `keep`; propose scheduled cleanup only after user adopts the retention policy. Show deletion manifest; no live-job deletion. Secret/cookie retention is shorter and separately controlled. Separate cancel, archive, expire, and purge.

## 15. Implementation backlog and dependency gates

Each row is a handoff unit. Roles name responsibilities, not a mandate to spawn agents. Implement in a separate repository/branch; code review before rollout.

| ID | Owner/module | Deliverable | Acceptance | Depends |
|---|---|---|---|---|
| P00 | Operations | Inventory current jobs, source hashes, mounts, networking, service units, auth modes and clients without dumping secrets | Signed inventory and recoverable backup; no live job lost | — |
| P01 | Runtime | Offline regressions for current concurrency and argument handling | Original fails tests; candidate passes without live hostile execution | P00 |
| P02 | Auth | Adapter auth feasibility spike and provider capability matrix | Refresh/expiry/cancel/restart proved; unsupported modes blocked; owner decisions explicit | P00 |
| P03 | Storage | SQLite schema/migrations, event store, persistent result metadata | Transaction/crash tests and restore test pass | P00 |
| P04 | API | Validated requests, named clients/scopes/idempotency/error model | Cross-client denial and retry tests pass | P03 |
| P05 | Runtime | Argv-only repo/agent launch, pinned image/CLI inventory | Literal prompt preserved; option/protocol injection rejected | P01 |
| P06 | Auth | Dedicated credential capsules/leases and selected config injection | No personal-home copyback; same-account conflicts queue; no unrelated secrets | P02,P03 |
| P07 | Scheduler | Durable queue, atomic reservations, budgets and provider backoff | 20 simultaneous submissions run at most configured slots; 429/OOM visible | P03,P06 |
| P08 | Runtime | Runtime/network/storage confinement and egress gateway | Host/LAN/other-job denial plus provider auth/refresh/Git success; enforce PID/disk/log limits | P05,P06 |
| P09 | Runtime | Supervision, reconciliation, cancellation | Crash before/after create does not duplicate runtime; stop within bound | P07,P08 |
| P10 | Adapters | First proven adapter with structured events | Real repo task, tool receipt, failure and usage-unknown handling verified | P05,P06,P09 |
| P11 | Storage | Artifact export/download and retention controls | Binary roundtrip; traversal/symlink/oversize/HTML tests pass | P03,P08 |
| P12 | Client | CLI v2 and v1 compatibility | Existing smoke workflow works; errors return nonzero; disconnect safe | P04,P10,P11 |
| P13 | Sessions | Follow-ups, checkpoints and continuation capability | Same task follow-up succeeds; unsupported native resume labeled | P10,P11 |
| P14 | Environments | Versioned manifests/build/start/readiness | Failed build leaves active version intact; new job records digest | P05,P08 |
| P15 | Verification | Independent check runner and result bundle | Deliberate broken behavior cannot be marked verified from exit zero | P10,P11,P14 |
| P16 | Browser | Browser automation, screenshots and scoped previews | Reproduce/fix/test sample web bug; unauthorized preview denied | P08,P14,P15 |
| P17 | UI | Dashboard list/chat/events/results/diff | Reconnect resumes timeline without omissions/duplicates; accessible keyboard controls | P04,P12,P13,P15 |
| P18 | Interaction | Browser/terminal takeover leases | No agent writes during lease; disconnect holds; timeout follows section 6; release revokes write tokens | P13,P16,P17 |
| P19 | Integrations | Repo-scoped Git/PR delivery and action approvals | Wrong repo denied; approval replay/content change denied; draft PR checked | P04,P15 |
| P20 | Backends | Additional supported model/provider backends behind Hermes, including Codex and Grok | Same capability/acceptance suite; actual Hermes runtime, transport and model/provider identity verified | P02,P10,P13 |
| P21 | Integrations | Scoped MCP/skills and notifications | Selected tools only; notification dedupe; no arbitrary home config import | P04,P06,P19 |
| P22 | Operations | Reboot/offline/backups/retention deployment runbook | AC/lid/network/reboot and off-host restore rehearsals; no false success | P09,P11 (A core); P17 (B UI) |
| P23 | Portability | Second-worker protocol and stronger VM isolation design | Authenticated fenced worker; no active writer duplication; hostile profile gated | P22 |

Release A (useful headless service): P00–P15 plus P22 core reboot/restore/retention operations, Hermes runtime and pstack routing with at least two distinct qualified actual models required; H01–H08 in SPEC-FIRST-CONTRACT.md also apply. Direct Claude Code does not satisfy this gate. Release B (visual cloud workbench): P16–P19 and P22 dashboard/visual operations. Release C (broader integrations/providers): P20–P21 as each Hermes backend/integration passes. Hermes itself is not deferred to C. P23 is optional expansion, required before claiming hostile multi-tenant isolation. No calendar promise until P02 resolves auth feasibility and P00 inventory is complete.

## 16. Mandatory failure and acceptance tests

T01 simultaneous submissions: cap counts preparing/live-wait/verifying; no negative reservations.
T02 HTTP timeout after submission: duplicate idempotency key yields same attempt; mismatched payload rejected.
T03 crash controller before/after Docker create and before/after result commit: reconcile same attempt without rerunning external effects.
T04 prompt/repo/ref/model shell metacharacters, option injection and local path clone: literal inputs preserved or rejected.
T05 provider expired auth and refresh under same-account contention: serialized ownership, no personal auth corruption or secret logs.
T06 no native resume support: explicit fallback and persisted workspace; no claim of full conversation restoration. Input-wait expiry follows section 6 for supported and unsupported checkpoint adapters. In Release A, an unexpected native permission prompt immediately fails(permission_unsupported) with workspace retained, without entering an approval wait.
T07 memory/PID/disk/log exhaustion: bounded damage, truthful failure reason, sibling/host remains usable.
T08 host/control endpoint/LAN/tailnet/metadata/other-job reachability: blocked except declared grants, IPv4+IPv6.
T09 credential exposure: allowlisted files only; no broad Hermes integrations copied; canary secrets absent from ordinary logs and exports.
T10 artifact traversal, symlink switch, hardlinks, special files, huge binary and active HTML: cannot read outside authorized exported tree or execute in dashboard origin.
T11 cancellation during clone/test/tool execution: descendants stopped, partial artifacts retained, no stale lease.
T12 takeover race/disconnect/stale token: one writer, no silent resume, expired tokens rejected.
T13 exit zero plus broken feature/failed mandatory check: completed + rejected, never verified; verifier launch/tool failure instead gives failed(verifier_infrastructure). Evidence bound to exact revision.
T14 preview URL replay/cross-session HTTP/WebSocket request: authentication and origin isolation enforced.
T15 PR/approval target mutation or replay: deny; merge/deploy require separate authority. Approval expiry produces failed(approval_expired), no action execution and no automatic replay.
T16 host reboot/sleep/network outage, client disconnect and backup restore: tasks truthful and recoverable; old credentials not accidentally reactivated.
T17 real task benchmark: one known failing web interaction -> fix -> tests -> browser evidence -> patch -> follow-up adjustment. Owner opens delivered preview and checks behavior. Repeat for every adapter claiming equivalent capability.
T18 document task from supplied inputs: structured report/file exported intact and checked against rubric without fictitious software tests. T18R later adds open-web sourcing only after the gateway-backed research profile passes.
T19 reaper failure: simulated missing staging directory or failed Docker inspect affects only that attempt; heartbeat detects a dead supervisor.
T20 missing auth file or interrupted staging: request gets a stable failed/blocked record and reservations are released; no eternal `starting` entry.
T21 denied DNS/redirect/private-IP/UDP escape: chosen egress profile remains enforced or refuses launch.

Gate A-QUOTA is mandatory: demonstrate enforced per-workspace disk limit and bounded logs. If unsupported on current storage, Release A is blocked; a monitored soft-limit experiment may run only as explicitly labeled pre-release, never pass T07.

| Release gate | Required tests/evidence before rollout |
|---|---|
| A: headless production cutover | H01–H08 plus T01–T11, T13, T16, T18, T19–T21; T09 includes two-session native-state isolation; A-QUOTA; P02 positive auth probes, P08 positive egress probes, P13 continuation probes; T17 headless portion (known failing behavior, tests, patch, follow-up) |
| B: visual workbench | All A gates plus T12, T14, T15, full T17 with browser evidence/owner preview; dashboard CSRF/SSE/preview-origin and input upload tests |
| C: additional adapters/integrations | Repeat A and applicable B tests for each claimed adapter; scoped MCP/notification tests; no unsupported capability claimed |
| Research expansion | T18R, gateway token scope/expiry/revocation, credential-free research runtime, exfiltration/cross-project denial tests |
| Multi-worker / hostile-code expansion | Separate worker fencing, VM containment, cross-tenant and recovery qualification; no inherited pass from Docker-only A/B |

A gate involving an unavailable optional capability is explicitly N/A with reason and that feature disabled; foundational auth, quotas, state, recovery and artifact gates cannot be N/A.

Store each test's revision, environment digest, command/procedure, result, timestamps and evidence link. Any missing release-gate evidence stays explicitly unverified.

Run destructive/flood/fault tests only in an isolated test deployment with small synthetic quotas and a fake provider or designated test identity. Never fill the personal laptop disk, delete live job directories, force-expire personal logins or kill the production daemon as an unannounced test. Production validation is a bounded canary with rollback and the operator's rollout authority.

## 17. Rollout and owner decisions

Do not replace port 7777 immediately. Build v2 on a separate loopback/tailnet-only test endpoint with synthetic projects and test credential grants. Copy source and existing workspace manifests before touching v1. Import completed jobs as legacy records; preserve files and mark missing metadata unknown. During overlap, v2 admission counts v1 containers and their CPU/RAM/disk reservations too; do not let each scheduler independently consume the host reserve. Drain v1 rather than kill live jobs. Canary one real authorized task, verify result and restart recovery, then switch clients deliberately. Rollback keeps v2 DB/artifacts intact and restores previous service/client endpoint without replaying completed actions.

Required owner decisions before relevant phase: provider credential identities/exposure (P02/P06), any API spend, supported initial project and acceptance task, production integration authority, backup destination and retention policy. Default to two compute slots and existing laptop; no new hardware purchase assumed. Read-only research and specification can proceed without those choices.

## 18. Research references and boundaries

Accessed 2026-09-17; product features are vendor documentation claims, not hands-on comparative results.

- https://cursor.com/docs/cloud-agent — agents, tooling, artifacts, desktop, multi-repo context.
- https://cursor.com/docs/cloud-agent/setup — prepared environments, build/start separation.
- https://cursor.com/docs/cloud-agent/builds — versioned environment builds and activation.
- https://prod.cursor.com/docs/cloud-agent/security — isolated VM lifecycle, stored state and audit.
- https://prod.cursor.com/docs/cloud-agent/security-network — secret types and network controls.
- https://docs.devin.ai/onboard-devin/environment — snapshots, setup, repo and dependency readiness.
- https://docs.devin.ai/work-with-devin/devin-session-tools — shell, IDE, desktop and human takeover.
- https://docs.devin.ai/work-with-devin/testing-and-recordings — testing evidence.
- https://docs.devin.ai/product-guides/knowledge — reusable relevant project context.
- https://docs.devin.ai/product-guides/secrets — scoped credentials and browser authentication.

Design choices above are recommendations for the operator's private service, not a statement that either vendor implements this exact architecture. Commercial VM isolation and availability must not be attributed to the current Docker-on-laptop implementation.
