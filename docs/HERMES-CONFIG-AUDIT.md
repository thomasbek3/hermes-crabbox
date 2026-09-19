# Pinned Hermes source/config audit

Source commit:3b0e392e5a6922034feccac5771041ac78467757 (0.21.3). This is source evidence for an unqualified native launch foundation, not activation proof for the D1 provider-service topology. The clean archive is under work/hermes-pstack-image-context/hermes; source-manifest.json binds it. The final isolated discovery receipt records hashes for172 loaded source files and the harness itself.

| Surface | Pinned source consumer | Evidence and limit |
|---|---|---|
| chat query-file/stream-json/oneshot, provider/model/reasoning, in/toolsets, max-turns/run-budget, ignore-rules | hermes_cli/_parser.py:213–286; main.py:1805–1830 | Actual parser accepted the exact generated argv: evidence/hermes-argv-parser-proof.json. No provider startup. |
| Env safe mode vs CLI flag | hermes_cli/main.py:2930 `_apply_safe_mode`; plugins.py:1236 | Only the CLI flag sets IGNORE_USER_CONFIG; env flag alone skips plugin discovery. Actual loader under env flag preserved model/config, with bypass env absent. |
| config model/provider/api_mode and agent effort | cli.py `_init_model_routing`; cli_agent_setup_mixin.py:186–242,548 | Config and CLI are requested values. Resolution can normalize provider/model/effort; actual post-resolution attestation remains required. |
| terminal backend/cwd/timeout | cli.py:325–355 `_mirror_config_to_env` | Local backend uses process cwd; --in must select workspace before launch. Timeout capped180s by our generator. Runtime mount/resource proof remains separate. |
| memory flags | tools/memory_tool.py:222; agent/agent_init.py:1252–1284 | Actual get_builtin_memory_store_flags parsed generated config as (False,False). ignore-rules also supplies skip-memory/context. |
| approval single_query_mode | tools/approval_context.py:280–298; cli.py:4518–4525 | Actual consumer returned deny. This governs approval decisions; it does not deny every filesystem/tool operation or replace sandbox isolation. |
| empty fallbacks | hermes_cli/fallback_config.py:89; cli.py:2806; agent/agent_init.py:1026–1048 | Empty generated list loaded unchanged. Other provider-specific defaults still require effective-route qualification. |
| auxiliary provider/model/effort | agent/auxiliary_client.py:6040,6076,6149–6173 | Keys consumed per task; backend effort support/clamping is not proved by accepted syntax. No auxiliary inference performed. |
| title/background disabled | agent/title_generator.py:144–164; agent/background_review.py:1290–1312 | Named enabled/model_upgrade_enabled settings consumed. Config reader preserved them; full CLI startup remains unqualified. |
| delegation same-route settings | tools/delegate_tool_config.py:421–432,494–507 | Empty fallback disables inherited delegation fallback. Native delegation is not in launch toolsets and is forbidden for initial routed workflow. |
| hooks and MCP | hermes_cli/main.py:2912–2928; cli_agent_setup_mixin.py:523 | Empty generated maps are preserved. No plugin or MCP startup is claimed by foundation. |
| isolated HOME/config roots; project plugins | hermes_constants.py get_hermes_home; plugins_discovery.py:140–169 | Temporary-home proof excludes personal state; project plugin env flag is0. Empty HERMES_BUNDLED_PLUGINS is NOT disabling: use a nonempty protected empty-directory path in future workflow profile. |
| provider modules | providers/__init__.py:48–53,232–320,354–420 | Separate builtin provider path ignores the ordinary bundled-plugin override. Proof loads only clean builtin sources; no user provider directories; an unapproved entrypoint cannot load. Native provider registration is not entitlement/credential proof. |
| secret redaction | agent/redact.py:89; cli.py:374 | Upstream env flag is defense in depth. Our parser performs independent exact-known-secret screening. D1 must keep actual provider secrets outside Hermes in the first place. |

Hermes's config loader is permissive: the harness inserts a harmless unknown key and confirms it survives. We do not call this strict validation. Our generator controls its own finite schema and hashes it; source-consumer checks document intent, while qualification must establish actual behavior. The loader/import probe never launches an agent, touches personal profiles or invokes providers.

The emitter supports system/init, text, tool_use, tool_result and result. Other tool-progress callbacks are dropped by the emitter; unknown public JSONL types are rejected by our parser. emit_result propagates explicit exit or failed=true; error-only payloads may have exit0, which our parser now conservatively treats as failure.
