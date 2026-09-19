import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

PATH=Path(__file__).resolve().parents[1]/'scripts/legacy/qualify-routed-driver-linux.py'
spec=importlib.util.spec_from_file_location('routed_driver_qualification',PATH)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


@pytest.fixture
def bundle(tmp_path,monkeypatch):
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:(_ for _ in ()).throw(AssertionError('offline prepare')))
    return module.prepare(tmp_path/'prepared.json','sha256:'+'a'*64)


def test_prepare_offline_frozen_module_closure(bundle):
    assert module.validate_bundle(bundle) is None
    for name in ('routed_driver.py','routed_collection.py','scheduler.py','workflow_revisions.py','routed_observation_store.py'):
        assert name in bundle['sources']
    assert bundle['profile']==['openai-codex','gpt-6-astra','high']


@pytest.mark.parametrize('field',['image','harness','support','source','hash'])
def test_tampered_bundle_refused(bundle,field):
    value=copy.deepcopy(bundle)
    if field in ('image','harness','support'):value[field]='tampered'
    elif field=='source':value['sources']['routed_driver.py']+='tampered'
    else:value['hashes']['routed_driver.py']='b'*64
    with pytest.raises(ValueError):module.validate_bundle(value)


def test_prepare_refuses_existing_output(bundle,tmp_path):
    with pytest.raises(FileExistsError):module.prepare(tmp_path/'prepared.json',bundle['image'])


