"""Full catalog workflow with real controllers and explicitly synthetic runtime boundaries."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from cloudworkbench.environments import EnvironmentRegistry
from cloudworkbench.inference_budget import RootScope, AttemptScope
from cloudworkbench.inference_relay import AttemptBinding, WorkerDispatcher, WorkerSocketServer
from cloudworkbench.inference_transport import PinnedCLI
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.provider_dispatch import ProviderDispatch
from cloudworkbench.provider_executor import ProviderExecutor
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.pstack_routing import BackendProfile, RoleRouter, requested_policy
from cloudworkbench.role_broker import RoleBroker, ParentScope
from cloudworkbench.routed_driver import drive_prepared_child
from cloudworkbench.routed_progression import register_root_input, stage_inputs
from cloudworkbench.routed_results import publish_child_results
from cloudworkbench.routed_stage import prepare_child_stage
from cloudworkbench.routed_stage_decision import complete_stage, release_stage_child, load_stage_release
from cloudworkbench.routed_verification import prepare_qualified_verification, run_verification
from cloudworkbench.routed_verifier_runtime import VerifierRuntime, CheckResult
from cloudworkbench.routed_decision import complete_task_verification, release_decided_child
from cloudworkbench.routed_export_protocol import collector_program
from cloudworkbench import routed_export
from cloudworkbench.scheduler import RoleScheduler, _child_control_tx
from cloudworkbench.stage_contracts import stage_contract
from cloudworkbench.store import Store, StoreError
from cloudworkbench.supervised_executor import SupervisedProviderExecutor
from cloudworkbench.worker_service import WorkerService
from cloudworkbench.workflow_instructions import _reference
from cloudworkbench.workflow_revisions import RevisionBinding, capture_revision, verify_revision
from cloudworkbench.workflow_routing import EXPECTED_IDENTITIES, WORKFLOWS, select_workflow
from cloudworkbench.workflow_submission import enqueue_qualified_workflow
from tests.test_environments import passed
from tests.test_provider_dispatch import FakeRuntime
from tests.test_routed_collection import event
from tests.test_routed_runtime import setup as runtime_setup, Docker, IMAGE
from tests import test_routed_driver as driver_fixture
from tests.test_routed_cleanup import request as synthetic_provider_request

EXPECTED = [
    ('plan','planning','claude-fable-5-1','max'),
    ('challenge_plan','plan_review','gpt-6-astra','high'),
    ('finalize_plan','plan_revision','claude-fable-5-1','max'),
    ('implement','feature','grok-4.6','xhigh'),
    ('review_code','code_review','gpt-6-astra','high'),
    ('verify','acceptance_verification','gpt-5.6-sol','max'),
]


def sha(raw): return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def flow(tmp_path):
    store=Store(tmp_path/'state.db')
    owner=store.add_client('six-stage-synthetic','x'*40,['submit','observe','cancel'],['demo'])
    store.migrate_scheduler()
    leases=ProviderLeases(store,cleanup_verifier=lambda _:None,inspector_id='synthetic-inspector',clock=lambda:1000)
    leases.register_account('account',legacy_agent='hermes',persistent_owner_id='owner')
    scheduler=RoleScheduler(store,leases,clock=lambda:1000)
    providers={'anthropic':'claude-code','openai':'openai-codex','xai':'xai-oauth'}
    profiles={name:BackendProfile(name,providers[family],model,
        'chat_completions' if family=='anthropic' else 'codex_responses',effort,family,'a'*64,(effort,))
        for name,(family,model,effort) in EXPECTED_IDENTITIES.items()}
    pstack=tmp_path/'pstack'
    for step in WORKFLOWS['feature'].steps:
        path=pstack/_reference(step.role);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('Synthetic pinned instructions for '+step.role+'.')
    protected=tmp_path.resolve()/'protected-check.py'
    protected.write_bytes(b'from pathlib import Path\nassert Path("code.py").read_text() == "answer = 43\\n"\nprint("criterion passed")\n')
    protected.chmod(0o600)
    registry=EnvironmentRegistry(tmp_path/'environments.db')
    manifest={'project_id':'demo','version':'v1','architecture':'amd64',
        'base_image_digest':IMAGE,'image_digest':IMAGE,'cli_versions':{'hermes':'0.21.3'},
        'readiness_probes':[{'id':'python','argv':['python3','--version']}],
        'checks':[{'id':'answer','description':'code.py sets answer to 43.',
            'argv':['/usr/bin/python3','/run/task/check.py'],'script_id':'answer',
            'script_name':'check.py','script_sha256':sha(protected.read_bytes())}]}
    registry.register(manifest);registry.qualify('demo','v1',passed)
    request={'project_id':'demo','agent':'hermes','goal':'Change answer in code.py from42 to43.',
        'environment_version':'v1','acceptance':[{'id':'answer','description':'code.py sets answer to 43.','mandatory':True}]}
    root=enqueue_qualified_workflow(scheduler,owner,request,'full-feature',registry=registry,
        allowed_versions=('v1',),script_sources={'answer':protected},forbidden_values=(),
        selection=select_workflow(request['goal'],allowed_workflows=['feature'],explicit_workflow='feature'),
        allowed_workflows=['feature'],router=RoleRouter(requested_policy(),profiles,ready=lambda _:True),
        parent=profiles['fable-max'],accounts={'account':'owner'},trusted_pstack_root=pstack)
    scheduler.admit_root(root['attempt_id'],accounts={'account':'owner'})
    scheduler.transition(root['attempt_id'],'running',expected_generation=1)
    source=tmp_path/'initial';source.mkdir();(source/'code.py').write_text('answer = 42\n')
    revisions=tmp_path/'revisions';revisions.mkdir()
    binding=RevisionBinding(owner['id'],'demo',root['session_id'],root['turn_id'],root['attempt_id'],1,root['attempt_id'],1)
    revision=capture_revision(store,binding,source,revisions,selected_paths=('code.py',),controller_attests_quiesced=True)
    scope=ParentScope(owner['id'],'demo',root['attempt_id'],root['attempt_id'],1,'native')
    register_root_input(scheduler,scope,revision)
    broker=RoleBroker(store.path,authorize_parent=scheduler.authorize_parent,clock=lambda:1000)
    _,token=broker.issue(scope,roles=tuple(row[1] for row in EXPECTED),expires_at=2000)
    with store._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots').fetchone()[0])
    assert [(x['id'],x['role'],x['profile']['model'],x['profile']['effort']) for x in frozen['provenance']['workflow']['steps']]==EXPECTED
    return SimpleNamespace(store=store,s=scheduler,owner=owner,root=root,scope=scope,broker=broker,token=token,
        frozen=frozen,pstack=pstack,protected=protected,revision=revision,initial=revision,stages=[])


@contextmanager
def provider_fixture(flow,driven,profile,path):
    scheduler=flow.s;child=driven[2];prepared=driven[3]
    reservation=scheduler.leases.current('account')['reservation']
    grant=scheduler.leases.issue_grant(reservation,attempt_id=child['id'],generation=1)
    provider=FakeRuntime();provider.after_request_cleanup=lambda spec:None
    scheduler.leases.verifier=provider.cleanup
    root=flow.root
    budget=AttemptScope(RootScope(flow.owner['id'],'demo',root['session_id'],root['turn_id'],root['attempt_id'],1),child['id'],1)
    dispatch=ProviderDispatch(scheduler.leases,provider,budget=scheduler.budget,budget_scope=budget)
    binding=AttemptBinding(child['id'],1,prepared.profile_digest)
    executor=ProviderExecutor(dispatch,reservation=reservation,grant_id=grant,binding=binding,
        profile=profile,remaining_seconds=lambda:90)
    wrapped=SupervisedProviderExecutor(executor)
    worker=WorkerDispatcher(journal_path=path/'worker.db',binding=binding,
        capability_sha256=sha(b'a'*64),authorize=wrapped.authorize,execute_request=wrapped)
    with tempfile.TemporaryDirectory(prefix='six-stage-',dir='/tmp') as socket_home:
        service=WorkerService(WorkerSocketServer(Path(socket_home).resolve()/'w',worker));service.start()
        value={'scheduler':scheduler,'provider':provider,'dispatch':dispatch,'binding':binding,
            'reservation':reservation,'grant':grant,'service':service,'wrapped':wrapped}
        try:
            synthetic_provider_request(value)
            yield value
        finally:
            receipt=service.close(timeout_seconds=2)
            assert receipt.thread_stopped and receipt.resources_closed


def answer_for(contract,*,reject=False):
    answer={'schema_version':1,'contract_sha256':contract.digest,'role':contract.role,
        'input_revision_sha256':contract.input_revision_sha256,
        'context_refs':contract.to_dict()['context_refs'],
        'summary':'Synthetic '+contract.role+' report for exact assigned work.','evidence':[]}
    if contract.role in ('plan_review','code_review'):
        answer.update(recommendation='revise' if reject else 'approve',findings=[])
    else:
        answer['status']='complete'
        if contract.role in ('planning','plan_revision'):
            answer['plan']=[{'step':1,'action':'Set answer to43 in code.py.','validation':'Check exact code.py contents.'}]
    return answer


def export_boundary(monkeypatch,path,workspace,identity):
    stream=subprocess.run([sys.executable,'-I','-S','-c',collector_program(('code.py',),workspace=workspace)],
        capture_output=True,check=True,timeout=10).stdout
    class ExportDocker(Docker):
        def __call__(self,args,**kwargs):
            if args[:2]==['start','--attach']:
                self.calls.append(args);self.objects[args[-1]]['State'].update(Status='exited',Running=False,ExitCode=0)
                return stream
            result=super().__call__(args,**kwargs)
            if args[0]=='create':
                obj=self.objects.pop(result);obj['Id']=identity;self.objects[identity]=obj;return identity
            return result
    docker=ExportDocker()
    def bounded(runtime,args,**kwargs):
        result=docker(args,**kwargs);data=result if type(result) is bytes else result.encode()
        if kwargs.get('stdout_fd') is not None:os.write(kwargs['stdout_fd'],data);return b''
        return data
    monkeypatch.setattr(routed_export,'_bounded_run',bounded)
    return docker


def protected_runtime(monkeypatch,runtime,path):
    verifier=VerifierRuntime(runtime.runtime,journal_root=path/'verifier-journal')
    saved={};calls=[]
    def run(spec,*,authorize,forbidden_values=()):
        authorize('prepare');authorize('start');calls.append(('run',spec.digest))
        assert spec.digest not in saved
        check=Path(spec.task_path)/'check.py'
        result=subprocess.run([sys.executable,'-I','-S',str(check)],cwd=spec.candidate_path,
            capture_output=True,timeout=10)
        logs=result.stdout+result.stderr
        saved[spec.digest]=CheckResult(spec.digest,sha(('verifier:'+spec.digest).encode()),result.returncode,
            False,False,True,sha(logs),logs)
        authorize('publish');return saved[spec.digest]
    def load(spec,*,authorize,forbidden_values=()):
        authorize('load');calls.append(('load',spec.digest));return saved[spec.digest]
    def reconcile(spec,*,authorize,forbidden_values=()):
        authorize('reconcile');calls.append(('reconcile',spec.digest))
        return {'spec_sha256':spec.digest,'runtime_id':sha(('verifier:'+spec.digest).encode()),
            'cleanup_confirmed':True,'result_available':spec.digest in saved}
    monkeypatch.setattr(verifier,'run',run);monkeypatch.setattr(verifier,'load',load);monkeypatch.setattr(verifier,'reconcile',reconcile)
    return verifier,calls


def execute_stage(flow,index,path,monkeypatch,*,reject=False):
    step_id,role,model,effort=EXPECTED[index]
    coding=role in {'feature','bug_fix','refactoring','perf_issue','hillclimb'}
    input_bytes=(flow.revision.path/'files/code.py').read_bytes()
    chosen=stage_inputs(flow.s,flow.scope,step_id)
    assert chosen['input_revision_sha256']==flow.revision.sha256
    assert chosen['context_refs']==[{'artifact_id':v['decision'].artifact_id,'sha256':v['decision'].sha256} for v in flow.stages]
    pending=flow.broker.prepare(flow.token,native_session_id='native',native_call_id=step_id,role=role,
        task='Perform '+role+' on code.py.',context_refs=tuple(chosen['context_refs']))
    with pytest.raises(StoreError,match='differs from finalized output'):
        flow.s.admit_request(flow.broker,flow.scope,pending['id'],plan=flow.frozen['role_plans'][role],
            step_id=step_id,input_revision_sha256='f'*64)
    flow.s.admit_request(flow.broker,flow.scope,pending['id'],plan=flow.frozen['role_plans'][role],
        step_id=step_id,input_revision_sha256=flow.revision.sha256)
    child=flow.s.claim_child(flow.root['attempt_id']);assert child
    profile_spec=flow.frozen['role_plans'][role][0]['profile']
    profile=(PinnedCLI(Path('/opt/claude'),'a'*64,'2.1.274',model,effort)
        if profile_spec['provider']=='claude-code' else NativeProfile(profile_spec['provider'],model,effort))
    stage=(flow.s,flow.owner,child,flow.revision,flow.pstack,path/'materialized')
    runtime_value=runtime_setup.__wrapped__(path,monkeypatch)
    runtime,_,docker=runtime_value
    original_run=runtime.runtime._run;caller_id=sha(('caller:'+child['id']).encode())
    def synthetic_docker(args,**kwargs):
        result=original_run(args,**kwargs)
        if args[0]=='create':
            obj=docker.objects.pop(result);obj['Id']=caller_id;docker.objects[caller_id]=obj;return caller_id
        if args[0]=='exec' and args[-1]=='hermes' and coding:
            file=path/'materialized'/'source'/'code.py'
            assert file.read_bytes()==input_bytes;file.write_bytes(b'answer = 43\n')
        return result
    monkeypatch.setattr(runtime.runtime,'_run',synthetic_docker)
    def prepare(value):
        s,_,c,r,p,d=value
        return prepare_child_stage(s,c['id'],expected_generation=1,revision=r,destination=d,
            execution_profile=profile,trusted_pstack_root=p,qualify=lambda *_:True)
    monkeypatch.setattr(driver_fixture,'prepare',prepare)
    driven=driver_fixture.driven.__wrapped__(stage,runtime_value,monkeypatch)
    prepared,spec=driven[3],driven[5]
    receipt=json.loads(prepared.launch.receipt_json)
    assert receipt['qualified_environment']['manifest_sha256']==flow.frozen['provenance']['environment']['manifest_sha256']
    assert prepared.materialization.readonly == (not coding)
    contract=stage_contract(role,flow.revision.sha256,chosen['context_refs'])
    driven[-1].files['events']=json.dumps(event(summary=json.dumps(answer_for(contract,reject=reject)))).encode()+b'\n'
    if index:
        assert 'Set answer to43 in code.py.' in prepared.launch.prompt
    with provider_fixture(flow,driven,profile,path) as provider:
        quiesced=drive_prepared_child(flow.s,runtime,prepared,spec,execution_profile=profile)
        assert quiesced.execution_status=='execution_observed',quiesced
        assert quiesced.caller_cleanup['caller_removed'] and not docker.objects
        assert not quiesced.verification_pass and not quiesced.scheduler_seat_released
        exporter=export_boundary(monkeypatch,path,spec.workspace,sha(('export:'+child['id']).encode()))
        published=path/'published';published.mkdir(mode=0o700)
        revisions=path/'candidate-revisions';revisions.mkdir(mode=0o700)
        results=publish_child_results(flow.s,runtime,prepared,spec,quiesced,execution_profile=profile,
            services=(provider['service'],),publication_root=published,revision_root=revisions,
            selected_paths=('code.py',),forbidden_values=())
        assert not exporter.objects and not provider['provider'].objects
        assert sum(kind=='create' for kind,_ in provider['provider'].events)==1
        assert sum(kind=='start' for kind,_ in provider['provider'].events)==1
        assert sum(kind=='cleanup' for kind,_ in provider['provider'].events)==1
        assert provider['service'].receipt.resources_closed
        assert (results.candidate.revision.path/'files/code.py').read_bytes()==(b'answer = 43\n' if coding else input_bytes)
        storage=path/'decision';storage.mkdir(mode=0o700)
        if role!='acceptance_verification':
            args=dict(input_revision=flow.revision,storage_root=storage,services=(provider['service'],),forbidden_values=())
            decision=complete_stage(flow.s,runtime,spec,results,**args)
            assert complete_stage(flow.s,runtime,spec,results,**args)==decision
            release_args=dict(input_revision=flow.revision,decision_sha256=decision.sha256,
                services=(provider['service'],),forbidden_values=())
            released=release_stage_child(flow.s,runtime,spec,results,**release_args)
            assert release_stage_child(flow.s,runtime,spec,results,**release_args)==released
            with _child_control_tx(flow.store,write=False) as db:assert load_stage_release(db,child['id'])==released
            assert decision.outcome==('needs_review' if reject else 'unverified')
        else:
            material=path/'verification-material';material.mkdir(mode=0o700)
            plan=prepare_qualified_verification(flow.s,runtime,spec,results,allowed_versions=('v1',),
                script_sources={'answer':flow.protected},forbidden_values=(),services=(provider['service'],),
                storage_root=storage,material_root=material)
            verifier,calls=protected_runtime(monkeypatch,runtime,path)
            verified=run_verification(flow.s,runtime,spec,results,plan,verifier,
                services=(provider['service'],),forbidden_values=())
            assert verified.outcome=='passed'
            args=dict(input_revision=flow.revision,services=(provider['service'],),forbidden_values=())
            decision=complete_task_verification(flow.s,runtime,spec,results,plan,verifier,**args)
            assert complete_task_verification(flow.s,runtime,spec,results,plan,verifier,**args)==decision
            released=release_decided_child(flow.s,runtime,spec,results,plan,verifier,
                decision_sha256=decision.sha256,**args)
            assert release_decided_child(flow.s,runtime,spec,results,plan,verifier,
                decision_sha256=decision.sha256,**args)==released
            assert sum(kind=='run' for kind,_ in calls)==1
            assert decision.outcome=='verified'
        assert decision.gate==('reject' if reject else 'pass')
        with flow.store._connect() as db:
            assert db.execute('SELECT child_attempt_id FROM workflow_roots WHERE root_id=?',(flow.root['attempt_id'],)).fetchone()[0] is None
            assert db.execute('SELECT COUNT(*) FROM workflow_step_gates WHERE root_id=?',(flow.root['attempt_id'],)).fetchone()[0]==index+1
        before=flow.revision
        if coding:flow.revision=results.candidate.revision
        assert decision.carried_revision_sha256==flow.revision.sha256
        if not coding:assert flow.revision==before
        verify_revision(flow.store,flow.revision)
        assert sum(args[0]=='create' for args in docker.calls)==1
        assert sum(args[0]=='create' for args in exporter.calls)==1
        record=dict(step=step_id,role=role,model=model,effort=effort,decision=decision,
            input_revision=before.sha256,carried_revision=flow.revision.sha256,
            context_refs=chosen['context_refs'],caller_id=caller_id,
            caller_creates=1,export_creates=1,synthetic_provider_requests=1,
            protected_local_script_runs=1 if role=='acceptance_verification' else 0,
            physical_Docker_qualified=False,provider_inference_qualified=False)
        flow.stages.append(record)
        return record


@pytest.mark.parametrize('reject_review',[False,True],ids=['full-six-stage-pass','review-rejection-blocks-verification'])
def test_full_qualified_feature_composition(flow,tmp_path,monkeypatch,reject_review,record_property):
    import socket
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('Network/provider connection forbidden in synthetic composition'))
    for index in range(6):
        path=tmp_path/f'stage-{index}';path.mkdir(mode=0o700)
        with monkeypatch.context() as boundary:
            execute_stage(flow,index,path,boundary,reject=reject_review and index==4)
        if reject_review and index==4:
            with pytest.raises(StoreError,match='output chain changed'):
                stage_inputs(flow.s,flow.scope,'verify')
            break
    assert len(flow.stages)==(5 if reject_review else 6)
    assert len({v['caller_id'] for v in flow.stages})==len(flow.stages)
    assert [(v['step'],v['role'],v['model'],v['effort']) for v in flow.stages]==EXPECTED[:len(flow.stages)]
    assert sum(v['input_revision']!=v['carried_revision'] for v in flow.stages)==1
    assert flow.stages[3]['input_revision']==flow.initial.sha256
    assert flow.stages[3]['carried_revision']==flow.stages[4]['input_revision']
    assert (flow.initial.path/'files/code.py').read_bytes()==b'answer = 42\n'
    assert (flow.revision.path/'files/code.py').read_bytes()==b'answer = 43\n'
    with flow.store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM workflow_steps').fetchone()[0]==6
        assert db.execute('SELECT COUNT(*) FROM workflow_requests').fetchone()[0]==len(flow.stages)
        assert not db.execute("SELECT 1 FROM attempts WHERE execution_kind='hermes_child' AND state!='completed'").fetchone()
        assert db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.stage_decision'").fetchone()[0]==5
        assert db.execute("SELECT COUNT(*) FROM events WHERE type='workflow.task_decision'").fetchone()[0]==(0 if reject_review else 1)
        assert db.execute('SELECT COUNT(*) FROM provider_request_leases').fetchone()[0]==len(flow.stages)
        assert not db.execute("SELECT 1 FROM provider_request_leases WHERE state!='released'").fetchone()
        assert db.execute('SELECT used FROM inference_budget_roots').fetchone()[0]==len(flow.stages)
        assert db.execute('SELECT COUNT(*) FROM inference_budget_requests').fetchone()[0]==len(flow.stages)
    assert flow.s.store.get_attempt(flow.root['attempt_id'])['state']=='running'
    assert flow.s.leases.current('account')['reservation'] is not None
    record_property('composition_receipt',json.dumps({
        'schema_version':1,'scenario':'review_rejected' if reject_review else 'full_six_stage_pass',
        'root_id':flow.root['attempt_id'],'session_id':flow.root['session_id'],
        'frozen_workflow_steps':6,'completed_steps':len(flow.stages),
        'initial_revision_sha256':flow.initial.sha256,'final_carried_revision_sha256':flow.revision.sha256,
        'qualified_environment_sha256':flow.frozen['provenance']['environment']['snapshot_sha256'],
        'root_state':'running','delivery_promoted':False,'provider_account_retained':True,
        'budget_requests_used':len(flow.stages),
        'steps':[{**item,'decision':asdict(item['decision'])} for item in flow.stages],
        'boundary':{'Docker':'synthetic','UID_permissions':'fixture metadata only',
            'provider':'synthetic dispatch and resource cleanup; no model inference',
            'worker_service':'actual local Unix service lifecycle',
            'collector':'actual local isolated Python subprocess',
            'protected_check':'actual local script with verifier runtime observations simulated',
            'registry_qualification':'synthetic trusted_builder receipt',
            'profiles':'exact policy binding only, not provider qualification'},
    },sort_keys=True))
