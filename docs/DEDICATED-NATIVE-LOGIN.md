# Dedicated native provider device logins

Prepared on verified host `omarchy` as root, then initiated only as cloud-worker UID959/GID960. New private root `/var/lib/cloud-workbench/auth/native-login-20260918` owns separate `codex` and `grok` HOME trees; each0700. No personal auth/config was copied. Native binaries were copied from installed distributions to the new private bin directory and SHA matched. They are unchanged, root-owned0555 files under the new worker-owned setup directory; pin/copy into a reviewed production image remains separate.

Codex0.154.0 binary SHA3188814c35471432d4123203e0eb38e5bddc60226e3d7ddf0e59e649ea140022. Grok1.0.34 native binary SHAbe5905e107d2b8b5f3c142d21ecfe4c8fd32a913d2fd551b788707930c4dc80d. The personal Grok shell wrapper invokes global mise configuration before CLI startup; it is not used for these login commands. Installed Grok bootstrap source confirms GROK_HOME isolation; the native binary exposes the same variable. Codex supports CODEX_HOME and the new config.toml fixes `cli_auth_credentials_store="file"`.

Commands already initiated under supervised PTYs (these are not instructions to create duplicate logins):

```sh
sudo -u cloud-worker env -i \
 HOME=/var/lib/cloud-workbench/auth/native-login-20260918/codex \
 CODEX_HOME=/var/lib/cloud-workbench/auth/native-login-20260918/codex/.codex \
 XDG_CONFIG_HOME=/var/lib/cloud-workbench/auth/native-login-20260918/codex/.config \
 PATH=/usr/bin:/bin TERM=xterm-256color \
 /var/lib/cloud-workbench/auth/native-login-20260918/bin/codex login --device-auth

sudo -u cloud-worker env -i \
 HOME=/var/lib/cloud-workbench/auth/native-login-20260918/grok \
 GROK_HOME=/var/lib/cloud-workbench/auth/native-login-20260918/grok/.grok \
 XDG_CONFIG_HOME=/var/lib/cloud-workbench/auth/native-login-20260918/grok/.config \
 PATH=/usr/bin:/bin TERM=xterm-256color \
 /var/lib/cloud-workbench/auth/native-login-20260918/bin/grok login --device-auth
```

Grok1.0.34 rejects combining `--oauth` with `--device-auth`; the device flag alone produces the xAI browser flow. Clean environment removes ambient keys, profiles, D-Bus/keychain references and loader variables. Worker UID cannot traverse Thomas's personal home. These facts establish scoped setup, not a proof against malicious installed binary code.

Only browser verification URL/user-entry code are surfaced to the owner. OAuth polling protocol device codes, token responses and saved credential contents are never printed. Login completion must be established from CLI terminal status and metadata, not presumed from browser click. No inference calls, model entitlement claims, service changes, production config references, automated refresh or credential reuse occur here. Login authorization and billed inference authorization remain separate.

Both supervised remote login processes subsequently completed with exit0. Scoped Codex status confirms ChatGPT authentication. Saved Codex/Grok auth files are private0600, UID959:GID960, one-link regular files. See evidence/native-login-success.json. Earlier expired/pending checkpoint is historical and preserved. No model inference or entitlement test was run.
