> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Supervised provider execution

`SupervisedProviderExecutor` wraps the real `ProviderExecutor` and requires its `ProviderDispatch` to have a configured durable inference budget. Configure the Docker adapter's `cancel_check` with this wrapper's `cancel_check` before invoking it. The wrapper delegates authorization to the underlying executor.

Each wrapper permits one active invocation. It starts a cancellation supervisor bound to the exact reservation, grant, attempt, generation, profile and controller instance. The supervisor must close successfully before a successful result can leave the wrapper. Outside an active invocation, the adapter cancellation callback returns true. Keep one wrapper per attempt rather than constructing one for every HTTP request.

The supervisor receipt proves observer shutdown and records cancellation/revocation observations. It does not prove physical cleanup. The result's `outer_cleanup_confirmed` is inherited from the executor's actual dispatch cleanup evidence. The cancellation tests separately exercise successful physical cleanup and failed cleanup; the latter leaves synthetic resources present and forbids a cleanup claim.

Current local evidence: `evidence/supervised-executor-final-tests.log` records 109 passing tests across the wrapper, cancellation supervisor, budget integration and budget foundation. Source hashes and the running review handle are in `evidence/supervised-budget-executor-binding.json`. This is not deployed or real-provider qualification.

Remaining integration gates include active root/ancestor cancellation and deadline observation, actual Docker composition, controller crash recovery, native Hermes through the full relay path, real dedicated-provider authentication and the full specification's multi-model acceptance. A separate read-only budget-authority component is being prepared for the existing bounded supervisor database operation. Admission-time checks alone do not prove that root cancellation stops an already active descendant call.

## Activation ordering

This wrapper is a new candidate execution path, not a replacement installed into existing workers. The current live jobs must continue using their existing runtime until the migration checkpoint explicitly authorizes a cutover. Do not attach BudgetAuthority to historical live attempts or infer their budget ancestry from Store follow-up provenance.

For a newly admitted candidate attempt, the controller first installs the budget schema in its selected control database, registers the explicit root/attempt scope and policy within admission, then creates the exact provider account reservation/grant, adapter and executor. Only then construct this wrapper and expose its callback to the authenticated worker relay. Missing registration is a refusal for the new candidate, never permission to reconstruct a budget from history. All of this ordering still requires the production scheduler integration and migration tests; local tests and disposable proof databases are not evidence of a live cutover.

The wrapper now supplies BudgetAuthority as an additional supervisor predicate. Existing standalone supervisor callers keep their previous behavior unless they explicitly supply that predicate. Active budget deadline observation is locally tested; the earlier physical Docker qualification predates this change and must not be cited as its proof.
