> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Release B implementation plan

Source audit: 2026-09-17. This is a read-only audit and remaining-work specification, not implementation, deployment authorization, or a Release B pass. The concurrent Release A auth/retention/image rollout has its own source freeze and receipts. Recheck the bound deployed revision before building on it. The full-spec checkpoint in SPEC-FIRST-CONTRACT.md now precedes further implementation. The source-status table below is a historical audit, not a live deployment inventory.

## Required outcome and boundaries

Release B is **P16–P19 plus P22 dashboard/visual operations**, on top of every Release A gate. The required tests are T12 takeover races/disconnect/stale tokens, T14 cross-session preview HTTP/WebSocket authentication and origin isolation, T15 exact-target approvals/PR replay denial, and full T17: known failing web interaction → agent fix → independent tests → browser evidence → patch → follow-up adjustment → owner opens the delivered preview and checks behavior. Dashboard CSRF/SSE/preview-origin and supplied-input upload tests are also mandatory. A screenshot, an enum value, a disabled tab, a mocked approval receipt, or a successful localhost request cannot close these gates.

This plan does not expand into public signup, a hosted cluster, unrestricted research browsing, personal desktop control, notifications, additional adapters, or hostile multi-tenant guarantees. It does not drop the required interactive terminal/isolated desktop, environment builder, approvals UI, Git/PR integration or full browser benchmark. Phase 2 preview/browser work and Phase 3 terminal/desktop work are sequential parts of B, not excuses to call B done after a preview.

Authority remains scoped to the operator's existing build request. After the full-spec checkpoint closes, ordinary local implementation, fixture tests, approved-image preparation and isolated synthetic verification can proceed. Real credential grants, external repository writes, login identities, new externally managed domains and production cutover need their exact scope resolved from existing authorization or an owner choice; prepare the concrete target first. Preserve legacy v1, the personal Moonlight desktop, other jobs and current network policy during this planning unit.

## Current implementation versus required capability

| Area | Current source, reusable foundation | Remaining Release B gap |
|---|---|---|
| Dashboard auth | `dashboard.py` has exact-origin HTTPS, bounded short-lived hashed cookie sessions, Secure/HttpOnly/SameSite=Strict host cookies, CSRF/Origin checks, client revocation checks and nonce CSP. | No separate preview audience/session-cookie exchange, preview origin map, WebSocket authentication or takeover grants. One dashboard host does not provide per-session preview isolation. |
| Dashboard task flow | `static/dashboard.html` submits configured projects/models/environments, lists up to 100 tasks, shows turns/events, follows up, cancels/resumes, downloads immutable files and renders result JSON. | No list pagination/filter UI, upload UI (`input_ids` is always empty), formatted assistant/tool conversation, diff viewer, preview/desktop, environment builder, detailed adapter/auth/worker/resource pages, approvals, archive/retention controls, or real keyboard/reconnect acceptance suite. Preserve truthful reconstructed continuation and readiness labels while adding these. |
| Timeline | API supports SSE sequences and finite catch-up; page polls finite SSE every 3 seconds, deduplicates by sequence, keeps a memory cursor and displays at most 500 events. | Prove disconnect/reconnect and session switching in a real browser; explicit older-history/catch-up UI so the 500-row window does not imply older history was lost. Prove backlog >1,000, simultaneous new events, logout/expiry and stale responses without gaps or duplicates. |
| Runtime/network | `runtime.py` creates labeled, bounded containers and isolated attempt networks with scoped egress; no direct published job ports. Worker owns Docker. | No trusted application endpoint registry, preview/desktop/PTY roles, lifecycle/resource accounting for them, HTTP/WS routing or authenticated attachment. Do not turn the controller into a generic Docker/host-port proxy. |
| Browser | Current image/adapter launch is for coding; browser access is not a declared adapter capability. Claude uses a fixed shell/file tool set, no ambient MCP, and native persistence is disabled. | Pinned browser/automation interface and profile store inside the session boundary, explicit tool grants, trusted independent browser verifier, encrypted sensitive profile handling and isolation proofs. No personal Chrome/Moonlight reuse. |
| Takeover | State enums include `held` and `checkpointing`; API takeover/release return 501. Current adapter reports live delivery and native resume unsupported. | Actual barrier, durable exclusive expiring lease, human transport, per-message fencing, disconnect/timeout/release/recovery behavior and adapter capability probes. A transition to `held` alone does not stop tool writes. |
| Approvals | Approval endpoint returns 501; `approve`/`administer` are reserved client scopes. Unexpected provider permissions remain fail-closed in A. | Controlled action proposal/decision/execution lifecycle, exact digest binding, single use and timeout, explicit scope provisioning, trusted receipts, and complete UI. Agent text cannot approve itself. |
| Git delivery | `repositories.py`, runner delivery and `/diff` bind a registered base snapshot, sanitized workspace files and bounded patch/artifact evidence. | No repo-scoped push/PR credential broker, remote branch/commit/PR executor, approval binding or external readback. Source snapshot/diff support is not a remote commit/PR implementation. |
| Operations | A includes recovery/quota/backup/retention groundwork, with separate current qualification receipts. | Extend restart/readiness/cancel/retention behavior to preview/browser/PTY/desktop/approval leases and their resources; prove browser reconnect after service/client failures without replay or authority revival. |

