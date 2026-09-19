#!/usr/bin/env python3
"""Frozen, opt-in root-preprovisioned routed-driver fixture; no provider calls."""

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

import argparse
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
START_MODULES=('routed_results','worker_service','provider_executor','supervised_executor','provider_dispatch','adapters','routed_driver','routed_stage','routed_runtime','runtime','routed_caller','scheduler',
               'store','provider_leases','provider_docker','role_broker','workflow_submission',
               'workflow_revisions','native_responses','inference_relay','inference_service')
CALLER_SOURCE=('__init__.py','routed_caller.py','hermes_adapter.py','pstack_routing.py',
               'inference_relay.py','inference_service.py','adapters.py')
SUPPORT='qualify-routed-caller-linux.py'
PROFILE=('openai-codex','gpt-6-astra','high')
PROFILE_DIGEST='5b1ca2f887eacf4257899149de4cdaf3514d4d0938bc6ff292e2225d00003296'
SERVICES=('cloud-workbench-api.service','cloud-workbench-worker.service','cloudd.service')
REMOTE='operator@worker.example.invalid'


def sha(data):return hashlib.sha256(data).hexdigest()
def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()


def source_closure(root):
    pending=list(START_MODULES)+['__init__'];sources={}
    while pending:
        name=pending.pop()
        if name+'.py' in sources:continue
        if not re.fullmatch('[a-z_]+',name):raise ValueError('invalid_source_name')
        body=(root/'src/cloudworkbench'/ (name+'.py')).read_text();sources[name+'.py']=body
        for node in ast.walk(ast.parse(body)):
            if isinstance(node,ast.ImportFrom) and node.level==1:
                names=[node.module.split('.')[0]] if node.module else [a.name for a in node.names]
                pending.extend(names)
    return sources


def validate_bundle(bundle):
    if (type(bundle.get('version')) is not int or bundle.get('version')!=2
            or bundle.get('scenario') not in ('results','secret-refusal') or not re.fullmatch('driverqual-[0-9a-f]{16}',bundle.get('run_id',''))
            or not re.fullmatch('sha256:[0-9a-f]{64}',bundle.get('image',''))
            or bundle.get('profile')!=list(PROFILE) or set(bundle.get('sources',{}))!=set(bundle.get('hashes',{}))
            or not ({n+'.py' for n in START_MODULES}|set(CALLER_SOURCE))<=set(bundle['sources'])
            or any(not re.fullmatch('[a-z_]+\\.py',n) or sha(t.encode())!=bundle['hashes'][n] for n,t in bundle['sources'].items())
            or sha(bundle['support'].encode())!=bundle.get('support_sha256')
            or sha(bundle['harness'].encode())!=bundle.get('harness_sha256')):
        raise ValueError('frozen_bundle_mismatch')
    for source in bundle['sources'].values():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node,ast.ImportFrom) and node.level:
                if node.level!=1:raise ValueError('unsupported_source_import')
                names=[node.module.split('.')[0]] if node.module else [a.name for a in node.names]
                if any(name+'.py' not in bundle['sources'] for name in names):raise ValueError('incomplete_source_closure')


def prepare(output,image,*,scenario="results"):
    import uuid
    sources=source_closure(ROOT);support=(ROOT/'scripts/legacy'/SUPPORT).read_text();harness=Path(__file__).read_text()
    bundle={'version':2,'scenario':scenario,'run_id':'driverqual-'+uuid.uuid4().hex[:16],'image':image,'profile':list(PROFILE),
        'sources':sources,'hashes':{n:sha(t.encode()) for n,t in sources.items()},
        'support':support,'support_sha256':sha(support.encode()),'harness':harness,'harness_sha256':sha(harness.encode())}
    validate_bundle(bundle)
    with Path(output).open('x') as stream:json.dump(bundle,stream,indent=2);stream.write('\n')
    return bundle


def support_module(bundle):
    import types
    value=types.ModuleType('frozen_caller_fixture');exec(compile(bundle['support'],SUPPORT,'exec'),value.__dict__)
    return value


def setup_stage(folder, *, required_tool_gid=None):
    """Only fresh local fixture DB; no production admission or qualified model claim."""
    from cloudworkbench.store import Store
    from cloudworkbench.provider_leases import ProviderLeases
    from cloudworkbench.scheduler import RoleScheduler
    from cloudworkbench.pstack_routing import BackendProfile,RoleRouter,requested_policy
    from cloudworkbench.workflow_routing import EXPECTED_IDENTITIES,select_workflow
    from cloudworkbench.workflow_submission import enqueue_workflow
    from cloudworkbench.workflow_revisions import RevisionBinding,capture_revision
    from cloudworkbench.role_broker import RoleBroker,ParentScope
    from cloudworkbench.routed_stage import prepare_child_stage
    from cloudworkbench.native_responses import NativeProfile
    store=Store(folder/'controller.db');principal=store.add_client('synthetic driver','x'*40,['submit','observe','cancel'],['synthetic'])
    store.migrate_scheduler()
    leases=ProviderLeases(store,cleanup_verifier=lambda _:None,inspector_id='synthetic-no-provider')
    leases.register_account('synthetic-account',legacy_agent='hermes',persistent_owner_id='synthetic-owner')
    scheduler=RoleScheduler(store,leases)
    providers={'anthropic':'claude-code','openai':'openai-codex','xai':'xai-oauth'}
    profiles={name:BackendProfile(name,providers[family],model,'chat_completions' if family=='anthropic' else 'codex_responses',
        effort,family,'a'*64,(effort,)) for name,(family,model,effort) in EXPECTED_IDENTITIES.items()}
    router=RoleRouter(requested_policy(),profiles,ready=lambda _:True)
    pstack=folder/'synthetic-pstack';ref=pstack/'skills/interrogate/references/reviewer-prompt.md'
    ref.parent.mkdir(parents=True);ref.write_text('Synthetic fixture: read the assigned file, do not modify or delegate.')
    request={'project_id':'synthetic','agent':'hermes','goal':'Review existing code'}
    root=enqueue_workflow(scheduler,principal,request,'synthetic-root',
        selection=select_workflow(request['goal'],allowed_workflows=['code_review'],explicit_workflow='code_review'),
        allowed_workflows=['code_review'],router=router,parent=profiles['fable-max'],
        accounts={'synthetic-account':'synthetic-owner'},trusted_pstack_root=pstack)
    assert scheduler.admit_root(root['attempt_id'],accounts={'synthetic-account':'synthetic-owner'})
    scheduler.transition(root['attempt_id'],'running',expected_generation=1)
    work=folder/'revision-input';work.mkdir();(work/'fixture.txt').write_text('SYNTHETIC_ROUTED_CALLER_FILE\n')
    revisions=folder/'revisions';revisions.mkdir()
    binding=RevisionBinding(principal['id'],'synthetic',root['session_id'],root['turn_id'],root['attempt_id'],1,root['attempt_id'],1)
    revision=capture_revision(store,binding,work,revisions,selected_paths=('fixture.txt',),controller_attests_quiesced=True)
    broker=RoleBroker(store.path,authorize_parent=scheduler.authorize_parent)
    scope=ParentScope(principal['id'],'synthetic',root['attempt_id'],root['attempt_id'],1,'synthetic-native')
    import time
    _,token=broker.issue(scope,roles=('code_review',),expires_at=time.time()+300)
    pending=broker.prepare(token,native_session_id='synthetic-native',native_call_id='synthetic-call',role='code_review',
        task='Read /workspace/fixture.txt using read_file, then report SYNTHETIC_ROUTED_CALLER_OK.')
    with store._connect() as db:frozen=json.loads(db.execute('SELECT frozen FROM workflow_roots WHERE root_id=?',(root['attempt_id'],)).fetchone()[0])
    scheduler.admit_request(broker,scope,pending['id'],plan=frozen['role_plans']['code_review'],step_id='review_code',input_revision_sha256=revision.sha256)
    child=scheduler.claim_child(root['attempt_id']);assert child
    stages=folder/'stages';stages.mkdir()
    if required_tool_gid is not None:os.chown(stages,0,required_tool_gid);stages.chmod(0o2750)
    profile=NativeProfile(*PROFILE)
    if profile.digest!=PROFILE_DIGEST:raise RuntimeError('fixture_profile_changed')
    prepared=prepare_child_stage(scheduler,child['id'],expected_generation=1,revision=revision,destination=stages/'child',
        execution_profile=profile,trusted_pstack_root=pstack,qualify=lambda *_:True,max_turns=3,
        run_budget_seconds=90,required_tool_gid=required_tool_gid)
    grant=leases.issue_grant(leases.current('synthetic-account')['reservation'],attempt_id=child['id'],generation=1)
    return scheduler,child,prepared,profile,grant


