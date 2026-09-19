"""Qualified Hermes policy is explicit and cannot inherit Claude credentials."""
import copy

import pytest

from cloudworkbench.runner import PolicyRejected
from tests.test_runner import setup
from tests.test_environment_integration import enable_registry
from tests.test_environments import passed

NETWORK='hermes-coordinator-bridge-tools-none'
REFS=['grok-dedicated-oauth']
MISSING=object()


@pytest.fixture
def qualified(setup,tmp_path):
    store,owner,runtime,runner=setup
    registry,manifest,_=enable_registry(setup,tmp_path)
    runner.config['environment_network_profile']='claude-only'
    runner.config['environment_secret_refs']=['claude-subscription']
    runner.config['hermes_runtime']={'environment_network_profile':NETWORK,'environment_secret_refs':list(REFS)}
    manifest={**manifest,'version':'hermes-v1','network_profile':NETWORK,'secret_refs':list(REFS)}
    registry.register(manifest);registry.qualify('project','hermes-v1',passed)
    created=store.create_session(owner,{'agent':'hermes','project_id':'project','goal':'One job',
        'model':'grok-4.6','environment_version':'hermes-v1'},'hermes-env')
    a=store.get_attempt(created['attempt_id'])
    yield runner,registry,manifest,a
    runner.close()


def pinned(runner,registry,a,manifest):
    # Distinct immutable registry candidate; no forged qualification bypass.
    candidate={**manifest,'version':'candidate-v2'}
    registry.register(candidate);registry.qualify('project','candidate-v2',passed)
    result=copy.deepcopy(a)
    result['request']['environment_version']='candidate-v2'
    return result


def test_hermes_qualified_policy_pins_independently_of_global_claude(qualified):
    runner,registry,manifest,a=qualified
    result,project=runner.environment_project(a,pin=True)
    assert result['result']['environment']['manifest']['network_profile']==NETWORK
    assert result['result']['environment']['manifest']['secret_refs']==REFS
    assert result['result']['environment']['qualified'] is True
    assert runner.store.get_attempt(a['id'])['result']['environment']==result['result']['environment']
    assert runner.config['environment_network_profile']=='claude-only'
    assert runner.config['environment_secret_refs']==['claude-subscription']
    assert project['checks'][0]['id']=='protected'


@pytest.mark.parametrize('field,value',[
    ('environment_network_profile',MISSING),('environment_network_profile',None),
    ('environment_network_profile',True),('environment_network_profile',[]),
    ('environment_network_profile',''),('environment_network_profile','bad\nname'),
    ('environment_network_profile','x'*129),
    ('environment_secret_refs',MISSING),('environment_secret_refs',None),
    ('environment_secret_refs','grok-dedicated-oauth'),('environment_secret_refs',True),
    ('environment_secret_refs',('grok-dedicated-oauth',)),
    ('environment_secret_refs',[False]),('environment_secret_refs',[[]]),
    ('environment_secret_refs',['bad/ref']),('environment_secret_refs',['grok']*2),
    ('environment_secret_refs',['r'+str(n) for n in range(17)]),
])
def test_missing_or_malformed_hermes_policy_refuses_without_global_fallback(qualified,field,value):
    runner,_,_,a=qualified
    # Matching global policy must not rescue missing Hermes policy.
    runner.config['environment_network_profile']=NETWORK
    runner.config['environment_secret_refs']=REFS
    if value is MISSING:runner.config['hermes_runtime'].pop(field)
    else:runner.config['hermes_runtime'][field]=value
    with pytest.raises(PolicyRejected,match='Hermes.*policy'):
        runner.environment_project(a,pin=True)
    assert not runner.store.get_attempt(a['id'])['result']


@pytest.mark.parametrize('policy',[None,False,[],NETWORK])
def test_invalid_hermes_policy_mapping_refused(qualified,policy):
    runner,_,_,a=qualified;runner.config['hermes_runtime']=policy
    with pytest.raises(PolicyRejected,match='Hermes environment policy'):
        runner.environment_project(a)


