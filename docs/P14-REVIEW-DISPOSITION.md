> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# P14 review disposition

Fable's bounded review (private historical record, not distributed) returned **REVISE** against frozen source (private historical record, not distributed). The initial invocation reached its $2 limit without a verdict; one corrective request completed within the same five-turn/$2 limits. No further review loop was run.

- **Confirmed and fixed:** omitted resource limits imported different defaults than Runtime. Manifest/import defaults now match Runtime, with a direct constructor comparison regression; integer CPU upper bound also matches Runtime's six-CPU maximum.
- **Confirmed and fixed:** qualification errors were indistinguishable. Bounded error categories are now durable and visible through operator `show`; tests cover image inspection, CLI mismatch, readiness, timeouts, cleanup uncertainty and suppression of secret-bearing raw diagnostics.
- **Disproved as stated:** the review inferred the live worker omitted resource keys. Fresh host evidence (private historical record, not distributed) confirms all four keys explicitly present. The existing live qualification (private historical record, not distributed) therefore is not invalidated by the defaults bug.
- **Disproved as stated:** missing registry does not crash or leave an attempt queued forever. The frozen-runner reproduction (private historical record, not distributed) ends failed(interrupted_during_preparation), no launches or retained active attempts, heartbeat alive. Parent owns improving this failure category in the current runner.
- **Documented limits:** activation is not revocation/default selection; local image IDs and exact CLI versions are required; schema evolution needs a versioned decoder/migration. Full environment build/start orchestration and live API/worker P14 activation are separate acceptance steps.

Correction tests (private historical record, not distributed) and source/test binding (private historical record, not distributed) apply to the corrected local source. The earlier live Docker qualification remains bound to its recorded frozen module hash; it is not presented as a run of later source. No live service configuration was changed by these corrections.