class SimulatedProviderRuntime:
    """Synthetic provider effects only; never a physical-provider cleanup proof."""
    inspector_id='simulated-result-composition'

    def __init__(self,support,profile):
        self.support,self.profile=support,profile
        self.objects={};self.responses={};self.requests=[];self.material_cleaned=set()

    def create(self,spec,payload,*,deadline):
        from cloudworkbench.provider_dispatch import Resources,ObservedResource
        from cloudworkbench.native_responses import validate_request
        value=json.loads(payload);validate_request(self.profile,value)
        seen=any(item.get('type')=='function_call_output' and item.get('call_id')=='call_routed_fixture'
            and self.support.MARKER in str(item.get('output','')) for item in value.get('input',[]) if isinstance(item,dict))
        ordinal=len(self.requests)+1
        if ordinal>2 or seen!=(ordinal==2):raise RuntimeError('synthetic_sequence_invalid')
        self.requests.append({'ordinal':ordinal,'nonce':spec.request_nonce,'payload_sha256':sha(payload),
            'tool_result_seen':seen,'request_id':spec.lease.request_id})
        events=[json.loads(line[6:]) for line in self.support.synthetic_sse(PROFILE,ordinal).splitlines() if line.startswith(b'data: ')]
        response=events[-1]['response']
        self.responses[spec.lease.request_id]=json.dumps({'version':1,'status':'ok','error':None,
            'result':{'profile_digest':self.profile.digest,'response':response,'transport_stopped':True},
            'container_cleanup_required':True,'credential_reuse_authorized':False}).encode()
        resources=Resources(*(sha((spec.launch_nonce+role).encode()) for role in ('p','g','i','e')))
        for component,identity,name in (('provider',resources.provider_id,spec.provider_name),('gateway',resources.gateway_id,spec.gateway_name)):
            self.objects[identity]=ObservedResource(identity,name,spec.labels(component),'created')
        for role,identity,name in (('internal',resources.internal_network_id,spec.internal_network_name),('external',resources.external_network_id,spec.external_network_name)):
            self.objects[identity]=ObservedResource(identity,name,spec.network_labels(role),'created')
        return resources

    def inspect(self,spec,resources,*,deadline):
        return tuple(self.objects[key] for key in (resources.provider_id,resources.gateway_id,resources.internal_network_id,resources.external_network_id))

    def start(self,spec,resources,*,deadline):
        from dataclasses import replace
        for key in (resources.provider_id,resources.gateway_id):self.objects[key]=replace(self.objects[key],state='running')

    def collect(self,spec,resources,*,limit,deadline):
        value=self.responses[spec.lease.request_id]
        if len(value)>limit:raise RuntimeError('synthetic_response_bound')
        return value

    def resolve(self,spec,row,*,deadline):return None

    def cleanup(self,target):
        import time
        from cloudworkbench.provider_leases import CleanupReceipt
        for items in target.dispatches:
            row=dict(items)
            for key in ('provider_id','gateway_id','internal_network_id','external_network_id'):
                identity=row[key]
                if identity is not None:
                    obj=self.objects.get(identity)
                    if obj is not None:
                        if obj.labels['io.cloudworkbench.provider-launch']!=row['launch_nonce']:raise RuntimeError('synthetic_cleanup_binding')
                        del self.objects[identity]
        return CleanupReceipt(target,self.inspector_id,'terminated',time.time(),sha(('simulated:'+target.cleanup_id).encode()))

    def after_request_cleanup(self,spec):self.material_cleaned.add(spec.lease.request_id)


def supervised_fixture(scheduler,child,prepared,profile,grant,support):
    from cloudworkbench.inference_budget import RootScope,AttemptScope
    from cloudworkbench.inference_relay import AttemptBinding
    from cloudworkbench.provider_dispatch import ProviderDispatch
    from cloudworkbench.provider_executor import ProviderExecutor
    from cloudworkbench.supervised_executor import SupervisedProviderExecutor
    value=prepared.materialization.consumer
    scope=AttemptScope(RootScope(value.owner_id,value.project_id,value.session_id,value.turn_id,
        value.root_attempt_id,value.root_generation),child['id'],1)
    provider=SimulatedProviderRuntime(support,profile)
    scheduler.leases.verifier=provider.cleanup;scheduler.leases.inspector_id=provider.inspector_id
    dispatch=ProviderDispatch(scheduler.leases,provider,budget=scheduler.budget,budget_scope=scope)
    reservation=scheduler.leases.current('synthetic-account')['reservation']
    executor=ProviderExecutor(dispatch,reservation=reservation,grant_id=grant,
        binding=AttemptBinding(child['id'],1,profile.digest),profile=profile,remaining_seconds=lambda:600)
    return SupervisedProviderExecutor(executor),provider


def services():
    import subprocess
    values={}
    for unit in SERVICES:
        p=subprocess.run(['systemctl','show',unit,'--property=Id,ActiveState,MainPID'],capture_output=True,text=True,timeout=5,check=True)
        fields=dict(line.split('=',1) for line in p.stdout.splitlines())
        if fields.get('Id')!=unit or fields.get('ActiveState')!='active' or not fields.get('MainPID','').isdigit() or int(fields['MainPID'])<=0:
            raise RuntimeError('service_identity_unproven')
        values[unit]={'ActiveState':'active','MainPID':int(fields['MainPID'])}
    return values


