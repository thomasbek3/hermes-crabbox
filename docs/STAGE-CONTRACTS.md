> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Stage final-answer contracts

`stage_contracts.py` is a pure versioned parser and instruction renderer. It
validates the model's final JSON answer, received from `adapter.result.summary`.
It does not read files, run commands, publish artifacts, mutate a database, write
gates, or grant runtime authority. Parsed evidence and review recommendations
remain `worker_reported`; `StageOutput.task_acceptance_verified` is always false.

## Controller API

```python
contract = stage_contract(role, input_revision_sha256, context_refs)
prompt_section = render_stage_instructions(contract)
result = parse_stage_output(adapter_summary, contract)
normalized_json_object = result.to_dict()
```

`StageContract` and nested results are frozen dataclasses. The contract exposes
`to_dict()`, `canonical_json`, and `digest` (SHA256 of compact, sorted-key,
UTF-8 JSON). `VERSION = 'stage-final-answer-v1'` binds the schema and validation
semantics. The controller must freeze this version with the workflow and use
its independently constructed expected contract when parsing. Output-provided
bindings are echoes, not authorization.

Context references have exactly `artifact_id` and `sha256` fields. At most 32
references are accepted; declaration order is preserved and checked. Repeated
artifact IDs are rejected. IDs are 1–128 characters from the bounded identifier
alphabet; hashes and input revision use lowercase 64-character SHA256 syntax.
Reference provenance/content validation belongs to the trusted context loader.

## JSON structure

Every answer has exactly these common fields:

- `schema_version`: integer 1 (not boolean or floating point).
- `contract_sha256`, `role`, `input_revision_sha256`, `context_refs`: exact echoes.
- `summary`: nonempty conclusion, up to 8192 decoded UTF-8 bytes.
- `evidence`: up to 64 `{location, reason}` objects, respectively at most
  1024 and 2048 decoded UTF-8 bytes. Empty evidence is permitted when unavailable;
  the instructions require uncertainty to be stated rather than invented proof.

Role-specific fields:

| Roles | Additional fields |
| --- | --- |
| `planning`, `plan_revision` | `status`: `complete` or `needs_review`; `plan`: up to 64 `{step,action,validation}` objects. Consecutive integer steps start at 1. A complete plan must be nonempty; action and validation are each nonempty and at most 2048 bytes. |
| `plan_review`, `code_review` | `recommendation`: `approve`, `revise`, or `needs_review`; `findings`: up to 64 `{severity,location,reason,resolved}` objects. Severity is `critical`, `high`, `medium`, or `low`; resolved is a boolean. Unresolved critical or high findings forbid approval. Location/reason use evidence text limits. |
| Coding roles: `feature`, `bug_fix`, `refactoring`, `perf_issue`, `hillclimb` | `status`: `complete` or `needs_review`. |
| `acceptance_verification`, `how_explorer`, `how_explainer`, `why_investigator`, `why_synthesizer`, `reflect_tooling` | `status`: `complete` or `needs_review`. Summary/evidence hold the findings or explanation; completion of this report is not protected acceptance of the task. |

The entire original final JSON must fit **16384 UTF-8 bytes**, including binding
fields, whitespace and escapes. This matches the current Hermes adapter summary
bound. Individual field maxima cannot all be used simultaneously. JSON escape
expansion counts toward the raw bound; decoded string bounds are checked too.
No code fences, trailing content, duplicate keys, unknown fields, nonfinite
numbers, coerced scalar types, lone surrogates, control characters or Unicode
format characters are accepted. Text cannot embed escaped newlines/tabs either.
Invalid input yields a fixed `StageContractError.code` without echoing contents.

## Integration boundaries

A structurally valid plan can still be a poor plan. A claimed resolved finding
can still be unresolved. Evidence locations are opaque claims, never commands
or file-read instructions. No parser result independently approves a revision,
authorizes mutation, or establishes protected check success. The controller
must validate authenticated saved observations, artifact provenance, current
assignment/revision, verification and cleanup using its existing lifecycle.
The trusted controller artifact envelope and completion transaction are separate
work; this module neither creates nor authenticates those records.

All current `WORKFLOWS` roles are covered, with a regression that detects role
catalogue drift. Remediation-only `judgment` is not currently a workflow stage
and is explicitly unsupported in version 1. Larger plans/artifacts and multiline
source snippets require a separate versioned output path; no adapter limits were
changed here.

## Local evidence

`evidence/stage-contracts-tests.xml` and `.log`: 129 passing local tests, including
all role roundtrips, exact context order, version/digest and immutable copies,
malformed JSON and bindings, duplicate nested keys, scalar type confusion,
aggregate UTF-8 bounds, concrete plan steps, severe review findings and render
roundtrips. `evidence/stage-contracts-binding.json` binds the source and tests.
No provider/model calls, remote operations or deployment occurred in this unit.
