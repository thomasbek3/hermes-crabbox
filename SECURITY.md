# Security and access

Hermes Crabbox is a preview integration operated on a private Tailscale network.
It is not advertised as a hostile multi-tenant sandbox. Container isolation and
host policies should be evaluated for the work you permit.

## Access boundaries

- Network access requires the caller's actual tool host to reach the approved
  tailnet. There is no public Funnel endpoint in the configured deployment.
- The API/MCP also requires a service-issued credential with project and action
  scopes. Task ownership is checked independently of network membership.
- Provider access, task credentials, and GitHub credentials are distinct.
  Downloading the skill or cloning this repo grants none of them.
- Dedicated task model access is present where workers need it. Personal login
  directories, parent GitHub authorization, and the host Docker socket are not
  intended worker mounts. Keep credentials out of prompts and exported evidence.
- Desktop access requires the separate authorized SSH viewer setup; it is not
  exposed as a public VNC service by the MCP endpoint.

## Reporting a vulnerability

For a sensitive finding, contact the repository owner through an established
private channel. GitHub private vulnerability reporting is not assumed enabled.
Do not include working tokens, private keys, personal data, full auth responses,
or exploit details in a broadly visible issue. Use synthetic/redacted examples.

For a non-sensitive bug, use [the issue form](https://github.com/thomasbek3/hermes-crabbox/issues/new?template=bug_report.yml).
There is no published response-time commitment or long-term support policy yet.

## Operator actions

Provision one credential per caller, use the host's secret manager, and revoke
access when it is no longer needed. Review source snapshots and artifacts for
secrets before transfer. Do not run operator deployment scripts against another
host without adapting and reviewing their paths, image pins, and policy.
