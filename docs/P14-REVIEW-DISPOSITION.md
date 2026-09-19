# P14 review disposition

Fable's bounded [review](../reviews/checkpoint-p14-fable.md) returned **REVISE** against [frozen source](../evidence/p14-source-snapshot/manifest.json). The initial invocation reached its $2 limit without a verdict; one corrective request completed within the same five-turn/$2 limits. No further review loop was run.

- **Confirmed and fixed:** omitted resource limits imported different defaults than Runtime. Manifest/import defaults now match Runtime, with a direct constructor comparison regression; integer CPU upper bound also matches Runtime's six-CPU maximum.
- **Confirmed and fixed:** qualification errors were indistinguishable. Bounded error categories are now durable and visible through operator `show`; tests cover image inspection, CLI mismatch, readiness, timeouts, cleanup uncertainty and suppression of secret-bearing raw diagnostics.
- **Disproved as stated:** the review inferred the live worker omitted resource keys. [Fresh host evidence](../evidence/p14-live-resource-policy.json) confirms all four keys explicitly present. The existing [live qualification](../evidence/p14-live-qualification.json) therefore is not invalidated by the defaults bug.
- **Disproved as stated:** missing registry does not crash or leave an attempt queued forever. The [frozen-runner reproduction](../evidence/p14-findings-reproduction.json) ends failed(interrupted_during_preparation), no launches or retained active attempts, heartbeat alive. Parent owns improving this failure category in the current runner.
- **Documented limits:** activation is not revocation/default selection; local image IDs and exact CLI versions are required; schema evolution needs a versioned decoder/migration. Full environment build/start orchestration and live API/worker P14 activation are separate acceptance steps.

[Correction tests](../evidence/p14-corrections-tests.xml) and [source/test binding](../evidence/p14-corrections-binding.json) apply to the corrected local source. The earlier live Docker qualification remains bound to its recorded frozen module hash; it is not presented as a run of later source. No live service configuration was changed by these corrections.
