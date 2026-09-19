> Engineering reference from the initial implementation. For current setup, use
> [Agent setup](AGENT-SETUP.md) and [Host installation](HOST-INSTALL.md). Historical
> scripts and machine/image receipts are not fresh-install instructions or proof
> that a new deployment has passed these checks.

# D4 native role-call identity: source and isolated proof

The pinned Hermes synchronous tool path provides the native session ID as a dispatch keyword, but does not provide the native tool-call ID as a handler keyword. A handler can read both IDs from private approval ContextVars during ordinary dispatch. The original unpatched path works in the isolated source fixture, including concurrent calls, but is **insufficient as the production D4 identity contract**: the upstream observability binder fails open on a binding exception and can leave a stale outer tool-call ID visible. No shared Hermes or adapter source was changed by this investigation.

Source pin:3b0e392e5a6922034feccac5771041ac78467757. Exact five-file hashes and the executable probe hash are in `evidence/hermes-role-call-identity.json`. The probe imports the clean git archive, not personal Hermes source, with dependencies supplied by the installed Python environment. It creates only disposable local profile/plugin/work directories. Network calls are denied, external auth/config/dotenv reads are guarded, and the completed run records zero attempts at either. No provider or auth operation is invoked.

## Actual path

1. `agent/tool_executor.py:_ToolCallRef` retains name/args/task_id/call_id/trace. `_resolve_sequential_dispatch` at lines1543–1600 creates the registered-tool executor closure.
2. That closure calls `model_tools.handle_function_call`, supplying `tool_call_id=ref.call_id`, `session_id=agent.session_id`, plus turn/API-request IDs. These are separate keyword arguments, not fields copied out of model tool arguments.
3. `model_tools.py:867` constructs `_CallIds`; `_execute_tool` at817 supplies `task_id`, `session_id` and `user_task` to registry dispatch. It does **not** supply `tool_call_id` to an ordinary handler.
4. `_approval_observability` at799 temporarily binds the IDs with `tools.approval_context.set_current_observability_context` around dispatch and resets them afterward.
5. A synchronous handler registered using the actual `PluginContext.register_tool` API reads `_approval_session_id.get()` and `_approval_tool_call_id.get()` from `tools.approval_context.py:25–29`. These are private implementation details, not a supported public getter. `get_current_session_key` is a different routing identity with fallbacks and must not substitute for the native session ID.

The probe does not merely set ContextVars and call its handler. It loads a disposable plugin through the real plugin manager, verifies actual registry registration, and invokes the real agent executor closure with a synthetic agent state and native call reference. That closure runs the real model_tools and registry dispatch. No model execution is used. Hermes requires this handler to return a supported JSON string/result shape; the first fixture returned an arbitrary dict and failed, retained as `hermes-role-call-identity-fixture-failure1.stderr`, then corrected only the fixture.

## Verified normal behavior

- Repeating the same session/call identity and payload returns the same derived request key and payload digest.
- Changing either session or call changes the key. Changing only payload preserves the key but changes the digest, which would allow a future broker to return409. No broker/database conflict handling is implemented here.
- Missing session/call IDs return a fixed refusal; no random or default identity is substituted.
- Model-supplied session/tool-call fields are refused by the fixture's finite argument contract. The real handler also checks the session ContextVar equals the trusted dispatch session keyword.
- Eight simultaneous handlers rendezvous at a barrier and observe their own distinct contexts before and after waiting. Every worker's original context is restored after dispatch; nested dispatch restores its outer context as well.
- The public schema contains only role/task fields. The fixture's request-key seed includes controller-owned attempt/generation constants; these are fixture constants, not evidence that a real capability or controller identity has been installed.

This tests stable extraction when the runtime supplies the same ID. It does not prove native IDs survive a full CLI restart, transcript replay or transport retry. The future broker must persist the pending native call and its immutable attempt/generation binding before external transmission, enforce uniqueness and payload conflicts, and retain the explicit native-resume/reconstruction policy.

## Binding-failure reproduction

The probe binds an outer context `(session-A, stale-call)`, makes the native observability setter raise a synthetic exception, then dispatches `(session-A, fresh-call)` through the real executor closure. `_approval_observability` catches the exception and yields without clearing the outer context. The handler sees `stale-call`; its session cross-check still passes because both sessions match. The probe asserts and records this failure mode, then restores the original setter and outer context in the disposable process.

Consequently, merely checking nonempty private ContextVars does not fail closed for all binding failures. The recorded status is `PASS_normal_dispatch_WITH_known_binding_failure_gate`, not D4-ready. Private Python context is also mutable by trusted in-process code, and same-attempt arbitrary tool execution is not an authentication boundary. Native IDs are correlation inputs; owner/project/attempt/generation/authority must still come from the controller's scoped capability.

## Smallest proposed isolated-cloud patch