def docker_command(folder,role='root'):
    import time
    from cloudworkbench.provider_docker import BoundedDocker
    cli=BoundedDocker('/usr/bin/docker',folder/(role+'-docker-cli'))
    def command(args,timeout=15):return cli(args,deadline=time.monotonic()+min(timeout,30),limit=2*1024**2).decode().strip()
    return command


PHASES=frozenset({'setup','material','service','driver','readback','results','shutdown','complete'})
ERROR_TYPES=frozenset({'StageDriveError','ResultPhaseError','ChildCleanupError','ObservationStoreError',
    'PublicationError','WorkspaceExportError','StoreError','RevisionError','RuntimeError','ValueError',
    'AssertionError','KeyError','TypeError','OSError','PermissionError','FileNotFoundError','JSONDecodeError',
    'LeaseError','DispatchError','Exception'})


def diagnostic_codes(bundle):
    codes={'unclassified_qualification_failure'}
    for body in (bundle['harness'],*bundle['sources'].values()):
        for node in ast.walk(ast.parse(body)):
            if isinstance(node,ast.Raise) and isinstance(node.exc,ast.Call):
                for arg in node.exc.args:
                    if isinstance(arg,ast.Constant) and type(arg.value) is str and re.fullmatch('[a-zA-Z0-9 _;.-]{1,128}',arg.value):codes.add(arg.value)
    return codes


def worker_failure(exc,phase,bundle):
    codes=diagnostic_codes(bundle);code=getattr(exc,'code',None);message=str(exc)
    return {'phase':phase if phase in PHASES else 'setup',
        'type':type(exc).__name__ if type(exc).__name__ in ERROR_TYPES else 'Exception',
        'code':code if type(code) is str and code in codes else message if message in codes else 'unclassified_qualification_failure'}


def checked_worker_failure(value,bundle):
    if (type(value) is not dict or set(value)!={'phase','type','code'}
            or value['phase'] not in PHASES or value['type'] not in ERROR_TYPES or value['code'] not in diagnostic_codes(bundle)):
        raise ValueError('worker_diagnostic_invalid')
    return value


CONTROL_METHODS=('read_child_launch','bind_child_caller','begin_child_start',
    'check_child_start_authority','confirm_child_started','fence_child_execution')
CONTROL_ERRORS={
    'Child control database deadline':'database_deadline',
    'Child control database unavailable':'database_unavailable',
    'Child control commit acknowledgement deadline':'commit_ack_deadline',
    'Child control commit outcome uncertain':'commit_uncertain',
    'Child launch authority unavailable':'authority_unavailable',
    'Child launch budget authority unavailable':'budget_authority_unavailable',
    'Child teardown ownership differs':'teardown_owner_mismatch',
    'Child teardown account owner differs':'teardown_account_mismatch',
    'Child teardown generation differs':'teardown_generation_mismatch',
    'Child caller fence differs':'caller_fence_mismatch',
    'Child caller execution fenced':'caller_fenced',
    'Child caller assignment changed':'assignment_changed',
}


def checked_control_diagnostics(value):
    if (type(value) is not dict or set(value)!={'failures','total','truncated'}
            or type(value['total']) is not int or value['total']<0
            or type(value['failures']) is not list or len(value['failures'])!=min(value['total'],8)
            or type(value['truncated']) is not bool or value['truncated']!=(value['total']>8)):
        raise ValueError('control_diagnostic_invalid')
    for item in value['failures']:
        if (type(item) is not dict or set(item)!={'method','status','code'}
                or item['method'] not in CONTROL_METHODS
                or type(item['status']) is not int or item['status'] not in (0,400,401,403,404,409,422,429,500,503)
                or item['code'] not in (*CONTROL_ERRORS.values(),'unclassified_store_error')):
            raise ValueError('control_diagnostic_invalid')
    return value


def instrument_scheduler_control(scheduler,folder):
    """Capture failure categories only; never retry, replace, or suppress authority errors."""
    from cloudworkbench.store import StoreError
    from functools import wraps
    state={'failures':[],'total':0,'truncated':False}
    def decorate(method,original):
        @wraps(original)
        def observed(*args,**kwargs):
            try:return original(*args,**kwargs)
            except StoreError as exc:
                state['total']+=1;state['truncated']=state['total']>8
                if len(state['failures'])<8:
                    status=exc.status_code
                    state['failures'].append({'method':method,
                        'status':status if type(status) is int and status in (400,401,403,404,409,422,429,500,503) else 0,
                        'code':CONTROL_ERRORS.get(exc.detail,'unclassified_store_error') if type(exc.detail) is str else 'unclassified_store_error'})
                try:(folder/'scheduler-control-diagnostics.json').write_text(json.dumps(state))
                except OSError:pass
                raise
        return observed
    for method in CONTROL_METHODS:
        setattr(scheduler,method,decorate(method,getattr(scheduler,method)))
    return state


def worker_phase(folder,phase):
    if phase not in PHASES:raise ValueError('invalid_phase')
    (folder/'worker-phase.json').write_text(json.dumps({'phase':phase}))


