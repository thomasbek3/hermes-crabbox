#!/usr/bin/env python3
"""Isolated local compatibility proof; never contacts an inference provider."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HERMES_COMMIT = '3b0e392e5a6922034feccac5771041ac78467757'


def run_isolated(source: Path, python: Path):
    source = source.resolve()
    with tempfile.TemporaryDirectory(prefix='cwb-responses-compat-') as folder:
        env = {'PATH': '/usr/bin:/bin', 'HOME': folder, 'HERMES_HOME': folder + '/hermes',
               'CODEX_HOME': folder + '/codex', 'CLAUDE_CONFIG_DIR': folder + '/claude',
               'XDG_CONFIG_HOME': folder + '/config', 'XDG_CACHE_HOME': folder + '/cache',
               'PYTHONPATH': str(source) + os.pathsep + str(ROOT / 'src'),
               'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_ENABLE_PROJECT_PLUGINS': '0',
               'HERMES_INTERACTIVE': '0', 'CWB_RESPONSES_COMPAT_CHILD': '1'}
        proc = subprocess.run([str(python), str(Path(__file__).resolve()), '--child',
            '--hermes-source', str(source)], cwd=folder, env=env, capture_output=True,
            text=True, timeout=45)
        if proc.returncode:
            # Only synthetic fixture inputs exist in this clean child environment.
            raise RuntimeError('compatibility_child_failed\n' + proc.stderr[-8000:])
        result = json.loads(proc.stdout)
        result['child_stderr_bytes'] = len(proc.stderr.encode())
        result['private_environment_removed'] = True
        return result


def child(source: Path):
    assert os.environ.get('CWB_RESPONSES_COMPAT_CHILD') == '1'
    import datetime
    import importlib.metadata
    import io
    import socket
    import threading
    from types import SimpleNamespace

    source = source.resolve()
    manifest = json.loads((source.parent / 'source-manifest.json').read_text())
    assert manifest['hermes_commit'] == HERMES_COMMIT
    connection_targets = []
    original_connect, original_connect_ex, original_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def loopback_only(address):
        if not isinstance(address, tuple) or address[0] != '127.0.0.1':
            raise RuntimeError('non_loopback_network_forbidden')
        connection_targets.append(address)

    def guarded_connect(sock, address):
        loopback_only(address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        loopback_only(address)
        return original_connect_ex(sock, address)

    def guarded_lookup(host, *args, **kwargs):
        if host != '127.0.0.1':
            raise RuntimeError('non_loopback_dns_forbidden')
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex = guarded_connect, guarded_connect_ex
    socket.getaddrinfo = guarded_lookup

    workbench_hashes = {name: hashlib.sha256((ROOT / 'src/cloudworkbench' / name).read_bytes()).hexdigest()
                        for name in ('native_responses.py', 'inference_service.py')}

    import httpx
    import openai
    import agent.codex_runtime as native_runtime
    import agent.codex_responses_adapter as native_adapter
    from cloudworkbench.inference_service import InferenceService, ServiceResponse
    from cloudworkbench.native_responses import NativeProfile, NativeResponses

    assert Path(native_runtime.__file__).resolve() == source / 'agent/codex_runtime.py'
    assert Path(native_adapter.__file__).resolve() == source / 'agent/codex_responses_adapter.py'
    profiles = [NativeProfile('openai-codex', 'gpt-6-astra', 'high'),
                NativeProfile('openai-codex', 'gpt-5.6-sol', 'max'),
                NativeProfile('xai-oauth', 'grok-4.6', 'xhigh')]
    tool = {'type': 'function', 'name': 'read_file', 'description': 'Read the synthetic fixture.',
            'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path'], 'additionalProperties': False}}
    fixture_text = 'SYNTHETIC_RESPONSES_TOOL_OUTPUT\nline 2 with Unicode: café'
    reasoning = {'type': 'reasoning', 'id': 'rs_fixture', 'summary': [], 'encrypted_content': 'opaque-synthetic-reasoning'}
    call = {'type': 'function_call', 'id': 'fc_provider_fixture', 'call_id': 'call_correlated_fixture',
            'name': 'read_file', 'arguments': '{"path":"fixture.txt"}', 'status': 'completed'}
    final = {'type': 'message', 'id': 'msg_fixture', 'role': 'assistant', 'status': 'completed',
             'content': [{'type': 'output_text', 'text': 'SYNTHETIC_RESPONSES_COMPAT_OK', 'annotations': []}]}

    def wire(profile, items, ordinal):
        events = [{'type': 'response.output_item.done', 'output_index': index, 'item': item}
                  for index, item in enumerate(items)]
        events.append({'type': 'response.completed', 'response': {'id': 'resp_' + str(ordinal),
            'status': 'completed', 'model': profile.model, 'output': None, 'usage': None}})
        return b''.join(b'event: ' + e['type'].encode() + b'\ndata: ' + json.dumps(e).encode() + b'\n\n' for e in events)

    class FixtureResponse:
        status = 200
        def __init__(self, raw): self.body = io.BytesIO(raw)
        def getheader(self, name, default=''): return 'text/event-stream' if name == 'Content-Type' else default
        def read1(self, size): return self.body.read(min(size, 17))

    class FixtureConnection:
        def __init__(self, raw): self.response = FixtureResponse(raw); self.sock = None; self.closed = False
        def connect(self): pass
        def request(self, method, path, **kwargs): pass
        def getresponse(self): return self.response
        def close(self): self.closed = True

    receipts = []
    for profile in profiles:
        for mode in ('raw_terminal_null', 'native_adapter'):
            captured, upstream_calls, deliveries = [], [], []
            token = hashlib.sha256(b'synthetic-local-http-capability').hexdigest()

            def execute(request, cancel):
                captured.append(request)
                items = [reasoning, call] if len(captured) == 1 else [final]
                raw = wire(profile, items, len(captured))
                if mode == 'native_adapter':
                    fixture = FixtureConnection(raw)
                    def factory(*args, **kwargs):
                        upstream_calls.append(args[0])
                        return fixture
                    decision = NativeResponses(profile, connection_factory=factory).execute(
                        request, credential='synthetic-not-a-provider-credential', cancel=cancel)
                    if decision.status != 'ok':
                        raise RuntimeError('native_adapter_refused:' + str(decision.error))
                    assert decision.transport_stopped and fixture.closed
                    assert decision.container_cleanup_required and not decision.credential_reuse_authorized
                    raw = decision.sse
                return ServiceResponse(200, 'text/event-stream', raw)

            server = InferenceService(('127.0.0.1', 0), request_path='/v1/responses',
                capability_sha256=hashlib.sha256(token.encode()).hexdigest(),
                authorize=lambda: True, execute=execute, delivery=deliveries.append)
            thread = threading.Thread(target=server.serve_forever, name='responses-compat-listener', daemon=False)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_address[1]) + '/v1'
            client = openai.OpenAI(api_key=token, base_url=base, max_retries=0,
                                  http_client=httpx.Client(trust_env=False, timeout=5))
            agent = SimpleNamespace(provider='synthetic-responses', model=profile.model, session_id='synthetic-compat',
                _interrupt_requested=False, _is_codex_backend=lambda: False,
                _touch_activity=lambda *a: None, _fire_stream_delta=lambda *a: None,
                _fire_reasoning_delta=lambda *a: None, _client_log_context=lambda: 'synthetic-compat')
            request = {'model': profile.model, 'reasoning': {'effort': profile.effort}, 'store': False,
                       'stream': True, 'include': ['reasoning.encrypted_content'], 'instructions': 'Use fixture tools.',
                       'tools': [tool], 'input': [{'role': 'user', 'content': 'Read fixture.txt'}]}
            try:
                first = native_runtime.run_codex_stream(agent, request, client=client)
                message, finish = native_adapter._normalize_codex_response(first,
                    issuer_kind='synthetic-compat', issuer_model=profile.model)
                assert finish == 'tool_calls' and len(message.tool_calls) == 1
                tc = message.tool_calls[0]
                assert tc.id == call['call_id'] and tc.response_item_id == call['id']
                assert message.codex_reasoning_items[0]['id'] == reasoning['id']
                assert message.codex_reasoning_items[0]['encrypted_content'] == reasoning['encrypted_content']
                messages = [{'role': 'user', 'content': 'Read fixture.txt'},
                    {'role': 'assistant', 'content': message.content,
                     'codex_reasoning_items': message.codex_reasoning_items,
                     'tool_calls': [{'id': tc.id, 'call_id': tc.call_id, 'type': tc.type,
                         'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}]},
                    {'role': 'tool', 'tool_call_id': tc.id, 'content': fixture_text}]
                replay = native_adapter._chat_messages_to_responses_input(messages,
                    current_issuer_kind='synthetic-compat', current_issuer_model=profile.model)
                second_request = dict(request, input=replay)
                second = native_runtime.run_codex_stream(agent, second_request, client=client)
                final_message, final_reason = native_adapter._normalize_codex_response(second,
                    issuer_kind='synthetic-compat', issuer_model=profile.model)
                assert final_reason == 'stop' and final_message.content == 'SYNTHETIC_RESPONSES_COMPAT_OK'
                assert len(captured) == 2 and captured == [request, second_request]
                replay_reason = next(item for item in captured[1]['input'] if item.get('type') == 'reasoning')
                replay_call = next(item for item in captured[1]['input'] if item.get('type') == 'function_call')
                replay_result = next(item for item in captured[1]['input'] if item.get('type') == 'function_call_output')
                assert replay_reason['encrypted_content'] == reasoning['encrypted_content']
                assert replay_call['call_id'] == replay_result['call_id'] == call['call_id']
                assert replay_call['arguments'] == call['arguments'] and replay_result['output'] == fixture_text
                assert 'id' not in replay_reason and 'id' not in replay_call
                receipts.append({'profile': {'provider': profile.provider, 'model': profile.model, 'effort': profile.effort},
                    'mode': mode, 'requests': len(captured), 'first_finish': finish, 'second_finish': final_reason,
                    'native_function_item_id': tc.response_item_id, 'call_id': tc.id,
                    'encrypted_reasoning_preserved': True, 'tool_output_preserved': True,
                    'replay_strips_provider_item_ids': True, 'raw_upstream_terminal_output': None,
                    'fixture_upstream_connections': len(upstream_calls), 'passed': True})
            finally:
                client.close()
                server.shutdown(); server.server_close(); thread.join(3)
            assert not thread.is_alive() and deliveries == [True, True]
            receipts[-1]['listener_stopped'] = True
            receipts[-1]['delivery_socket_writes'] = deliveries

    dangling = [t.name for t in threading.enumerate()
                if t.name in ('responses-compat-listener', 'native-provider-request')]
    assert not dangling
    assert len(connection_targets) == 12
    imported = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if not filename: continue
        path = Path(filename).resolve()
        if not path.is_relative_to(source) or not path.is_file(): continue
        name = 'hermes/' + str(path.relative_to(source))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert manifest['files_sha256'].get(name) == digest, 'pinned_source_mismatch:' + name
        imported[name] = digest
    assert all(hashlib.sha256((ROOT / 'src/cloudworkbench' / name).read_bytes()).hexdigest() == digest
               for name, digest in workbench_hashes.items()), 'workbench_source_changed_during_proof'
    return {'passed': True, 'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'scope': 'Pinned Hermes stream driver, normalizer and replay via local Responses listener; synthetic provider transport and tool output only.',
        'hermes_commit': HERMES_COMMIT, 'hermes_imports_sha256': imported,
        'workbench_sources_sha256': workbench_hashes,
        'dependencies': {name: importlib.metadata.version(name) for name in ('openai', 'httpx', 'pydantic')},
        'profiles': receipts, 'loopback_connections': len(connection_targets),
        'dangling_fixture_threads': dangling,
        'all_targets_loopback': all(address[0] == '127.0.0.1' for address in connection_targets),
        'real_provider_calls': 0, 'real_credentials_read': False, 'actual_hermes_tool_execution': False,
        'full_cli_startup': False, 'outer_container_cleanup_qualified': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source', type=Path, default=ROOT.parent / 'work/hermes-pstack-image-context/hermes')
    parser.add_argument('--python', type=Path, default=Path.home() / '.hermes/hermes-agent/venv/bin/python')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--child', action='store_true')
    args = parser.parse_args()
    result = child(args.hermes_source) if args.child else run_isolated(args.hermes_source, args.python)
    raw = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.write_text(raw)
    else:
        sys.stdout.write(raw)


if __name__ == '__main__':
    main()
