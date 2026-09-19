# Protected check runtime: isolated Linux qualification

Actual Omarchy run `verifierqual-633ea851f3b041b7` passed eight synthetic container scenarios against 43 hash-bound current controller modules. Evidence is `evidence/routed-verifier-linux-v4.json` and its frozen `-source.json`. The one-shot harness is `evidence/prove-routed-verifier-linux.py`.

The controller fixture ran as root, while each check used container UID/GID1000 and the existing immutable image `sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693`. This does not qualify worker959 provisioning, the complete controller composition, physical providers, model/effort identities, or the complete workflow. No real provider calls or credentials were used.

Verified behavior:

- The check read the expected candidate. Candidate, protected script and root filesystem write attempts failed. Only loopback existed, no-new-privileges was active, and expected credential variables were absent. Both stdout and stderr appeared in the retained diagnostics.
- A script printing a forged success object and exiting7 produced exit7, not success.
- A sleeping script hit its one-second timeout, exited137 after cleanup, and retained `timed_out=true`.
- A synthetic forbidden string prevented private diagnostic persistence and any usable result, both on its own and when followed by1,200 additional lines. Exact cleanup still completed. The runtime reads all Docker-retained output with byte/time caps; Docker rotation remains a limit on full-lifetime completeness.
- Publication/execution authorization revoked immediately after start caused exact cleanup without a usable result.
- A zero-log-budget script with empty output completed. This profile rejects nonempty diagnostics rather than silently discarding them.
- Fresh-runtime loading and repeated runtime calls returned equal durable results without another create/start.
- An actual exited container with no durable exit observation was retained through a simulated controller interruption. Fresh-runtime reconciliation cleaned it and returned completion_timing_unproven with no usable result. It did not invent a timeout or a pass.
- All source mounts contained a colon in the candidate directory name. The actual Docker --mount CSV parser accepted these paths; the proposed colon restriction was unnecessary.

All eight container identities were reconciled; none remained. The exact owned temporary fixture was removed after identity and mount checks. The image and API/worker/cloudd service PIDs1240083/1222709/974255 remained unchanged. This is component qualification, with no deployment or activation.

The initial v1 harness attempt failed with a generated Python SyntaxError before executing any remote code. Its empty output and stderr are retained. Fixing the bootstrap source delimiter produced the fresh v2 run; no application source changed between v1/v2. Successful v2 and v3 receipts remain historical. Fresh v4 qualifies the later recovered-timing, POSIX-access and full-retained-log corrections against runtime SHA ad0dfcde4be07f547df995839c7de7297bf37e9f0c5eeae0553edc6f0007ccf7.
