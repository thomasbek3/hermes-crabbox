"""Real controller composition with synthetic Docker/provider boundaries only."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

from cloudworkbench import routed_stage_runner as runner
from cloudworkbench.routed_driver import StageDriveError
from tests import test_six_stage_composition as six


class ComposedFixtureExit(Exception):
    pass


def execute(flow, index, path, monkeypatch, *, reject=False, before=None, publication_fault=False):
    """Reuse preparation fixtures, replace their old pipeline with the new runner."""
    owned={}; captured={}; original_provider=six.provider_fixture
    real_publish=runner.publish_child_results
    @contextmanager
    def provider(*args, **kwargs):
        with original_provider(*args, **kwargs) as value:
            owned.update(value)
            yield value
    def publish(*args, **kwargs):
        if publication_fault:
            raise OSError('injected publication failure')
        spec=args[3]
        captured['exporter']=six.export_boundary(monkeypatch,path,spec.workspace,
            six.sha(('export:'+spec.attempt_id).encode()))
        return real_publish(*args, **kwargs)
    def composed(scheduler,runtime,prepared,spec,*,execution_profile,**_):
        role=six.EXPECTED[index][1]
        for name in ('published','candidate-revisions','decision'):
            (path/name).mkdir(mode=0o700)
        args=dict(execution_profile=execution_profile,services=(owned['service'],),
            input_revision=flow.revision,publication_root=path/'published',
            revision_root=path/'candidate-revisions',decision_root=path/'decision',
            selected_paths=('code.py',),forbidden_values=())
        if role=='acceptance_verification':
            material=path/'verification-material';material.mkdir(mode=0o700)
            verifier,calls=six.protected_runtime(monkeypatch,runtime,path)
            args.update(verifier=verifier,verification_material_root=material,
                allowed_environment_versions=('v1',),script_sources={'answer':flow.protected})
            captured['verifier_calls']=calls
        if before:before(scheduler,runtime,prepared,spec,args,owned)
        try:
            result=runner.run_prepared_stage(scheduler,runtime,prepared,spec,**args)
        except BaseException:
            captured['on_exception']=owned['service'].receipt
            raise
        captured.update(result=result,runtime=runtime,spec=spec,prepared=prepared,args=args)
        # Finish here: the old fixture must never execute its duplicate pipeline.
        raise ComposedFixtureExit
    with monkeypatch.context() as patch:
        patch.setattr(six,'provider_fixture',provider)
        patch.setattr(six,'drive_prepared_child',composed)
        patch.setattr(runner,'publish_child_results',publish)
        with pytest.raises(ComposedFixtureExit):
            six.execute_stage(flow,index,path,patch,reject=reject)
    result=captured['result'];old=flow.revision
    flow.revision=result.carried_revision
    flow.stages.append({'decision':result.decision})
    assert result.decision.input_revision_sha256==old.sha256
    assert result.decision.carried_revision_sha256==flow.revision.sha256
    assert not captured['exporter'].objects and not owned['provider'].objects
    assert owned['service'].receipt.resources_closed
    assert sum(call[0]=='create' for call in captured['exporter'].calls)==1
    assert sum(name=='create' for name,_ in owned['provider'].events)==1
    assert sum(name=='cleanup' for name,_ in owned['provider'].events)==1
    return captured


def test_actual_six_stage_composition_runs_once_with_exact_carried_revisions(tmp_path,monkeypatch):
    from cloudworkbench import scheduler
    # This integration test checks six-stage state/revision composition. Allow
    # shared CI filesystem latency; dedicated control-transaction tests retain
    # the production 50 ms deadline and exercise timeout/rollback behavior.
    monkeypatch.setattr(scheduler, '_CHILD_CONTROL_SECONDS', 5.0)
    flow=six.flow.__wrapped__(tmp_path);original=flow.revision
    for index in range(6):
        stage=tmp_path/f'stage-{index}';stage.mkdir(mode=0o700)
        previous=flow.revision
        def reject_invalid_configuration(scheduler,runtime,prepared,spec,args,owned):
            for change in ({'services':()}, {'services':(object(),)},
                {'input_revision':replace(args['input_revision'],sha256='0'*64)},
                {'selected_paths':None}):
                with pytest.raises(ValueError):
                    runner.run_prepared_stage(scheduler,runtime,prepared,spec,**{**args,**change})
                assert not runtime._folder(spec).exists()
                assert owned['service'].receipt.resources_closed is False
            if index==5:
                for change in ({'verifier':None},{'script_sources':{}},{'allowed_environment_versions':()},
                    {'verification_material_root':None},{'required_tool_gid':1001},{'required_tool_gid':True}):
                    with pytest.raises(ValueError):
                        runner.run_prepared_stage(scheduler,runtime,prepared,spec,**{**args,**change})
                    assert not runtime._folder(spec).exists()
        value=execute(flow,index,stage,monkeypatch,before=reject_invalid_configuration)
        done=value['result'];assert done.decision.gate=='pass'
        assert done.decision.outcome==('verified' if index==5 else 'unverified')
        assert (done.carried_revision!=previous)==(index==3)
        assert done.release_receipt['decision_sha256']==done.decision.sha256
        detached=done.release_receipt;detached.clear();assert done.release_receipt
        with pytest.raises(FrozenInstanceError):done.carried_revision=original
        with flow.store._connect() as db:
            assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] is None
            assert db.execute('SELECT COUNT(*) FROM workflow_step_gates').fetchone()[0]==index+1
            assert db.execute('SELECT state FROM workflow_roots').fetchone()[0]=='held'
        if index==5:
            assert sum(kind=='run' for kind,_ in value['verifier_calls'])==1
        # This entry point is intentionally not a historical replay API.
        with pytest.raises((runner.StageRunError,StageDriveError)):
            runner.run_prepared_stage(flow.s,value['runtime'],value['prepared'],value['spec'],**value['args'])
    assert (flow.revision.path/'files/code.py').read_bytes()==b'answer = 43\n'
    assert (original.path/'files/code.py').read_bytes()==b'answer = 42\n'
    assert flow.s.leases.current('account')['state']=='held'


def test_review_rejection_is_finalized_and_released_without_next_stage(tmp_path,monkeypatch):
    flow=six.flow.__wrapped__(tmp_path)
    for index in range(2):
        stage=tmp_path/f'stage-{index}';stage.mkdir(mode=0o700)
        value=execute(flow,index,stage,monkeypatch,reject=index==1)
    result=value['result']
    assert result.decision.gate=='reject' and result.decision.outcome=='needs_review'
    assert result.carried_revision==flow.initial
    with flow.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM workflow_requests').fetchone()[0]==2


def test_publication_failure_keeps_handles_and_never_retries_or_releases(tmp_path,monkeypatch):
    flow=six.flow.__wrapped__(tmp_path);stage=tmp_path/'stage';stage.mkdir(mode=0o700)
    observed={}
    real_drive=runner.drive_prepared_child
    def drive(*args,**kwargs):
        observed['drives']=observed.get('drives',0)+1
        result=real_drive(*args,**kwargs);observed['result']=result;return result
    def fail(*args,**kwargs):
        observed['service']=kwargs['services'][0]
        assert not observed['service'].receipt.resources_closed
        raise OSError('injected publication failure')
    # execute installs its own transport publisher; fail at the real publisher boundary.
    with monkeypatch.context() as patch:
        patch.setattr(runner,'drive_prepared_child',drive)
        patch.setattr(runner,'publish_child_results',fail)
        with pytest.raises(OSError,match='injected publication failure'):
            execute(flow,0,stage,patch)
    assert observed['drives']==1 and observed['result'].caller_cleanup['caller_removed']
    with flow.store._connect() as db:
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] is not None
        assert db.execute('SELECT COUNT(*) FROM workflow_step_gates').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM events WHERE type=?',('workflow.stage_child_released',)).fetchone()[0]==0


@contextmanager
def synthetic_stage_execution(flow, child, input_revision, assignment, path, monkeypatch, *, reject=False, evidence=None):
    """Prepare an already-claimed child; admission belongs exclusively to caller.

    Retain this context through stage completion. Docker/provider lifecycles are
    synthetic; the exporter program and protected check execute locally.
    """
    from cloudworkbench.routed_root_worker import StageExecution
    evidence={} if evidence is None else evidence
    role=assignment['role'];coding=role in runner.CODING_ROLES
    profile_spec=assignment['profile']
    profile=(six.PinnedCLI(Path('/opt/claude'),'a'*64,'2.1.274',profile_spec['model'],profile_spec['effort'])
        if profile_spec['provider']=='claude-code' else six.NativeProfile(
            profile_spec['provider'],profile_spec['model'],profile_spec['effort']))
    path.mkdir(mode=0o700,parents=False,exist_ok=False)
    with monkeypatch.context() as patch:
        runtime_value=six.runtime_setup.__wrapped__(path,patch)
        runtime,_,docker=runtime_value;original_run=runtime.runtime._run
        caller_id=six.sha(('caller:'+child['id']).encode())
        def synthetic_docker(args,**kwargs):
            result=original_run(args,**kwargs)
            if args[0]=='create':
                obj=docker.objects.pop(result);obj['Id']=caller_id;docker.objects[caller_id]=obj;return caller_id
            if args[0]=='exec' and args[-1]=='hermes' and coding:
                file=path/'materialized'/'source'/'code.py';file.write_bytes(b'answer = 43\n')
            return result
        patch.setattr(runtime.runtime,'_run',synthetic_docker)
        def prepare(value):
            s,_,c,r,p,d=value
            return six.prepare_child_stage(s,c['id'],expected_generation=c['generation'],revision=r,destination=d,
                execution_profile=profile,trusted_pstack_root=p,qualify=lambda *_:True)
        patch.setattr(six.driver_fixture,'prepare',prepare)
        driven=six.driver_fixture.driven.__wrapped__((flow.s,flow.owner,child,input_revision,
            flow.pstack,path/'materialized'),runtime_value,patch)
        prepared,spec=driven[3],driven[5]
        contract=six.stage_contract(role,input_revision.sha256,assignment['context_refs'])
        driven[-1].files['events']=json.dumps(six.event(summary=json.dumps(six.answer_for(contract,reject=reject)))).encode()+b'\n'
        class CollectorDocker(six.Docker):
            def __call__(self,args,**kwargs):
                if args[:2]==['start','--attach']:
                    self.calls.append(args)
                    self.objects[args[-1]]['State'].update(Status='exited',Running=False,ExitCode=0)
                    return six.subprocess.run([six.sys.executable,'-I','-S','-c',
                        six.collector_program(('code.py',),workspace=spec.workspace)],
                        capture_output=True,check=True,timeout=10).stdout
                value=super().__call__(args,**kwargs)
                if args[0]=='create':
                    obj=self.objects.pop(value);obj['Id']=six.sha(('export:'+child['id']).encode())
                    self.objects[obj['Id']]=obj;return obj['Id']
                return value
        collector=CollectorDocker()
        def bounded(_runtime,args,**kwargs):
            value=collector(args,**kwargs);data=value if type(value) is bytes else value.encode()
            if kwargs.get('stdout_fd') is not None:
                six.os.write(kwargs['stdout_fd'],data);return b''
            return data
        patch.setattr(six.routed_export,'_bounded_run',bounded)
        for name in ('published','candidate-revisions','decision'):(path/name).mkdir(mode=0o700)
        options={'publication_root':path/'published','revision_root':path/'candidate-revisions',
            'decision_root':path/'decision','selected_paths':('code.py',)}
        if role=='acceptance_verification':
            material=path/'verification-material';material.mkdir(mode=0o700)
            verifier,calls=six.protected_runtime(patch,runtime,path)
            options.update(verifier=verifier,verification_material_root=material,
                allowed_environment_versions=('v1',),script_sources={'answer':flow.protected})
            evidence['verifier_calls']=calls
        with six.provider_fixture(flow,driven,profile,path) as provider:
            evidence.update(caller=docker,collector=collector,provider=provider,child=child)
            yield StageExecution(runtime,prepared,spec,profile,(provider['service'],),options)


def test_reusable_factory_consumes_claimed_child_without_new_admission(tmp_path,monkeypatch):
    flow=six.flow.__wrapped__(tmp_path)
    chosen=six.stage_inputs(flow.s,flow.scope,'plan')
    pending=flow.broker.prepare(flow.token,native_session_id='native',native_call_id='plan',role='planning',
        task='Plan the requested correction.',context_refs=())
    flow.s.admit_request(flow.broker,flow.scope,pending['id'],plan=flow.frozen['role_plans']['planning'],
        step_id='plan',input_revision_sha256=chosen['input_revision_sha256'])
    child=flow.s.claim_child(flow.root['attempt_id'])
    assignment=flow.s.child_assignment(child['id'],expected_generation=1);evidence={}
    with synthetic_stage_execution(flow,child,flow.revision,assignment,tmp_path/'stage',monkeypatch,evidence=evidence) as execution:
        result=runner.run_prepared_stage(flow.s,execution.runtime,execution.prepared,execution.spec,
            execution_profile=execution.execution_profile,services=execution.services,
            input_revision=flow.revision,forbidden_values=(),**execution.options)
        assert result.decision.gate=='pass' and result.carried_revision==flow.revision
        assert not evidence['caller'].objects and not evidence['collector'].objects
        with flow.store._connect() as db:assert db.execute('SELECT COUNT(*) FROM workflow_requests').fetchone()[0]==1


@pytest.mark.parametrize('outcome',['rejected','needs_review'])
def test_protected_nonpass_returns_exact_decision_without_promotion(tmp_path,monkeypatch,outcome):
    register=six.EnvironmentRegistry.register
    def environment(registry,manifest):
        manifest=json.loads(json.dumps(manifest))
        if outcome=='rejected':
            script=tmp_path/'protected-check.py';script.write_bytes(b'raise SystemExit(1)\n')
            manifest['checks'][0]['script_sha256']=six.sha(script.read_bytes())
        else:manifest['checks']=[]
        return register(registry,manifest)
    with monkeypatch.context() as patch:
        patch.setattr(six.EnvironmentRegistry,'register',environment)
        flow=six.flow.__wrapped__(tmp_path)
    for index in range(6):
        stage=tmp_path/f'stage-{index}';stage.mkdir(mode=0o700)
        previous=flow.revision
        value=execute(flow,index,stage,monkeypatch)
    result=value['result']
    assert result.decision.outcome==outcome
    assert result.decision.gate==('reject' if outcome=='rejected' else None)
    assert result.carried_revision==previous
    assert sum(kind=='run' for kind,_ in value['verifier_calls'])==(1 if outcome=='rejected' else 0)
    with flow.store._connect() as db:
        assert db.execute('SELECT state FROM workflow_roots').fetchone()[0]=='held'
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM workflow_step_gates').fetchone()[0]==(6 if outcome=='rejected' else 5)