def worker(folder,bundle):
    import threading,time,secrets
    from dataclasses import asdict,replace
    from cloudworkbench.routed_observation_store import load_observation,_location
    from cloudworkbench.runtime import Runtime
    from cloudworkbench.routed_runtime import RoutedRuntime
    from cloudworkbench.routed_driver import drive_prepared_child
    from cloudworkbench.inference_relay import AttemptBinding,WorkerDispatcher,WorkerSocketServer,DispatchResult
    from cloudworkbench.inference_service import ServiceResponse
    from cloudworkbench.worker_service import WorkerService
    from cloudworkbench import routed_export
    support=support_module(bundle);command=docker_command(folder,'worker')
    worker_phase(folder,'setup')
    scheduler,child,prepared,profile,grant=setup_stage(folder,required_tool_gid=1000)
    instrument_scheduler_control(scheduler,folder)
    binding=AttemptBinding(child['id'],1,profile.digest)
    worker_phase(folder,'material')
    task=folder/'task';task.mkdir(mode=0o751);os.chown(task,0,1000)
    package=task/'source/cloudworkbench';package.mkdir(parents=True)
    for directory in (package.parent,package):os.chown(directory,0,1000);directory.chmod(0o555)
    def write(path,body,gid=1000,mode=0o440):path.write_text(body);os.chown(path,0,gid);path.chmod(mode)
    for name in CALLER_SOURCE:write(package/name,bundle['sources'][name],mode=0o444)
    for name,body in [('prompt.txt',prepared.launch.prompt),('config.yaml',prepared.launch.config_json),('launch.json',json.dumps(asdict(prepared.launch)))]:write(task/name,body)
    http_cap=secrets.token_hex(32);worker_cap=secrets.token_hex(32);write(task/'http-capability',http_cap)
    worker_dir=folder/'socket';worker_dir.mkdir(mode=0o750);os.chown(worker_dir,0,1001)
    write(worker_dir/'client.json',json.dumps({'binding':asdict(binding),'worker_capability':worker_cap,'http_capability':http_cap,
        'request_path':prepared.launch.request_path,'port':9876}),gid=1001)
    wrapped,provider=supervised_fixture(scheduler,child,prepared,profile,grant,support)
    requests=provider.requests
    worker_phase(folder,'service')
    dispatcher=WorkerDispatcher(journal_path=folder/'worker.db',binding=binding,capability_sha256=sha(worker_cap.encode()),
        authorize=wrapped.authorize,execute_request=wrapped,max_requests=2)
    uds=WorkerSocketServer(worker_dir/'socket',dispatcher,socket_gid=1001,deadline_seconds=100)
    service=WorkerService(uds)
    runtime=Runtime({'root':folder,'image':bundle['image'],'owner':bundle['run_id'],'cpus':1.4,'memory_mib':3008,'pids':352,
        'approved_mount_roots':[folder],'approved_writable_mount_roots':[folder]})
    checks={'durable_binding_before_start':False,'grant_fenced_before_stop':False}
    def checked(args,**kwargs):
        if args[0]=='start':
            launch=scheduler.read_child_launch(child['id'],expected_generation=1)
            assert launch.state=='start_intent' and launch.runtime_id==args[1]
            assert scheduler.store.get_attempt(child['id'])['runtime_id']==args[1]
            checks['durable_binding_before_start']=True
        if args[0]=='stop':
            with scheduler.store._connect() as db:revoked=db.execute('SELECT revoked_at FROM provider_execution_grants WHERE id=?',(grant,)).fetchone()[0]
            assert revoked is not None and scheduler.read_child_launch(child['id'],expected_generation=1).state=='fenced'
            checks['grant_fenced_before_stop']=True
            snapshot=_location(routed.root,spec)
            assert (snapshot/'manifest.json').is_file()==(bundle['scenario']=='results')
            checks['snapshot_policy_before_stop']=True
        return command(args,**kwargs)
    runtime._run=checked;routed=RoutedRuntime(runtime,journal_root=folder/'caller-journal')
    export_calls=[];original_export_run=routed_export._bounded_run
    def observed_export(base,args,**kwargs):
        export_calls.append(args[0]);return original_export_run(base,args,**kwargs)
    routed_export._bounded_run=observed_export
    service.start()
    try:
        spec=routed.prepare_caller(child['id'],child['session_id'],generation=1,plan=prepared.launch,
            workspace=prepared.materialization.source,scratch=prepared.materialization.scratch,task_dir=task,worker_socket_dir=worker_dir)
        (folder/'caller-identity.json').write_text(json.dumps({'spec':asdict(spec),'digest':spec.digest,'name':spec.name}))
        worker_phase(folder,'driver')
        result=drive_prepared_child(scheduler,routed,prepared,spec,execution_profile=profile,timeout_seconds=110,relay_timeout_seconds=15,
            forbidden_values=(support.FINAL.encode(),) if bundle['scenario']=='secret-refusal' else ())
        worker_phase(folder,'readback')
        (folder/'driver-status.json').write_text(json.dumps({'execution_status':result.execution_status,
            'grant_fence_confirmed':result.grant_fence_confirmed,'cleanup_error':result.cleanup_error,
            'caller_removed':bool(result.caller_cleanup and result.caller_cleanup.get('caller_removed'))}))
        with scheduler.store._connect() as db:
            root=dict(db.execute('SELECT state,child_attempt_id,cancel_requested FROM workflow_roots WHERE root_id=?',(child['workflow_root_id'],)).fetchone())
            gates=db.execute('SELECT COUNT(*) FROM workflow_step_gates').fetchone()[0]
        checks.update(occupancy_retained=root=={'state':'held','child_attempt_id':child['id'],'cancel_requested':0},no_gate=gates==0,
            child_not_cancelled=scheduler.store.get_attempt(child['id'])['cancel_requested']==0,
            source_preserved=(prepared.materialization.source/'fixture.txt').read_text()==support.MARKER+'\n')
        if bundle['scenario']=='secret-refusal':
            body=secret_refusal_proof(scheduler,routed,prepared,spec,result,service,wrapped,provider,checks,export_calls)
        else:
            observation=result.observation
            if observation is None or not result.caller_cleanup or not result.caller_cleanup['caller_removed']:
                raise RuntimeError('driver_observation_unavailable')
            if command(['ps','-aq','--no-trunc','--filter','label=io.cloudworkbench.owner='+bundle['run_id']]):
                raise RuntimeError('caller_removal_unproven')
            durable=durable_observation_proof(prepared.materialization.scratch,observation)
            recovered=load_observation(scheduler,routed.root,spec,replace(result,observation=None))
            snapshot=_location(routed.root,spec)
            assert recovered==observation
            snapshot_proof={'attempt_id':spec.attempt_id,'generation':spec.generation,
                'runtime_id':result.runtime_id,'binding_digest':result.binding_digest,
                'caller_spec_digest':spec.digest,'manifest_sha256':sha((snapshot/'manifest.json').read_bytes()),
                'result_sha256':recovered.result_evidence.sha256,'events_sha256':recovered.events_evidence.sha256,
                'owner_uid':snapshot.stat().st_uid,'directory_mode':oct(snapshot.stat().st_mode&0o777),
                'reload_equal':True,'same_controller_authority':True,'verification_pass':False}
            composition=compose_results(scheduler,routed,prepared,spec,result,profile,service,provider,folder,export_calls,support)
            body={'composition':composition,'result':asdict(result),'checks':checks,'requests':requests,'root_id':child['workflow_root_id'],
                'child_id':child['id'],'binding':asdict(binding),'durable_after_remove':durable,
                'controller_snapshot_after_remove':snapshot_proof,
                'caller_spec_digest':spec.digest,'launch_binding_digest':scheduler.read_child_launch(child['id'],expected_generation=1).binding_digest,'launch_state':scheduler.read_child_launch(child['id'],expected_generation=1).state,
                'tool_event_seen':bool(observation and any(e.type=='tool.started' and e.payload.get('name')=='read_file' for e in observation.events)),
                'final_event_seen':bool(observation and any(e.type=='adapter.result' and e.payload.get('summary')==support.FINAL for e in observation.events))}

    except Exception as exc:
        phase=json.loads((folder/'worker-phase.json').read_text())['phase']
        (folder/'worker-failure.json').write_text(json.dumps(worker_failure(exc,phase,bundle)))
        raise
    finally:
        worker_phase(folder,'shutdown')
        try:
            service.close(timeout_seconds=5);service.closed_receipt()
            if not wrapped.quiesce().supervisors_stopped:raise RuntimeError('worker_supervisor_still_live')
        finally:routed_export._bounded_run=original_export_run
    body['worker_thread_stopped']=True
    body['provider_runtime']='simulated_in_memory'
    body['provider_physical_cleanup_qualified']=False
    (folder/'worker-result.json').write_text(json.dumps(body))
    worker_phase(folder,'complete')


