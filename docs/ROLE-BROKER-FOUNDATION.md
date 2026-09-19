# Durable role broker foundation

Local module only. No Unix socket, HTTP route, Store/Runner integration, child scheduler, provider call or deployment is implemented here. Native role-call IDs are correlation inputs, never authentication. The approved D8 patch only delivers those inputs; it does not grant controller authority.

## Trust and transaction contract

`RoleBroker(controller_db_path, authorize_parent=callback)` creates only namespaced grant/pending tables. The mandatory read-only callback receives `(db, ParentScope)` inside the same SQLite transaction as mutation and must return exactly True. Integration must query the authoritative parent/owner/project/root ancestry, current generation, cancellation/revocation/lifecycle and relevant deadlines using that connection. Exceptions fail closed with a fixed503 code. A callback returning True unconditionally, as a fake might, is not an accepted production authorization implementation. The module owns no alternate parent/scheduler state.

`ParentScope` freezes owner, project, root/parent attempt, generation and the persisted native session ID. Controller-only `issue` generates32 random bytes and stores only their SHA256; capability roles/actions/expiry are fixed in the controller DB. A parent/generation cannot be rebound to another native session or owner even after its grants are revoked. A new generation gets distinct request identity and cannot adopt/read the prior generation's rows. The controller must obtain and persist a stable native session before granting the capability; no job-selected session handshake is implemented.

`issue`, `revoke`, `reject`, `transaction` and `admit_in_transaction` are private controller entry points, never exposed to a job. Only `prepare` and `read` are candidates for the future capability-facing protocol. The raw capability is intentionally usable by code inside its authorized attempt, not a secret from that job. It is not a platform token and never permits choosing owner, project, generation, endpoint, backend, filesystem path or Docker target.

## Preparing and observing requests

`prepare(capability, native_session_id=..., native_call_id=..., role=..., task=..., context_refs=())` validates capability expiry/revocation/action/role and the current parent transaction fence. It requires the exact frozen native session and nonempty bounded call ID. Arguments contain only a canonical role, UTF-8 task up to32KiB and at most32 `{artifact_id, sha256}` references. This validates reference shape, not artifact ownership, existence or total resolved context bytes: the controller's eventual admission must resolve each reference under owner/project permissions, verify hashes and enforce the1MiB aggregate context bound.

The key hashes `[parent_attempt_id, generation, native_session_id, native_call_id]`. The digest binds canonical tool `cloud_request_roles` and all canonical arguments. Identical retries return the same durable record; changed arguments for the same key return409. Genuinely new call IDs count as new requests even with equivalent text. IDs supplied by providers/models may repeat, so transcript preservation and the provider pairing/uniqueness qualification remain essential. The controller capability supplies authority independently of these IDs.

SQLite WAL (confirmed at initialization) with synchronous=FULL and BEGIN IMMEDIATE commits the pending record before `prepare` returns. The return is safe to use as the wrapper's controller-owned pending receipt before forwarding. This is SQLite local durability, not an off-host backup or a filesystem/platform power-loss qualification. Root request admission is transactionally bounded (default32 lifetime requests per owner/project/root), shared across grants and parents of that root. It does not replace the scheduler's total-seat/depth/resource budgets; one panel request can expand into several seats. Rows are not silently deleted, and linked/cancelled work never refunds this count.

`read(capability, request_id)` rechecks the same fences and result-read action, exact parent scope and role permission. Guessed cross-owner/project/generation request IDs return404. It uses a consistent WAL read snapshot, not a write reservation, and returns one bounded pending/blocked/rejected/linked record, including its controller mapping pointer; it does not return actual child outcomes or implement result pagination. The maximum generated record is below1MiB under the input caps. Future socket HTTP parsing, rate limits, bounded body allocation, cursor pagination and cancellation responses remain integration work.

## Atomic integration seam

Use the controller's same database transaction:

```python
with broker.transaction() as db:
    result = broker.admit_in_transaction(
        db, trusted_parent_scope, pending_request_id,
        create_mapping=reserve_role_request_and_child_rows,
    )
```

