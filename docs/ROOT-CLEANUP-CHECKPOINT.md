> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Root cleanup and release checkpoint

Local controller implementation only. No production migration, provider request,
credential access, image change or service restart. Tests use real Store, scheduler,
leases and dispatcher with disposable SQLite and synthetic runtime inspectors.

`bind_runtime(attempt_id, expected_generation=..., runtime_id=...)` binds the actual
routed job caller once, while its root remains held. The authority check and SQL
trigger prevent a first bind after cleanup freezes. Legacy runtime identity behavior
is unchanged. Missing runtime ID is not absence proof; a trusted inspector must
resolve exact attempt/generation labels and outstanding creator operations.

`release_root(root_id, expected_generation=..., verifier=...)` requires terminal
root/children and cleared child occupancy. It takes ordered account locks, freezes
root/generation/version/controller/cleanup ID/timestamp/inspector, all caller runtime
bindings and exact provider reservation IDs/epochs/owners, then marks root releasing.
Both capacity slots stay reserved. Trusted runtime verification runs outside SQLite;
its typed exact-target receipt becomes durable before provider cleanup.

Existing provider cleanup now supports `retain_fence=True`. A confirmed owner proof
leaves the account active and reservation cleaning/root_cleanup_verified. The root
final transaction validates all original proofs, clears only those account fences,
marks reservations released, writes the aggregate receipt and releases capacity.
Commit failure rolls back every fence change. Workflow reservations cannot be
released through the default non-retaining owner cleanup path. Unknown creator,
start or cleanup outcomes remain held pending existing trusted reconciliation.

A fresh controller can finalize an all-proven frozen target without any external
callback. It cannot adopt a partial or unproven foreign cleanup. Proof checks use
the inspector frozen at cleanup start, not the successor's configured inspector.
Already-released original reservations are accepted only with exact proofs; their
accounts are not updated, so a newer owner remains untouched. Released-root replay
returns the original aggregate receipt. There is no TTL release or force-clear.

`reviews/root-cleanup-grok-disposition.md` records independently reproduced findings
and corrections. The initial two failures are preserved; the corrected affected suite
passed223tests in7.91s. Exact source/test hashes and snapshots are in
`evidence/root-cleanup-review-corrected-binding.json` and
`evidence/root-cleanup-review-corrected-source/`. Grok's review was partial; no new
Fable verdict or complete native integration qualification is claimed.

Schema2 remains an undeployed explicit migration candidate. The new target requires
inspector_id, table DDL is pinned, and the runtime trigger changed. Old experimental
schema2/cleanup-target formats require explicit migration/recovery; this code does
not silently upgrade them. Legacy production schema1 is unchanged. Actual caller
launch/creator journal, bounded native runtime inspector, live proof, incomplete
foreign-cleanup recovery and workflow artifact/remediation integration remain gates.