def composition_state(scheduler,child_id):
    with scheduler.store._connect() as db:
        child=scheduler.store._attempt(db,child_id)
        root=dict(db.execute('SELECT state,child_attempt_id,cancel_requested FROM workflow_roots WHERE root_id=?',(child['workflow_root_id'],)).fetchone())
        artifacts=[dict(row) for row in db.execute('SELECT id,metadata FROM artifacts ORDER BY id')]
        counts={'request_leases':db.execute('SELECT count(*) FROM provider_request_leases').fetchone()[0],
            'released_requests':db.execute("SELECT count(*) FROM provider_request_leases WHERE state='released'").fetchone()[0],
            'dispatches':db.execute('SELECT count(*) FROM provider_dispatch').fetchone()[0],
            'budget_requests':db.execute('SELECT count(*) FROM inference_budget_requests').fetchone()[0],
            'root_used':db.execute('SELECT used FROM inference_budget_roots').fetchone()[0],
            'attempt_used':db.execute('SELECT used FROM inference_budget_attempts WHERE attempt_id=?',(child_id,)).fetchone()[0],
            'gates':db.execute('SELECT count(*) FROM workflow_step_gates').fetchone()[0]}
    return {'root':root,'artifacts':artifacts,'counts':counts}


def compose_results(scheduler,routed,prepared,spec,result,profile,service,provider,folder,export_calls,support):
    from dataclasses import replace
    from cloudworkbench.routed_results import publish_child_results
    from cloudworkbench.routed_observation_store import ObservationStoreError
    from cloudworkbench.routed_export_protocol import ExportLimits
    from cloudworkbench.workflow_revisions import verify_revision
    worker_phase(folder,'results')
    publication=folder/'published';publication.mkdir(mode=0o700)
    revisions=folder/'output-revisions';revisions.mkdir(mode=0o700)
    quiesced=replace(result,observation=None)
    reservation=scheduler.leases.current('synthetic-account')['reservation']
    before=composition_state(scheduler,spec.attempt_id)
    options={'execution_profile':profile,'services':(service,),'publication_root':publication,
        'revision_root':revisions,'selected_paths':('fixture.txt',),'forbidden_values':(),
        'export_limits':ExportLimits(max_bytes=4096,max_entries=10,max_depth=4,max_seconds=10)}
    first=publish_child_results(scheduler,routed,prepared,spec,quiesced,**options)
    cut=len(export_calls)
    second=publish_child_results(scheduler,routed,prepared,spec,quiesced,**options)
    assert first==second and first.candidate is not None
    assert 'create' not in export_calls[cut:] and 'start' not in export_calls[cut:]
    after=composition_state(scheduler,spec.attempt_id)
    secret_refused=False
    try:publish_child_results(scheduler,routed,prepared,spec,quiesced,**{**options,'forbidden_values':(support.FINAL.encode(),)})
    except ObservationStoreError as exc:
        if exc.code!='observation_secret_refused':raise
        secret_refused=True
    final=composition_state(scheduler,spec.attempt_id)
    assert secret_refused and final==after and after['root']==before['root']
    assert len(after['artifacts'])==2 and before['artifacts']==[] and after['counts']['gates']==0
    assert scheduler.leases.current('synthetic-account')['reservation']==reservation
    assert not provider.objects and len(provider.material_cleaned)==2 and service.closed_receipt().resources_closed
    assert export_calls.count('create')==1 and export_calls.count('start')==1
    candidate=first.candidate;verify_revision(scheduler.store,candidate.revision)
    output=(candidate.revision.path/'files/fixture.txt').read_bytes()
    assert output==(support.MARKER+'\n').encode()
    artifacts=[json.loads(row['metadata']) for row in after['artifacts']]
    for item in artifacts:
        raw=Path(item['storage_path']).read_bytes()
        assert len(raw)==item['bytes'] and sha(raw)==item['sha256'] and item['verification_pass'] is False
    return {'repeat_equal':first==second,'used_saved_observation':quiesced.observation is None,
        'artifact_count':len(artifacts),'artifact_ids':sorted(item['id'] for item in artifacts),
        'artifact_hashes':{item['id']:item['sha256'] for item in artifacts},
        'artifact_provenance':sorted(item['provenance'] for item in artifacts),
        'candidate_sha256':candidate.revision.sha256,'candidate_file_sha256':sha(output),
        'input_revision_sha256':candidate.input_revision_sha256,'export_receipt_sha256':candidate.export_receipt_sha256,
        'cleanup_sha256':first.cleanup.evidence_sha256,'observation_sha256':first.observation.sha256,
        'export_create_calls':export_calls.count('create'),'export_start_calls':export_calls.count('start'),
        'replay_no_create_start':True,'secret_replay_refused':secret_refused,'secret_replay_state_unchanged':final==after,
        'provider_objects_remaining':len(provider.objects),'provider_material_cleaned':len(provider.material_cleaned),
        'service_closed':service.closed_receipt().resources_closed,'root_occupancy_retained':after['root']==before['root'],
        'account_reservation_retained':True,'counts':after['counts'],
        'verification_pass':first.verification_pass,'candidate_verified':candidate.verification_pass,
        'scheduler_seat_released':first.scheduler_seat_released}


def secret_refusal_proof(scheduler,routed,prepared,spec,result,service,wrapped,provider,checks,export_calls):
    from dataclasses import asdict
    from cloudworkbench.routed_observation_store import _location
    service.close(timeout_seconds=5);closed=service.closed_receipt();quiescence=wrapped.quiesce()
    state=composition_state(scheduler,spec.attempt_id)
    absent=not _location(routed.root,spec).exists()
    assert result.execution_status=='observation_persistence_failed' and result.observation is None
    assert result.grant_fence_confirmed and result.cleanup_error is None and result.caller_cleanup['caller_removed']
    assert absent and state['artifacts']==[] and state['counts']['gates']==0 and not provider.objects
    assert not export_calls and len(provider.material_cleaned)==2 and quiescence.supervisors_stopped
    assert state['root']=={'state':'held','child_attempt_id':spec.attempt_id,'cancel_requested':0}
    return {'result':asdict(result),'child_id':spec.attempt_id,'root_id':prepared.materialization.consumer.root_attempt_id,
        'checks':checks,'requests':provider.requests,'caller_spec_digest':spec.digest,
        'launch_binding_digest':result.binding_digest,'launch_state':scheduler.read_child_launch(spec.attempt_id,expected_generation=1).state,
        'binding':asdict(wrapped.executor.binding),
        'secret_refusal':{'snapshot_absent':absent,'artifacts':0,'export_rpcs':0,'service_closed':closed.resources_closed,
            'supervisors_stopped':quiescence.supervisors_stopped,'provider_objects_remaining':0,
            'provider_material_cleaned':len(provider.material_cleaned),'counts':state['counts']}}


