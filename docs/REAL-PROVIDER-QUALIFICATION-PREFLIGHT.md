> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Real provider qualification preflight

Read-only Omarchy observation, 2026-09-18: `/etc/cloud-workbench/worker.json` points to `/var/lib/cloud-workbench/control/state.db` and the historical direct-Claude runtime image `sha256:f1b15bb81917de9b48df7c9b5c2be438e686f692b42bdaa58eb2aeedf5b9ea0b`. Provider egress is limited to api.anthropic.com, claude.ai and platform.claude.com. Project model lists are empty; they do not prove any specific native model entitlement.

The live database contains ten completed Claude attempts and twelve terminal fixture attempts (eleven completed, one failed), with no active attempts at that observation. It contains no `provider_*` tables. The dedicated token was previously checked by metadata only: UID/GID959:959, mode0640, regular file. No token value was read or copied into local evidence.

The absence of active attempts is a point-in-time observation, not exclusive account ownership. A candidate account reservation in a separate test database does not fence the live worker. Real-token qualification must first establish a reviewed shared-account admission fence or a specifically authorized quiescence window that prevents new legacy admissions for the entire test. Do not create the new provider schema in the production database as a side effect of a qualification script, and do not borrow personal credentials.

After that gate, use the pinned actual CLI in the separate provider container, fixed native model/effort/profile digest, existing dedicated credential mounted read-only, approved egress, supervised budget, exact cleanup and token-safe logs. Qualify supported behavior and actual model identity separately from synthetic labels and CLI estimates. The full spec still requires two distinct actual models and routed child execution; one successful provider call cannot satisfy that requirement.

No production change or real provider call was performed during this preflight.


## Existing legacy exclusion primitive

Source inspection found `runner.main` already holds `state_root/runner.lock` with an exclusive flock for the worker lifetime before constructing Runner or reconciling/ticking. A guarded qualification harness can use this exact inode/path rather than introducing a separate lock the legacy worker ignores. It must refuse while the live worker holds it, keep it held through all provider cleanup, and never unlink/recreate the file. The actual configured path/ownership must be freshly verified. A quiescence window remains necessary while the service normally owns the lock; this discovery does not authorize stopping the service or starting real inference automatically. A dedicated harness is being prepared and reviewed.

## Official model and billing documentation check

Checked 2026-09-18: https://code.claude.com/docs/en/model-config identifies `claude-fable-5-1` and requires Claude Code2.1.257 or later. Our offline-qualified2.1.274 binary meets that version floor; this does not prove account entitlement.

The same official page's Fable usage-credit section says billing depends on plan/seat, and non-interactive `-p`/SDK calls can consume usage credits without a consent prompt. Therefore existing subscription login alone is insufficient evidence that this particular model can run without new spending. The qualification manifest must bind account-specific entitlement/billing evidence or explicit spending authorization. No inference is performed to discover billing by trial. The source also documents provider-specific alias resolution, so the manifest uses the exact native ID rather than assuming `fable` is universally identical.
