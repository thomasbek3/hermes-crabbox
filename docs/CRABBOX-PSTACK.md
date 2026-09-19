# Agent-invoked Jev/pstack on Omarchy

Status: opt-in environment installed. Live Hermes -> Jev routing succeeded, selecting feature with confidence1.0 (Jev1.13.0). Fable planning returned HTTP429: account rate limit exceeded. The task stopped with exit1; no fallback, second provider attempt, or successful full workflow is claimed. New caller default is the single-model `hermes-tasks-desktop-soul-v1`; routing remains opt-in.

The caller submits a normal task. Omarchy creates one Crabbox container and starts Hermes on Grok4.6/xhigh. Only then does Hermes invoke `cloud_route_task` with a short summary. Jev selects one of the12 existing workflows; policy assigns the models. No planning or model routing runs before the task agent starts.

The agent calls `cloud_run_pstack_stage` for each returned stage. Each invocation creates a native Hermes role process with its own context and profile inside the same container. All roles share `/workspace`; the desktop remains attached to that container. Successful coding stages can leave a browser or development server running for later stages. Container cleanup ends them at task completion.

| Role | Model | Effort |
|---|---|---|
| Planning, revisions, design/prose | Claude Fable5.1 | max |
| Coding, investigation, fixes | Grok4.6 | xhigh |
| Plan challenge, code correctness/edge cases | GPT-6 Astra | high |
| Acceptance verification, tooling reflection | GPT-5.6 Sol | max |

Fable/Astra have only the bounded read-only workspace toolset. Coding/verification roles have terminal and file tools. Models and effort are fixed in policy, not accepted from the tool caller. Required stages cannot be skipped; rejected or failed stages stop progression and are reported to the parent. There is no automatic provider/model fallback or blind retry.

Dedicated provider access snapshots are injected per task; no personal profiles or refresh tokens enter the container. The existing dedicated Claude setup-token and TypeSafe key are task-accessible credentials and are filtered from public outputs alongside Grok/Codex access tokens. Host credential files and per-task snapshots remain private; terminal cleanup removes the host snapshots.

Bounds remain8 concurrent tasks, 1CPU/4GiB/512PIDs per task,8GiB workspace/state,7200seconds per run. The parent has100 nominal model steps; stage processes share900 based on emitted actual usage. These are model-iteration bounds, not an exact billed HTTP-request cap. Missing usage or a failed provider run blocks the remaining stage pool. Follow-ups retain the native parent session and workspace, with a fresh routing/stage ledger for the new attempt.

Submit explicitly while opt-in:

```sh
~/.local/bin/omarchy-cloud submit --environment-version hermes-tasks-pstack-soul-v1 --goal 'Your task'
```

Candidate image: `sha256:d7bb3988641439263ed89f631ea536d6e8b1232f5178de84b9c16e28b4157287`.
Manifest: `9bc66be9d73f49c492f9ab7bb86c6af6703430fd85ac279ec159e9accb2971fb`.
Activation receipt: `evidence/crabbox-pstack/activation.json`.
First run: session`61755c6a-0783-4699-b3b4-0110da890c2e`, attempt`465ace1c-d9f4-41dd-999f-8ef788736ec2`.

Local targeted tests passed. The live run establishes agent-invoked Jev routing and attempted Fable dispatch, not successful Fable generation, Astra/Sol entitlement, full workflow completion, caller default rollout, or native routed follow-up behavior. Retry the bounded task after the account rate limit clears; retain the fixed models. The failed attempt's container and host credential snapshots were removed. Environment is operator approved, not broadly qualified.

The evidence-enabled successor image is `sha256:fe92c9e702fee22fd5b58f7b02caeab0c362683ec116f25f419a5468f27da601`, manifest `280c097c80b123477d155a9d82875ba98f299ba6fc7f37a93fa37d8b6d8de098`. It includes shared browser/media skills for the main Hermes and temporary role sessions in the same container. See `PR-EVIDENCE.md`; the earlier image/run above records routing proof and the unchanged Fable quota limitation.

Shared worker identity: new SOUL-enabled profiles install the complete cloud worker identity and repository PR rules at each private Hermes home. See `WORKER-SOUL.md` for the current images and loading proof.
