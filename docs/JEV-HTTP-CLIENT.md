> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# TypeSafe HTTP transport

`cloudworkbench.jev_client.JevClient(api_key, timeout_seconds=10).evaluate(payload)` returns a JSON object. `JevError.code` is a fixed sanitized error code. The constructor accepts an in-memory server-side credential; it never loads credentials, logs bodies/keys, or exposes them in repr. Keep the client in the trusted controller, never browser/job configuration.

The implementation uses direct stdlib `HTTPSConnection` to `api.typesafe.ai:443`, POST `/v1/systemone`, with a verified default TLS context and bearer authorization. It does not consult proxy environment variables, follow redirects or automatically retry. In particular 429 and 529 remain failures; callers must decide whether another paid request is authorized. Error response bodies are not read or propagated, and underlying network exception text/chains do not escape.

Limits are 256 KiB serialized request and 1 MiB response body, depth64 and 20,000 JSON nodes. Request objects must contain ordinary JSON types and string keys. Responses must be UTF-8 `application/json` objects with no duplicate keys or nonfinite numbers (including exponent overflow). Compressed responses are refused. Declared oversize responses are rejected before reading the body; undeclared lengths are capped while reading. Standard-library HTTP header limits also apply.

The transport intentionally does not validate Choice options, probability ranges/sums, model identity, question IDs or usage fields. The workflow selector owns those semantic checks. A successful HTTP/JSON parse alone does not authorize a workflow.

`timeout_seconds` must be finite, positive and at most60. It supplies socket-operation timeouts using the remaining monotonic call budget; checks between operations reject success after the budget expires. This is **not a hard wall-clock limit**: Python DNS resolution, header trickling during library parsing, and OS I/O may exceed it before control returns. There are no background timeout threads or abandoned network requests. Each response/connection is closed on completion or error. `client.last_error_code` holds only the fixed last failure code (or `None` after success); `client.last_elapsed_seconds` measures the observed call duration, and errors also carry `elapsed_seconds`. Last-call metadata is shared mutable observation, so use a client per controller task rather than sharing it concurrently. A timeout after transmission can mean the provider already evaluated/billed the request; no retry is implied.

The live [HTTP API documentation](https://docs.typesafe.ai/api.md), [documentation index](https://docs.typesafe.ai/llms.txt) and [Choice documentation](https://docs.typesafe.ai/primitives/choice.md) were read on2026-09-18 UTC. The documented endpoint, bearer scheme and request/answer shape match this transport. The examples in the tests use synthetic keys; network error cases are mocked, and eight framing/body-size cases use an actual loopback TLS server with a temporary synthetic certificate. The production endpoint is redirected only inside those tests. No actual API evaluation, credential read or provider billing occurred in this unit.

Verification: `pytest -q tests/test_jev_client.py` —66 passed. Parent workflow-selector integration and its checkpoint review are separate from this transport unit.

## Content-Length socket lifecycle correction

A real loopback TLS regression reproduced the original failure: a complete133-byte Content-Length response with Connection:close left `HTTPResponse.isclosed() == True` and the captured socket descriptor at-1. The old loop attempted another `settimeout`, incorrectly returning `network_error` after successfully receiving the body. The corrected loop checks `response.isclosed()` before the next socket timeout/read and still verifies declared length, body cap, JSON and elapsed budget. Real TLS tests exercise small and200KB bodies under Content-Length, chunked and EOF framing, plus truncation in both sizes. No additional TypeSafe inference calls were made to reproduce or verify this correction.
