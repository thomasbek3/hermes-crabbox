# Disposable relay UID isolation qualification

Canonical receipt: `evidence/relay-distinct-uid-20260918T003610Z.json`; transferred source is preserved in the matching `.source-snapshot.json`, and actual native Hermes output in `.tool-stdout.json`. The earlier `20260918T002712Z` receipt is retained as historical evidence with the review gaps described in `reviews/relay-distinct-uid-disposition.md`. Fable returned REVISE; the concrete fixes were independently requalified, without seeking a replacement PASS.

The target was Omarchy, SSH account UID 1000, using existing image `sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693`. A new disposable caller container used init UID/GID 1002. Trusted host `docker exec --user 1001:1001` started the relay, and `docker exec --user 1000:1000` ran hostile access probes and actual Hermes 0.21.3 in that same container. Numeric IDs exist only inside the disposable container; no host account provisioning occurred. A third init UID closes the tested indirect SIGTERM path where a tool-owned PID 1 could terminate the whole caller and relay.

Source was sent over stdin to a separately labelled preparation container and new named volume. Root-owned files were 0644 with 0755 directories; the caller mounted the volume read-only. A deliberately 0777 directory allowed a behavioral read-only mount test independent of file permissions. No host source or auth directory was mounted. The relay journal lived on a private tmpfs with UID/GID 1001, directory mode 0700 and SQLite DB mode 0600. No CAP_SETUID or privileged init was needed.

All 15 checks passed from UID 1000:

| Probe | Observed result |
| --- | --- |
| Read, append, or create relay journal files | EACCES |
| Send SIGTERM to live relay PID 7 or init PID 1 | EPERM |
| Connect directly to private worker UDS | EACCES |
| Read relay proc environment, fd listing, maps, cwd, or root link | EACCES |
| Append root-owned relay source | EACCES |
| Create a file in source volume's 0777 directory | EROFS |
| Change UID to 1001 | EPERM |
| Actual Hermes file-tool loop | Native terminal exit 0 |

The relay remained alive and handled two synthetic inference callbacks: actual Hermes executed `read_file`, then supplied the fixture contents to the second request. Hostile probes are direct Python operations at the same numeric tool UID, not model-generated tool calls. All transferred source hashes remained unchanged. Both UIDs recorded CapEff=0, CapBnd=0, NoNewPrivs=1, Seccomp=2 and one seccomp filter; the complete filter policy was not independently audited.

During native Hermes execution, the tool process and both relay callbacks independently read `cgroup.procs` containing init PID 1, relay PID 7, probe PID 20 and actual Hermes PID 26. Both recorded `cpu.max=140000 100000`, `memory.max=3154116608`, and `pids.max=352`. Every exec targeted the unique caller name, with its full container ID checked against creation and inspect. The former comparison of private namespace strings (`0::/`) is no longer the evidence for shared accounting. These live kernel values demonstrate one shared 1.4 CPU / 3008 MiB / 352 PID allocation. They do not prove availability isolation: a tool can exhaust its shared budget and starve the relay. Deliberate resource exhaustion was outside this bounded proof.

Caller network was none, root filesystem read-only, capabilities ALL dropped, and no-new-privileges enabled. The only persistent mount was the owned source volume, mounted read-only; tmpfs state was disposable. Source-controlled synthetic capabilities, synthetic worker callbacks and no provider execution path establish this fixture's credential boundary. These are not host-wide credential-audit measurements. Before/after container, volume and cloud-workbench unit inventories had no differences; `/etc/passwd` hashes matched. Snapshots cannot rule out transient changes or attribute concurrent operations.

Snapshot relay hash: `63f978ae32a56e425e03cbf6b1cc3371e06f9981d9b2bcf0a90bdaa8a02ac379`. Service hash: `0bf7b90ae23368d5e2ea887e857f9c1e825c986265198b959751af13a8027ff5`. The legacy `execute` callback was used. New `DispatchContext`/`execute_request` provider integration is not qualified here. Hermes plugins and tirith were disabled in this fixture; neither is claimed production-ready by this result.

Cleanup checked owner labels using explicit conditional guards, removed containers by inspected full ID and removed the exact owned volume. Final owner-filtered reads returned zero containers and zero volumes. Per-resource cleanup exceptions are recorded and do not prevent subsequent removal attempts or the local receipt write. This does not guarantee cleanup after SIGKILL, host loss or an unreachable daemon; any recovery must inspect the receipt's exact owner label and identities before removing resources. No broad sweep is performed.

Not qualified: real providers or credential leases, production UID provisioning, service activation, restart persistence, cross-container worker mounting, ptrace attach, shared `/tmp` behavior, cgroup escape resistance, resource exhaustion resilience, or signal types other than SIGTERM. Controller interruption of the caller after response loss remains required by the separate D9 relay checkpoint.
