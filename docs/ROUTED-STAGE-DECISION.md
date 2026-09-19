# Routed stage finalization

`routed_stage_decision.py` finalizes the current versioned workflow roles other
than `acceptance_verification`. It authenticates saved worker output, its exact
launch contract, captured revisions, current assignment, and combined cleanup.
It records a **stage-contract** decision, not protected task acceptance.

## API

```python
complete_stage(scheduler, routed, caller, results, *, input_revision,
               storage_root, services, forbidden_values) -> CommittedStage
release_stage_child(scheduler, routed, caller, results, *, input_revision,
                    decision_sha256, services, forbidden_values) -> dict
load_finalized_stage(db, child_id, *, forbidden_values=()) -> (body, metadata)
load_stage_release(db, child_id, *, forbidden_values=()) -> dict | None
```

The two loaders require a caller-owned SQLite transaction. They perform bounded
historical reads with no current-owner, runtime, or provider callback. The first
reads at most 256 KiB of canonical decision JSON and validates referenced
artifact metadata/events; it does not reread the potentially large worker
transcript or traverse workspace contents inside the transaction. The release
loader returns `None` only when an authenticated finalized stage has no release
event; a malformed event fails closed.

`complete_stage` requires a captured candidate for every supported role. Before
committing it reloads the authenticated saved observation, requiring successful
worker execution. It compares the published observation with those exact bytes,
then reads the private saved `launch.json`, bound by the caller-spec file hash
and observed launch-receipt hash. The expected contract is reconstructed from
the immutable workflow version and assigned role, input revision and ordered
context references. Only its saved final `adapter.result.summary` is parsed.
An in-memory replacement observation cannot override those bytes.

Both input and candidate revision trees are verified outside the final database
transaction. Read-only roles require identical file/directory/selection/scope
content and carry the original input revision forward. Coding roles carry the
exact captured candidate revision. Candidate identity must belong to this child
and generation; both revision bindings must belong to the same owner, project,
session, turn, root and root generation. The immutable artifact records both
revision hashes and bindings, even when read-only content is identical.

## Meaning of decisions

| Model contract | Attempt outcome | Stage gate |
| --- | --- | --- |
| `status: complete` or `recommendation: approve` | `unverified` | `pass`, scope `stage_contract` |
| `recommendation: revise` | `needs_review` | `reject`, scope `stage_contract` |
| `status/recommendation: needs_review` | `needs_review` | no gate |
| Invalid contract, incomplete execution, unknown cleanup or revoked authority | no terminal commit | no new gate |

A model's evidence, plan, resolution flags and recommendation remain unverified
claims. A stage pass permits only controller-enforced stage progression; it does
not establish acceptance criteria, root completion, or release an account.
Rejected/review-needed stages require explicit future remediation; this unit
creates no automatic retry or bypass. `acceptance_verification` must use the
separate protected-check decision path. Remediation-only and unknown roles are
unsupported.

## Artifact and replay contract

The artifact provenance is `controller_stage_contract`; its metadata has
`verification_scope: stage_contract` and `verification_pass: false`. The attempt
stores `result.routed_stage_decision = {artifact_id, sha256}`. The immutable JSON
body includes:

- Schema 1 and scope `stage_contract`; child/session/root/generation identity.
- Frozen step, role, workflow, assignment, caller-spec, runtime, profile and
  launch-binding hashes.
- Frozen stage-contract version/digest, normalized `stage_output` and its hash.
- Input/candidate/carried revision hashes, both revision bindings and read-only
  policy.
- Exact published observation/candidate artifact IDs and metadata hashes.
- Cleanup digest, reservation version, controller instance and retained account
  identities; derived outcome and scoped gate.

The artifact, terminal lifecycle, result pointer, grant revocation, optional
stage gate and `workflow.stage_decision` event commit in one bounded transaction
under the existing per-caller lock. Final authority, artifact events, metadata
and cleanup membership are rechecked there. A failed commit may leave an
unregistered immutable file; the same scoped retry can reuse it, without
rerunning the worker or inventing a result. Files are owner-readable/writable
0600 inside a caller-provisioned private 0700/2700 controller directory, fsynced
before registration. Storage must be disjoint from worker mounts and revisions.

Replay authenticates the complete canonical artifact, immutable launch,
contract, normalized output, reference metadata/events, lifecycle and exact gate.
It grants no new authority and can remain available after the root owner has
changed or finished. The controller database and immutable artifact storage are
trusted boundaries; corruption regressions defend consistency, not an assertion
that a model can directly edit those stores.

## Physical release

Completion retains child/root occupancy and account reservations.
`release_stage_child` freshly obtains exact caller/worker/provider cleanup, then
validates its target, retained accounts, reservation version and controller
instance against the committed decision. The final transaction rechecks current
ownership and cleanup membership, clears occupancy by exact version CAS, and
records `workflow.stage_child_released` atomically. Cancellation does not prevent
exact cleanup, but unknown ownership or effects retain occupancy. Release does
not create/start anything and never releases the root's provider reservations.
Historical release replay validates its full receipt and performs no cleanup.

## Qualification boundary

Local tests use real SQLite scheduler, versioned launch construction, durable
observation publication, candidate export/registration and service shutdown,
with synthetic Docker/provider adapters. Planning fixtures retain subsequent
frozen review/revision steps for composition tests. The isolated coding unit
fixture freezes only its implementation step; it is not evidence of full
production workflow progression. Linux execution, real model semantic behavior,
provider qualification and deployment are separate gates. The parent owns
scheduler/context integration and the shared PinnedCLI path serialization fix
found while exercising the planning fixture.

`evidence/routed-stage-decision-tests.xml` records 33 passing cases plus one
setup-only bounded SQLite deadline failure; the exact failed `needs_review`
case passed in `routed-stage-decision-needs-review-recheck.xml`. The original
failure is preserved. No deadline was relaxed and no live operation was retried.