def durable_observation_proof(scratch, observation, *, expected_uid=1000):
    import stat
    root=scratch/'.cwb-observations'
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=expected_uid or stat.S_IMODE(info.st_mode)!=0o700:
        raise RuntimeError('durable_observation_directory_untrusted')
    proof={}
    for name,evidence in (('result.json',observation.result_evidence),('events.jsonl',observation.events_evidence)):
        fd=os.open(root/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(fd)
            cap=65536 if name=='result.json' else 16*1024**2
            if (not stat.S_ISREG(before.st_mode) or before.st_uid!=expected_uid or before.st_nlink!=1
                    or not 0<before.st_size<=cap):raise RuntimeError('durable_observation_file_untrusted')
            digest=hashlib.sha256();count=0
            while True:
                data=os.read(fd,min(65536,cap+1-count))
                if not data:break
                count+=len(data);digest.update(data)
                if count>cap:raise RuntimeError('durable_observation_limit')
            after=os.fstat(fd)
            if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
                    or count!=evidence.size or before.st_ino!=evidence.inode or digest.hexdigest()!=evidence.sha256):
                raise RuntimeError('durable_observation_changed')
            proof[name]={'sha256':digest.hexdigest(),'bytes':count,'uid':before.st_uid,'matches_collected':True}
        finally:os.close(fd)
    return proof


def exact_owned(obj,bundle,identity):
    labels=obj.get('Config',{}).get('Labels',{});spec=identity['spec']
    if (obj.get('Image')!=bundle['image'] or obj.get('Name')!='/'+identity['name']
            or labels.get('io.cloudworkbench.owner')!=bundle['run_id']
            or labels.get('io.cloudworkbench.attempt')!=spec['attempt_id']
            or labels.get('io.cloudworkbench.generation')!=str(spec['generation'])
            or labels.get('io.cloudworkbench.role')!='routed-caller'
            or labels.get('io.cloudworkbench.caller-spec')!=identity['digest']
            or not re.fullmatch('[0-9a-f]{64}',obj.get('Id',''))):raise RuntimeError('cleanup_identity_mismatch')
    return obj['Id']


def validate_receipt(receipt,bundle):
    try:return _validate_receipt(receipt,bundle)
    except (KeyError,TypeError,AttributeError,IndexError):raise ValueError('qualification_receipt_invalid') from None


def _validate_receipt(receipt,bundle):
    if (receipt.get('proof_version')!=2 or type(receipt.get('proof_version')) is not int or receipt.get('scenario')!=bundle['scenario']
            or receipt.get('passed') is not True or receipt.get('run_id')!=bundle['run_id'] or receipt.get('host')!='archived-worker.invalid'
            or receipt.get('image')!=bundle['image'] or receipt.get('source_hashes')!=bundle['hashes']
            or receipt.get('harness_sha256')!=bundle['harness_sha256'] or receipt.get('remaining')!=[]
            or receipt.get('cleanup_complete') is not True or receipt.get('export_cleanup',{}).get('collector_removed') is not True or receipt.get('unknown_worker_effects') is not False
            or receipt.get('worker_exit_code')!=0 or type(receipt.get('worker_exit_code')) is not int
            or receipt.get('root_preprovisioned_fixture') is not True or receipt.get('worker959_provisioning_qualified') is not False
            or receipt.get('real_provider_calls') is not False or receipt.get('real_credentials_used') is not False
            or receipt.get('services_before')!=receipt.get('services_after')):raise ValueError('qualification_incomplete')
    service=receipt.get('services_before',{})
    if set(service)!=set(SERVICES) or any(v.get('ActiveState')!='active' or type(v.get('MainPID')) is not int or v['MainPID']<=0 for v in service.values()):raise ValueError('services_unproven')
    worker=receipt.get('worker',{});result=worker.get('result',{})
    if worker.get('provider_runtime')!='simulated_in_memory' or worker.get('provider_physical_cleanup_qualified') is not False:
        raise ValueError('simulation_boundary_missing')
    binding=worker.get('binding',{});cleanup=result.get('caller_cleanup',{})
    if (not isinstance(worker.get('child_id'),str) or not worker['child_id']
            or result.get('attempt_id')!=worker['child_id'] or type(result.get('generation')) is not int or result['generation']!=1
            or binding!={'attempt_id':worker['child_id'],'generation':1,'profile_digest':PROFILE_DIGEST}
            or type(binding.get('generation')) is not int
            or not re.fullmatch('[0-9a-f]{64}',result.get('runtime_id',''))
            or not re.fullmatch('[0-9a-f]{64}',result.get('binding_digest',''))
            or result['binding_digest']!=worker.get('launch_binding_digest')
            or not re.fullmatch('[0-9a-f]{64}',worker.get('caller_spec_digest',''))
            or cleanup!={'runtime_id':result['runtime_id'],'attempt_id':worker['child_id'],'generation':1,
                'spec_digest':worker['caller_spec_digest'],'caller_stopped':True,'caller_removed':True,
                'authority':'controller_observed_caller_only','provider_cleanup_qualified':False}):
        raise ValueError('driver_identity_unproven')
    if bundle['scenario']=='secret-refusal':
        if receipt['export_cleanup'].get('journal_present') is not False:raise ValueError('secret_export_unexpected')
        validate_common_checks(worker)
        proof=worker.get('secret_refusal',{})
        if (result.get('execution_status')!='observation_persistence_failed' or result.get('observation') is not None
                or result.get('grant_fence_confirmed') is not True or result.get('cleanup_error') is not None
                or worker.get('worker_thread_stopped') is not True or worker.get('launch_state')!='fenced'
                or any(result.get(k) is not False for k in ('verification_pass','provider_cleanup_qualified','scheduler_seat_released'))
                or any(proof.get(k) is not True for k in ('snapshot_absent','service_closed','supervisors_stopped'))
                or any(type(proof.get(k)) is not int or proof[k]!=0 for k in ('artifacts','export_rpcs','provider_objects_remaining'))
                or type(proof.get('provider_material_cleaned')) is not int or proof['provider_material_cleaned']!=2 or not valid_counts(proof.get('counts'))):
            raise ValueError('secret_refusal_unproven')
        return True
    if (result.get('execution_status')!='execution_observed' or result.get('grant_fence_confirmed') is not True
            or result.get('cleanup_error') is not None or not result.get('caller_cleanup',{}).get('caller_removed')
            or any(result.get(k) is not False for k in ('verification_pass','provider_cleanup_qualified','scheduler_seat_released'))
            or result.get('observation',{}).get('status')!='completed'
            or result['observation'].get('provenance')!='worker_reported'
            or result['observation'].get('verification_pass') is not False
            or result['observation'].get('outer_cleanup_required') is not True or worker.get('launch_state')!='fenced'
            or worker.get('worker_thread_stopped') is not True or not worker.get('tool_event_seen') or not worker.get('final_event_seen')):
        raise ValueError('driver_observation_unproven')
    durable=worker.get('durable_after_remove',{})
    if set(durable)!={'result.json','events.jsonl'} or any(v.get('matches_collected') is not True or v.get('uid')!=1000
            or not re.fullmatch('[0-9a-f]{64}',v.get('sha256','')) or type(v.get('bytes')) is not int or v['bytes']<=0 for v in durable.values()):
        raise ValueError('durable_observations_unproven')
    snapshot=worker.get('controller_snapshot_after_remove',{})
    if (set(snapshot)!={'attempt_id','generation','runtime_id','binding_digest','caller_spec_digest',
            'manifest_sha256','result_sha256','events_sha256','owner_uid','directory_mode',
            'reload_equal','same_controller_authority','verification_pass'}
            or snapshot['attempt_id']!=worker['child_id'] or type(snapshot['generation']) is not int
            or snapshot['generation']!=1 or snapshot['runtime_id']!=result['runtime_id']
            or snapshot['binding_digest']!=result['binding_digest']
            or snapshot['caller_spec_digest']!=worker['caller_spec_digest']
            or not re.fullmatch('[0-9a-f]{64}',snapshot['manifest_sha256'])
            or snapshot['result_sha256']!=durable['result.json']['sha256']
            or snapshot['events_sha256']!=durable['events.jsonl']['sha256']
            or type(snapshot['owner_uid']) is not int or snapshot['owner_uid']!=0
            or snapshot['directory_mode']!='0o700' or snapshot['reload_equal'] is not True
            or snapshot['same_controller_authority'] is not True or snapshot['verification_pass'] is not False):
        raise ValueError('controller_snapshot_unproven')
    validate_common_checks(worker)
    if receipt['export_cleanup'].get('journal_present') is not True or receipt['export_cleanup'].get('bytes_recoverable') is not True or receipt['export_cleanup'].get('terminal_aborted') is not False:
        raise ValueError('result_export_cleanup_unproven')
    composition=worker.get('composition',{})
    required_true=('repeat_equal','used_saved_observation','replay_no_create_start','secret_replay_refused',
        'secret_replay_state_unchanged','service_closed','root_occupancy_retained','account_reservation_retained')
    if (any(composition.get(k) is not True for k in required_true)
            or any(composition.get(k) is not False for k in ('verification_pass','candidate_verified','scheduler_seat_released'))
            or composition.get('artifact_count')!=2 or composition.get('artifact_provenance')!=['controller_captured_candidate','worker_reported']
            or composition.get('export_create_calls')!=1 or composition.get('export_start_calls')!=1
            or composition.get('provider_objects_remaining')!=0 or composition.get('provider_material_cleaned')!=2
            or not valid_counts(composition.get('counts'))
            or any(type(composition.get(k)) is not int for k in ('artifact_count','export_create_calls','export_start_calls','provider_objects_remaining','provider_material_cleaned'))):raise ValueError('result_composition_unproven')
    validate_result_artifacts(composition)

    return True


