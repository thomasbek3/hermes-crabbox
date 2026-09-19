> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Role broker to child scheduler: smallest correct integration

Design proposal from current source inspection, 2026-09-18. No Store/schema/runtime
implementation, migration or live operation is performed by this document. Preserve
Hermes as both coordinator and child runtime; a provider CLI remains inference-only.
This is an H2 dependency plan, not an A/H04 completion claim or a replacement spec.

## Required behavior and current gaps

D2 requires one admitted root to reserve two envelopes: its coordinator plus one
child slot. Panel seats serialize; a running leaf cannot wait for another compute
child. D6 makes the coordinator input read-only, coding seats isolated writers and
review seats readers of an exact immutable candidate. Roots and descendants share
explicit owner/project/session/turn and frozen policy, with independently fenced
attempt generations. See [D2](SPEC-FIRST-CONTRACT.md#d2--admission-and-time-budgets),
[D4](SPEC-FIRST-CONTRACT.md#d4--broker-channel-idempotency-and-external-reads),
[D6](SPEC-FIRST-CONTRACT.md#d6--revision-ownership-and-follow-up), and the normative
[D9 lifecycle/resource decisions](PROVIDER-TOPOLOGY-REVISION.md#concrete-decisions-after-fable-revise).
The supplement supersedes the older blanket one-active-attempt-per-turn wording
in SPEC §5 for routed child seats; it does not remove one active root or writer.

| Existing source | What is already usable | Concrete missing integration |
|---|---|---|
| [RoleBroker.prepare, lines275–301](../src/cloudworkbench/role_broker.py#L275) | Hashed scoped capability, native-call/payload identity and durable pending record | Creates no child, resolves no model, verifies no artifact ownership/content; its32-request limit is not32 seats |
| [RoleBroker.admit_in_transaction, lines316–355](../src/cloudworkbench/role_broker.py#L316) | Same-file SQLite transaction/savepoint for request linkage plus SQL child creation | No controller callback currently materializes authoritative request/seat/attempt rows |
| [RoleSocketServer.dispatch, lines264–280](../src/cloudworkbench/role_broker_transport.py#L264) | Bounded request/read transport to broker | Read returns pending/link metadata, not a paginated scheduler result graph |
| [RoleRouter, lines106–156](../src/cloudworkbench/pstack_routing.py#L106) | Pure configuration identity, exact inheritance, ordered duplicate panel seats, cross-family preference | No durable policy activation/seat records; readiness callback is external, and `seats` throws before returning the full panel if any selected route is unavailable |
| [Store schema, lines71–74](../src/cloudworkbench/store.py#L71) | Existing Attempts lifecycle/events/idempotency | `one_active_turn` prevents even a queued child next to its root; `one_live_session` and `one_live_credential(agent)` prevent concurrent root/child execution |
| [Store.claim_next, lines297–323](../src/cloudworkbench/store.py#L297) | Atomic legacy queue claim | `busy_sessions` and `busy_agents` independently reject same-session/Hermes children even if indexes were removed; counts live attempts rather than reserved root envelopes |
| [Store._new_attempt, lines167–175; fence_attempt, lines351–359](../src/cloudworkbench/store.py#L167) | IDs and CAS/event fences | Generation is allocated as session-wide `MAX+1`; it conflates chronology with the per-attempt recovery fence needed by routed budgets |
| [Store.get_session/resume/context, lines239–278 and380–396](../src/cloudworkbench/store.py#L239), [result bundle selection, line108](../src/cloudworkbench/result_bundle.py#L108) | Legacy parent-facing API/results | Latest generation may become a child; must select explicit roots, never return a child's result as the session result |
| [Runner.prepare, line432; capture_events, line549](../src/cloudworkbench/runner.py#L432), [Runtime workspace/native state, lines132–167](../src/cloudworkbench/runtime.py#L132) | Proven legacy workspace/runtime lifecycle | Uses session-scoped writable work/native directories and session repository baseline; routing two seats through it would share mutable state and reuse the wrong context |
| [InferenceBudget registration](../src/cloudworkbench/inference_budget.py#L212), [BudgetAuthority](../src/cloudworkbench/budget_authority.py#L76) | Frozen root/attempt ancestry, quotas/deadlines and read-only live ancestor predicate | Need trusted scheduler registration/atomic cancellation, not ancestry inferred from legacy parent fields |
| [ProviderLeases.reserve](../src/cloudworkbench/provider_leases.py#L241) | Logical account ownership and legacy exclusion | Public reserve opens its own transaction; multi-account root reservation cannot be made atomic by calling it repeatedly inside a Store transaction |

`attempts.parent_attempt_id` is legacy continuation provenance: `Store.resume`
references an earlier attempt, including an interrupted one. It is not routed
execution ancestry. Preserve it unchanged; never use it to infer a root or an
ancestor authorization edge. A legitimate new reconstructed root may have a legacy
parent pointer while its authoritative workflow parent is NULL.

## Minimal persistent extension of the existing scheduler

Use the same controller SQLite database, existing Attempts states, events and
worker loop. Do not introduce a second job-state store or launch directly from
broker HTTP. Co-locate RoleBroker tables in that database before issuing new route
capabilities; its same-file transaction check deliberately rejects a separate DB.
Existing standalone experimental broker DBs are not silently copied/adopted.
Instantiate the co-located broker with the explicitly registered service-group
permission mode when Store uses0660; do not broaden filesystem access or mount
the controller DB into jobs to work around its default0600 permission check.

Add an explicit `execution_kind` to attempts, default `legacy`, with allowed values
`legacy`, `hermes_root`, `hermes_child`; add `workflow_root_id`,
`workflow_parent_id`, `workflow_parent_generation`, `role_seat_id`, and a nullable
root-only `root_sequence` for chronology independent of fencing. Legacy rows
retain NULL workflow references. Root fields bind itself and no workflow parent;
child fields bind a registered root, actual parent/generation and seat. Validate
shape/references in SQL as well as the controller. No user request may set this
internal discriminator; the configured qualified workflow submission path owns it.

Use four orchestration records, plus the immutable policy registry described below:

1. **WorkflowRoot**, keyed by root attempt: immutable owner/project/session/turn,
   root sequence, policy snapshot/digest, coordinator profile snapshot, environment
   and qualification identities, base revision, frozen limits; mutable reservation
   state, child occupancy, cancellation and delivery CAS state. Store complete
   versioned route/profile/qualification snapshots, not only pointers into mutable
   files. `reserved`, `releasing` and `quarantined` all hold the two envelopes.
2. **RoleRequest**, keyed by controller request ID with unique broker pending ID:
   frozen parent/generation/native key/payload digest, canonical role, policy digest,
   dependencies/input context and planned seats; visible queued/blocked/resolved
   admission status. These are orchestration facts, not a duplicate Attempt lifecycle.
3. **RoleSeat**, unique `(role_request_id, ordinal)`: frozen requested alias and
   resolved BackendProfile, provider/model/effort/family, inheritance/diversity flag,
   tool authority, qualification/environment/runtime/skill digests, exact input
   revision, required-result flag. Attempts reference it; explicit retries create
   a new Attempt for the same seat, preserving failed attempts and route identity.
4. **WorkspaceRevision**, immutable manifest/content digest and scoped owner/project/
   session/root/producing attempt, base revision and export receipt. Session delivered
   revision is a separate CAS pointer; candidate existence is not delivery.

Root reservation/account references can be normalized into mapping tables where
foreign keys require it; these do not create another scheduler. A root policy
snapshot includes all profiles needed by the enabled workflow and chosen account
IDs. Initial policies must freeze32 total child seats,8 seats/request, depth 2, and
explicit attempt limits. A safe initial retry policy is zero automatic retry,
max 1 attempt/seat and32 child attempts/root; a different bounded retry policy is an
explicit version, never an implicit loop around failed launch.

Retain the current BackendProfile validation/resolution logic, but wrap it in an
immutable versioned policy record with the missing runtime/source/image/config/
tool/skill/auth-reference/endpoint-policy fields required by SPEC-FIRST §3. Reuse
the environment registry's register/qualify/CAS pattern, not its Docker-image probe
as proof of model readiness. `qualification_reference_verified=False` in current
Seat provenance is accurate until the trusted qualification lookup actually verifies
that bound receipt. Store planned identity separately from observed Hermes/provider
identity and effective effort; neither model text nor an arbitrary hash is proof.

## Database constraints and migration

Replace the three broad indexes **and both provider legacy guard triggers** only
in an explicit schema-version migration,
with equivalent legacy/root constraints plus child-specific guards:

- One pending/live **root or legacy** attempt per turn: retain the original
  queued-plus-live predicate, restricted to `execution_kind != 'hermes_child'`.
- One live **root or legacy** attempt per session, same restriction. This still
  prevents a follow-up/legacy root starting beside an existing root in that session.
- One live legacy credential slot per `agent`, restricted to `execution_kind='legacy'`.
  All routed agent identities remain `hermes`; actual backend serialization uses
  ProviderLeases account/request leases, not fake agent strings per model/seat.
- One live child per `workflow_root_id`, including preparing, waiting, held,
  checkpointing and verifying. Queued panel attempts are allowed beside the root.
- One queued/live attempt per role seat. SQL guards require a child to have matching
  registered root/request/seat/parent-generation records and consistent scope.

The same migration explicitly drops/recreates `provider_guard_legacy_insert` and
`provider_guard_legacy_update`: apply their existing account-reservation denial
only when `NEW.execution_kind='legacy'`. Replace, do not rely on `CREATE IF NOT
EXISTS` to alter predicates. Restrict legacy account-busy queries to legacy kinds;
call `legacy_blocked` only on the legacy claim branch. A routed root/child must
instead prove its exact root-owned reservation and per-request grant. This handles
both registered `legacy_agent='claude'` and any legacy Hermes mapping without
self-blocking the routed root or permitting simultaneous legacy capsule use.
Preflight inventories actual account mappings; no live mapping is assumed here.

Add SQL transition guards: execution kind/workflow identity is immutable after
insert; routed root queued→preparing requires its held two-envelope/root-account
records; routed child queued→preparing requires that exact live root/parent
fence, registered seat, reserved root and matching child-occupancy CAS. Foreign,
missing or stale occupancy is a refusal even if a wrong code path tries the update.
Legacy `Store.claim_next` must select `execution_kind='legacy'` exclusively.
A single worker admission dispatcher selects the typed legacy/root/child claim
routine; it cannot let the old loop race a second scheduler over all queued rows.
All lifecycle monitoring/preparation/export/recovery also branches by kind before
calling a legacy Runner method, not only at initial claim. A terminal root with a
queued child is rejected/fenced by workflow reconciliation; legacy claim never
picks it up on the shared session workspace.

Split backlog controls: preserve the configured100-pending public root/legacy-job
limit, excluding `hermes_child`; enforce committed child seat/attempt limits and
one-live-child occupancy separately. Only an admitted live root can materialize
children, so capacity2 permits at most one root's bounded unresolved child queue.
A failed transaction charges no seat/attempt quota; never-refund means committed
failed/cancelled seats remain counted, not that a rolled-back insertion consumes a
seat. The broker's separately committed pending-call count remains its own limit.

Live roots' held reservations cost 2 slots each; live legacy Attempts cost 1 slot each.
Under `BEGIN IMMEDIATE`, admission checks their sum plus independently inventoried
external reservations against configured capacity and host headroom. The root's
one child borrows its already reserved second envelope; never add a third slot.
At capacity 2, any live legacy attempt delays a new root, and a reserved root delays
all other roots/legacy starts. Keep visible queue reasons. Unknown runtime or
cleanup uncertainty retains reservation/occupancy even after process disappearance.
Do not count a child/provider gateway twice as both a root reservation and external
untracked work: labels and the owned-resource ledger must explain all components.

For both `hermes_root` and `hermes_child`, **generation starts at1** regardless of
legacy history. A typed `create_workflow_root_in_transaction` allocates root rows;
role admission allocates child rows. Neither calls the legacy `_new_attempt`
generation allocator. Typed routed fencing advances that attempt's current value
by exactly1 with CAS, budget-generation registration and grant revocation in the
same transaction. Legacy creation/fencing retains its old allocation semantics.
Broker keys, native binding, runtime labels and provider grants all use the stored
routed generation; no arithmetic based on siblings or session maxima remains in
that branch. Parent recovery fences old child edges instead of adopting them.

Allocate `root_sequence` monotonically per session for **every new root/legacy
submission**, never for children and never again during fencing. Migration assigns
this new chronology metadata to existing legacy rows in their current unambiguous
generation order, preserving existing fields; ambiguous existing chronology is a
preflight failure requiring disposition, not a silent tie-break/ancestry guess.
Do not add a new legacy `(session,generation)` uniqueness requirement: that index
is absent today and is unnecessary to fence a routed attempt by `(id,generation)`.
`get_session`, active-root selection, resume, environment/context lookup and default
bundle/diff choose non-child roots using explicit sequence/turn and delivered-
revision pointers. Their declared session state remains the latest root's state;
active root and child graph are separate fields. Routed resume/follow-up creates a
new root with next sequence, generation1, cold-context provenance and a new explicit
RootScope. A deliberate switch from legacy to routed execution likewise uses this
path; existing legacy parent pointers/results are retained, never relabeled.

Migration ordering: back up and prove restore; inspect exact schema/index definitions,
active attempts, legacy account owners and image references; quiesce all controller/
worker binaries sharing this DB; wait/reconcile owned live work rather than dropping
its constraints in place. Add nullable fields/default legacy classification, records
and guards, backfill legacy classification and the explicit root chronology metadata
only, drop/recreate the named guard triggers and indexes, and bump schema version
in one SQLite migration. Dump/read back `sqlite_master` against the exact approved
index/trigger DDL; checking object names alone is insufficient. Do not invent workflow ancestry for historical rows. Old
Store binaries must refuse the new schema before they can recreate old indexes or
claim rows. Rehearse against a copied real schema with queued/completed/failed legacy
jobs and preservation hashes first. Resume matching API/worker binaries only after
readback and startup validation. Preserve external legacy service/history/credentials.

Pre-activation rollback restores the verified DB/config/source set before new writes
resume. After new workflow writes are accepted, prefer a forward correction;
blindly restoring the old backup would discard valid new work. Do not run old workers
against the evolved schema. This document authorizes none of those live actions.

## Atomic broker admission and serial child execution

1. Broker `prepare` commits the exact native-call pending record as today. A worker
   scans pending records, not arbitrary messages. Its controller resolver joins
   ParentScope to the registered WorkflowRoot/parent and verifies native session,
   live/cancel/generation, owner/project/session/turn, capability and policy scope.
2. In one `BEGIN IMMEDIATE` transaction, invoke `broker.admit_in_transaction` with
   a SQL-only callback. Resolve from the root's **frozen** route snapshot; validate
   context artifact ownership, supplied digests and aggregate 1 MiB against trusted
   artifact metadata, then verify bytes before materialization. Reject arbitrary
   context paths. Preserve tool/task payload limits.
3. Materialize the complete ordered plan, RoleRequest, all seats and initial queued
   Attempts, root seat/attempt charges and events, then link the broker pending row
   in that same transaction. Count seats, not just requests: a3-seat panel charges 3;
   duplicate aliases retain 3 ordinals; cancelled/failed seats never refund quota.
   Identical native retry returns that mapping; conflicting payload returns409.
   Any insertion failure rolls back every seat/charge/link. No Docker/file/network
   work or nested independently committing Store method may run in the callback.
4. Split current `RoleRouter.seats` into pure deterministic planning plus readiness
   validation, preserving its public compatibility. This permits storing the full
   ordered panel even if one selected profile is unavailable. Freeze all planned
   routes; block the request visibly if a required selected route is unavailable.
   Do not shrink the panel, try another family or silently change effort. Revalidate
   trusted qualification/revocation at each actual start. Expensive image/file probes
   happen outside the write transaction; their immutable receipts/CAS version are
   checked inside it.
5. Root admission reserves all enabled workflow account identities in stable ID
   order, and both envelopes, **before** coordinator start and root wall timing.
   Acquire the account flocks in stable account-ID order **before** opening
   `BEGIN IMMEDIATE`, matching existing ProviderLeases lock order. Never acquire a
   flock while holding a SQLite write transaction. Extend ProviderLeases with a
   caller-transaction SQL reservation seam under those already-held locks; its
   current public `reserve` cannot provide this. Either
   acquire the complete set or roll back the new owner reservations; never hold one
   while waiting indefinitely for another. Historical account holders block this
   root before compute admission. Existing unresolved owner epochs require cleanup
   reconciliation, not automatic adoption.
6. Worker claims the next dependency-ready seat by request order then seat ordinal,
   CAS-acquires root child occupancy, sets Attempt preparing, registers its explicit
   budget ancestry/deadline and records launch intent in one transaction. Root
   inference budget starts at root admission. Freeze the leaf's overall deadline
   to min(root deadline, leaf admission + 10 minutes preparation + 1 hour execution).
   Preparation deadline is leaf admission + 10 minutes, clamped to that overall
   deadline. At the first preparing→running transition, persist a write-once
   execution-start timestamp and execution deadline min(overall deadline, start +
   1 hour); verification/waits remain inside that execution window. Add explicit
   phase-limit fields to routed attempt metadata and validate them during active
   supervision as well as worker claim. The overall InferenceBudget deadline never
   extends or resets; BudgetAuthority remains its read-only overall fence. The
   current single-deadline helper alone does not enforce these distinct phases.
7. Outside SQL, materialize isolated seat paths and launch pinned Hermes with its
   frozen role/profile/tool plan and existing D9 relay/provider service. Native
   session, relay journal, event spool, scratch, socket/capability directory and
   workspace are attempt/generation scoped. Reconcile durable launch intent and
   exact resource labels/IDs after lost create/start responses; no duplicate child.
8. Child work completes, exact runtime/provider cleanup is confirmed, immutable
   candidate/export/result receipt commits, then release child occupancy. An
   independent verifier may use that same slot sequentially; never run both under
   one uncounted allocation. Next panel/review/synthesis seat gets the slot only
   after prior occupancy is proven released. Parent stays alive/read-only and may
   wait, but holds no active provider request lease while a child needs it. Its
   logical account reservation remains root-owned between requests per D9.

For initial two-slot nested calls, **return `nesting_unsupported` before inserting
a broker pending row for an active leaf**. Check the trusted registered execution
kind inside `prepare`'s transaction after scope/capability validation and before
request-key/count/INSERT. Root issuance alone grants role-creation action; initial
leaf profiles omit that action/tool/socket, and a mistakenly provisioned leaf grant
still fails the server-side pre-insert kind check. Do not implement this as an
empty roles tuple: current `RoleBroker.issue` rejects empty roles. A required future
read-only role grant may use an explicitly qualified action set, not create authority.
This makes leaf rejection consume no broker request/seat quota. This is the explicit D2 alternative to flattening;
it avoids occupying the sole child slot while awaiting another child. Future
flattening must preserve the requester/native key and declared dependency graph;
do not reclassify a leaf as a root or create hidden extra slots.

## Quarantine reconciliation and reservation release

Root reservation state transitions are `reserved → releasing → released`; any
uncertain owned create/start/cleanup leads to `quarantined`, retaining both slots.
Blocked work shows `root_cleanup_required` with the scoped root identity and held
capacity. There is no timeout-based force-clear. The trusted reconciliation path
first fences every start/grant/descendant, takes the same ordered account locks,
and inspects exact root/attempt/generation/request/launch labels and full resource
IDs, including gateways and pending operation intents. A single absent inspection
after uncertain Docker create/start is insufficient under D9.

A fixed privileged operator command may request reconciliation of a root by ID and
expected reservation version; it does not accept arbitrary resources, a model's
"clean" assertion, or a force-release switch. It records exact inventory/operation
receipts and needs the configured administer authority (or explicit local operator
identity until that API exists). Normal definitive cleanup may use the same trusted
service path automatically. Once every owned/pending effect is confirmed terminated
or equivalently fenced, transition by version-CAS `quarantined → releasing`, then
commit the cleanup receipt plus released occupancy/accounts/root slots in the same
controller transaction. Recheck graph/grant/resource versions before that commit;
a stale receipt or new pending effect fails closed. Interrupted nonterminal
attempts receive final status/events, but their history stays intact.

If no definitive external outcome is available, retain quarantine and name the
required operator-assisted Docker/resource reconciliation; do not stop the shared
daemon automatically. The release command itself remains an implementation/acceptance
gate. Tests must prove refusal with absent/stale/wrong-owner receipts and successful
unblocking only after exact clean inventory. Thus quarantine can stop capacity2
admission deliberately, with an explicit safe recovery route rather than undefined
manual SQL or silent slot reuse.

## Workspaces, delivery, cancellation and results

A routed launch must not call the legacy `make_workspace(session_id)` path for its
mutable seat/native data. Retain the legacy branch unchanged. Root mounts exact
input revision read-only plus private scratch; coding/synthesis gets a private
writable copy; reviewers mount the stopped candidate read-only plus separate scratch.
No shared writable session tree or auth home is inherited. Enforced mount/tool
policy and nofollow export validation are required, not prompt instructions.

Reviewer failure is a visible required outcome. A correction is a new bounded
role request/seat or policy-authorized retry, with a new immutable revision. Root
success requires all mandatory results resolved, final protected checks bound to
the chosen revision, no live/unknown descendant, and CAS delivery promotion against
the expected prior revision. A child exit0, linked broker record or passing model
review alone cannot complete the root. Root result includes bounded child/route/
revision references, preserving controller-vs-provider trust labels and unknown
usage/cost rather than inventing it.

Cancellation is a same-transaction root/descendant graph fence: mark queued children
cancelled, flag active ones, revoke broker/execution grants, persist events and deny
new mappings/claims. Each active provider call uses BudgetAuthority plus existing
grant/account/profile checks during execution; actual Hermes/tool/verifier/provider
containers still require bounded stop/remove proof. Never release uncertain leases
or two-envelope reservation merely because a root becomes terminal. A leaf cancel
need not cancel the coordinator, but its required failure must remain visible.
Root wall expiry follows the root-cancel graph and fixed `root_budget_exceeded` reason.

Controller restart reconstructs existing graph/occupancy/launch/export intents.
Reconnect a still-alive coordinator only under matching native session, generation
and runtime identity. If it vanished and native continuation is unqualified, interrupt
it, fence descendants, retain results, and require the explicit reconstructed
follow-up contract. Do not replay the root prompt or regenerate broker call IDs.
Historical direct-Claude results and original model/image labels remain retrievable. If the coordinator exits before mandatory outcomes are resolved,
mark the workflow incomplete and fence/reconcile descendants; do not continue
unowned child launches or call that root successful.

Resolve file input revisions from qualified `context_refs`, not task prose. Initial
implementation may default to the root base when policy explicitly permits it;
review requires exactly one scoped revision-manifest reference with matching hash.
Correction implementation likewise identifies that candidate. Synthesis's declared
ordered context distinguishes one base and additional candidate artifacts; freeze
those references before launch. Missing/ambiguous revision selection is a refusal,
not "latest workspace". Panel writers share the declared immutable input baseline,
not each other's mutable or automatically selected previous output.

Keep POST role calls bounded and nonblocking: they return request identity/status.
Extend scoped broker reads through a controller-owned result projection to paginate
seat state/provenance/result references; it currently returns only linkage. Public
workflow/route/policy read endpoints reuse observe/project/owner checks. SSE emits
persisted child events. Existing session result selectors return the root, never
whichever child has the largest generation. Context assembly admits only approved
summaries/revision references; no native hidden state or unrelated child transcripts.

The trusted root `cloud_get_role_results` handler should wait using bounded repeated
socket reads for the same native tool call, with cancellation/remaining-root deadline
checks and bounded polling backoff. Each socket operation retains existing short
absolute limits; no DB transaction or provider request lease remains held while
waiting. It returns a result/refusal to Hermes once the requested outcome resolves;
it must not spend a new model inference just to poll every second. Parent Attempt
stays running with a visible orchestration wait reason, not human `waiting_input`
or takeover `held`. A lost role-read response retries the same read/native identity;
this does not relax D9's separate lost-inference-response interruption rule. This
awaiting behavior needs an actual pinned native-tool fixture before activation.

## Acceptance sequence before activation

| Gate | Required test/evidence |
|---|---|
| Migration | Copied actual DB: old row/content hashes and artifacts retained; queued legacy behavior unchanged; new binaries enforce root/child guards; old binaries refuse; backup restore succeeds |
| Atomic linkage | Kill process before/after callback/link commit; same native retry yields one request, each ordinal once and one initial Attempt/seat; injected partial failure leaves no orphan charge/mapping |
| Capacity |20 parallel root submissions at capacity 2 yield exactly one two-slot reservation; root+one child active, second root/legacy queued; panel seats serial, waiter footprint counted, failed cleanup retains slot |
| Account ordering | Same-agent legacy guards exclude routed kinds, no self-deadlock; ordered flocks precede SQL; two profiles sharing an account serialize inference; different accounts reserved all-or-none; legacy jobs block acquisition; no idle credential-bearing process or hidden provider allocation |
| Immutable identity | Config activation/restart does not reroute seats; duplicates preserve ordinals; inheritance exact; unavailable requested Grok/Fable/Opus route stays blocked; actual Hermes/profile/provider/effort receipts distinguished |
| Budget graph | Mixed legacy history then root+child uses routed generation1 and exact+1 fence; child creation registers exact explicit ancestry; no legacy-parent inference; quota 32 seats and32/128 inference counts survive failure/retry/restart; root/leaf/phase expiry and ancestor cancellation revoke active calls |
| Data authority | Cross-owner context and changed hash reject; root/reviewer cannot modify delivery; separate native/relay/scratch state; candidate export safe; CAS prevents stale promotion |
| Recovery | Crash before/after Docker creation and completion/export commit; no duplicate seat runtime; lost role response preserves mapping; lost inference response interrupts without transparent re-execution |
| Queue isolation | Legacy claim/monitor never touches routed rows; root death with queued children creates no shared-workspace launch; leaf role request writes no pending row; committed quota differs from rolled-back admission |
| Quarantine release | Capacity remains held without proof; privileged reconciliation rejects stale/foreign/absent receipts, releases only exact cleaned root, then next queued work can claim |
| Outcome | Seeded bug→Hermes implementation→distinct real Hermes review model detects issue→correction→protected verifier passes exact revision; failed/missing required review cannot mark root verified |
| Results/regression | Paginated graph/CLI/SSE/result bundle bind root+all children and revisions; wrong owner denied; follow-up reconstructs delivered revision; old direct-Claude jobs remain original identities |

Implement in that order as narrow reviewable checkpoints: schema/reservation and
atomic broker mapping fixtures; routed workspace/Hermes lifecycle with synthetic
inference; cancellation/recovery/results; then authorized actual-model qualification.
A same-model synthetic pair proves mechanics only. The requested Grok/Fable role
policy remains blocked until exact routes/account authority qualify; no silent
same-backend alternative and no direct-provider-CLI child can satisfy the target.