The callback receives that connection and the frozen pending record. It must recheck policy/qualified routes, parent/root limits, context permissions and resource/credential reservations, create the controller RoleRequest/child identities using SQL only, and return the durable mapping ID. This helper creates no child itself. It first checks the current parent fence and original grant's revocation/expiry, then returns an existing mapping without calling the callback again. The callback's SQL and linkage commit atomically; a savepoint rolls back partial callback writes on an exception or invalid returned ID even if the caller catches that error. Actual process-death tests show uncommitted child writes roll back and retry admits exactly once.

The callback is trusted controller code and **must not commit, roll back or perform external/runtime/network effects**. A detected ended transaction raises `controller_callback_ended_transaction`, whether it committed or rolled back; it cannot undo a commit that already happened. Do not treat this diagnostic as a sandbox against malicious controller callbacks. Do not call an external scheduler and link afterward: a crash could duplicate children. Real runtime launch must follow the existing durable generation/reservation lifecycle after committed child creation. A supplied caller transaction must be on the same database; use BEGIN IMMEDIATE to serialize cancellation and admission.

Pending admission remains tied to its originating grant. Renewed same-scope capabilities may observe or retry an identical record, but do not silently replace an expired/revoked original admission grant. Observation reports `blocked` with a finite expiry/revocation reason. Controller-only `reject(exact_trusted_scope, request_id, reason)` terminalizes an unlinked row, even after parent cancellation, without reauthorizing work or refunding lifetime quota. The same rejection is idempotent; conflicting reasons and linked rows refuse. There is no automatic reauthorization or background cleanup. Linked history is immutable. A pending record or linked pointer does not establish successful children or completed workflow.

## Storage and diagnostic boundaries

New database files use0600 and a newly created immediate directory uses0700. Existing files must already be0600, or0660 with explicit `shared_group=True` for the controller service group. Unsafe modes, hardlinks and final-component symlinks are refused; existing shared Store permissions are never changed. The operator must supply a trusted non-job-writable parent path and protect existing directories and SQLite sidecars. This module does not qualify hostile parent-path races or upgrade an existing incompatible broker schema.

SQLite busy/I/O errors map to fixed503 `storage_unavailable` without raw exception text. Busy timeout defaults to1000ms, configurable1–10000ms. A supplied deferred caller transaction can experience snapshot conflict; it fails closed with503 and no partial mapping. Use BEGIN IMMEDIATE for admission. Optional trusted `diagnostic_hook` receives only fixed code, exception class and SQLite numeric code, never exception message or task/token bytes. Hook errors do not change the refusal. Parent authorization callback errors have the same safe diagnostic seam.

Parent IDs must be globally unique: the current Store's `uid()` uses UUID4 and attempts have a primary key. Internal mapping IDs are correlation identifiers, not credentials; every downstream interface must authorize them independently. The future socket handler still needs polling limits and cancellation/lifecycle-driven grant revocation. Tokens can be copied by authorized job code, and no grant lifecycle service exists in this foundation.

## Evidence and unresolved integration

Tests use a real temporary SQLite DB and an authoritative fixture parent table. They cover concurrent identical/conflicting calls, root quota concurrency, expiry/revocation, cross-scope denial, narrowed read-role scope, generation changes, failed authorization, exact DB transaction binding, child/link atomicity, savepoint recovery, concurrent admission and a spawned process exiting mid-admission. Fixture child rows are not production Attempts. No live Store schema or service was changed.

The next integration owner must provide real same-transaction parent checks and role/child admission, schema deployment/backup procedure, artifact resolution, capability issuance/revocation lifecycle and socket ownership/channel handling. Then prove cancellation races, controller process recovery, durable pending-call replay, runtime launches and actual provider/tool IDs against the integrated Store/Runner. The module alone is not a live broker or a release gate pass.

Corrected local validation:50 focused tests, including read during an active writer, fixed busy/snapshot errors, explicit expired/revoked pending resolution, private/shared-group file policy, WAL refusal, controller validation and diagnostic redaction. `evidence/role-broker-final-binding.json` binds the exact command, Python/SQLite versions, source hashes before/after, XML and logs. Original Fable review remains frozen; independent disposition is `reviews/role-broker-disposition.md`.
