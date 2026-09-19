# Install a worker computer

[Home](../README.md) · [Agent setup](AGENT-SETUP.md) · [Connect the calling agent](QUICKSTART.md)

Run host setup **on the machine that will execute tasks**, not automatically on
the computer displaying the chat. The installer creates a new installation and
refuses to replace an existing deployment. It is a preview; see
[verification status](STATUS.md) for exactly what has been exercised.

## Platform support

| Machine | Role |
| --- | --- |
| Linux x86_64 with systemd and Docker Engine | Worker-host installer target |
| Intel Mac or x86_64 Windows PC | Caller directly; worker host inside an appropriately configured Linux x86_64 VM |
| Apple Silicon / ARM Linux | Caller; native ARM worker images are not supplied in this release |
| Cloud/SaaS agent | Caller only if its actual tool-execution environment can join/reach your Tailscale network and hold its service credential |

A Linux VM must support rootful Docker, systemd, loop devices and ext4 mounts;
install Tailscale **inside that VM**. Docker Desktop alone is not this Linux host
installation. Emulated x86_64 VMs on ARM have not been qualified here. Do not
claim that an unsupported platform became supported by bypassing preflight.

## 1. Prepare prerequisites

Inspect existing software first and use the operating system's normal package
manager for Python 3.11+ with `venv`, Git, sudo, e2fsprogs, and util-linux.
Use the official [Docker Engine installation guide](https://docs.docker.com/engine/install/)
and [Tailscale setup](https://tailscale.com/download/linux) for the selected OS.
Do not replace an existing Docker daemon or reset an existing Tailscale setup.

Join the host to the owner's tailnet, then identify its full MagicDNS hostname:

```sh
tailscale status
```

The host origin is `https://<hostname>.<tailnet>.ts.net`. Enable Tailscale HTTPS
certificates when required by [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve).
The installer uses private Serve routes, never Funnel. An existing Serve
configuration requires deliberate operator integration; it is not overwritten.

Ensure enough free disk for the image build, per-task workspaces and exported
results. The installer reserves capacity for the OS and queues work that does
not fit. Its default is two concurrent tasks, each with a 4 GiB memory ceiling
and a 20 GiB bounded workspace. These are limits, not measured idle usage.

## 2. Authenticate a dedicated worker login

The initial installer uses **Grok through the Grok CLI's OIDC login** and the
`grok-4.6` worker model. It does not accept an arbitrary model-provider API key or
a Claude/Codex login in place of that credential. The optional multi-model pstack
profile is separate from fresh-host setup.

Install [the official Grok CLI](https://github.com/xai-org/grok-build#installing-the-released-binary)
if needed. Create a separate credential home as the operator, without altering
the operator's personal Grok configuration:

```sh
umask 077
mkdir -p "$HOME/.local/share/hermes-crabbox/grok"
GROK_HOME="$HOME/.local/share/hermes-crabbox/grok" grok login --device-auth
```

Complete the provider's [device login](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md)
when prompted. Keep the resulting `auth.json` private. The installer accepts the
file path; never paste its contents into a prompt, command argument, issue, or
Git. The login needs provider entitlement to the configured model; successful
login alone does not prove that entitlement.

## 3. Inspect the installation plan

Clone the repository on the worker host:

```sh
git clone https://github.com/thomasbek3/hermes-crabbox.git
cd hermes-crabbox
python3 scripts/install-host.py --plan \
  --origin https://worker.example.ts.net \
  --grok-auth "$HOME/.local/share/hermes-crabbox/grok/auth.json"
```

Replace the example origin with this host's actual Tailscale origin. The command
prints a non-secret JSON report. Exit 0 means preflight is ready; exit 2 means
prerequisites are missing; exit 1 means an invalid configuration or other error.
An agent should resolve the specific `issues` and rerun the plan. A plan does
not create services, authenticate accounts, or execute model tasks.

Optional limits: `--capacity 1` through `8`, `--memory-mib`, and `--workspace-mib`.
Start small and measure actual workload behavior before increasing concurrency.

## 4. Install on the selected host

Once the owner has authorized this host installation, apply the same arguments:

```sh
sudo python3 scripts/install-host.py --apply \
  --origin https://worker.example.ts.net \
  --grok-auth "$HOME/.local/share/hermes-crabbox/grok/auth.json"
```

The explicit path expands in the operator's shell before sudo runs. Installation
builds the pinned public-source image, installs Crabbox with a checked release
hash, creates service identities, prepares the environment registry and task
configuration, and starts the API, worker, MCP and credential-refresh services.
Provider login stays outside task containers; tasks receive dedicated access
snapshots. The Docker socket is not handed to the worker's tool environment.

| Installed component | Location |
| --- | --- |
| Service source and Python environment | `/opt/cloud-workbench` |
| MCP service environment | `/opt/omarchy-mcp` |
| Configuration and install receipt | `/etc/cloud-workbench` |
| Task state, results and dedicated auth | `/var/lib/cloud-workbench` |
| Service units | `cloud-workbench-api`, `cloud-workbench-worker`, `cloud-workbench-mcp` |

The JSON receipt returns the origin, MCP URL, project, environment and image
identity. It does **not** claim a model task completed. Repeating an identical
completed installation returns its receipt; changed settings or an existing
unmanaged installation require an explicit operator workflow. If an install
stops partway, inspect the error and preserve its state instead of deleting
paths or pretending a partial setup succeeded.

## 5. Authorize and connect the caller

Create one credential for the intended agent on the host:

```sh
sudo /opt/cloud-workbench/.venv/bin/python \
  /opt/cloud-workbench/scripts/provision-delegation-client.py laptop-agent
```

Use a different name per caller. The result contains a protected credential
**file path**, not the secret. Transfer it through an approved secure channel or
secret manager to the actual tool-execution host. Do not reuse the model login as
a task API credential. Repeated provisioning preserves a matching valid caller.

Continue with [caller setup](QUICKSTART.md) on that computer. Agents using MCP
should read `get_delegation_guide`; its connection settings come from this host.

## 6. Verify what works

Check services and the read-only caller connection before submitting work. Then,
with authorization to use model quota, submit a small task, save its IDs, retrieve
the resulting file and confirm container cleanup. Test desktop viewing separately
if needed. A running service is not proof of model entitlement or a completed job.

[Operations and recovery](HOST-OPERATIONS.md) · [Desktop viewing](DESKTOP.md) ·
[Build inputs and upstream licenses](../BUILDING.md)
