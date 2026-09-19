> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Routed worker observation publication

`routed_publication.py` is a local controller composition component. It does not install an API, start a process, migrate a schema, or publish a protected success. A completed worker result remains `worker_reported`; verification, gate approval and revision promotion remain false.

## Controller interface

```python
published = publish_observation(
    scheduler,
    prepared=prepared_stage,
    spec=caller_spec,
    quiesced=quiesced_stage,
    execution_profile=qualified_profile,
    collector=trusted_child_cleanup,
    cleanup=combined_cleanup_evidence,
    publication_root=controller_artifact_directory,
    forbidden_values=known_secrets,
)
```

`PublishedObservation` returns artifact ID, child ID/generation, SHA256 and observation event sequence. The output directory must already exist, be absolute, controller-owned, with mode0700/0750 (optional setgid). Group readability is for a deliberately provisioned API read group; this function does not provision users or grant filesystem access. Files are0440. The operator must locate the directory beneath the configured artifact root for downloads.

The controller supplies real `PreparedStage`, `CallerSpec`, `QuiescedStage`, `RoutedChildCleanup` and `ChildCleanupEvidence` instances. None is a model/API authorization payload. The helper reads the frozen hash-bound launch file, matches the whole prepared plan and root/child consumer scope, and recomputes the launch receipt digest. It revalidates the observation from exact raw `result_json` and `events_jsonl`, retained privately by the collector. Optional raw byte arguments must reproduce the complete observation exactly, including hashes and offsets. Limits are64KiB result,16MiB events,10,000 events and32MiB serialized artifact. Unrepresentable or contradictory records fail closed. Known-secret scanning rejects publication; it does not silently alter hash-bound observations.

The publisher replays the trusted combined cleanup operation, requiring exact typed evidence equality. It then reads the expected private hash-named receipt through bounded nofollow file access and recomputes its hash. Cleanup replay must remain idempotent, including private-material hooks. The final transaction checks current grant/request membership and durable physical-receipt hashes again; worker flags cannot replace these controller proofs.

## Atomicity and recovery

A hash-named immutable artifact is fsynced and atomically published without replacing existing files. Existing paths must contain exact bytes and be regular, single-link files. Ancestor symlinks, links, writable untrusted files, size changes and byte drift fail closed. A cancellation between staging and database commit may leave an unreferenced artifact; it does not create an artifact record or event. There is no silent cleanup of these orphans.

The existing `artifacts` table is the durable record. A deterministic child/generation artifact ID and fixed observation path are compared under the bounded child-control SQLite write transaction. The artifact row, `artifact.created` event and `workflow.observation_published` event commit together. Exact retry returns the original event sequence; conflicting metadata, bytes or incomplete records refuse. A commit-acknowledgement error remains uncertain until exact replay/readback. No attempt state, workflow gate, revision head, account reservation or child occupancy is changed.

All publication attempts, including retries, require current root and child generations, session/turn/owner/project binding, current controller and root account ownership, frozen assignment/profile/input revision, fenced exact caller launch, valid client permission, no cancellation, and unexpired root/ancestry/attempt budget. Successful publication persists the budget clock high water without charging inference. Publication after authority loss is refused even if a prior artifact exists.

## Reuse by output-candidate composition

- `authorize_result(scheduler, spec, quiesced)` performs a bounded read-only authorization check and returns the frozen `ChildCallerBinding`. It does not validate observation bytes or authorize new execution/cleanup.
- `result_authority(scheduler, db, spec, quiesced)` runs the same read-only predicate inside the caller's transaction on the exact Store database. It returns `child`, `root`, `assignment`, `binding`, `accounts`, and `observed_at`.
- `read_cleanup_evidence(collector, evidence)` replays trusted cleanup then validates the private file and returns parsed proof.
- `validate_cleanup_membership(db, proof, binding, root, accounts, scheduler)` rechecks receipt identities and complete child grant/request membership inside the final transaction. It is paired with the preceding trusted read, not an independent validator of arbitrary JSON.

A caller must keep cleanup possible after cancellation; `authorize_result` is for candidate/observation publication, never a prerequisite for physically stopping or removing exact owned resources.

Tests use real SQLite/scheduler/budget/leases/driver composition with fake Docker and provider operations. They cover cancellation before/after staging, revocation, stale generation/account/profile/revision, conflicting worker authority claims, known-secret refusal, exact replay, concurrent publication, rollback and postcommit uncertainty. This is candidate source, not a deployed workflow, protected verifier, or successful end-to-end user job.