def validate_result_artifacts(composition):
    ids=composition.get('artifact_ids');hashes=composition.get('artifact_hashes')
    if (type(ids) is not list or len(ids)!=2 or any(type(v) is not str for v in ids)
            or len(set(ids))!=2 or type(hashes) is not dict or set(ids)!=set(hashes)):
        raise ValueError('result_artifacts_unproven')
    candidates=[v for v in ids if re.fullmatch('[0-9a-f]{64}',v)]
    observations=[v for v in ids if re.fullmatch('observation-[0-9a-f]{64}',v)]
    digest_fields=('candidate_sha256','candidate_file_sha256','input_revision_sha256',
        'export_receipt_sha256','cleanup_sha256','observation_sha256')
    if (len(candidates)!=1 or len(observations)!=1
            or any(type(v) is not str or not re.fullmatch('[0-9a-f]{64}',v) for v in hashes.values())
            or any(type(composition.get(k)) is not str or not re.fullmatch('[0-9a-f]{64}',composition[k]) for k in digest_fields)
            or composition['candidate_file_sha256']!=sha(b'SYNTHETIC_ROUTED_CALLER_FILE\n')):
        raise ValueError('result_artifacts_unproven')
    if (hashes[candidates[0]]!=composition['candidate_sha256']
            or hashes[observations[0]]!=composition['observation_sha256']):
        raise ValueError('result_artifacts_unproven')
    return True


def expected_counts():
    return {'request_leases':2,'released_requests':2,'dispatches':2,'budget_requests':2,
        'root_used':2,'attempt_used':2,'gates':0}


def valid_counts(value):
    return type(value) is dict and value==expected_counts() and all(type(v) is int for v in value.values())


def validate_common_checks(worker):
    required={'durable_binding_before_start','grant_fenced_before_stop','occupancy_retained','no_gate',
        'source_preserved','child_not_cancelled','snapshot_policy_before_stop'}
    if set(worker.get('checks',{}))!=required or any(v is not True for v in worker['checks'].values()):raise ValueError('driver_checks_missing')
    requests=worker.get('requests',[])
    if (len(requests)!=2 or [r.get('tool_result_seen') for r in requests]!=[False,True]
            or len({r['nonce'] for r in requests})!=2):raise ValueError('synthetic_roundtrip_unproven')


def reconcile_fixture_export(folder,bundle,identity):
    """Exact frozen fixture journal only; never infer cleanup from a label alone."""
    from cloudworkbench.runtime import Runtime
    from cloudworkbench.routed_runtime import RoutedRuntime,CallerSpec
    from cloudworkbench.routed_export import reconcile_workspace_export
    raw=dict(identity['spec']);raw['task_files']=tuple(tuple(pair) for pair in raw['task_files']);raw['limits']=tuple(raw['limits'])
    spec=CallerSpec(**raw)
    if (spec.digest!=identity['digest'] or spec.name!=identity['name'] or spec.owner!=bundle['run_id']
            or spec.image!=bundle['image'] or spec.limits!=(1.4,3008,352)
            or any(not Path(path).is_relative_to(folder) or Path(path)==folder for path in
                (spec.workspace,spec.scratch,spec.task_dir,spec.worker_socket_dir))):
        raise RuntimeError('cleanup_export_fixture_binding')
    journal_root=folder/'caller-journal';journal=journal_root/(spec.attempt_id+'.'+str(spec.generation))/'workspace-export.json'
    try:journal.lstat()
    except FileNotFoundError:return {'journal_present':False,'collector_removed':True,'outcome':'not_attempted'}
    runtime=Runtime({'root':folder,'image':spec.image,'owner':spec.owner,'cpus':1.4,'memory_mib':3008,'pids':352,
        'approved_mount_roots':[folder],'approved_writable_mount_roots':[folder]})
    routed=RoutedRuntime(runtime,journal_root=journal_root)
    value=reconcile_workspace_export(routed,spec,authorize=lambda action:action in
        {'reconcile','cleanup_inspect','stop','remove','publish'})
    if value.get('collector_removed') is not True:raise RuntimeError('cleanup_export_unconfirmed')
    return {'journal_present':True,'collector_removed':True,'outcome':value.get('outcome'),
        'bytes_recoverable':value.get('bytes_recoverable'),'terminal_aborted':value.get('terminal_aborted')}


