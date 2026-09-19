> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# CLI feasibility boundary, refreshed 2026-09-17

The current official [CLI reference](https://code.claude.com/docs/en/cli-reference) documents disabling built-in tools, denying tools, strict MCP selection, structured JSON output, bounded turns, and setup-token for scripts. Safe mode alone retains built-in tools. The controls support the proposed experiment, but their combined behavior at the pinned binary must be measured. This is documentation evidence, not a passing real transport request.

The [authentication/use documentation](https://code.claude.com/docs/en/legal-and-compliance) distinguishes an end user signing into an unmodified hosted CLI from third-party credential intermediation. The intended private single-owner implementation keeps the published CLI and its authentication flow intact; it does not export the subscription token to a native Hermes HTTP client or offer login to other users. These docs do not establish model entitlement, remaining allowance, billing or approval of arbitrary bridges. No legal conclusion or new account authority is asserted.

## Concrete next live qualification prerequisites

- Use the already authorized dedicated cloud identity through the official CLI's supported setup-token mechanism. The existing plain token file is not a proven refreshable auth-home layout; do not fabricate credentials.json or borrow personal files to make a test pass.
- Obtain the existing controller's exclusive account execution grant before staging any token. An idle Docker listing alone does not prevent the normal worker admitting a conflicting job. A standalone host-shell probe without that lease is not acceptable.
- Run one bounded provider-only container with pinned unmodified CLI, clean configuration, existing exact provider egress, no project workspace/tool mounts and no account key fallback. Keep token values out of argv, output and artifacts. Preserve canonical token bytes and quarantine state.
- Validate the exact no-tools/no-MCP/structured-output/one-turn combination. If it cannot return a valid structured decision, classify the actual terminal failure; do not silently enable tools or remove execution limits.
- First request a deterministic tool decision as data; confirm CLI executes no tool. A later Hermes integration must execute that tool outside the provider container and return its result with matching ID. These are distinct proof steps.
- Requested Fable model/effort and actual reported identity remain distinct; missing attestation is unverified. Native model availability is not established by Cursor aliases. Do not introduce a different model or API key to obtain a green result.
- Cleanup proof includes provider container/cgroup termination and no staged credentials before releasing the account grant. Local subprocess process-group cleanup alone is insufficient.

No real request, token read, credential change or deployment occurred in this documentation refresh. The fake transport's checkpoint review and controller/service integration precede this live qualification.