Before H2, amend the permitted patch scope in D4/D5 (parent/spec owner) and review a narrowly named handler contract. In pinned `model_tools._execute_tool`, for only the two exact approved routed handler names, validate native session/tool-call IDs from `_CallIds`, refuse missing/invalid IDs before dispatch, and pass the native pair directly as trusted keyword arguments to those handlers. The handlers must ignore/refuse identity fields in model arguments and derive the request key from these keywords plus their controller-issued immutable scope. Ordinary handlers must retain their existing kwargs behavior; no global registry ABI change or generic model-controlled selector.

That patch avoids depending on an approval observability side channel and its fail-open behavior. Freeze upstream+patch hashes, tool names/schema/handler signatures and tests. Test missing IDs, binding exceptions, nested/concurrent calls, payload mismatch, native replay and controller cancel/generation fences. Apply only to the isolated cloud candidate; never edit the personal/shared installation. The original investigation proposed this patch. Normative D8 subsequently authorized local preparation; the follow-on patch below is now implemented only in a disposable overlay, pending checkpoint disposition and later image qualification. A future public stable upstream getter/dispatch API could replace the patch after equivalent qualification.

The initial service transport must also preserve unique native tool IDs and pairing semantics. Source-fixture extraction alone cannot establish those provider/protocol guarantees, nor the durable broker and actual pstack workflow behavior.


## D8 isolated patch checkpoint

The patch in `patches/hermes/native-role-call-identity.patch` adds six executable lines and an explanatory docstring only within `_execute_tool`. For exactly `cloud_request_roles` and `cloud_get_role_results`, it checks `_CallIds.session_id` and `_CallIds.tool_call_id` are exact strings of1–256 printable ASCII graphic characters (no whitespace/control/non-ASCII). It returns the fixed `native_role_call_identity_unavailable` error before registry handler execution on failure. On success, the existing native `session_id` kwarg is retained and native `tool_call_id` is added. Ordinary handler kwargs are unchanged. These are bounded opaque correlation IDs, not proof of authorization or provider authenticity; actual transport ID format/length remains a qualification gate.

The patch metadata records the exact upstream commit, all five involved source-file SHAs, patch SHA and expected output SHA; each source pin is checked before the overlay is created. Five individual dependency-drift cases prove refusal. The probe refuses a mismatched upstream hash before copying or applying anything, checks the patch digest, applies with zero fuzz only inside a fresh disposable overlay, and verifies the output digest. The unchanged clean source supplies dependencies; Python imports model_tools from the verified overlay. The original source/image preparation tree and personal/shared installs are untouched.

`tests/hermes_role_identity_patch_probe.py` registers both exact names using the real PluginContext API and exercises the real agent executor closure and registry. It proves48 invalid ID cases refuse before either handler increments its invocation counter,256-character boundaries are accepted, forged model identity fields are refused by the fixture schema/handler, repeat and changed-ID/payload semantics hold, eight concurrent calls preserve identity, a handler performs a real nested dispatch without losing outer identity, and every caller context restores. An ordinary registered handler gets exactly session_id/task_id/user_task, without tool_call_id. With the original observability-binding fault and stale outer context injected, both routed handlers now get the fresh native call ID; missing IDs still refuse before handler.

The fixture handlers read only direct kwargs, not private observability ContextVars. Their JSON-string return shape matches the actual registry contract. This is a synchronous-handler checkpoint; no asynchronous handler guarantee or full broker/tool implementation is inferred. The patch validates native IDs, while model argument schemas remain the responsibility of the two trusted handlers. The broker must still bind capability, canonical tool/argument digest, attempt/generation and persisted pending-call identity and enforce uniqueness/cancel/revocation before admitting work.

Original review evidence: `evidence/hermes-role-identity-patch.json`; corrected evidence: `evidence/hermes-role-identity-patch-corrected.json`; frozen review inputs: `evidence/hermes-role-identity-patch-review-snapshot`. The prior unpatched reproduction and its original document are preserved separately. No image build, deployment, provider call, credential read or durable role request occurred. Actual provider/restart/replay semantics and durable broker integration remain open even if this narrow patch checkpoint passes review.


The single Fable patch review returned REVISE. Independent corrective disposition is in `reviews/hermes-role-identity-patch-disposition.md`. The corrected fixture also exercises real Tool Search bridge unwrapping for both names, delivering the same native pair. Eight parallel fixture calls use real sequential dispatch closures; native parallel executor bookkeeping, gateway/ACP/delegation paths and live provider IDs remain unqualified. Metadata limits the supported evidence claim accordingly. Pinned Hermes `coalesce_tool_call_id` reads call_id/id from the call record, so model/provider-origin IDs are not inherently unique or privileged; source helper fixtures confirm this without inference. The patched docstring and metadata explicitly state that trust limit. The forged-field refusal is labeled as fixture-handler behavior, not patch argument validation.
