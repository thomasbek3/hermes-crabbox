import ast
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
PATH=ROOT/'scripts/legacy/qualify-native-supervised-linux.py'
spec=importlib.util.spec_from_file_location('native_supervised_proof',PATH)
proof=importlib.util.module_from_spec(spec);spec.loader.exec_module(proof)


def test_four_exact_distinct_resource_ids_required():
    value={name:str(i)*64 for i,name in enumerate(proof.RESOURCE_KEYS,1)}
    assert proof.full_ids(value)==value
    for other in ({},dict(value,provider_id=None),dict(value,provider_id='short'),dict(value,provider_id=value['gateway_id'])):
        with pytest.raises(ValueError):proof.full_ids(other)


def test_new_budget_and_recovery_dependencies_are_snapshotted():
    assert {'budget_authority.py','provider_recovery.py','supervised_executor.py'}<=set(proof.MODULES)
    assert len(proof.MODULES)==len(set(proof.MODULES))
    for name in proof.MODULES:ast.parse((ROOT/'src/cloudworkbench'/name).read_text())
    for name in ('qualify-native-supervised-linux.py','qualify-native-supervised-caller.py.txt'):
        ast.parse((ROOT/'scripts/legacy'/name).read_text())


def test_validator_rejects_incomplete_execution():
    with pytest.raises(ValueError):proof.validate({'passed':False},{})


def canonical():
    import json
    base=ROOT/'evidence/native-supervised-linux-20260918T012005Z'
    if not all(base.with_suffix(s).is_file() for s in ('.json', '.source-snapshot.json', '.harness.py')):
        pytest.skip('Optional historical native Docker evidence is not distributed')
    return json.loads(base.with_suffix('.json').read_text()),json.loads(base.with_suffix('.source-snapshot.json').read_text()),base.with_suffix('.harness.py').read_bytes()


def test_actual_native_docker_evidence_and_all_sources_are_bound():
    receipt,bundle,harness=canonical()
    assert proof.validate_archive(receipt,bundle,harness)


@pytest.mark.parametrize('kind',['source','harness','helper','receipt_hash','tool_result','nonce','journal','isolation','uid','cgroup','mount','cleanup_order','duplicate_runtime','budget','unstopped','token','run_id','caller_image','fallback','caller_cleanup','volume_cleanup','image_cleanup','cleanup_exit','content_type','tool_use','first_tool_result'])
def test_actual_evidence_corruption_fails(kind):
    receipt,bundle,harness=canonical()
    if kind=='run_id':receipt['run_id']='different-run'
    if kind=='caller_image':receipt['caller_image']='sha256:'+'0'*64
    if kind=='fallback':receipt['cleanup']['actions'][0]['fallback_provider_cleanup']=True
    if kind=='caller_cleanup':receipt['cleanup']['actions'][0]['id']='0'*64
    if kind=='volume_cleanup':receipt['cleanup']['actions'][1]['name']='different-volume'
    if kind=='image_cleanup':receipt['cleanup']['actions'][2]['id']='sha256:'+'0'*64
    if kind=='cleanup_exit':receipt['cleanup']['actions'][0]['exit']=1
    if kind=='content_type':receipt['inferences'][0]['content_type']='application/json'
    if kind=='tool_use':receipt['native']['events']=[e for e in receipt['native']['events'] if e.get('type')!='tool_use']
    if kind=='first_tool_result':receipt['inferences'][0]['actual_tool_result']=True
    if kind=='source':bundle['sources']['store.py']+=' '
    if kind=='harness':harness+=b' '
    if kind=='helper':bundle['caller_script']+=' '
    if kind=='receipt_hash':receipt['source_hashes']['store.py']='0'*64
    if kind=='tool_result':receipt['inferences'][1]['actual_tool_result']=False
    if kind=='nonce':receipt['inferences'][1]['nonce']=receipt['inferences'][0]['nonce']
    if kind=='journal':receipt['worker_rows'][0][2]='admitted'
    if kind=='isolation':receipt['native']['isolation_checks'][0]['errno']=0
    if kind=='uid':receipt['relay_process']['uid']=1000
    if kind=='cgroup':receipt['native']['process']['cgroup']['cgroup.procs']='1\n'
    if kind=='mount':receipt['caller_boundary']['mounts'][0]['RW']=True
    if kind=='cleanup_order':receipt['inferences'][0]['cleanup']['at']=receipt['inferences'][0]['returned_at']+1
    if kind=='duplicate_runtime':receipt['inferences'][1]['cleanup']['resources']=receipt['inferences'][0]['cleanup']['resources']
    if kind=='budget':receipt['budget']['requests']=3
    if kind=='unstopped':receipt['inferences'][1]['supervisor']['stopped']=False
    if kind=='token':receipt['synthetic_token_removed']=False
    with pytest.raises(ValueError):proof.validate_archive(receipt,bundle,harness)
