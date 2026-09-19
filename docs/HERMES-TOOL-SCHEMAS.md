> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# Pinned Hermes base tool-schema evidence

The source-only fixture at `tests/fixtures/hermes/base-tool-schemas.json` preserves the pinned Hermes read_file, write_file, patch, search_files and terminal base declarations. Extraction reads AST data; it imports or executes no Hermes module and accesses no credentials. The script verifies the original candidate source-manifest hash and each of three source-file hashes before extraction. `evidence/hermes-base-tool-schemas.json` binds the resulting fixture and extraction assumptions.

These actual declarations include `default` annotations and terminal.notify's `anyOf` boolean/array-of-string alternatives. A validator supporting only plain object/string schemas would refuse real tools despite passing simplified fixtures. The provider transport must deliberately support these bounded shapes or refuse the tool profile before launch. Defaults are annotations; the transport does not silently insert defaults into returned model arguments. Hermes's actual handler still owns execution/default semantics.

The source-default foreground timeout is600seconds and DEFAULT_READ_LIMIT is2000. Runtime settings may change the former. This fixture does not prove the loaded runtime's effective values. Dynamic OCR wording and OpenAI-family patch schema overlays are likewise outside this base-source fixture; their route-specific effective schemas require actual isolated registry/startup proof before claiming support. No broad plugin/tool registry export or provider call occurred.

D4 identity follow-up source finding: model_tools._execute_tool passes task_id/session_id to registry handlers but not tool_call_id as a keyword. Its surrounding _approval_observability context binds native tool-call/session IDs into tools.approval_context ContextVars, and agent/tool_executor supplies those IDs from native dispatch. A separately isolated probe is checking whether the trusted handler can reliably read these pinned context fields, including retry/missing/concurrent cases. This is not yet durable role-broker or idempotency proof; model-supplied arguments cannot fill missing identity fields.
