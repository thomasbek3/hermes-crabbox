> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Qualified environment enforcement at verification composition

The qualified admission wrapper is not the only policy boundary. For any root
whose immutable provenance contains `environment`, `prepare_verification` now
validates the complete frozen snapshot and requires the supplied environment's
normalized manifest and digest to match it before writing preparation files.
Historical callers cannot substitute an easier same-project check policy.

Run and published-receipt load revalidate the frozen snapshot, the plan's
environment digest, its exact image, and every normalized check definition.
Comparing only the declared digest would permit an independently constructed
plan to retain that digest while changing its checks or image. Existing
private preparation/material/script validation remains in force. Publication
repeats the same binding check against fresh `result_authority` inside the
artifact transaction. Decision composition calls `load_published_verification`
and therefore inherits this enforcement.

Exact cleanup reconciliation remains before result-authority/policy refusal;
an old incompatible preparation or receipt cannot prevent cleanup. Such a
receipt cannot launch checks or be accepted as current qualified verification.
Legacy roots without an environment snapshot, including stage-contract-only
fixtures, retain their previous internal composition behavior.

Evidence: `evidence/qualified-verification-binding-tests.xml` records 88 passing
local tests across the new binding regressions, existing verification
composition, and decision composition. New regressions use actual qualified
registry records and scheduler roots, actual candidate/preparation/publication
storage, and stub only per-check verifier observations. They cover policy
replacement before writes; pre-fix wrong preparation and receipt replay;
stale declared digest with changed image/checks; exact successful replay; and
fresh authority revalidation at publication. This is not a live Docker or
provider qualification and does not activate a production submission route.
