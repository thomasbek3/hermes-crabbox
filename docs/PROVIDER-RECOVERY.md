> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Recovery of interrupted provider requests

`provider_recovery.recover_request` is a trusted controller operation. Supply the existing ProviderDispatch, exact request ID, reservation, grant ID and immutable AttemptBinding. It does not discover requests from user input, issue new grants, release the logical account owner, or start a provider.

The operation takes the existing reentrant account lock, validates all supplied bindings against the persisted dispatch, and accepts only quarantined requests or already cleaned/delivered records. Unknown effects must first pass `ProviderDispatch.reconcile` using the adapter's durable operation evidence. Missing evidence raises and preserves the request. A successfully reconciled request still requires `ProviderDispatch.cleanup`; only a confirmed cleaned record produces a cleanup receipt. The receipt does not authorize a new request or prove provider identity.

Keep this operation in the trusted controller after the execution supervisor has stopped. A running, admitted or partially created non-quarantined request is refused. Crash scanning/adoption and old-controller ownership transfer remain separate integration gates; this helper deliberately requires the current controller instance.

Local tests exercise unresolved effects, exact resolved cleanup, idempotence, active-request refusal, wrong generation/grant/reservation/profile and cleanup failure. The prior physical Docker proof exercised the underlying reconcile/cleanup sequence; it did not invoke this helper. Its own Fable review is in progress. No production activation is implied.

## Previous-controller takeover candidate

`recover_previous_owner` is a separate candidate API. It defaults to refusing takeover: a different controller UUID is not evidence that the previous process is dead. The trusted caller must supply explicit `takeover_authorized=True` based on an external controller-fencing or operator decision. No production caller currently supplies this authorization, and automatic startup takeover must not be enabled before the controller-liveness mechanism is implemented and qualified.

Its synthetic tests authorize takeover only within isolated test databases. Without authorization a second controller leaves the healthy owner's grants and runtime untouched. With authorization it quarantines/revokes first, resolves uncertain effects, and runs owner cleanup before release. This is not a completed startup recovery implementation. The review also identified resolution-receipt history preservation and additional owner cleanup failure states as remaining work.

Resolution audit correction: each successful reconcile now appends a provider_operation_resolved event in the same SQLite transaction as the state transition. The dispatch row retains the latest receipt for immediate recovery; earlier event receipts survive later cleanup resolution. This is an application append-only event history in the trusted controller database, not tamper-proof storage. The two-step cleanup-failure test verifies both start and cleanup receipts remain retrievable.
