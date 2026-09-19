import importlib.util
from pathlib import Path
import pytest

PATH=Path(__file__).resolve().parents[1]/'scripts/legacy/qualify-supervised-provider-linux.py'
spec=importlib.util.spec_from_file_location('supervised_qualification',PATH)
proof=importlib.util.module_from_spec(spec);spec.loader.exec_module(proof)


def receipt():
    import json
    path = PATH.parents[2]/'evidence/supervised-provider-linux-20260918T010306Z.json'
    if not path.is_file():
        pytest.skip('Optional historical provider receipt is not distributed')
    return json.loads(path.read_text())


def test_complete_receipt_accepted():
    assert proof.validate_receipt(receipt())


@pytest.mark.parametrize('kind',['not_passed','wrong_host','missing_check','leftover','double_charge','observer_alive','cancel_success','service_changed'])
def test_partial_or_misleading_receipt_rejected(kind):
    value=receipt()
    if kind=='not_passed':value['passed']=False
    if kind=='wrong_host':value['host']='mac-mini'
    if kind=='missing_check':value['checks'].pop('actual_cli_running_before_cancel')
    if kind=='leftover':value['cleanup']['remaining_objects']=['owned-container']
    if kind=='double_charge':value['budget_counts']['requests']=3
    if kind=='observer_alive':value['success']['supervisor']['stopped']=False
    if kind=='cancel_success':value['cancel']['raw_status']=200
    if kind=='service_changed':value['services_after']=['inactive']*3
    with pytest.raises(ValueError):proof.validate_receipt(value)


def test_source_inventory_is_complete_and_snapshottable():
    root=PATH.parents[2]
    for name in proof.MODULES:
        assert (root/'src/cloudworkbench'/name).is_file()
    assert len(proof.MODULES)==len(set(proof.MODULES))
    assert {'supervised_executor.py','cancellation_supervisor.py','inference_budget.py','provider_executor.py','provider_docker.py'}<=set(proof.MODULES)


def test_canonical_receipt_snapshot_and_archived_harness_match():
    import json
    base=PATH.parents[2]/'evidence/supervised-provider-linux-20260918T010306Z'
    if not base.with_suffix('.source-snapshot.json').is_file():
        pytest.skip('Optional historical provider archive is not distributed')
    snapshot=json.loads(base.with_suffix('.source-snapshot.json').read_text())
    assert proof.validate_archive(receipt(),snapshot,base.with_suffix('.harness.py').read_bytes())


@pytest.mark.parametrize('kind',['null_identity','clean_cancel','cancelled_success','missing_top_level','token_left','cleanup_after_success'])
def test_stronger_proof_invariants_reject_corruption(kind):
    value=receipt()
    if kind=='null_identity':value['success']['cleanup']['identities'][value['success']['request_id']]['provider_id']=None
    if kind=='clean_cancel':value['cancel']['raw_outer_cleanup_confirmed']=True
    if kind=='cancelled_success':value['success']['supervisor']['cancelled']=True
    if kind=='missing_top_level':value.pop('success')
    if kind=='token_left':value['synthetic_token_removed']=False
    if kind=='cleanup_after_success':value['success']['cleanup']['observed_monotonic']=value['success']['return_monotonic']+1
    with pytest.raises(ValueError):proof.validate_receipt(value)


@pytest.mark.parametrize('kind',['receipt_hash','source_byte','harness_byte','schema2_echo'])
def test_archive_binding_rejects_drift(kind):
    import json
    base=PATH.parents[2]/'evidence/supervised-provider-linux-20260918T010306Z'
    value=receipt();snapshot=json.loads(base.with_suffix('.source-snapshot.json').read_text());harness=base.with_suffix('.harness.py').read_bytes()
    if kind=='receipt_hash':value['source_hashes']['store.py']='0'*64
    if kind=='source_byte':snapshot['sources']['store.py']+=' '
    if kind=='harness_byte':harness+=b' '
    if kind=='schema2_echo':value.update(schema_version=2,harness_sha256='0'*64)
    with pytest.raises(ValueError):proof.validate_archive(value,snapshot,harness)
