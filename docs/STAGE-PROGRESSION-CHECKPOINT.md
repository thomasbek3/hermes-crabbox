# Durable stage outputs and revision progression

This candidate adds controller decisions for every current workflow role except
`acceptance_verification`, which retains the separate protected-check decision
path. The complete coding workflow remains Fable plan, Astra plan review, Fable
plan revision, Grok implementation, Astra code review, and Sol verification.
There is no model substitution or shortened production workflow.

`complete_stage` validates the frozen contract, authenticated saved launch and
worker observation, published candidate and cleanup, and current authority. It
atomically commits an immutable `controller_stage_contract` artifact, terminal
lifecycle, revoked grants, result pointer and scoped gate. A complete report or
approved review is `unverified` with a `stage_contract` passing gate, never
protected task acceptance. A review requesting revision gets a rejecting gate;
`needs_review` has no passing gate. Automatic repair/review retries remain
unfinished and are not inferred from these outcomes.

Read-only stages must have unchanged selected file contents and carry their
assigned input revision. Coding stages carry the exact captured output candidate.
Input and output hashes retain their distinct scope bindings. Inert historical
readers authenticate the canonical decision, artifacts, launch, lifecycle and
gate events. Separate `release_stage_child` checks exact caller/provider cleanup
and clears occupancy with a compare-and-swap and durable release receipt. Replay
does not invoke runtimes or touch newer owners. The legacy generic child release
rejects stage-decision-managed children.

New qualified roots also freeze `stage-progression-v1`. Before first admission,
`register_root_input` verifies and registers an explicit root-bound captured
revision. Replacing that registration or beginning with an arbitrary revision is
refused. `stage_inputs` returns the next step's controller-selected revision and
ordered references. The scheduler rechecks these within broker admission: each
predecessor must have a finalized passing stage decision and durable release,
every input must equal the preceding carried output, and context must be exactly
all earlier stage-decision artifacts in frozen order. No latest-file lookup or
model-selected extra context can replace that chain. Existing unversioned roots
retain their historical behavior; they are not retroactively qualified.

Context now projects complete normalized plans, findings and evidence from
authenticated stage decisions or their linked observations. It preserves the
untrusted-data framing and existing bounds. Five maximal outputs may exceed the
64 KiB combined context ceiling; that explicitly refuses preparation rather than
silently discarding parts of a plan. A larger-output protocol remains separate
work.

Actual local composition exposed a shared PinnedCLI serialization defect: the
launch binding tried to JSON-encode a `Path`. It now records the path as a string,
preserving the independently calculated provider-profile digest. The planning
fixture uses Fable/max and the next review uses Astra/high. These are synthetic
runtime/provider fixtures and do not prove actual model access or execution.

Current evidence includes root-input/admission regressions, actual planning
finalization/release to next-review prompt, stage decision and context tests, and
a full six-stage local composition check. That test retains all six original
frozen steps and exact model/effort profiles. Only implementation changes the
sample from `answer = 42` to `answer = 43`; the review and final protected check
receive that output. All prior structured stage artifacts reach later stages,
and finalization/release replay creates no extra runtime actions. A second
scenario rejects the code review and blocks final verification. The protected
script actually runs locally against the candidate; its Docker boundary and
all provider inference are simulated. Root delivery remains deliberately
uncommitted and root account reservations remain held.

The initial parent fixtures
incorrectly passed list context to a tuple-only broker and changed the native
session for one root; both test errors were corrected without weakening product
checks. The first registration implementation used a private-publication reader
mode incompatible with the verified read-only revision directory; it now uses
the existing nofollow bounded file reader after full revision validation.

Local test evidence: `stage-progression-scheduler-tests.xml` records 86 passing
admission/legacy scheduler/stage tests; `stage-progression-parent-final-tests.xml`
records 28 passing strict admission/real planning handoff/qualified acceptance
tests; `routed-stage-context-tests.xml` records 56 passing context cases. The
stage finalizer's initial suite records 33 passes and one predecision SQLite
deadline setup error; that exact case passed separately. This is not a claim
that the original suite was green. No deadline or uncertain-action retry was
introduced. `six-stage-composition-tests.xml` records both full-flow scenarios;
initial fixture permission failures are retained separately. Counts overlap.

No remote changes, provider inference, Jev requests, login or service changes,
schema migration, or deployment occurred in this checkpoint. Fable review is
still pending after the previously observed account limit. The older Grok review
does not cover these new files. Final delivery promotion, follow-up import of
delivered revisions, explicit remediation, root/worker integration, recovery,
current-source Linux qualification and real model/effort qualification remain.
All original Release A/B/C gates remain in scope. See `ROOT-DELIVERY-NEXT.md` for
the delivery schema and cleanup prerequisites.
