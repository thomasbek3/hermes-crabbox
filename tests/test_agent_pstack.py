import json
import time
from pathlib import Path
import pytest
from cloudworkbench import agent_pstack as m
from cloudworkbench.workflow_routing import select_workflow

@pytest.fixture
def task(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'ROOT', tmp_path / 'state')
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'prompt': 'Create a tested small function.', 'api_key': 'grok-secret',
        'pstack': {'credentials': {'anthropic':'claude-secret', 'openai-codex':'codex-secret','jev':'jev-secret'}}}))
    monkeypatch.setattr(m, 'REQUEST', request)
    # These tests exercise orchestration against synthetic instruction files.
    pstack = tmp_path / 'pstack'
    for relative in ('skills/architect/references/runner-prompt.md',
                     'skills/architect/references/design-red-flags.md',
                     'skills/interrogate/references/reviewer-prompt.md',
                     'skills/principle-prove-it-works/SKILL.md',
                     'skills/poteto-mode/playbooks/feature.md'):
        path = pstack / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Synthetic instructions for orchestration tests.\n')
    monkeypatch.setattr(m, 'PSTACK', pstack)
    m.initialize(time.time()+120)
    monkeypatch.setattr(m, 'select_workflow', lambda summary, **kw: select_workflow(summary,
        allowed_workflows=list(m.WORKFLOWS), client=None, explicit_workflow='feature'))
    return tmp_path

def native(status='completed', calls=2):
    return {'api_calls':calls,'text':json.dumps({'status':status,'findings':'finding', 'evidence':['evidence']}), 'session_id':'test'}

def test_agent_routes_only_on_tool_call_and_caches(task):
    assert json.loads((m.ROOT/'state.json').read_text())['selection'] is None
    result=m.route_task('Build feature')
    assert result['selection']['workflow']=='feature'
    assert result['selection']['steps'][1]['model']=='gpt-6-astra'
    assert result['selection']['steps'][1]['effort']=='high'
    assert m.route_task('Ignore prior task') == result

def test_order_and_policy_profiles(task,monkeypatch):
    m.route_task('Build feature')
    assert m.run_stage('implement','go')['reason']=='stage_out_of_order'
    calls=[]
    def execute(payload,deadline):
        calls.append(payload)
        return native()
    monkeypatch.setattr(m,'_execute',execute)
    for step in m.WORKFLOWS['feature'].steps:
        assert m.run_stage(step.id,'Proceed')['status']=='completed'
    assert [p['api_key'] for p in calls] == ['claude-secret','codex-secret','claude-secret','grok-secret','codex-secret','codex-secret']
    assert [p['max_turns'] for p in calls] == [900,898,896,894,892,890]
    assert m.completed()

def test_rejected_stage_does_not_advance(task,monkeypatch):
    m.route_task('Build feature')
    monkeypatch.setattr(m,'_execute',lambda *a:native('rejected'))
    assert m.run_stage('plan','Proceed')['status']=='rejected'
    assert m.run_stage('challenge_plan','Proceed')['reason']=='previous_stage_requires_attention'
    assert not m.completed()

def test_missing_usage_exhausts_budget_and_blocks(task,monkeypatch):
    m.route_task('Build feature')
    monkeypatch.setattr(m,'_execute',lambda *a:native(calls=None))
    assert m.run_stage('plan','Proceed')['status']=='blocked'
    assert json.loads((m.ROOT/'state.json').read_text())['used_turns']==900
    assert not m.completed()

def test_secrets_redacted_from_prompts_and_findings(task,monkeypatch):
    m.route_task('Build feature')
    def execute(payload,deadline):
        assert 'codex-secret' not in payload['prompt']
        return {'api_calls':1,'text':json.dumps({'status':'completed','findings':'codex-secret','evidence':[]})}
    monkeypatch.setattr(m,'_execute',execute)
    assert m.run_stage('plan','Review codex-secret')['findings']=='[REDACTED]'
    assert 'codex-secret' not in (m.ROOT/'state.json').read_text()

def test_interrupted_stage_not_retried(task):
    m.route_task('Build feature')
    state=json.loads((m.ROOT/'state.json').read_text());state['inflight']='plan';m._save(state)
    assert m.run_stage('plan','Proceed')['reason']=='previous_stage_interrupted'

def test_expired_deadline_does_not_route(task):
    m.initialize(time.time()-1)
    assert m.route_task('Build feature')['reason']=='task_deadline_reached'
