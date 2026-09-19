"""Real pinned Hermes/SDK integration over local sockets, with synthetic inference."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('responses_compat_proof', ROOT / 'scripts/qualify-native-responses-local.py')
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


@pytest.fixture(scope='module')
def qualified():
    source = Path(os.environ.get('CWB_HERMES_SOURCE', ROOT.parent / 'work/hermes-pstack-image-context/hermes'))
    python = Path(os.environ.get('CWB_HERMES_DEPENDENCY_PYTHON', Path.home() / '.hermes/hermes-agent/venv/bin/python'))
    if not (source / 'agent/codex_runtime.py').is_file() or not python.is_file():
        pytest.skip('Pinned Hermes source archive and existing dependency interpreter required; no downloads/install in this test')
    return proof.run_isolated(source, python)


@pytest.mark.parametrize('model', ['gpt-6-astra', 'gpt-5.6-sol', 'grok-4.6'])
@pytest.mark.parametrize('mode', ['raw_terminal_null', 'native_adapter'])
def test_actual_pinned_stream_driver_and_two_turn_replay(qualified, model, mode):
    item = next(p for p in qualified['profiles'] if p['profile']['model'] == model and p['mode'] == mode)
    assert item['passed'] and item['requests'] == 2
    assert (item['first_finish'], item['second_finish']) == ('tool_calls', 'stop')
    assert item['native_function_item_id'] == 'fc_provider_fixture'
    assert item['call_id'] == 'call_correlated_fixture'
    assert item['encrypted_reasoning_preserved'] and item['tool_output_preserved']
    assert item['raw_upstream_terminal_output'] is None
    assert item['replay_strips_provider_item_ids']
    assert item['fixture_upstream_connections'] == (2 if mode == 'native_adapter' else 0)
    assert item['listener_stopped'] and item['delivery_socket_writes'] == [True, True]


def test_source_provenance_and_actual_sdk_versions_are_recorded(qualified):
    assert qualified['hermes_commit'] == proof.HERMES_COMMIT
    assert {'hermes/agent/codex_runtime.py', 'hermes/agent/codex_responses_adapter.py',
            'hermes/agent/sdk_transform_bypass.py'} <= qualified['hermes_imports_sha256'].keys()
    assert all(len(value) == 64 for value in qualified['hermes_imports_sha256'].values())
    assert set(qualified['dependencies']) == {'openai', 'httpx', 'pydantic'}
    assert set(qualified['workbench_sources_sha256']) == {'native_responses.py', 'inference_service.py'}


def test_proof_is_local_and_not_provider_or_cli_qualification(qualified):
    assert qualified['loopback_connections'] == 12 and qualified['all_targets_loopback']
    assert qualified['real_provider_calls'] == 0 and not qualified['real_credentials_read']
    assert not qualified['full_cli_startup'] and not qualified['actual_hermes_tool_execution']
    assert not qualified['outer_container_cleanup_qualified']
    assert qualified['private_environment_removed'] and not qualified['dangling_fixture_threads']
    assert qualified['child_stderr_bytes'] == 0