def remote_entry(bundle):
    import socket,tempfile,subprocess,signal,time
    validate_bundle(bundle)
    if socket.gethostname()!='archived-worker.invalid' or os.geteuid()!=0:raise RuntimeError('wrong_host')
    folder=Path(tempfile.mkdtemp(prefix='cwb2-'+bundle['run_id']+'-')).resolve();folder.chmod(0o700)
    package=folder/'source/cloudworkbench';package.mkdir(parents=True)
    for name,body in bundle['sources'].items():(package/name).write_text(body)
    sys.path.insert(0,str(package.parent))
    (folder/'bundle.json').write_text(json.dumps(bundle));(folder/'harness.py').write_text(bundle['harness'])
    receipt={'proof_version':2,'scenario':bundle['scenario'],'run_id':bundle['run_id'],'host':socket.gethostname(),'image':bundle['image'],'folder':str(folder),
        'source_hashes':bundle['hashes'],'harness_sha256':bundle['harness_sha256'],'started_at':stamp(),
        'passed':False,'real_provider_calls':False,'real_credentials_used':False,'root_preprovisioned_fixture':True,
        'worker959_provisioning_qualified':False,'unknown_worker_effects':False,'phase':'preflight'}
    command=docker_command(folder);identity=None
    def inventory():return command(['ps','-aq','--no-trunc','--filter','label=io.cloudworkbench.owner='+bundle['run_id']]).splitlines()
    try:
        receipt['services_before']=services()
        available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
        disk=os.statvfs('/var/lib/docker')
        if available<6*1024**2 or disk.f_bavail*disk.f_frsize<5*1024**3:raise RuntimeError('headroom_unavailable')
        if json.loads(command(['image','inspect',bundle['image']]))[0]['Id']!=bundle['image'] or inventory():raise RuntimeError('preflight_identity_mismatch')
        receipt['phase']='controller'
        child=subprocess.Popen([sys.executable,str(folder/'harness.py'),'--worker-entry',str(folder)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':str(folder),'PYTHONDONTWRITEBYTECODE':'1'},cwd=str(folder))
        try:child.wait(timeout=150)
        except subprocess.TimeoutExpired:
            receipt['unknown_worker_effects']=True;os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3);raise RuntimeError('worker_timeout')
        receipt['worker_exit_code']=child.returncode
        phase_path=folder/'worker-phase.json'
        if phase_path.exists() and phase_path.stat().st_size<1024:receipt['worker_phase']=json.loads(phase_path.read_text())
        status_path=folder/'driver-status.json'
        if status_path.exists() and status_path.stat().st_size<1024:receipt['driver_status']=json.loads(status_path.read_text())
        diagnostics_path=folder/'scheduler-control-diagnostics.json'
        if diagnostics_path.is_file() and 0<diagnostics_path.stat().st_size<=8192:
            receipt['scheduler_control_diagnostics']=checked_control_diagnostics(json.loads(diagnostics_path.read_text()))
        failure_path=folder/'worker-failure.json'
        if failure_path.is_file() and 0<failure_path.stat().st_size<=2048:
            receipt['worker_failure']=checked_worker_failure(json.loads(failure_path.read_text()),bundle)
        if child.returncode:raise RuntimeError('worker_failed')
        result=folder/'worker-result.json'
        if not 0<result.stat().st_size<=2*1024**2:raise RuntimeError('worker_result_bound')
        receipt['worker']=json.loads(result.read_text())
    except Exception as exc:
        receipt['failure_phase']=receipt['phase'];receipt['error_type']=type(exc).__name__
    finally:
        receipt['phase']='cleanup'
        try:
            identity_file=folder/'caller-identity.json'
            receipt['export_cleanup']={'journal_present':False,'collector_removed':True,'outcome':'not_attempted'}
            if identity_file.is_file() and identity_file.stat().st_size<=262144:
                identity=json.loads(identity_file.read_text())
                receipt['export_cleanup']=reconcile_fixture_export(folder,bundle,identity)
            identities=inventory()
            if identities:
                if not identity_file.is_file() or identity_file.stat().st_size>262144:raise RuntimeError('cleanup_binding_absent')
                identity=json.loads(identity_file.read_text())
                for rid in identities:
                    obj=json.loads(command(['inspect',rid]))[0];exact_owned(obj,bundle,identity)
                    command(['stop','--time','3',rid],timeout=10)
                    if json.loads(command(['inspect',rid]))[0]['State']['Running']:raise RuntimeError('caller_stop_unconfirmed')
                    command(['rm',rid])
            receipt['remaining']=inventory();receipt['services_after']=services()
            receipt['cleanup_complete']=not receipt['remaining'] and not receipt['unknown_worker_effects'] and receipt['export_cleanup']['collector_removed'] is True
            receipt['passed']='worker' in receipt and not receipt.get('error_type') and receipt['cleanup_complete']
            if receipt['passed']:validate_receipt(receipt,bundle)
        except Exception as exc:
            receipt['passed']=False;receipt['cleanup_complete']=False;receipt['cleanup_error_type']=type(exc).__name__
        receipt['finished_at']=stamp();receipt['phase']='complete' if receipt['passed'] else 'failed'
        (folder/'receipt.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt))


def main():
    if sys.argv[1:2]==['--worker-entry']:
        folder=Path(sys.argv[2]);bundle=json.loads((folder/'bundle.json').read_text());validate_bundle(bundle)
        sys.path.insert(0,str(folder/'source'))
        try:worker(folder,bundle)
        except Exception as exc:
            failure_path=folder/'worker-failure.json'
            if not failure_path.exists():
                phase_path=folder/'worker-phase.json'
                phase=json.loads(phase_path.read_text())['phase'] if phase_path.exists() else 'setup'
                failure_path.write_text(json.dumps(worker_failure(exc,phase,bundle)))
            raise SystemExit(1)
        return
    if sys.argv[1:2]==['--remote-entry']:remote_entry(QUALIFICATION_BUNDLE);return
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare',type=Path);mode.add_argument('--remote',type=Path)
    parser.add_argument('--scenario',choices=('results','secret-refusal'),default='results')
    parser.add_argument('--image');parser.add_argument('--receipt',type=Path);args=parser.parse_args()
    if args.prepare:
        bundle=prepare(args.prepare,args.image,scenario=args.scenario);print(json.dumps({'prepared':str(args.prepare),'run_id':bundle['run_id'],'remote_executed':False}));return
    if args.receipt is None or args.receipt.exists():parser.error('--remote requires new --receipt')
    import subprocess
    bundle=json.loads(args.remote.read_text());validate_bundle(bundle)
    program='QUALIFICATION_BUNDLE='+repr(bundle)+'\n'+bundle['harness']
    p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',REMOTE,
        'sudo -n /opt/cloud-workbench/.venv/bin/python - --remote-entry'],input=program,capture_output=True,text=True,timeout=360)
    with args.receipt.open('x') as output:output.write(p.stdout)
    receipt=json.loads(p.stdout);validate_receipt(receipt,bundle)
    print(json.dumps({'receipt':str(args.receipt),'passed':True}))


if __name__=='__main__':main()
