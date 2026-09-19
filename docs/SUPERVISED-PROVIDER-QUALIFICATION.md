# Actual Docker supervised provider qualification

Canonical execution: `evidence/supervised-provider-linux-20260918T010306Z.json`. The matching `.source-snapshot.json` contains every transferred controller module and hash; `.harness.py` preserves the executed harness. Target was verified as Omarchy; execution used a new root-owned private temporary directory and isolated Store database/account. No production Store, credentials, service configuration or live agent state was modified.

The actual chain was Store → InferenceBudget → ProviderLeases → ProviderDispatch → ProviderExecutor → SupervisedProviderExecutor, with `ProviderDocker.cancel_check` delegated to the active wrapper. The synthetic CLI ran in a disposable image derived from exact existing provider candidate `sha256:1e1948adf1632b9b1cf6c5356bca083d9465986a09ee93b266fc75020a5e1db6`. A synthetic token was generated only for the fixture and removed afterwards. The fake CLI checked provider UID/GID and disabled CLI tools, consumed the real transport's stdin, and returned a structured result. It made no provider requests.

Six proof gates passed:

1. **Success after cleanup and supervisor stop.** The real adapter cleanup verifier removed the provider container, gateway container and both networks. A separate Docker inventory read confirmed the exact four full IDs were absent at monotonic time 79189.984905597. Only afterwards did the wrapper return status 200 at 79189.988900171. Its supervisor receipt reported `stopped=true`, `cancelled=false`, and no supervisor thread remained. This is structured synthetic response qualification, not real-model inference or a native Hermes tool-loop proof.
2. **Exact replay charged once.** Calling the wrapper again with the same nonce, binding and payload returned identical bytes with one budget request row, one dispatch, root used=1 and no additional cleanup/runtime launch.
3. **Cancellation reached an actually running CLI.** Docker process readback observed `/opt/probe/fake-cli` inside the exact provider container before setting the caller cancel event. The fake request was sleeping, not producing a success response.
4. **Cancellation blocked success.** The raw wrapper returned 409 after about 1.60 seconds. Its supervisor was stopped, cancellation was sticky and durable grant revocation was confirmed. Critically, raw `outer_cleanup_confirmed=false`, with the dispatch `quarantined` and `uncertain=1`.
5. **Missing reconciliation evidence remained quarantined.** For this one call, the qualification fixture replaced the trusted adapter's resolver with a no-evidence result. `dispatch.reconcile(request_id)` refused with `operation_still_unknown`; quarantine and uncertainty remained. This is an injected resolver failure, not an actual lost Docker RPC test.
6. **Explicit controller reconciliation cleaned the cancelled scope.** Restoring the actual adapter resolver allowed journal-based reconciliation, followed by exact cleanup and physical absence readback. The final dispatch was `cleaned`. Across both unique requests, final root/attempt usage sums and request/dispatch counts were all 2.

## Controller integration required

Real ProviderDocker active cancellation raises from `start`. ProviderDispatch records the ambiguous start outcome, and ProviderExecutor does not claim cleanup from a launch that did not return. The required controller sequence demonstrated here is:

```python
raw_result = supervised_executor(context, payload, cancel)
# Non-success + cleanup unconfirmed: retain ownership; do not release credentials.
# Obtain the exact request ID from the trusted durable dispatch record.
dispatch.reconcile(request_id)  # may refuse: keep quarantined, no speculative cleanup
# Only after authoritative adapter operation evidence resolves the uncertainty:
dispatch.cleanup(request_id)   # exact bound resources, real cleanup verifier
```

This harness performs those steps explicitly. It does not claim the wrapper alone completes cancellation cleanup, and it does not turn a failed request into success after cleanup. A future controller integration must persist/retry this bounded reconciliation path while retaining account ownership.

All created provider containers, gateways and networks were removed through normal exact cleanup; no fallback resource removal was needed. The temporary synthetic image was removed by its unique tag after checking its qualification owner label. Final reservation-label inventories returned no containers or networks. The nonsensitive private source/DB/journals remain as remote evidence; the synthetic token was removed. Before/after snapshots showed the three live service units remained active. These snapshots are observations, not proof that no unrelated transient host change occurred.

Scope binding: wrapper hash `dd7551878dca4177dd79d569f46b4cda31df5fd486ab2ab3d2ae5fc4398185bf`, supervisor hash `ec274064e551dedaa0edd9c1da8a99f5dcfdf703c86e23b7176c5db3b833537c`; all other hashes are in the receipt. The subsequently proposed BudgetAuthority integration is **not covered**. No image/service activation, real credential authentication, real provider response, production latency guarantee, native Hermes tool loop, or actual unanswered Docker RPC is claimed.

Fable returned REVISE. The disposition is `reviews/supervised-provider-qualification-disposition.md`; 21 revised local tests now load the canonical artifacts and enforce their hash/outcome bindings. The historical receipt is schema1 and did not itself echo the harness hash; the archived harness matches the snapshot's recorded pre-execution hash. Future schema2 harness runs archive themselves and echo that hash. They have not been executed here.

A read-only follow-up to the retained isolated database is preserved in `evidence/supervised-provider-resolution-readback.json`. Its exact cancelled-request operation receipt records `start/completed`, the bound launch nonce, and all four matching resource IDs before subsequent cleanup. It does not invent a missing Docker kill/exit observation.