@pytest.mark.parametrize('delta',[
    {'network_profile':'claude-only'}, {'secret_refs':['claude-subscription']},
    {'network_profile':'claude-only','secret_refs':['claude-subscription']},
    {'secret_refs':[]},
])
def test_hermes_cannot_adopt_claude_or_other_manifest_policy(qualified,delta):
    runner,registry,manifest,a=qualified
    candidate=pinned(runner,registry,a,{**manifest,**delta})
    expected = 'network profile is not allowed' if 'network_profile' in delta else 'network/secret'
    with pytest.raises(PolicyRejected,match=expected):
        runner.environment_project(candidate)


def test_secret_reference_order_is_exact(qualified):
    runner,registry,manifest,a=qualified
    runner.config['hermes_runtime']['environment_secret_refs']=['grok','second']
    candidate=pinned(runner,registry,a,{**manifest,'secret_refs':['second','grok']})
    with pytest.raises(PolicyRejected,match='network/secret'):
        runner.environment_project(candidate)


@pytest.mark.parametrize('network_enabled,explicit,network',[(False,False,'none'),(True,False,'legacy-configured'),(True,True,'claude-only')])
def test_claude_preserves_existing_global_policy_and_network_fallback(qualified,network_enabled,explicit,network):
    runner,registry,manifest,a=qualified
    runner.config['runtime']['network_enabled']=network_enabled
    runner.config['hermes_runtime']=None
    if not explicit:runner.config.pop('environment_network_profile')
    candidate=pinned(runner,registry,a,{**manifest,'network_profile':network,'secret_refs':['claude-subscription']})
    candidate['request']['agent']='claude'
    result,project=runner.environment_project(candidate)
    assert result is candidate and project['checks'][0]['id']=='protected'


def test_qualified_image_guard_still_precedes_acceptance(qualified):
    runner,registry,manifest,a=qualified
    candidate=pinned(runner,registry,a,{**manifest,'image_digest':'sha256:'+'d'*64})
    with pytest.raises(PolicyRejected,match='image is not allowed'):
        runner.environment_project(candidate)


def test_persisted_hermes_snapshot_rechecks_current_explicit_policy(qualified):
    runner,registry,manifest,a=qualified
    current,_=runner.environment_project(a,pin=True)
    registry.path.rename(registry.path.with_suffix('.offline'))
    assert runner.environment_project(current)[0] is current
    runner.config['hermes_runtime']['environment_secret_refs']=['different']
    with pytest.raises(PolicyRejected,match='network/secret'):
        runner.environment_project(current)


def test_legacy_no_registry_behavior_is_unchanged(setup):
    store,owner,_,runner=setup
    created=store.create_session(owner,{'agent':'hermes','project_id':'project','goal':'Explicit isolated fixture',
        'model':'grok-4.6','environment_version':'fixture-v1'},'no-registry')
    a=store.get_attempt(created['attempt_id'])
    assert runner.environment_project(a)[0] is a
    runner.close()


def test_image_secret_override_preserves_other_images_default(qualified):
    runner,registry,manifest,a=qualified
    other='sha256:'+'e'*64
    refs=['grok-dedicated-oauth','claude-dedicated-subscription','codex-dedicated-oauth','typesafe-jev']
    runner.config['hermes_runtime']['environment_secret_refs_by_image']={other:refs}
    runner.environment_project(a)
    runner.config.setdefault('operator_approved_images',[]).append(other)
    candidate=pinned(runner,registry,a,{**manifest,'image_digest':other,'secret_refs':refs})
    runner.environment_project(candidate)
    assert runner.config['hermes_runtime']['environment_secret_refs']==REFS


@pytest.mark.parametrize('override',[None,[],{'latest':['key']},
    {'sha256:'+'a'*64:['bad/ref']},{'sha256:'+'a'*64:['duplicate','duplicate']},
    {'sha256:'+'a'*64:[False]},{'sha256:'+'a'*64:'secret'}])
def test_invalid_image_secret_overrides_rejected(qualified,override):
    runner,_,_,a=qualified
    runner.config['hermes_runtime']['environment_secret_refs_by_image']=override
    with pytest.raises(PolicyRejected,match='image secret policy'):
        runner.environment_project(a)
