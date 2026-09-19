> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Qualified environment binding for routed roots

`resolve_routed_environment(registry, *, project_id, version, allowed_versions,
script_sources, forbidden_values)` returns a `ResolvedRoutedEnvironment` with
fresh detached `snapshot` and `manifest` dictionaries and a `scripts` mapping
from protected script ID to exact bytes. Explicit version selection and the
operator's current version allowlist are mandatory. There is no active-default
fallback and no registry write or qualification operation.

Resolution opens a read-only `EnvironmentRegistry` view. It validates the
manifest and its digest, the qualification receipt's image, platform, CLI
versions and exact successful readiness probes, its canonical UUIDv4 and
ordered timezone-aware timestamps, and the matching successful durable
`qualification_attempts` row. A true `qualified` flag alone is insufficient.
A later failed qualification does not erase the registry's first successful
qualification.

The versioned snapshot contains `schema_version`, `project_id`, `version`, the
normalized `manifest` and `manifest_sha256`, `qualification`,
`qualification_started_at`, `qualification_sha256`, and `snapshot_sha256`.
The latter hashes the canonical snapshot without its own digest field. The
snapshot is at most 32 KiB, with explicit refusal rather than truncation. It
contains no operator script source paths or script bytes. Known secret values
are rejected in encoded bytes and decoded JSON strings/keys.

`validate_routed_environment_snapshot(snapshot, *, project_id,
allowed_versions, forbidden_values=())` is pure: it validates the entire frozen
binding without registry or script reads and returns a detached normalized
snapshot. `load_routed_environment(snapshot, *, project_id, allowed_versions,
script_sources, forbidden_values)` revalidates that binding and loads precisely
the pinned scripts without any registry/default lookup. The current trusted
allowlist and secret policy still apply. Snapshots are trusted controller
provenance; their self-hash is an integrity check, not authorization for
user-supplied qualification claims.

Script paths come exclusively from the trusted operator's `script_sources`
mapping. Each selected path is absolute, descriptor traversed with nofollow,
and every directory/file must belong to root or the current controller UID.
Group/world writable components are refused except root-owned sticky ancestor
directories such as the real `/tmp`. Subsequent files still require protected
permissions. Symlink ancestors, symlink files, hardlinks, special files, and
empty or oversized scripts are refused. Files are limited to 1 MiB each and
4 MiB in aggregate, with a shared five-second cooperative read deadline.
File identity, size and mutation timestamps are checked before/after reading;
each path component is checked again against its pinned descriptor identity.
This does not promise interrupting a kernel/filesystem I/O hang. Operators must
supply canonical real paths instead of paths containing symlink aliases.

At most 32 checks are accepted; protected script IDs, names and hashes are
required, script filenames cannot bind conflicting content, and argv must
match the existing protected verifier's interpreter/script grammar. Each
loaded script is checked through `ProtectedCheck`. Empty check lists remain
empty for downstream `needs_review` assessment, never fabricated acceptance.
Manifest resource limits govern caller/workspace allocation. Independent
`VerificationLimits` govern protected verification; a valid six-CPU caller
manifest is not rejected because protected verification permits fewer CPUs.

Local evidence: `evidence/routed-environment-tests.xml` covers 50 resolver
cases plus existing registry/policy cases (154 total). It includes actual
SQLite register/qualification records, frozen-default drift, corrupted
qualification/attempt bindings, descriptor mutation/replacement and a real
protected script under this Mac's root-owned sticky temporary directory.
This is local resolver qualification only: no live registry activation,
Docker execution, provider inference, scheduler/submission changes, or remote
operations were performed by this unit.
