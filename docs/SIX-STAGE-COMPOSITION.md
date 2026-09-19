# Local six-stage controller composition proof

`tests/test_six_stage_composition.py` runs the unchanged qualified `feature`
catalog workflow in this exact order:

| Step | Role | Bound model | Effort |
|---|---|---|---|
| plan | planning | claude-fable-5-1 | max |
| challenge_plan | plan_review | gpt-6-astra | high |
| finalize_plan | plan_revision | claude-fable-5-1 | max |
| implement | feature | grok-4.6 | xhigh |
| review_code | code_review | gpt-6-astra | high |
| verify | acceptance_verification | gpt-5.6-sol | max |

There are no fabricated prior gates and no trimmed workflow. A real SQLite
registry record and explicit qualified admission freeze the manifest, stage
contract and progression versions; its readiness receipt is synthetic. The
controller registers an immutable root input. Each stage uses real
`stage_inputs`, broker admission, stage preparation, driver, saved observation,
publication, candidate capture, completion and release APIs. All previous
finalized stage artifacts reach subsequent prompts in exact order. Wrong
input substitution is rejected before every stage's actual admission.

Each stage has separate material/task/scratch/runtime journals and a distinct
synthetic full caller ID. A synthetic tool execution changes `answer = 42` to
`answer = 43` exactly once, in coding. Readonly stages preserve content and
carry their assigned input; coding alone changes the carried revision. The
initial immutable revision remains42. The first five decisions are
stage-contract `unverified/pass`, never protected acceptance.

Final acceptance loads the exact qualified check script and executes that
script locally against the readonly verification candidate. Its assertion
checks the complete file contents. Real verification composition publishes the
receipt, then real task decision and cleanup release complete the sixth child.
Replay runs no second protected check. Each stage has one simulated provider
dispatch, exactly one budget charge, exact simulated resource cleanup and
released request lease. All child seats are cleared. The root deliberately
remains running and retains its account reservation: root promotion/final
capacity release is outside this proof.

A second scenario freezes the same full six-step workflow and executes its
first five stages. Code review returns `revise`; the real finalizer commits a
reject gate and releases that child. Controller selection refuses verification,
so no sixth request, child or protected check is created. No stage is removed
to make this case pass.

## Evidence and boundaries

`evidence/six-stage-composition-tests.xml` records two passing scenarios and
per-scenario JSON receipts containing root/session identity, exact revisions,
profiles, context references, decision hashes, resource counts and boundaries.
`evidence/six-stage-composition-binding.json` binds those receipts to current
source/test dependencies. Latest focused run:2passed4.04s.

Real local work: SQLite authorities, immutable files/hashes, actual local Unix
worker-service lifecycle, standalone collector subprocess, protected script
subprocess. Synthetic boundaries: Docker lifecycle/inspection, Linux UID/GID
metadata, provider response/resource cleanup, and verifier runtime receipts.
Socket connection creation is forbidden by the test; no model/credential,
Jev, provider API, remote operation, or deployment occurs. Matching
Fable/Astra/Grok/Sol profile identities is not evidence of provider inference
or inference quality. This is not a Linux/Docker isolation proof or completion
of root delivery, six-stage production execution, or A/B/C release acceptance.

Two initial fixture-only permission failures are preserved separately:
`six-stage-composition-initial-fixture-failure.xml` (worker journal parent755)
and `six-stage-composition-candidate-fixture-failure.xml` (candidate root755).
Both fixtures were corrected to required0700 permissions. No production
permissions, deadlines, authority predicates or retry behavior were changed.
