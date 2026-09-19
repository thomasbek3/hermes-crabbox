> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Pstack role policy foundation

Local resolver checkpoint; no deployed route, account qualification or worker launch. `pstack_routing.py` owns only trusted role/profile resolution. `requested_policy()` returns desired aliases and cannot activate them without concrete profiles and the later trusted qualification path.

## Behavior

Canonical roles follow SPEC-FIRST-CONTRACT D3. Display labels from the provided Cursor map are explicit aliases; unknown strings are rejected. Canonical and alias duplicates in one policy are rejected, rather than silently overriding. The ambiguous combined label `judgment and prose` is rejected; configure both canonical roles explicitly. The requested policy includes both with the same Fable route.

Each panel entry remains a distinct ordinal seat, including repeated and inherited entries. `auto` and `inherit-parent` resolve the frozen parent profile passed by the trusted caller. A cross-judge selects the first different-provider-family candidate, or the first same-family entry with `diversity_fallback=true` if none differs. Only that selected profile's readiness is checked. Its failure blocks the request; no other ready model is substituted.

The policy digest binds normalized roles and configured profile values. Seat provenance records runtime, effective configured profile/provider/model/effort, inheritance, original ordinal, selection fallback and policy/qualification reference. It explicitly says controller_configured and qualification_reference_verified=false: neither a digest nor callback proves an account or real model. The controller must later bind actual source/image/capability receipts and runtime resolution.

No prompt-selected provider URL, credentials, process invocation, personal-profile lookup, automatic account login, fallback provider or paid API operation exists in this module. The family field is trusted profile metadata; activation must enforce the versioned provider-lineage mapping, not accept a task's claimed family.

## Evidence

- `evidence/pstack-routing-corrected-tests.xml`: 24 focused tests before independent review, including invalid profile types, unknown routes, duplicate aliases, panel identities, explicit same-family fallback and no substitution on unavailable chosen judge.
- `reviews/pstack-routing-corrected-source/manifest.json`: frozen source/test hashes.
- `reviews/pstack-routing-corrected-fable.md`: bounded read-only review; inspect terminal receipt before claiming a verdict.

Outstanding integration: trusted qualification registry, provider/account/effort verification, persistent parent/child requests, reserved capacity, broker tool invocation, readonly review, real two-model execution and cancellation/recovery acceptance. This module alone satisfies none of those live gates.

Review corrections: blocked-route exceptions retain the selected seat provenance; invalid qualification-reference errors make no qualification verdict. Known native provider families must match the versioned lineage map, and family/provider casing is canonical. Inherited parent profiles must exactly match a configured frozen profile, binding inheritance to the routing digest. Unknown synthetic/provider profiles remain configuration data requiring external qualification, not activation.
