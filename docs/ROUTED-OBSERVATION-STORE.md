> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Durable routed worker observations

The controller now persists the exact validated worker result and event bytes before fencing and removing the caller. This closes the in-memory-only observation gap: the scratch files belong to the tool UID, and a controller cannot rely on reading those files directly after the container is gone.

`save_observation(scheduler, storage_root, spec, runtime_id, binding_digest, observation)` runs under current started-launch authority. It checks the original protected `launch.json` against the immutable `CallerSpec` hash, recomputes its launch-receipt hash, reparses raw result/events through `collect_execution`, and demands exact parsed-observation equality. It then writes a private staging directory containing only:

- `result.json`: exact collected UTF-8 bytes, at most64KiB.
- `events.jsonl`: exact collected UTF-8 bytes, at most16MiB and10,000 validated events.
- `launch.json`: original protected plan bytes, at most512KiB, permitting recovery without reading the original task directory.
- `manifest.json`: at most64KiB; exact original spec, runtime, child generation, frozen launch binding, profile/input-revision/assignment digests, launch-receipt hash, file hashes/sizes and original collected file evidence.

Staging files are0600; the snapshot directory is0700. Files and the staging directory are fsynced before atomic exclusive directory publication. The private parent is fsynced afterward. The final directory name is a fixed hash of child ID and generation. Exact replay validates and reuses existing bytes; it never overwrites an existing snapshot. Only this invocation's unpublished temporary directory is removed on failure. An existing conflicting, partial or corrupt snapshot remains inspectable and blocks reuse.

The driver invokes save after successful collection with fresh authority and before returning `execution_observed` or entering caller teardown. Another authority check follows publication. Persistence failure produces `observation_persistence_failed`; the driver's existing `finally` still fences provider execution and stops/removes the exact caller. Cancellation after publication may retain a private snapshot but returns no successful observation. No artifact, workflow gate, protected verdict or revision promotion is created by saving.

`load_observation(scheduler, storage_root, spec, quiesced)` explicitly loads the snapshot under current `authorize_result` authority and an exact fenced, removed-caller cleanup context. It opens fixed files through nofollow ancestor traversal, rejects nonregular files, links, unexpected modes/owners/layout, size changes and hash mismatches, checks all immutable bindings, and reparses through the collection validator. It rechecks current authority after disk reads. It returns an `ExecutionObservation` with worker provenance and false verification; it does not start a runtime, read a running caller, replay provider requests, or independently certify combined provider cleanup. Subsequent publication still requires the combined cleanup collector and its current database checks.

Original inode/time metadata in the manifest describes the collected worker files. New controller-owned snapshot files have different inode metadata; their exact raw hashes, private modes and stable reads are checked separately. No claim is made that the snapshot files are the original scratch files.

This is durable-byte recovery, not automatic controller ownership takeover. A process restart ordinarily receives a new provider controller instance ID. The loader refuses that foreign owner even when the bytes are valid. A separately authorized exact-scope root/controller recovery mechanism is still necessary before such a successor can publish. Current tests reopen Store/scheduler handles under the same valid controller and recover copied disk snapshots with all original in-memory observation bytes discarded; they do not claim fresh-owner crash recovery.

Tests cover persistence before stop, exact same-authority disk reload, tamper/link/truncation/layout and binding refusal, foreign-controller refusal, cancellation during reads and after publication, exact replay/conflict, and persistence failure with caller teardown. The Linux qualification harness now requires `snapshot_before_stop` and a `controller_snapshot_after_remove` proof with hashes, identity, private mode and same-authority reparse. Its existing root-preprovisioned fixture remains explicitly unqualified for worker959 provisioning. No new Linux execution or provider calls occurred in this checkpoint.

## Controller known-secret policy

`drive_prepared_child`, `save_observation` and `load_observation` now accept keyword-only `forbidden_values=()`. Production orchestration must supply its known injected-secret values explicitly; the empty default preserves existing source fixtures and does not claim automatic discovery. The driver freezes the policy before preparation, reconciliation checks or Docker calls. Save/load validate before context checks as well. A policy must be an actual list/tuple containing at most64 nonempty `bytes` entries, each at most4096 bytes and totaling at most65536 bytes. Invalid policies fail with `observation_secret_policy_invalid`, without rendering their contents.

The shared helpers are `validate_forbidden_values(values) -> tuple[bytes, ...]` and `scan_output_secrets(result, events, *, forbidden_values=()) -> None`, both in `routed_observation_store.py`. The scanner bounds result/events, searches exact raw bytes, then parses each JSON document and searches every decoded string value and key. Unicode/JSON escapes cannot hide a matching secret in a structured field. It does not infer unknown secrets, join separate string fields, or promise recognition of arbitrary encodings such as base64. Accepted raw bytes remain unchanged.

Save scans after collection/schema/hash validation and before creating its private staging directory or writing any raw output. A match raises fixed `observation_secret_refused`; the driver's failure is `observation_persistence_failed`, and its existing fencing/removal still runs. Ordinary observation publication uses the same bounded validator and decoded-field scanner, mapped to its existing fixed `publication_secret_policy_invalid` / `publication_secret_rejected` errors.

Recovery rescans the actual snapshot bytes under the currently supplied policy before returning them. A stricter policy can refuse previously retained output; it does not silently delete or rewrite that private snapshot. No policy values are added to manifests, artifact metadata, errors or evidence. This policy change does not alter provenance, authority checks, budgets, verification, gates, or promotion.

Local correction evidence: `evidence/routed-observation-secret-tests.log` records141 passing observation-store, publication, driver and driver-qualification tests. New regressions cover refusal before staging, raw and escaped secret matches, decoded keys/nested strings, invalid policy bounds before early exits, freezing mutable caller policy, exact allowed bytes, stricter recovery and driver teardown after refusal. No remote/provider calls were made for this correction.
