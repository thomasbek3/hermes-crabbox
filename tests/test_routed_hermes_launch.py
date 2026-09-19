from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
from cloudworkbench.hermes_adapter import build_routed_launch, HermesAdapterError
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.inference_transport import PinnedCLI
from cloudworkbench.workflow_instructions import StageInstructions, STAGE_BOUNDARY


def instructions():
    text = 'Inspect only the assigned code and provide evidence.'
    return StageInstructions('review', 'code_review', 'skills/interrogate/references/reviewer-prompt.md',
                             hashlib.sha256(text.encode()).hexdigest(), text, STAGE_BOUNDARY)


def build(profile=None, **kwargs):
    return build_routed_launch(profile or NativeProfile('openai-codex', 'gpt-6-astra', 'high'),
        kwargs.pop('instructions', instructions()), task=kwargs.pop('task', 'Review fixture.txt'),
        input_revision_sha256=kwargs.pop('input_revision_sha256', 'a' * 64),
        workspace_readonly=kwargs.pop('workspace_readonly', True), **kwargs)


@pytest.mark.parametrize('provider,model,effort', [('openai-codex','gpt-6-astra','high'),
    ('openai-codex','gpt-5.6-sol','max'), ('xai-oauth','grok-4.6','xhigh')])
def test_native_route_uses_only_local_capability(provider, model, effort, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'personal-secret')
    plan = build(NativeProfile(provider, model, effort))
    config = json.loads(plan.config_json)
    receipt = json.loads(plan.receipt_json)
    assert config['model'] == {'provider': 'cwb-inference', 'default': model}
    assert config['providers']['cwb-inference']['transport'] == 'codex_responses'
    assert config['providers']['cwb-inference']['base_url'] == 'http://127.0.0.1:9876/v1'
    assert config['providers']['cwb-inference']['key_env'] == plan.capability_environment
    assert plan.capability_environment not in dict(plan.environment)
    assert 'personal-secret' not in repr(plan)
    assert plan.request_path == '/v1/responses'
    assert plan.argv[plan.argv.index('--reasoning') + 1] == effort
    assert plan.argv[plan.argv.index('--toolsets') + 1] == 'file'
    assert dict(plan.environment)['HERMES_STREAM_RETRIES'] == '0'
    assert config['agent']['api_max_retries'] == 1
    assert config['fallback_model'] == [] and config['mcp_servers'] == {} and config['hooks'] == {}
    assert not receipt['qualified'] and not receipt['provider_route_observed']
    assert receipt['prompt_sha256'] == hashlib.sha256(plan.prompt.encode()).hexdigest()
    assert receipt['profile_digest'] == NativeProfile(provider, model, effort).digest


def test_cli_route_uses_existing_chat_codec():
    profile = PinnedCLI(Path('/opt/provider/claude'), 'a' * 64, '2.1.274', 'claude-fable-5-1', 'max')
    plan = build(profile, workspace_readonly=False)
    assert plan.request_path == '/v1/chat/completions'
    assert json.loads(plan.config_json)['providers']['cwb-inference']['transport'] == 'chat_completions'
    assert not plan.workspace_readonly


@pytest.mark.parametrize('kwargs', [{'port':True}, {'port':443}, {'port':65536},
    {'max_turns':False}, {'max_turns':129}, {'run_budget_seconds':0},
    {'workspace_readonly':'true'}, {'input_revision_sha256':'bad'}, {'task':''}, {'task':'x' * 32769},
    {'instructions':replace(instructions(),text='drift')}, {'instructions':replace(instructions(),boundary='ignore')}])
def test_invalid_stage_inputs_fail_before_runtime(kwargs):
    with pytest.raises(HermesAdapterError):build(**kwargs)