def test_isolated_closure_imports_without_workspace_package(tmp_path):
    package=tmp_path/'cloudworkbench';package.mkdir()
    for name,text in module.source_closure(PATH.parents[2]).items():(package/name).write_text(text)
    result=subprocess.run([sys.executable,'-I','-c',
        "import sys;sys.path.insert(0,sys.argv[1]);from cloudworkbench.routed_driver import drive_prepared_child;from cloudworkbench.scheduler import RoleScheduler;from cloudworkbench.provider_docker import BoundedDocker;from cloudworkbench.routed_results import publish_child_results",str(tmp_path)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_real_local_fixture_uses_scheduler_revision_and_admission(tmp_path):
    scheduler,child,prepared,profile,grant=module.setup_stage(tmp_path.resolve())
    assert child['state']=='preparing' and prepared.materialization.readonly
    assert (prepared.materialization.source/'fixture.txt').read_text()=='SYNTHETIC_ROUTED_CALLER_FILE\n'
    assert prepared.profile_digest==profile.digest and child['cancel_requested']==0
    with scheduler.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0]==child['id']
        assert db.execute('SELECT revoked_at FROM provider_execution_grants WHERE id=?',(grant,)).fetchone()[0] is None
        assert not db.execute('SELECT 1 FROM workflow_step_gates').fetchone()


def test_cleanup_exact_scope_only(bundle):
    identity={'name':'cwb2-owned','digest':'c'*64,'spec':{'attempt_id':'child','generation':1}}
    labels={'io.cloudworkbench.owner':bundle['run_id'],'io.cloudworkbench.attempt':'child',
        'io.cloudworkbench.generation':'1','io.cloudworkbench.role':'routed-caller','io.cloudworkbench.caller-spec':'c'*64}
    obj={'Id':'d'*64,'Image':bundle['image'],'Name':'/cwb2-owned','Config':{'Labels':labels}}
    assert module.exact_owned(obj,bundle,identity)=='d'*64
    for key in labels:
        changed=copy.deepcopy(obj);changed['Config']['Labels'][key]='unrelated'
        with pytest.raises(RuntimeError):module.exact_owned(changed,bundle,identity)


def good_receipt(bundle):
    return {'proof_version':2,'scenario':bundle['scenario'],'passed':True,'run_id':bundle['run_id'],'image':bundle['image'],'source_hashes':bundle['hashes'],
        'harness_sha256':bundle['harness_sha256'],'host':'archived-worker.invalid','remaining':[],'cleanup_complete':True,
        'export_cleanup':{'collector_removed':True,'journal_present':True,'bytes_recoverable':True,'terminal_aborted':False},
        'worker_exit_code':0,'root_preprovisioned_fixture':True,'worker959_provisioning_qualified':False,
        'unknown_worker_effects':False,'real_provider_calls':False,'real_credentials_used':False,
        'services_before':{u:{'ActiveState':'active','MainPID':100+i} for i,u in enumerate(module.SERVICES)},
        'services_after':{u:{'ActiveState':'active','MainPID':100+i} for i,u in enumerate(module.SERVICES)},
        'worker':{'provider_runtime':'simulated_in_memory','provider_physical_cleanup_qualified':False,
            'composition':{'repeat_equal':True,'used_saved_observation':True,'replay_no_create_start':True,
                'secret_replay_refused':True,'secret_replay_state_unchanged':True,'service_closed':True,
                'root_occupancy_retained':True,'account_reservation_retained':True,'verification_pass':False,
                'candidate_verified':False,'scheduler_seat_released':False,'artifact_count':2,
                'artifact_ids':['1'*64,'observation-'+'2'*64],'artifact_hashes':{'1'*64:'3'*64,'observation-'+'2'*64:'4'*64},
                'artifact_provenance':['controller_captured_candidate','worker_reported'],
                'candidate_sha256':'3'*64,'observation_sha256':'4'*64,'candidate_file_sha256':module.sha(b'SYNTHETIC_ROUTED_CALLER_FILE\n'),
                'input_revision_sha256':'5'*64,'export_receipt_sha256':'6'*64,'cleanup_sha256':'7'*64,
                'export_create_calls':1,'export_start_calls':1,'provider_objects_remaining':0,'provider_material_cleaned':2,
                'counts':module.expected_counts()},
            'child_id':'child','binding':{'attempt_id':'child','generation':1,'profile_digest':module.PROFILE_DIGEST},
            'caller_spec_digest':'d'*64,'launch_binding_digest':'e'*64,
            'result':{'attempt_id':'child','generation':1,'runtime_id':'f'*64,'binding_digest':'e'*64,'execution_status':'execution_observed','grant_fence_confirmed':True,'cleanup_error':None,
            'caller_cleanup':{'runtime_id':'f'*64,'attempt_id':'child','generation':1,'spec_digest':'d'*64,
                'caller_stopped':True,'caller_removed':True,'authority':'controller_observed_caller_only','provider_cleanup_qualified':False},'verification_pass':False,'provider_cleanup_qualified':False,
            'scheduler_seat_released':False,'observation':{'status':'completed','provenance':'worker_reported','verification_pass':False,'outer_cleanup_required':True}},
            'durable_after_remove':{name:{'sha256':'c'*64,'bytes':10,'uid':1000,'matches_collected':True} for name in ('result.json','events.jsonl')},
            'controller_snapshot_after_remove':{'attempt_id':'child','generation':1,'runtime_id':'f'*64,
                'binding_digest':'e'*64,'caller_spec_digest':'d'*64,'manifest_sha256':'a'*64,
                'result_sha256':'c'*64,'events_sha256':'c'*64,'owner_uid':0,'directory_mode':'0o700',
                'reload_equal':True,'same_controller_authority':True,'verification_pass':False},
            'launch_state':'fenced','worker_thread_stopped':True,'tool_event_seen':True,'final_event_seen':True,
            'checks':{k:True for k in ('durable_binding_before_start','grant_fenced_before_stop','occupancy_retained','no_gate','source_preserved','child_not_cancelled','snapshot_policy_before_stop')},
            'requests':[{'nonce':'a'*64,'tool_result_seen':False},{'nonce':'b'*64,'tool_result_seen':True}]}}


def test_complete_receipt_requires_driver_and_retained_occupancy(bundle):
    assert module.validate_receipt(good_receipt(bundle),bundle)


@pytest.mark.parametrize('case',['gate','occupancy','cancelled','fence','verified','provider','released','thread','unknown','pid','requests'])
def test_partial_evidence_never_passes(bundle,case):
    value=good_receipt(bundle)
    if case in ('gate','occupancy','cancelled'):
        value['worker']['checks'][{'gate':'no_gate','occupancy':'occupancy_retained','cancelled':'child_not_cancelled'}[case]]=False
    elif case=='fence':value['worker']['result']['grant_fence_confirmed']=False
    elif case in ('verified','provider','released'):
        value['worker']['result'][{'verified':'verification_pass','provider':'provider_cleanup_qualified','released':'scheduler_seat_released'}[case]]=True
    elif case=='thread':value['worker']['worker_thread_stopped']=False
    elif case=='unknown':value['unknown_worker_effects']=True
    elif case=='pid':value['services_after'][module.SERVICES[0]]['MainPID']+=1
    else:value['worker']['requests'].pop()
    with pytest.raises(ValueError):module.validate_receipt(value,bundle)


def test_no_mode_refuses_without_execution():
    value=subprocess.run([sys.executable,str(PATH)],capture_output=True,text=True,timeout=5)
    assert value.returncode != 0 and 'Archived operation is disabled' in value.stderr


def test_durable_file_proof_matches_collected_hashes_and_rejects_change(tmp_path):
    import os
    from types import SimpleNamespace
    root=tmp_path/'.cwb-observations';root.mkdir(mode=0o700)
    evidence=[]
    for name in ('result.json','events.jsonl'):
        path=root/name;path.write_bytes(b'fixture data\n');st=path.stat()
        evidence.append(SimpleNamespace(size=st.st_size,inode=st.st_ino,sha256=module.sha(path.read_bytes())))
    observed=SimpleNamespace(result_evidence=evidence[0],events_evidence=evidence[1])
    proof=module.durable_observation_proof(tmp_path,observed,expected_uid=os.getuid())
    assert set(proof)=={'result.json','events.jsonl'} and all(p['matches_collected'] for p in proof.values())
    (root/'events.jsonl').write_bytes(b'changed data\n')
    with pytest.raises(RuntimeError,match='changed'):
        module.durable_observation_proof(tmp_path,observed,expected_uid=os.getuid())


def test_missing_durable_after_remove_proof_refused(bundle):
    value=good_receipt(bundle);value['worker'].pop('durable_after_remove')
    with pytest.raises(ValueError,match='durable_observations'):
        module.validate_receipt(value,bundle)


@pytest.mark.parametrize('case',['exit','root','worker','attempt','generation','runtime','profile','binding','spec','cleanup','provenance','verification','outer'])
def test_final_receipt_exact_identity_and_observation_boundary(bundle,case):
    value=good_receipt(bundle);worker=value['worker'];result=worker['result']
    if case=='exit':value['worker_exit_code']=1
    elif case=='root':value['root_preprovisioned_fixture']=False
    elif case=='worker':value['worker959_provisioning_qualified']=True
    elif case=='attempt':result['attempt_id']='another'
    elif case=='generation':result['generation']=True
    elif case=='runtime':result['runtime_id']='short'
    elif case=='profile':worker['binding']['profile_digest']='0'*64
    elif case=='binding':worker['launch_binding_digest']='0'*64
    elif case=='spec':worker['caller_spec_digest']='0'*64
    elif case=='cleanup':result['caller_cleanup']['generation']=2
    elif case=='provenance':result['observation']['provenance']='trusted'
    elif case=='verification':result['observation']['verification_pass']=True
    elif case=='outer':result['observation']['outer_cleanup_required']=False
    with pytest.raises(ValueError):module.validate_receipt(value,bundle)


@pytest.mark.parametrize('field,value',[('owner_uid',959),('reload_equal',False),
    ('same_controller_authority',False),('verification_pass',True),('runtime_id','0'*64),
    ('result_sha256','0'*64),('manifest_sha256','bad'),('generation',True),('directory_mode','0o755')])
def test_controller_snapshot_proof_requires_exact_private_recovery(bundle,field,value):
    receipt=good_receipt(bundle)
    receipt['worker']['controller_snapshot_after_remove'][field]=value
    with pytest.raises(ValueError,match='controller_snapshot'):
        module.validate_receipt(receipt,bundle)


def test_bundle_freezes_complete_result_path_and_explicit_caller_payloads(bundle):
    assert set(module.CALLER_SOURCE)<=set(bundle['sources'])
    assert {'routed_results.py','routed_candidate.py','routed_publication.py','routed_cleanup.py',
        'routed_export.py','routed_export_protocol.py','worker_service.py','supervised_executor.py'}<=set(bundle['sources'])


def test_old_proof_cannot_pass_new_receipt_validator(bundle):
    value=good_receipt(bundle);value.pop('proof_version')
    with pytest.raises(ValueError):module.validate_receipt(value,bundle)
    value=good_receipt(bundle);value['worker'].pop('provider_runtime')
    with pytest.raises(ValueError,match='simulation'):module.validate_receipt(value,bundle)


@pytest.mark.parametrize('field,value',[('repeat_equal',False),('used_saved_observation',False),
    ('export_create_calls',2),('secret_replay_refused',False),('artifact_count',1),
    ('candidate_verified',True),('scheduler_seat_released',True),('provider_objects_remaining',1),
    ('candidate_file_sha256','0'*64),('root_occupancy_retained',False),('counts',{})])
def test_result_composition_claims_are_strict(bundle,field,value):
    receipt=good_receipt(bundle);receipt['worker']['composition'][field]=value
    with pytest.raises(ValueError):module.validate_receipt(receipt,bundle)


def secret_receipt(bundle):
    bundle=copy.deepcopy(bundle);bundle['scenario']='secret-refusal'
    value=good_receipt(bundle);value['export_cleanup']={'collector_removed':True,'journal_present':False,'outcome':'not_attempted'}
    worker=value['worker'];worker.pop('composition')
    worker['result']['execution_status']='observation_persistence_failed';worker['result']['observation']=None
    worker['secret_refusal']={'snapshot_absent':True,'service_closed':True,'supervisors_stopped':True,
        'artifacts':0,'export_rpcs':0,'provider_objects_remaining':0,'provider_material_cleaned':2,
        'counts':module.expected_counts()}
    return bundle,value


def test_distinct_secret_refusal_receipt_accepts_no_snapshot_or_publication(bundle):
    changed,value=secret_receipt(bundle)
    assert module.validate_receipt(value,changed)
    with pytest.raises(ValueError):module.validate_receipt(value,bundle)


@pytest.mark.parametrize('field,value',[('snapshot_absent',False),('artifacts',1),('export_rpcs',1),
    ('provider_objects_remaining',1),('supervisors_stopped',False),('artifacts',False)])
def test_secret_refusal_requires_cleanup_and_absence(bundle,field,value):
    changed,receipt=secret_receipt(bundle);receipt['worker']['secret_refusal'][field]=value
    with pytest.raises(ValueError):module.validate_receipt(receipt,changed)


def test_real_supervised_budgeted_fixture_two_turns_and_cleanup(tmp_path):
    import threading
    from cloudworkbench.inference_relay import DispatchContext,_json
    scheduler,child,prepared,profile,grant=module.setup_stage(tmp_path.resolve())
    support=module.support_module({'support':(PATH.parent/module.SUPPORT).read_text()})
    wrapped,provider=module.supervised_fixture(scheduler,child,prepared,profile,grant,support)
    tools=json.loads((PATH.parents[2]/'tests/fixtures/hermes/base-tool-schemas.json').read_text())
    payload={'model':profile.model,'reasoning':{'effort':profile.effort},'store':False,'stream':True,
        'instructions':'Synthetic read only.','tools':[{'type':'function',**t['function']} for t in tools if t['function']['name']=='read_file'],
        'input':[{'role':'user','content':'Read the fixture.'}]}
    responses=[]
    for turn in range(2):
        context=DispatchContext(wrapped.executor.binding,str(turn+1)*64,module.sha(_json(payload)))
        result=wrapped(context,payload,threading.Event())
        assert result.response.status==200,result.response.body
        assert result.outer_cleanup_confirmed and wrapped.last_receipt.stopped
        assert not provider.objects
        responses.append(result)
        if turn==0:
            events=[json.loads(line[6:]) for line in result.response.body.splitlines() if line.startswith(b'data: ')]
            payload['input']+=events[-1]['response']['output']+[{'type':'function_call_output',
                'call_id':'call_routed_fixture','output':support.MARKER}]
    assert [v['tool_result_seen'] for v in provider.requests]==[False,True]
    assert len(provider.material_cleaned)==2 and wrapped.quiesce().supervisors_stopped
    assert module.composition_state(scheduler,child['id'])['counts']==module.expected_counts()


def test_export_cleanup_uses_exact_private_journal_before_caller_inventory(tmp_path,bundle,monkeypatch):
    from dataclasses import asdict
    from cloudworkbench.routed_runtime import CallerSpec
    from cloudworkbench import routed_export
    folder=tmp_path.resolve();owner=bundle['run_id']
    spec=CallerSpec('child','session',1,bundle['image'],owner,*[str(folder/n) for n in ('source','scratch','task','socket')],True,(),(1.4,3008,352))
    identity={'spec':asdict(spec),'digest':spec.digest,'name':spec.name}
    assert module.reconcile_fixture_export(folder,bundle,identity)=={'journal_present':False,'collector_removed':True,'outcome':'not_attempted'}
    journal=folder/'caller-journal';journal.mkdir(mode=0o700);child=journal/'child.1';child.mkdir(mode=0o700)
    (child/'workspace-export.json').write_text('{}');calls=[]
    def reconcile(runtime,actual,*,authorize):
        assert actual==spec and runtime.root==journal
        assert all(authorize(action) for action in ('reconcile','cleanup_inspect','stop','remove','publish'))
        assert not authorize('create') and not authorize('start')
        calls.append(actual)
        return {'collector_removed':True,'outcome':'completed','bytes_recoverable':True,'terminal_aborted':False}
    monkeypatch.setattr(routed_export,'reconcile_workspace_export',reconcile)
    value=module.reconcile_fixture_export(folder,bundle,identity)
    assert value['collector_removed'] and value['journal_present'] and calls==[spec]
    changed=copy.deepcopy(identity);changed['spec']['workspace']='/unrelated/workspace'
    with pytest.raises(RuntimeError,match='binding'):module.reconcile_fixture_export(folder,bundle,changed)
    assert calls==[spec]


@pytest.mark.parametrize('key,value',[('collector_removed',False),('journal_present',False),('bytes_recoverable',False),('terminal_aborted',True)])
def test_complete_receipt_requires_collector_reconciliation(bundle,key,value):
    receipt=good_receipt(bundle);receipt['export_cleanup'][key]=value
    with pytest.raises(ValueError):module.validate_receipt(receipt,bundle)


def test_worker_diagnostic_has_only_fixed_phase_type_and_source_literal(bundle):
    from cloudworkbench.routed_results import ResultPhaseError
    known=module.worker_failure(ResultPhaseError('result_export_selection_changed'),'results',bundle)
    assert known=={'phase':'results','type':'ResultPhaseError','code':'result_export_selection_changed'}
    assert module.checked_worker_failure(known,bundle)==known
    secret='synthetic-sensitive-content-not-to-log'
    redacted=module.worker_failure(ValueError(secret),'results',bundle)
    assert redacted['code']=='unclassified_qualification_failure' and secret not in json.dumps(redacted)
    with pytest.raises(ValueError):module.checked_worker_failure({**known,'code':secret},bundle)


@pytest.fixture
def actual_composition():
    fixture=json.loads((PATH.parents[2]/'tests/fixtures/routed-results-composition.json').read_text())
    assert fixture['source_receipt_passed'] is False
    return fixture['composition']


def test_artifact_validator_accepts_actual_linux_observation_prefix(actual_composition):
    assert module.validate_result_artifacts(actual_composition)
    assert sum(key.startswith('observation-') for key in actual_composition['artifact_ids'])==1


@pytest.mark.parametrize('case',['two_candidates','two_observations','wrong_prefix','duplicate','swapped_hashes','wrong_candidate_hash','wrong_observation_hash','extra_hash'])
def test_actual_linux_artifact_mapping_cannot_be_confused(actual_composition,case):
    value=copy.deepcopy(actual_composition)
    candidate=next(v for v in value['artifact_ids'] if not v.startswith('observation-'))
    observation=next(v for v in value['artifact_ids'] if v.startswith('observation-'))
    if case in ('two_candidates','two_observations','wrong_prefix'):
        old=observation if case!='two_observations' else candidate
        new=observation.removeprefix('observation-') if case=='two_candidates' else 'observation-'+candidate if case=='two_observations' else 'result-'+observation.removeprefix('observation-')
        value['artifact_ids'][value['artifact_ids'].index(old)]=new
        value['artifact_hashes'][new]=value['artifact_hashes'].pop(old)
    elif case=='duplicate':value['artifact_ids']=[candidate,candidate]
    elif case=='swapped_hashes':value['artifact_hashes'][candidate],value['artifact_hashes'][observation]=value['artifact_hashes'][observation],value['artifact_hashes'][candidate]
    elif case=='wrong_candidate_hash':value['artifact_hashes'][candidate]='0'*64
    elif case=='wrong_observation_hash':value['artifact_hashes'][observation]='0'*64
    else:value['artifact_hashes']['3'*64]='4'*64
    with pytest.raises(ValueError,match='result_artifacts_unproven'):module.validate_result_artifacts(value)


@pytest.mark.parametrize('method',module.CONTROL_METHODS)
def test_control_diagnostic_rethrows_exact_error_once(tmp_path,method):
    from types import SimpleNamespace
    from cloudworkbench.store import StoreError
    calls=[];error=StoreError(503,'Child control database unavailable')
    def fail(*args,**kwargs):
        calls.append((args,kwargs));raise error
    scheduler=SimpleNamespace(**{name:fail for name in module.CONTROL_METHODS})
    state=module.instrument_scheduler_control(scheduler,tmp_path)
    with pytest.raises(StoreError) as caught:getattr(scheduler,method)('child',expected_generation=1)
    assert caught.value is error and calls==[(('child',),{'expected_generation':1})]
    assert state=={'failures':[{'method':method,'status':503,'code':'database_unavailable'}],'total':1,'truncated':False}
    assert module.checked_control_diagnostics(json.loads((tmp_path/'scheduler-control-diagnostics.json').read_text()))==state


def test_control_diagnostic_is_bounded_and_redacts_unknown_detail(tmp_path):
    from types import SimpleNamespace
    from cloudworkbench.store import StoreError
    secret='fixture-secret-must-not-appear';error=StoreError(999,secret)
    def fail():raise error
    scheduler=SimpleNamespace(**{name:fail for name in module.CONTROL_METHODS})
    state=module.instrument_scheduler_control(scheduler,tmp_path)
    for _ in range(12):
        with pytest.raises(StoreError) as caught:scheduler.check_child_start_authority()
        assert caught.value is error
    raw=(tmp_path/'scheduler-control-diagnostics.json').read_text()
    assert secret not in raw and len(raw)<8192
    assert module.checked_control_diagnostics(json.loads(raw))==state
    assert state['total']==12 and state['truncated'] and len(state['failures'])==8
    assert all(item['status']==0 and item['code']=='unclassified_store_error' for item in state['failures'])


def test_control_diagnostic_preserves_success_nonstore_error_and_write_failure(tmp_path):
    from types import SimpleNamespace
    from cloudworkbench.store import StoreError
    sentinel=object();calls=[]
    def succeed(*args,**kwargs):calls.append((args,kwargs));return sentinel
    scheduler=SimpleNamespace(**{name:succeed for name in module.CONTROL_METHODS})
    state=module.instrument_scheduler_control(scheduler,tmp_path)
    assert scheduler.begin_child_start('child',expected_generation=1) is sentinel
    assert calls==[(('child',),{'expected_generation':1})] and state['total']==0
    assert not (tmp_path/'scheduler-control-diagnostics.json').exists()
    for error in (ValueError('not store'),StoreError(503,'Child control database deadline')):
        def fail():raise error
        scheduler=SimpleNamespace(**{name:fail for name in module.CONTROL_METHODS})
        module.instrument_scheduler_control(scheduler,tmp_path/'absent')
        with pytest.raises(type(error)) as caught:scheduler.fence_child_execution()
        assert caught.value is error


@pytest.mark.parametrize('field,value',[('method','malicious-value'),('code','secret'),('status',True)])
def test_control_diagnostic_reader_refuses_untrusted_values(field,value):
    entry={'method':'fence_child_execution','status':503,'code':'database_unavailable'}
    entry[field]=value
    with pytest.raises(ValueError):module.checked_control_diagnostics({'failures':[entry],'total':1,'truncated':False})
