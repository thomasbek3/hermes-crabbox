"""Offline wrapper validation; actual Linux execution remains parent-owned."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_routed_driver_linux_qualification import good_receipt as driver_receipt

PATH = Path(__file__).resolve().parents[1]/'scripts/legacy/qualify-routed-verification-linux.py'
spec = importlib.util.spec_from_file_location('verification_qualification', PATH)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('offline only')))
    return module.prepare(tmp_path/'bundle.json', 'passed')


def proof_receipt(bundle):
    value = driver_receipt(bundle)
    mode = bundle['verification_mode']; empty = mode == 'empty'
    record = {'spec_sha256':'a'*64,'runtime_id':'b'*64,'exit_code':0 if mode=='passed' else 7,
        'cleanup_confirmed':True,'logs_sha256':'c'*64,
        'log_observation':{'uid':1000,'gid':1000,'candidate_read':True,'candidate_readonly':True,'scratch_writable':True}}
    value['worker']['composition']['protected_verification'] = {'version':1,'mode':mode,
        'driver_harness_sha256':bundle['driver_harness_sha256'],'final_artifact_count':3,
        'outcome':'needs_review' if empty else mode,'plan_sha256':'a'*64,'receipt_sha256':'b'*64,
        'artifact_id':'verification-'+'c'*64,'artifact_bytes':123,'candidate_sha256':'3'*64,
        'candidate_file_sha256':module.sha(b'SYNTHETIC_ROUTED_CALLER_FILE\n'),
        'input_revision_sha256':'5'*64,'cleanup_sha256':'7'*64,'records':[] if empty else [record],
        'create_calls':0 if empty else 1,'start_calls':0 if empty else 1,
        'replay_equal':True,'replay_no_create_start':True,'root_occupancy_retained':True,
        'no_gate_or_seat_release':True,'candidate_unchanged':True,'event_storage_path_absent':True,
        'required_tool_gid':1000,'source_gid':1000,'source_directory_gid':1000}
    value['export_cleanup']['verifier_cleanup'] = {'version':1,'remaining':[],
        'receipts':[] if empty else [{'spec_sha256':'a'*64,'cleanup_confirmed':True}]}
    return value


def test_prepare_freezes_original_driver_and_transitive_sources_without_mutation(bundle):
    assert module.validate_bundle(bundle) is None
    assert bundle['driver_harness'] == (PATH.parent/module.DRIVER).read_text()
    assert bundle['driver_harness_sha256'] == module.sha(bundle['driver_harness'].encode())
    assert bundle['harness'] == PATH.read_text()
    assert all(name+'.py' in bundle['sources'] for name in module.SEEDS)
    driver = module.configured_driver(bundle)
    assert driver.QUALIFICATION_BUNDLE is bundle
    assert driver.validate_receipt is module.validate_receipt


@pytest.mark.parametrize('field', ['driver_harness','driver_harness_sha256','harness','support','image','verification_version','verification_mode'])
def test_bundle_tampering_refused(bundle,field):
    value = copy.deepcopy(bundle); value[field] = 'changed'
    with pytest.raises(ValueError): module.validate_bundle(value)


def test_bundle_requires_complete_verifier_source_closure(bundle):
    value = copy.deepcopy(bundle)
    value['sources'].pop('routed_verification.py'); value['hashes'].pop('routed_verification.py')
    with pytest.raises(ValueError): module.validate_bundle(value)


def test_frozen_package_imports_independent_of_workspace(tmp_path):
    value = module.prepare(tmp_path/'bundle.json', 'passed')
    package = tmp_path/'cloudworkbench'; package.mkdir()
    for name, text in value['sources'].items(): (package/name).write_text(text)
    result = subprocess.run([sys.executable,'-I','-c',
        "import sys;sys.path.insert(0,sys.argv[1]);from cloudworkbench.routed_verification import prepare_verification,run_verification;from cloudworkbench.routed_verifier_runtime import VerifierRuntime", str(tmp_path)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('mode',module.MODES)
def test_receipt_requires_mode_specific_actual_check_outcome(bundle,mode):
    bundle['verification_mode'] = mode
    value = proof_receipt(bundle)
    assert module.validate_receipt(value,bundle)
    value['worker']['composition']['protected_verification']['outcome'] = 'passed' if mode!='passed' else 'rejected'
    with pytest.raises(ValueError): module.validate_receipt(value,bundle)


@pytest.mark.parametrize('key,value', [('version',True),('create_calls',True),('start_calls',0),
    ('driver_harness_sha256','f'*64),('final_artifact_count',2),('required_tool_gid',999),('source_gid',0),('source_directory_gid',0),('candidate_sha256','f'*64),
    ('replay_no_create_start',False),('candidate_unchanged',False),('event_storage_path_absent',False),
    ('no_gate_or_seat_release',False),('receipt_sha256','bad'),('artifact_bytes',True)])
def test_receipt_refuses_unproven_or_wrong_boundaries(bundle,key,value):
    receipt = proof_receipt(bundle); receipt['worker']['composition']['protected_verification'][key] = value
    with pytest.raises(ValueError): module.validate_receipt(receipt,bundle)


def test_historical_driver_receipt_cannot_pass_new_proof(bundle):
    with pytest.raises(ValueError): module.validate_receipt(driver_receipt(bundle),bundle)


@pytest.mark.parametrize('change', ['remaining','wrong_spec','unconfirmed','uid','exit'])
def test_receipt_requires_exact_cleanup_and_actual_tool_identity(bundle,change):
    value = proof_receipt(bundle)
    closed = value['export_cleanup']['verifier_cleanup']
    record = value['worker']['composition']['protected_verification']['records'][0]
    if change=='remaining': closed['remaining']=['b'*64]
    elif change=='wrong_spec': closed['receipts'][0]['spec_sha256']='f'*64
    elif change=='unconfirmed': closed['receipts'][0]['cleanup_confirmed']=False
    elif change=='uid': record['log_observation']['uid']=0
    else: record['exit_code']=True
    with pytest.raises(ValueError): module.validate_receipt(value,bundle)


def test_script_is_fixed_synthetic_readonly_probe_and_no_check_means_no_script():
    for mode,code in [('passed',0),('rejected',7)]:
        env,scripts=module.environment(mode,module.IMAGE)
        source=scripts['protected']; compile(source,'check.py','exec')
        assert f'sys.exit({code})'.encode() in source
        assert b'os.getuid()==1000' in source and b'errno.EROFS' in source
        assert env['checks'][0]['script_sha256']==module.sha(source)
    env,scripts=module.environment('empty',module.IMAGE)
    assert env['checks']==[] and scripts=={}


def test_preparation_never_overwrites_prior_bundle(bundle,tmp_path):
    with pytest.raises(FileExistsError): module.prepare(tmp_path/'bundle.json','passed')


def test_cleanup_specs_are_strictly_bound_to_original_caller_and_fixture(bundle,tmp_path):
    from cloudworkbench.routed_verifier_runtime import CheckRuntimeSpec
    folder=tmp_path.resolve(); material=folder/'verification-material'; material.mkdir()
    candidate=material/'candidate'; candidate.mkdir(); task=material/'task'; task.mkdir()
    spec=CheckRuntimeSpec(owner=bundle['run_id'],attempt_id='child',generation=1,root_id='root',root_generation=1,
        plan_sha256='a'*64,check_id='protected',image=bundle['image'],candidate_path=candidate,task_path=task,
        candidate_identity=(candidate.stat().st_dev,candidate.stat().st_ino),
        task_identity=(task.stat().st_dev,task.stat().st_ino),
        argv=('/opt/hermes/venv/bin/python','-I','-B','/run/task/check.py'),timeout_seconds=15)
    identity={'spec':{'attempt_id':'child'}}
    assert module.read_verifier_specs(folder,bundle,identity)==()
    path=folder/'verification-identities.json'
    entries=[{'spec':spec.document,'digest':spec.digest}]; path.write_text(json.dumps(entries))
    assert module.read_verifier_specs(folder,bundle,identity)==(spec,)
    with pytest.raises(RuntimeError):module.read_verifier_specs(folder,bundle,{'spec':{'attempt_id':'other'}})
    entries[0]['digest']='f'*64; path.write_text(json.dumps(entries))
    with pytest.raises(RuntimeError):module.read_verifier_specs(folder,bundle,identity)


def test_cleanup_hook_checks_verifier_absence_before_parent_cleanup(bundle,tmp_path):
    from types import SimpleNamespace
    folder=tmp_path.resolve(); calls=[]
    def command(args):
        calls.append(('docker',args))
        assert '--filter' in args and 'label=io.cloudworkbench.role=routed-verifier' in args
        return ''
    driver=SimpleNamespace(docker_command=lambda _:command)
    def parent(*args):calls.append(('parent',args));return {'collector_removed':True}
    value=module.reconcile_verifiers(driver,parent,bundle,folder,bundle,{'spec':{'attempt_id':'child'}})
    assert value['verifier_cleanup']=={'version':1,'remaining':[],'receipts':[]}
    assert [entry[0] for entry in calls]==['docker','parent']
    driver.docker_command=lambda _:lambda args:'a'*64
    with pytest.raises(RuntimeError,match='verification_cleanup_unconfirmed'):
        module.reconcile_verifiers(driver,parent,bundle,folder,bundle,{'spec':{'attempt_id':'child'}})
    assert len(calls)==2


def test_worker_entry_dispatches_frozen_wrapper_not_unmodified_driver(bundle,tmp_path,monkeypatch):
    folder=tmp_path/'fixture';folder.mkdir();(folder/'bundle.json').write_text(json.dumps(bundle))
    seen=[]
    monkeypatch.setattr(sys,'argv',[str(PATH),'--worker-entry',str(folder)])
    monkeypatch.setattr(module,'configured_driver',lambda value:type('Driver',(),{'main':lambda self:seen.append(value)})())
    module.main()
    assert seen==[bundle]


def test_postpublication_readback_allows_absence_check_without_launch_authority():
    assert module.READBACK_ACTIONS == {'load','publish','cleanup_inspect'}
    assert not module.READBACK_ACTIONS.intersection({'create','start','stop','remove','prepare'})