Source anchors: [SPEC lifecycle](SPEC.md#6-lifecycle-scheduling-and-recovery), [API](SPEC.md#11-api-contract-and-clients), [browser interaction](SPEC.md#12-browser-preview-and-human-interaction), [approvals](SPEC.md#13-verification-results-and-approvals), [UI](SPEC.md#14-dashboard-and-operations), [gates](SPEC.md#16-mandatory-failure-and-acceptance-tests); [dashboard auth](../src/cloudworkbench/dashboard.py), [UI](../src/cloudworkbench/static/dashboard.html), [API](../src/cloudworkbench/api.py), [Store](../src/cloudworkbench/store.py), [adapter capabilities](../src/cloudworkbench/adapters.py), [runtime](../src/cloudworkbench/runtime.py), [runner](../src/cloudworkbench/runner.py), [repositories](../src/cloudworkbench/repositories.py). Existing `test_dashboard.py` and `test_api.py` are reusable server/unit tests, not substitutes for B's complete in-browser interactions.

## Dependency order and independently reviewable units

Suggested module names below define responsibilities, not a requirement to split the service into microservices. Each unit gets bounded tests and a checkpoint review before its capability is enabled. No unit silently broadens egress or mounts credentials.

### B00 — Freeze baseline and contract the new capabilities

Depends on P04/P08/P13/P14/P15 foundations. Implementation can be prepared during A qualification; B rollout cannot precede all A gates.

1. Bind the accepted A source/image/environment, adapter version, identity model, owner/project authorization, current resource reserve and restore receipts. Copy forward explicit unsupported capability states.
2. Version the capability response for browser automation, preview, browser observation, browser/terminal write takeover, checkpoint, approvals and repository delivery. Each is supported/unsupported/untested with a concrete reason. Verify the qualified Hermes adapter and its routed descendants' ability to stop tools and recover from a hold rather than inferring it from Docker or CLI names.
3. Specify durable controller records/migrations: `PreviewRegistration`, audience grant, `InteractionLease`, checkpoint/browser-profile references, `Approval`, `IntegrationExecution` and its resource reservation. Bind session, attempt, generation, owner/project and version on every record. Back up before migration; rollback preserves data but disables new capabilities.
4. Allocate service principals and channels: API authorizes and writes records; worker performs Docker/network/PTY operations; integration executor alone gets repo credentials. No controller, job, browser or verifier gets a Docker socket or general owner token.

Exit: schema/authorization/transition tests, additive upgrade/rollback test, capability false-by-default test; no new live endpoint enabled.

### B01 — Versioned web environment and resource accounting

Depends on B00 and P08/P14. Own environment manifest/runtime boundaries before browser/UI work.

1. Extend immutable manifests with named preview services, fixed internal protocol/port, trusted startup argv, readiness probe, browser/tool versions and profile policy. Resolve names from trusted configuration; reject prompt-supplied upstream URLs, ports, commands and arbitrary Docker targets.
2. Build a pinned browser-capable Linux image; lock dependencies and probe browser startup under existing non-root/capability/seccomp constraints. If it requires a security exception, isolate it and qualify that exact change rather than adding `--privileged` or disabling the sandbox casually.
3. Add labeled roles for application service, browser automation/verifier, interactive desktop and terminal companion. Every process/sidecar gets CPU/RAM/PID/log/disk/time limits and is accounted within its attempt reservation or its own explicitly admitted reservation. Preserve host RAM/disk floors and v1 overlap accounting.
4. Register only readiness-proven service endpoints discovered from owned labels/private network identity. Persist launch intent, handle lost create/start responses, and reconcile by session/attempt/generation/role. Never publish job ports directly.
5. Keep build-time registries, provider/source-control egress and session-local application traffic distinct. Permit only the specific browser-to-application path required for the named service; demonstrate that host/LAN/tailnet/metadata/other-job destinations remain blocked in IPv4 and IPv6. Research browsing stays disabled.

Exit: pinned image/start receipt, false readiness blocks registration, resource exhaustion preserves host/sibling usability, same-session positive HTTP/WS probes and cross-session/host denial.

### B02 — Per-session origin and authenticated HTTP preview gateway

Depends on B00–B01. Own a narrow `previews` registry plus gateway; keep Docker actions in the worker.

1. Resolve the live hostname/TLS scheme before deployment. Require distinct hostnames per session (and origin epochs where needed); port-only separation is insufficient because cookies are not port-scoped. Do not invent wildcard DNS/cert support under the current Tailscale hostname. Use owned private DNS/TLS or another explicitly verified private hostname mechanism. Default no public exposure.
2. Registry entry binds opaque service id, owner/project, session, attempt/generation or immutable delivered revision, exact frontend origin, worker-owned network/backend identity, allowed protocol, expiry and readiness. Backend cannot be chosen from request URL, Host, forwarded headers or query strings.
3. Dashboard grants a short-lived, single-use, audience-bound exchange only after same-origin authenticated mutation. Transfer it in a POST body to the exact preview origin, not a URL, log, referrer or browser storage. Redeem atomically for a distinct Secure/HttpOnly host-only preview cookie; revalidate owner/project/revocation and origin/generation. Dashboard cookies and reusable bearer credentials never reach preview backends.
4. Strip inbound Authorization/Cookie/forwarded hop headers before proxying unless an explicit service policy owns a separate application cookie namespace. Do not let upstream `Set-Cookie` replace platform cookies or set a parent Domain; mediate headers without silently breaking declared app auth. Application/profile cookies remain sensitive per-session state.
5. Bound connections, headers/body sizes, streaming response time and backpressure. Pin the registered backend rather than resolving arbitrary user hosts; handle redirects only under the registered frontend/app policy. Reject CONNECT, absolute-form proxy requests, host-header confusion and private upstream overrides.
6. Isolate active content from the dashboard. Prefer a separate-origin sandboxed preview frame or explicitly opened preview tab. Apply exact `frame-ancestors`, Referrer-Policy, permissions and narrowly selected CSP/connect rules without breaking the approved application's necessary behavior. Do not enable dashboard unsafe-inline/eval or serve uploaded HTML under its origin. Test sibling-preview cookie/domain injection, cross-origin fetch and frame attempts.
7. Revoke registrations/grants on stop, cancel, expiry, retention action and generation change. Existing active connections must stop, not merely reject future requests. Owner/project revocation invalidates existing grants.

Exit T14 HTTP portion: unauthenticated, cross-owner/project/session, stale generation, expired/replayed exchange, wrong Host/Origin and revoked-cookie requests denied; correct preview works; dashboard cookie absence proven at the upstream fixture; no arbitrary host/port routing.

### B03 — WebSocket authentication and delivered-preview lifecycle

Depends on B02; necessary for application HMR/WebSockets and later interaction.

1. Authorize every WebSocket upgrade against exact Origin, scoped preview cookie, registry readiness, owner/project, generation and expiry. Never use query bearer tokens. Refuse unauthorized protocols/subprotocols; don't treat an already authenticated HTTP page as authorization for a different WS target.
2. Add bounded bidirectional messages, queue limits, idle timeout and close semantics. Recheck lease/revocation while open; close existing sockets on cancel/expiry/revocation. An accepted upgrade must not remain a permanent bypass.
3. Separate job previews from delivered previews. Once the job/runtime stops its route expires. To inspect a completed result, explicitly launch a new bounded preview from the **verified immutable delivery hash** with a new registration/reservation and no provider credentials. Do not keep the original agent alive, silently relaunch it, or release compute accounting while its app remains running.
4. Serve immutable source read-only where possible, with separate bounded temporary application/cache storage. If an application requires database state, use the manifest's versioned disposable seed/dump/restore hook; do not call a file archive a complete database snapshot.
5. Bind preview receipts to delivery revision, image, startup/probe and lease lifetime. Show exact expiry and restarted-preview identity in UI. A new preview cookie does not revive old interaction write tokens.

Exit T14 full: cross-session URL replay and WS attempt fail, active sockets revoke promptly, correct HMR/application WS works, completed-delivery preview is independently admitted and serves the tested revision, cancellation leaves no unaccounted runtime.

### B04 — Session browser harness and independent visual verification

Depends on B01/B02 and P15. Gateway and harness development can proceed in parallel after the registry contract is fixed.

1. Add a versioned browser automation interface inside the session boundary with explicit, session/generation-scoped commands. Provide it only to the selected adapter through a narrowly granted tool/broker; do not import personal Chrome profiles, ambient MCP servers or local skill/config secrets.
2. Create one profile per session, separate from provider auth and ordinary artifacts. Default no cross-session reuse. An opt-in project/test identity can authorize persistent profile reuse only through explicit policy. Cookies/storage are credentials; exclude them from downloads and general artifacts and include only approved encrypted sensitive checkpoints.
3. Handle navigation, DOM observation, screenshot, click/type and bounded download/upload through the scoped interface. Restrict requests/redirects/DNS/private addresses and permitted application services; new pages, service workers and browser proxy/QUIC paths must not bypass the network boundary. Browser diagnostics must redact secrets and bounded sensitive state.
4. Build a trusted independent browser-verifier harness against the exact delivered revision/image and protected criteria. Capture failing baseline, successful interaction assertions, console/network failures and screenshots with timestamps/viewports/revision hashes. The agent's screenshot or assertion is not an independent test pass.
5. Snapshot/quiesce browser writers and account for app/database sidecars before checkpointing. Version browser/profile layout; corruption/incompatible versions fail visibly. Cookies are not exposed just because a user requested a screenshot or general workspace backup.

Exit: real sample web bug fails before the fix and passes after it; screenshot/console/network receipts refer to the same immutable delivery; two-session browser-cookie/storage isolation and secret-export denial pass; unsupported browser actions are explicit failures.

### B05 — Finish the task dashboard and accessibility baseline

Depends on P04/P12/P13/P15; can proceed alongside B01–B04 using clearly disabled future controls.

1. Add active/queued/recent filtering, authorized pagination and stable session navigation. List state/outcome and blocked reasons; handle empty/revoked/archived records and slow/offline worker truthfully.
2. Add real input upload: select file, display limit/progress/errors, reserve→stream→finalize via existing API, reference only finalized owner inputs at submission, remove/cancel incomplete UI entries safely. File-picker success is not upload success.
3. Render available assistant messages/tool events/commands as escaped observable content; do not label it chain-of-thought. Show queued follow-up versus proven live steering, and native versus reconstructed continuation. Separate execution, mandatory verification, manual review and final outcome.
4. Add bounded diff/file/result views tied to attempt/base/delivered revision, safe binary download, omission/unsupported patch indicators, tests/evidence and unresolved issues. No active artifact HTML/PDF executes in the dashboard origin.
5. Make SSE/catch-up robust and test in an actual browser: >1,000-event backlog, new events while catching up, reconnect from last sequence, page refresh, session switching, duplicate batches, 401/revocation, throttled/offline network and logout. Persist only nonsecret cursors if useful; bounded visible history needs an explicit older-history retrieval path. Keep cursor advancement tied to successful event processing.
6. Implement keyboard tab roving/focus, form errors, announced asynchronous status, meaningful labels, contrast and responsive layouts. Test keyboard-only create→inspect→follow-up→download, navigation after modal/errors, and focus recovery on reconnect. Existing `role=tab` markup is insufficient evidence.
7. Add archive/retention controls using the implemented API, showing keep/deadline/protected state and **dry-run** manifest without suggesting purge works. Expose configured defaults/access limits before submission. No destructive control masquerading as cancel/archive.

Exit P17 core: browser tests and inspected screenshots on narrow/wide layouts, no console failures, XSS/CSRF regressions, full keyboard task flow, upload ownership/limits, timeline no gaps/duplicates. Placeholder tabs do not count.

### B06 — Complete environment/readiness/resource pages

Depends on B05 and the immutable environment registry; completes SPEC §14 UI breadth rather than omitting administration surfaces.

1. Build the project environment builder around trusted/versioned manifests: show existing versions, validate candidate changes, stage build, display build/probe receipts, explicitly activate only a passing candidate, retain the prior active version on failure. Do not run submitted shell commands on the controller or mutate a version already referenced by an attempt.
2. Add adapter/auth readiness and worker/resources views: supported/unsupported/untested actions, exact environment/CLI/model provenance, credential blocked/renewal status without values, reservations/queue/pressure, worker heartbeat and degraded reasons. Unknown usage remains unknown with reason.
3. Define explicitly granted administrative capability and owner provisioning for environment activation or identity management before exposing those mutations; current submit permission must not silently become unrestricted admin. Read-only views can be built first. No automatic approve/administer grants to legacy/client tokens.
4. Add preview/desktop and approvals views as real backend capabilities arrive in later units, displaying exact origin, revision, expiry, current writer, and any required owner action.

Exit: failed-build-keeps-active-version browser flow; no secret values; scope denial; resource views agree with authoritative reservations/worker receipts; full page inventory exercised.

### B07 — Trusted takeover barrier and durable interaction leases

Depends on B00/B04 and P13; UI follows B05. Implement worker safety before exposing a write transport.

1. Add a durable `InteractionLease`: owner/principal, session/attempt/generation, browser or terminal audience, permitted read/write actions, previous live state, issuance/expiry, revocation sequence and status. One exclusive writer per session; read-only observers can coexist under separate grants.
2. `POST /sessions/{id}/takeover` is idempotent and rejects unsupported state/capability. Worker closes new tool admissions, freezes/stops all relevant agent/tool descendants and side-effect dispatch, and drains or explicitly accounts for in-flight actions. Only after a proved barrier and generation check may the DB enter held and issue human write authority. Uncertain external effects mean reject/interrupted, never a fictional successful hold.
3. Split agent execution from the app/browser/terminal processes needed by the human. Merely SIGSTOPing the CLI parent is insufficient: detached tools/background writers and in-flight integration calls remain. A tested cgroup freeze and fenced broker may be part of the solution, but the current adapter's unsupported live delivery/native resume cannot be hand-waved away. Account for provider request timeouts during holds without automatic replay.
4. Held attempts retain compute and applicable credential/resource reservations. Lease disconnect leaves held and visibly awaiting a decision. On held timeout, checkpoint only after a durable consistent checkpoint is proven; otherwise interrupt with workspace retained. No implicit resume on disconnect, expiry, reconnect or worker restart.
5. Explicit release revokes human tokens and closes/drains write channels before removing the barrier or resuming the previous allowed state. A previously awaiting approval action stays gated by its exact still-valid approval; release itself cannot approve it. Resume policy and adapter acknowledgment are visible, never inferred from UI button success.
6. Reconcile crashes before/after barrier, lease commit, token issuance, release and runtime stop. A stale lease/generation cannot control a replacement attempt. Cancellation fences all channels and terminates descendants within the required bound.

Exit T12 barrier: instrumented agent/tool/app writers show no unauthorized agent write during held; race two takeovers, delayed input after release, token replay/cross-session, human disconnect, timeout with/without checkpoint support and reaper restart. No test may label paused without a durable checkpoint.

### B08 — Authenticated browser/desktop and terminal transport

Depends on B03/B07 and B05 UI. Complete both transports; screenshot viewing alone does not satisfy P18.

1. Browser/desktop observation runs in a session-isolated display/profile, not the operator's Moonlight/X11/Wayland desktop. Publish only through the authenticated bounded gateway. Read-only observers get no keyboard/mouse, clipboard, upload or terminal authority.
2. A separate write lease gates every browser action/input frame at the worker, not merely the initial WebSocket upgrade. Verify current lease id, audience, epoch/generation and expiry; reject queued late packets after release. Human login assistance stops agent observation/control as required by the lease and uses only the selected test identity/profile.
3. Terminal attaches to a dedicated bounded PTY/companion under the job UID and authorized workspace. No host shell, Docker socket, provider-auth mount, arbitrary container target or privileged user. Session working directory/command selection comes from trusted policy; input is data to the authorized PTY, not a controller-shell string.
4. Bound PTY output/frame size, resize messages, connection count, clipboard/file transfer, idle time and scrollback. Restrict potentially dangerous terminal escape/clipboard behavior. Redact/export policy is separate from interactive visibility; never persist typed credentials as ordinary event logs.
5. Add UI writer/observer badges, lease countdown, disconnected-held warning, explicit release and cancel, keyboard/accessibility behavior and token-safe reconnect. Streaming connection loss must not cancel the job or silently restore agent writes.

Exit: T12 full browser+terminal scenarios; malicious terminal output cannot escape into dashboard DOM/clipboard; human writes affect only their session; replay/expired/wrong-audience/cross-owner transports fail; no personal desktop or auth capsule reachable.

### B09 — Exact-action approvals and controlled executor

Depends on P04/P15 and B00; may develop alongside preview/UI work, but no consequential execution before this boundary passes.

1. Create immutable action proposals with versioned canonical digest: integration/action kind, approved repo/account/project target, exact revision/tree/diff/content, destination/ref and relevant PR body/title/base, principal, expiry and allowed invocation count. Show an escaped human-readable consequence plus exact target. Changing any bound content creates a new proposal and invalidates old authority.
2. Provision explicit `approve` and required administration rights to intended named owners only. Existing submit/observe/legacy scopes remain unchanged. Decisions require owner/project scope, CSRF when cookie-authenticated, idempotency and CAS; agent output can request an action but cannot record an owner decision.
3. Executor reserves approved single-use authority atomically before invoking a narrow registered integration operation. It gets only the scoped credential required; the agent/terminal/verifier gets none. Persist prepared/executing/confirmed/failed/unknown outcome and provider request/receipt identifiers. Crash after an uncertain external write triggers read-only reconciliation, never unguarded retry.
4. Explicit denial performs no action. Expiry of a required pending approval produces failed(approval_expired), releases resources/grants after confirmed cleanup and does not execute/replay. Release B enters awaiting_approval only after this controlled owner decision path exists; A permission prompts remain unsupported until the adapter-specific bridge is proven.
5. Implement an approval inbox/detail plus task timeline status, digest/target change warnings, expiry and execution receipt. Granting an approval and externally completing the action are distinct statuses.
6. Keep merge, deployment, account changes, spend and messages out of the initial executor unless a separately authorized exact operation is implemented. Git branch push/draft PR approval does not grant any of those actions.

Exit T15 approval half: wrong owner/repo/action, modified target/body/diff, expired/replayed/revoked/over-count approval and concurrent consume all deny; crash-before/after remote effect reconciles without duplicate action; model-forged approval events cannot authorize execution.

### B10 — Repo-scoped branch push and draft PR delivery

Depends on B09 and verified P15 delivery. Integrate through the executor, not shell credentials in the job.

1. Select the exact allowed remote repository/base, integration identity and branch namespace. Acquire a narrowly scoped Git-host grant only for that target. No credential-bearing URLs, personal Git config, agent-supplied hooks or unrestricted PAT in a container. Qualify necessary Git/API egress destinations explicitly.
2. Build the deliverable tree/commit from the independently verified immutable base and exported content/metadata in trusted staging. Preserve modes/binary changes/deletions intentionally; unsupported symlink/submodule/omitted paths block automatic push until handled explicitly. A bounded text diff alone is not a complete commit. Disable hooks/external config/filter execution and validate argv/ref/path inputs.
3. Bind approval to exact base and produced head/tree/content digest, remote repository, branch action and draft PR metadata. Recheck the verification target before executing; any newer agent/owner edit requires new verification/proposal. Do not default to force-push; remote branch drift is a conflict.
4. Push one session-owned branch and create/update a draft PR using a durable action correlation identity. On timeout inspect the exact remote ref/head and existing PR before deciding confirmed/unknown; don't mint another branch/PR to hide ambiguity.
5. Read back remote repository, head commit, base branch, draft status and PR URL; persist external receipts and expose them alongside local test/patch evidence. Push, PR creation, merge and deployment remain distinct outcomes. No automatic PR external comment/notification beyond the explicitly authorized creation body.

Exit T15 Git half: wrong repo/ref denied; target mutation/replay fails; remote ref conflict/timeout reconciles; approved bounded fixture yields a verified draft PR with correct head/base/body and no merge/deploy. Actual external fixture writes require the chosen repo/grant and action authority.

### B11 — Full web benchmark, operations and Release B evidence

Depends on B02–B10 plus all A gates; do not replace this with isolated unit pass counts.

1. Use one registered small web repository with a reproducible failing user interaction and protected independent acceptance checks. Capture baseline failure; have the proven adapter fix it; bind tests/screenshots/console/network evidence to exported revision; deliver patch and working authenticated preview; complete a follow-up adjustment and reverify the new preview/revision. Owner opens the delivered preview and checks the named behavior. Record owner acceptance separately; an automated browser is not that receipt.
2. Exercise takeover mid-tool/browser action, human disconnect, stale token after release, cancellation, held timeout and controller/worker restart. Prove no dual writers, no leaked route/credential/capability and no unaccounted resource reservation. Browser/profile checkpoint success requires its actual consistency receipt.
3. Exercise dashboard logout/revocation/restart/offline reconnect, preview grant expiry while HTTP/WS is open, container generation replacement, retention keep/deadline changes and integration executor uncertain outcomes. Client disconnect does not cancel tasks; route expiry does not replay them.
4. Extend backup/restore/runbook coverage to new durable records, integration receipts and explicitly approved encrypted browser/native state. Credentials remain excluded/revoked on isolated restore. Do not silently adopt a secret/cookie cleanup schedule or test restore over production.
5. Bind every test to source, schema, image/browser/adapter version, environment, procedure, target hash, timestamps and actual receipt. Preserve every failed/skipped/unknown result. Review each checkpoint; a prior REVISE with verified disposition is not a blanket B PASS.
6. Roll out B capabilities disabled by default, then enable the qualified subset for a bounded owner canary and record real HTTP/WS/browser behavior. Final B claim requires all required B capabilities/gates; a preview-only milestone is labeled that explicitly. Existing A access and v1 remain recoverable; rollback revokes new grants and preserves state rather than replaying actions.

## Decisions needed versus work already authorized

| Decision or prerequisite | Why it matters | Work that can proceed before it |
|---|---|---|
| Exact privately reachable per-session hostname/TLS scheme, using an existing owned domain/account if available | Current single dashboard HTTPS hostname is insufficient; no assumed wildcard Tailscale certificate and no public DNS/purchase/network change by inference. | Registry/auth/gateway and integration tests using controlled test origins; inventory existing owned options read-only; prepare concrete configuration. |
| Exact Git-host repository/base/branch namespace and integration identity/rights for draft PR qualification | Real push/PR is an external write; broad build authority does not identify a target or grant a token unrestricted repo rights. Reuse already authorized exact repository actions if present. | Proposal schema, digest/replay tests, trusted tree construction, fake integration and read-only repository validation. |
| Selected application/test identity for browser login, whether any profile persists beyond its session | Cookies are credentials; personal login/profile reuse is not implicit. | Cookie-free sample app and disposable fixture identities; per-session profile isolation, takeover and encrypted-state tests. |
| Explicit intended approve/administer principals and allowed administration operations | Existing scopes are deliberately reserved; do not upgrade every client. | Provisioning/schema tests and read-only admin views; prepare exact grants. |
| Native/cookie/secret lifetime and opt-in persistence policy | SPEC says separate/shorter but gives no numerical default. Current retention fields are null/unmanaged. No scheduled deletion authority follows. | Policy UI, dry-run inventories, sensitive-state exclusion and expired synthetic-lease tests. |
| Owner check of the delivered sample preview; later deliberate production/legacy cutover | Full T17 explicitly requires owner interaction; deployment/cutover and migration are separate from a screenshot or local pass. | Finish the tested candidate, working private preview, rollback and concrete acceptance instructions before requesting the required human step. |

Ordinary engineering choices do not need a new owner ceremony: module structure, schema field names, pinned fixture browser/tool choice, test data, retry/byte/time/lease bounds within the established resource policy, UI layout and synthetic gateway simulations. New paid services, public exposure, broad credential grants or weakened runtime restrictions are not such choices. Keep unresolved choices visible without blocking independent build units.

## Suggested handoff checkpoints and verification receipts

| Checkpoint | Ready-to-review bundle | Mandatory observable proof |
|---|---|---|
| B-C1 | B00–B03: schema, environment, origin/auth gateway and preview lifecycle | T14 HTTP+WS denial/positive matrix; no dashboard cookies upstream; resource accounting and generation revocation; delivered preview tied to hash. |
| B-C2 | B04–B06: harness plus complete task/admin/readiness UI | Protected failing/passing web interaction; real browser timeline/reconnect/keyboard/upload/XSS/CSRF suite; versioned environment build failure preserves active version. |
| B-C3 | B07–B08: barrier, leases and real desktop/terminal | T12 race/disconnect/timeout/restart; instrumented one-writer proof; observer cannot type; release revokes stale channels before agent resumes. |
| B-C4 | B09–B10: approval executor and Git/PR | T15 exact digest/target/single-use denial matrix; crash reconciliation; actual approved draft PR readback, no merge/deploy. |
| B-C5 | B11: integrated benchmark and operations | All A+B gates mapped to bound receipts, full T17 follow-up/owner preview, recovery/restore extension and reversible canary. |

Parallelization is possible after shared contracts are frozen: B05/B06 dashboard work, B02/B03 gateway and B09 approval storage/executor can advance independently. B07 write takeover cannot be enabled before barrier proof; B10 cannot receive real Git credentials before executor scope/approval tests; B11 cannot claim a full pass while any mandatory capability is a placeholder. The plan includes remaining ordinary work even when an owner-specific live target is unresolved.
