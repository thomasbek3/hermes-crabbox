> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Next unit: protected decisions and revision advancement

Current result composition persists unverified observations/candidates and exact cleanup evidence. It does not decide workflow success. Existing publication authority requires child state preparing/running, so finish result publication before entering verifying or a terminal state. Terminal recovery must use a durable decision receipt, not repeat publication under invalid authority.

The current scheduler gate binds `revision_sha256` to a step's assigned **input**. A coding stage's output candidate is a separate hash. A controller completion record must retain the input-to-output relation, and the next review/verification assignment must consume that exact output. Do not insert output into the existing input-bound gate.

The next implementation should add a small protected-decision adapter and narrow scheduler transaction helpers:

1. Independently validate the verifier receipt against exact root/child generations, launch binding, input and candidate-output revisions, configured protected check definitions/results and cleanup evidence. Mandatory criterion IDs and descriptions must match, following the existing legacy Runner coverage rule. Model prose, artifact metadata or a supplied evidence hash alone cannot prove acceptance.
2. Persist an immutable completion intent before terminal mutation. In one bounded SQLite transaction, recheck owner, cancellation/client revocation, deadline and assignment; commit child outcome, step gate and events. Exact replay returns the committed decision without running verification or publication again.
3. Release the child with fresh exact cleanup through the existing verifier, then make exact release replay durable. Unknown cleanup retains occupancy. A crash between decision and release must remain recoverable.
4. Admit the next stage only with the revision authorized by the committed preceding decision. Rejection stops advancement; remediation is an explicit workflow action.
5. Separately implement final delivery promotion: all required gates/checks pass, no occupied/live descendant, and a compare-and-swap against the session's expected delivered revision. Root result and promotion event must commit together. The current sessions schema has no delivered-revision pointer, so this requires a reviewed migration before activation.

Reuse `RoleScheduler.transition`, `record_step_gate`, `release_child`, `admit_request` and existing root result selection as internal building blocks, not as sufficient protected verification. Their current independent transactions do not provide atomic decision/gate completion; transition lacks protected receipt validation and terminal replay; admission does not prove the chosen input came from the preceding approved output. Root result selection is not promotion.

Suggested ownership: new `routed_decision.py` and focused regressions, narrow scheduler helpers, then separately reviewed Store migration/promotion. Preserve legacy Runner and result-selection behavior. Required tests include final-commit cancellation; forged verifier evidence; input/output substitution; crashes around decision/release; failed review blocking admission; stale delivery-base CAS; and root result selection independent of child generation.

This is an implementation handoff based on current source inspection, not a completed feature or release approval. Protected verifier runtime and the distinction between a model review decision and independently proven acceptance still require exact implementation and qualification.
