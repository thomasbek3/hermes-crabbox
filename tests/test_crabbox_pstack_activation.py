import copy
import importlib.util
from pathlib import Path


SCRIPT=Path(__file__).resolve().parents[1]/'scripts/activate-crabbox-pstack.py'
spec=importlib.util.spec_from_file_location('pstack_activation',SCRIPT)
activation=importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


def test_opt_in_config_clones_resources_and_preserves_defaults_and_approvals():
    image='sha256:'+'a'*64
    old='sha256:'+'b'*64
    project={'environment_versions':['hermes-tasks-desktop-v1'],
             'operator_approved_environments':{'hermes-tasks-desktop-v1':'existing-sha'},
             'environment_resources':{'hermes-tasks-desktop-v1':{'memory_mib':4096}}}
    configs={'api':{'capacity':8,'projects':{'hermes-tasks':copy.deepcopy(project)},'environment_registry':'old.db'},
             'worker':{'capacity':8,'hermes_capacity':8,'projects':{'hermes-tasks':copy.deepcopy(project)},
                       'operator_approved_images':[old],'environment_registry':'old.db',
                       'hermes_runtime':{'crabbox_image':old,'crabbox_images':[old],
                           'crabbox_desktop_images':[old], 'tool_image_allowlist':[old],
                           'environment_network_profile':'hermes-crabbox-bridge',
                           'environment_secret_refs':['grok-dedicated-oauth']}}}
    original=copy.deepcopy(configs)
    resources={'cpus':1,'memory_mib':4096,'pids':512,'workspace_mib':8192}
    candidate={'manifest_sha256':'new-sha','manifest':{'network_profile':'hermes-crabbox-bridge','resources':resources}}
    result=activation.updated_configs(configs,candidate,Path('/new.db'),image)
    assert configs==original
    for config in result.values():
        p=config['projects']['hermes-tasks']
        assert p['environment_versions']==['hermes-tasks-desktop-v1','hermes-tasks-pstack-v1']
        assert p['operator_approved_environments']=={'hermes-tasks-desktop-v1':'existing-sha','hermes-tasks-pstack-v1':'new-sha'}
        assert p['environment_resources']['hermes-tasks-pstack-v1']==resources
        assert config['capacity']==8 and config['environment_registry']=='/new.db'
    policy=result['worker']['hermes_runtime']
    assert policy['crabbox_image']==old and policy['environment_secret_refs']==['grok-dedicated-oauth']
    assert policy['environment_secret_refs_by_image']=={image:activation.SECRET_REFS}
    assert policy['crabbox_pstack_images']==[image]
    assert policy['crabbox_desktop_images']==[old,image]
    assert result['worker']['hermes_capacity']==8


def test_activation_source_syntax_and_default_execute_gate():
    raw=SCRIPT.read_text()
    compile(raw,str(SCRIPT),'exec')
    assert "parser.add_argument('--execute', action='store_true')" in raw
    assert "if not args.execute:" in raw
    assert activation.MODULES==('crabbox_runtime.py','crabbox_capture.py','runner.py')
