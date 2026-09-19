# Composed routed result phase

`publish_child_results` connects the trusted result APIs after `drive_prepared_child` has fenced inference and stopped/removed the caller. Inputs are controller-owned scheduler/runtime/prepared-stage/spec/quiescence handles, retained worker services, explicit output selectors and known-secret policy. It is not an HTTP or model-callable authority surface.

The function holds the existing per-child driver lock and performs these operations:

1. Drain the exact worker/provider handles and collect durable combined cleanup evidence. Shared root accounts and the scheduler seat remain held.
2. Read the private export journal status. If an export was previously attempted, reconcile its exact collector before checking cancellation-sensitive publication authority. A cancelled retry can therefore remove its stopped/running collector. Uncertain identity or removal remains held; no journal is reset and no second export is started.
3. Reload and reparse the private observation snapshot under current ownership/generation authority, applying the explicit current known-secret policy. In-memory observations are replaced by the durable bytes.
4. Publish the unverified observation artifact through the existing transaction and exact cleanup checks.
5. If output selectors are provided, start the first export or reload the reconciled existing export. A cleaned export without recoverable bytes refuses; changed selectors refuse. Capture and register its immutable, unverified candidate revision.
6. Recheck current result authority and return `ChildResults` containing the recovered quiescence record, combined cleanup receipt, observation artifact and optional candidate.

Passing `selected_paths=None` supports stages that only produce observations. It cannot hide an existing export on retry. `forbidden_values` is a required keyword at this composition boundary; no credential discovery or provider-auth read happens here. Root orchestration must obtain the policy from its approved secret boundary and also supply it before driver execution, so forbidden output is refused before its first snapshot write.

Artifact transactions are individually atomic. A failure after observation publication can leave that unverified artifact while candidate capture is retried. Retry reuses the exact stored snapshot/export and immutable revision, without re-running the caller or collector. None of these partial records grants a workflow gate, terminal verified outcome, child-slot release or session promotion.

Cleanup authorizes through the held exact owner/child/runtime context and remains available after cancellation or a result deadline. Reading/publishing output still requires fresh result authority. A malformed caller policy or context is rejected before this phase's side effects; the root remains responsible for its outer failure cleanup. New-controller takeover, recovery of uncertain original caller creation, protected verifier execution, terminal decisions and production root/service composition remain separate work.

Tests use the real Store/scheduler/driver/snapshot/cleanup/export/publication/candidate modules with explicitly simulated Docker/provider transports. They cover full result replay, partial candidate failure, changed selection, cancelled retry with a running collector, stricter secret policy and held scheduler/account state. They do not establish real model inference or production activation.


## Current Linux qualification

Actual Omarchy v3 results and secret-refusal fixtures pass against the corrected 48-module source closure. The results fixture uses real caller/Hermes/file-tool and read-only export containers with a simulated provider runtime, two actual inference admissions through the WorkerService/supervisor/dispatch/budget components, and exact unverified publication/candidate replay. It creates one export and two stable artifacts, retains occupancy/account reservations, and grants no workflow gate. A separate fresh fixture refuses a known synthetic secret before controller snapshot persistence and publishes nothing. Both exact fixtures are removed; live services and the pinned image remain unchanged.

Receipts are `evidence/routed-results-linux-20260918-v3.json` and `evidence/routed-secret-refusal-linux-20260918-v3.json`, with separate cleanup receipts. The source/test/proof binding is `evidence/routed-results-linux-final-binding.json`. The earlier failed ID-validation run and authority-unavailable run remain preserved. Brief SQLite writer contention was independently reproduced and corrected by bounded pre-BEGIN acquisition within the unchanged 50 ms control deadline; transaction bodies and uncertain commits are not replayed. That does not prove the exact historical exception, which the older harness did not record.

This root-provisioned qualification does not establish production worker provisioning, actual provider authentication/inference/physical cleanup, new-controller takeover, protected outcome decisions, delivery promotion or production activation.
