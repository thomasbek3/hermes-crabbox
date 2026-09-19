import json
import pytest
from cloudworkbench.server import worker_readiness


def check(tmp_path, heartbeat, **config):
    (tmp_path/'heartbeat.json').write_text(json.dumps(heartbeat))
    return worker_readiness({'state_root':str(tmp_path),'agents':['claude'],
                             'claude_enabled':True,**config}, now=100)


def healthy():
    return {'timestamp':99,'errors':[], 'available_agents':['claude'], 'blocked_agents':{}}


def test_ready_requires_live_usable_adapter(tmp_path):
    assert check(tmp_path, healthy())['ready']
    h=healthy();h['blocked_agents']={'claude':'provider_auth_rejected'}
    result=check(tmp_path,h)
    assert not result['ready'] and not result['provider_enabled']
    assert result['worker']['blocked_agents']['claude']=='provider_auth_rejected'


@pytest.mark.parametrize('change',[
    {'timestamp':101}, {'timestamp':85}, {'timestamp':float('nan')},
    {'timestamp':True}, {'errors':[{'error':'RuntimeError'}]},
    {'available_agents':[]}, {'available_agents':['unknown']},
    {'available_agents':[{}]}, {'blocked_agents':[]},
])
def test_bad_or_stale_heartbeat_fails_closed(tmp_path,change):
    assert not check(tmp_path,{**healthy(),**change})['ready']


def test_legacy_heartbeat_and_disabled_provider_not_ready(tmp_path):
    assert not check(tmp_path,{'timestamp':99,'errors':[]})['ready']
    assert not check(tmp_path,healthy(),claude_enabled=False)['ready']


def test_fixture_only_works_in_explicit_test_mode(tmp_path):
    h={**healthy(),'available_agents':['fixture']}
    assert not check(tmp_path,h,agents=['fixture'])['ready']
    result=check(tmp_path,h,agents=['fixture'],test_mode=True)
    assert result['ready'] and not result['provider_enabled']


def test_missing_and_oversized_heartbeat(tmp_path):
    assert not worker_readiness({'state_root':str(tmp_path)},now=100)['ready']
    (tmp_path/'heartbeat.json').write_bytes(b' '*32769)
    assert not worker_readiness({'state_root':str(tmp_path)},now=100)['ready']


@pytest.mark.parametrize('enabled', [None, False, 1, 'true', True])
def test_hermes_readiness_requires_explicit_gate_and_usable_heartbeat(tmp_path, enabled):
    heartbeat = {**healthy(), 'available_agents':['hermes']}
    result = check(tmp_path, heartbeat, agents=['hermes'], hermes_enabled=enabled)
    assert result['ready'] is (enabled is True)
    assert result['provider_enabled'] is (enabled is True)
    heartbeat['blocked_agents'] = {'hermes':'dedicated_auth_unavailable'}
    assert not check(tmp_path, heartbeat, agents=['hermes'], hermes_enabled=True)['ready']
