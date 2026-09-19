#!/usr/bin/env python3
"""Disposable synthetic supervised-provider proof. No live credentials or deployment.

Local invocation snapshots source and sends it over stdin to a private root-owned
Omarchy temporary directory. Remote mode is only entered by that generated input.
"""

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

from pathlib import Path
import datetime
import hashlib
import json
import subprocess
import sys

MODULES = ('__init__.py','models.py','retention.py','store.py','provider_leases.py',
    'provider_dispatch.py','provider_docker.py','provider_executor.py','supervised_executor.py','provider_recovery.py','provider_protocol.py','native_responses.py',
    'cancellation_supervisor.py','egress.py','profile_binding.py','inference_transport.py',
    'inference_budget.py','budget_authority.py','inference_relay.py','inference_service.py','hermes_inference_protocol.py')


def full_resource_ids(value):
    expected={'provider_id','gateway_id','internal_network_id','external_network_id'}
    if (not isinstance(value,dict) or set(value)!=expected or len(set(value.values()))!=4
            or any(not isinstance(v,str) or len(v)!=64 or set(v)-set('0123456789abcdef') for v in value.values())):
        raise ValueError('invalid_resource_identities')
    return value


def validate_receipt(receipt):
    try:
        if receipt.get('passed') is not True or receipt.get('host') != 'archived-worker.invalid':
            raise ValueError('qualification_not_passed')
        for key in ('success_after_cleanup_and_supervisor_stop','same_nonce_replay_charged_once',
                    'actual_cli_running_before_cancel','cancel_refused_success',
                    'unknown_resolution_retained_quarantine','cancel_scope_physically_cleaned'):
            if receipt.get('checks',{}).get(key) is not True:
                raise ValueError('missing_qualification_check')
        if not receipt['cleanup']['complete'] or receipt['cleanup']['remaining_objects']:
            raise ValueError('cleanup_incomplete')
        if receipt['budget_counts'] != {'roots_used':2,'attempts_used':2,'requests':2,'dispatches':2}:
            raise ValueError('budget_charge_mismatch')
        success,cancel=receipt['success'],receipt['cancel']
        if success['status']!=200 or not success['supervisor']['stopped'] or success['supervisor']['cancelled']:
            raise ValueError('success_supervision_unconfirmed')
        if (cancel['raw_status']==200 or not cancel['supervisor']['stopped'] or not cancel['supervisor']['cancelled']
                or cancel['raw_outer_cleanup_confirmed'] is not False or cancel['raw_dispatch_state']!='quarantined'
                or cancel['raw_uncertain']!=1 or cancel['final_dispatch_state']!='cleaned'):
            raise ValueError('cancel_state_mismatch')
        identities=[]
        for record in (success,cancel):
            cleanup=record['cleanup']
            if cleanup['physical_absence'] is not True:raise ValueError('physical_absence_unconfirmed')
            identities.extend(full_resource_ids(cleanup['identities'][record['request_id']]).values())
        if len(set(identities))!=8:raise ValueError('runtime_reused')
        if success['cleanup']['observed_monotonic']>success['return_monotonic']:
            raise ValueError('success_before_cleanup')
        if receipt['services_before'] != receipt['services_after'] or receipt['synthetic_token_removed'] is not True:
            raise ValueError('service_or_synthetic_token_cleanup_unconfirmed')
        return True
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError('invalid_receipt_shape') from exc


def validate_archive(receipt,snapshot,harness):
    validate_receipt(receipt)
    if receipt['source_hashes']!=snapshot['hashes']:
        raise ValueError('receipt_source_mismatch')
    if any(hashlib.sha256(value.encode()).hexdigest()!=snapshot['hashes'][name] for name,value in snapshot['sources'].items()):
        raise ValueError('snapshot_source_mismatch')
    if set(snapshot['sources'])!=set(snapshot['hashes']):raise ValueError('snapshot_inventory_mismatch')
    digest=hashlib.sha256(harness).hexdigest()
    if digest!=snapshot['harness_sha256']:raise ValueError('harness_snapshot_mismatch')
    # Historical schema1 did not echo this hash in the remote receipt. Preserve
    # it as historical evidence; snapshot+archived-byte comparison is explicit.
    if receipt.get('schema_version',1)>=2 and receipt.get('harness_sha256')!=digest:
        raise ValueError('receipt_harness_mismatch')
    return True


def remote_main(bundle):
    import io, os, shlex, socket, tarfile, tempfile, threading, time, uuid
    from dataclasses import asdict
    if socket.gethostname()!='archived-worker.invalid' or os.geteuid()!=0:
        raise RuntimeError('wrong_target')
    run_id='supervisedqual-'+uuid.uuid4().hex
    folder=Path(tempfile.mkdtemp(prefix='cwb2-'+run_id+'-')).resolve()
    folder.chmod(0o700)
    receipt={'host':socket.gethostname(),'effective_uid':os.geteuid(),'run_id':run_id,
        'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'folder':str(folder),
        'source_hashes':bundle['hashes'],'harness_sha256':bundle['harness_sha256'],'schema_version':2,'passed':False,'checks':{},
        'scope':'synthetic local CLI/token; no real provider or live service/config changes',
        'not_qualified':['actual native Hermes tool loop','real provider/authentication','production activation',
                         'unanswered Docker RPC (resolver-unknown is injected locally)']}
    local_receipt=folder/'qualification.json'
    package=folder/'source'/'cloudworkbench';package.mkdir(parents=True)
    for name,source in bundle['sources'].items():
        if name not in MODULES or hashlib.sha256(source.encode()).hexdigest()!=bundle['hashes'][name]:
            raise RuntimeError('source_binding_mismatch')
        (package/name).write_text(source)
    sys.path.insert(0,str(package.parent))
    from cloudworkbench.store import Store
    from cloudworkbench.inference_budget import InferenceBudget,RootScope,AttemptScope,ensure_schema
    from cloudworkbench.provider_leases import ProviderLeases
    from cloudworkbench.provider_dispatch import ProviderDispatch,DispatchError
    from cloudworkbench.provider_docker import DockerConfig,ProviderDocker
    from cloudworkbench.provider_executor import ProviderExecutor
    from cloudworkbench.supervised_executor import SupervisedProviderExecutor
    from cloudworkbench.inference_transport import PinnedCLI
    from cloudworkbench.inference_relay import AttemptBinding,DispatchContext,_json
    from cloudworkbench.profile_binding import canonical_pinned_profile_digest
    docker_config=folder/'docker-cli';docker_config.mkdir(mode=0o700)
    def docker(*args, timeout=15, check=True, data=None):
        result=subprocess.run(['/usr/bin/docker','--host','unix:///var/run/docker.sock','--config',str(docker_config),*args],
            input=data,capture_output=True,timeout=timeout,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
        if check and result.returncode:raise RuntimeError('docker_command_failed:'+str(args[:2]))
        return result
    def objects(reservation_id):
        selector='label=io.cloudworkbench.provider-reservation='+reservation_id
        return {'containers':docker('ps','-aq','--no-trunc','--filter',selector).stdout.decode().splitlines(),
                'networks':docker('network','ls','-q','--no-trunc','--filter',selector).stdout.decode().splitlines()}
    def absent(identities):
        full_resource_ids(identities)
        containers=set(docker('ps','-aq','--no-trunc').stdout.decode().splitlines())
        networks=set(docker('network','ls','-q','--no-trunc').stdout.decode().splitlines())
        return not any(value in (networks if 'network' in key else containers) for key,value in identities.items() if value)
    services=['cloud-workbench-api','cloud-workbench-worker','cloudd']
    def service_snapshot():
        result=subprocess.run(['systemctl','is-active',*services],capture_output=True,text=True,timeout=5)
        if result.returncode:raise RuntimeError('service_snapshot_failed')
        return result.stdout.splitlines()
    reservations=[];tag=None;image=None;token=None;watcher=None;watch_stop=threading.Event()
    try:
        receipt['services_before']=service_snapshot()
        available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
        disk=os.statvfs('/var/lib/docker');free=disk.f_bavail*disk.f_frsize
        if available<6*1024*1024 or free<5*1024**3:raise RuntimeError('insufficient_headroom')
        receipt['preflight']={'available_memory_kib':available,'docker_free_bytes':free}
        candidate=bundle['candidate']
        actual=docker('image','inspect',candidate['tag'],'--format','{{.Id}}').stdout.decode().strip()
        if actual!=candidate['image_id']:raise RuntimeError('base_image_changed')
        receipt['base_image']=actual
        cli=b'''#!/opt/hermes/venv/bin/python
import sys,json,os,time
request=sys.stdin.read()
assert os.environ['CLAUDE_CODE_OAUTH_TOKEN']=='sk-ant-oat-synthetic-supervised-only'
assert os.getuid()==958 and os.getgid()==959
assert sys.argv[sys.argv.index('--tools')+1]==''
assert sys.argv[sys.argv.index('--disallowedTools')+1]=='*'
if 'wait-for-cancel' in request:time.sleep(20)
print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':{'kind':'final','text':'synthetic supervised okay','tool_calls':[]}}))
'''
        context=io.BytesIO();tag='cwb2-supervised-qual:'+run_id
        dockerfile=('FROM '+candidate['tag']+'\nLABEL io.cloudworkbench.qualification='+run_id+'\nCOPY --chmod=0555 fake-cli /opt/probe/fake-cli\n').encode()
        with tarfile.open(fileobj=context,mode='w') as archive:
            for name,data in [('Dockerfile',dockerfile),('fake-cli',cli)]:
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o555 if name=='fake-cli' else 0o644
                archive.addfile(info,io.BytesIO(data))
        build=docker('build','--pull=false','--network','none','-t',tag,'-',data=context.getvalue(),timeout=75,check=False)
        (folder/'build.log').write_bytes(build.stdout+build.stderr)
        if build.returncode:raise RuntimeError('synthetic_image_build_failed')
        image=docker('image','inspect',tag,'--format','{{.Id}}').stdout.decode().strip()
        if docker('image','inspect',candidate['tag'],'--format','{{.Id}}').stdout.decode().strip()!=actual:raise RuntimeError('base_tag_changed_during_build')
        receipt.update(synthetic_image=image,synthetic_tag=tag,fake_cli_sha256=hashlib.sha256(cli).hexdigest())
        profile=PinnedCLI(Path('/opt/probe/fake-cli'),hashlib.sha256(cli).hexdigest(),'2.1.274','claude-fable-5-1','high')
        profile_path=folder/'profile.json';body=asdict(profile);body['path']=str(profile.path)
        profile_path.write_text(json.dumps(body));profile_path.chmod(0o444)
        token=folder/'synthetic-token';token.write_text('sk-ant-oat-synthetic-supervised-only');os.chown(token,0,959);token.chmod(0o640)
        staging=folder/'staging';staging.mkdir(mode=0o700)
        digest=canonical_pinned_profile_digest(profile)
        store=Store(folder/'state.db')
        principal=store.add_client('synthetic-proof','SYNTHETIC-CONTROL-TOKEN-'+'x'*40,['submit','observe','cancel'],['synthetic-proof'])
        holder={};cleanup_observations=[]
        runtime=ProviderDocker(DockerConfig(image,profile_path,hashlib.sha256(profile_path.read_bytes()).hexdigest(),digest,token,staging,('example.com',)),
            cancel_check=lambda spec:holder['wrapper'].cancel_check(spec) if holder.get('wrapper') else True)
        def verify_cleanup(target):
            result=runtime.cleanup(target)
            identities={}
            for items in target.dispatches:
                row=dict(items)
                current={key:row[key] for key in ('provider_id','gateway_id','internal_network_id','external_network_id')}
                if not absent(current):raise RuntimeError('physical_cleanup_unconfirmed')
                identities[row['request_id']]=current
            cleanup_observations.append({'request_id':target.request_id,'observed_monotonic':time.monotonic(),
                'identities':identities,'evidence_sha256':result.evidence_sha256,'physical_absence':True})
            return result
        leases=ProviderLeases(store,cleanup_verifier=verify_cleanup,inspector_id=runtime.config.inspector_id)
        leases.register_account(run_id,legacy_agent='claude',persistent_owner_id=run_id)
        reservation=leases.reserve(run_id,persistent_owner_id=run_id);reservations.append(reservation.reservation_id)
        receipt['reservation']=asdict(reservation)
        budget=InferenceBudget()
        def scenario(name):
            attempt=store.create_session(principal,{'project_id':'synthetic-proof','agent':'hermes','goal':name},name)
            claimed=store.claim_next()
            if claimed['id']!=attempt['attempt_id']:raise RuntimeError('wrong_attempt_claimed')
            with store._connect() as db:
                row=db.execute('SELECT a.session_id,a.turn_id,s.owner_id,s.project_id FROM attempts a JOIN sessions s ON s.id=a.session_id WHERE a.id=?',(attempt['attempt_id'],)).fetchone()
            root_scope=RootScope(row['owner_id'],row['project_id'],row['session_id'],row['turn_id'],attempt['attempt_id'],attempt['generation'])
            scope=AttemptScope(root_scope,attempt['attempt_id'],attempt['generation'])
            with store._tx() as db:
                ensure_schema(db);budget.register_root(db,root_scope,admitted_at=time.time());budget.register_attempt(db,scope)
            grant=leases.issue_grant(reservation,attempt_id=attempt['attempt_id'],generation=attempt['generation'])
            dispatch=ProviderDispatch(leases,runtime,budget=budget,budget_scope=scope)
            binding=AttemptBinding(attempt['attempt_id'],attempt['generation'],digest)
            wrapper=SupervisedProviderExecutor(ProviderExecutor(dispatch,reservation=reservation,grant_id=grant,binding=binding,profile=profile,remaining_seconds=lambda:600))
            holder['wrapper']=wrapper
            payload={'model':profile.native_model,'messages':[{'role':'user','content':name}],'tools':[],'stream':False,'reasoning_effort':'high'}
            ctx=DispatchContext(binding,uuid.uuid4().hex+uuid.uuid4().hex,hashlib.sha256(_json(payload)).hexdigest())
            return attempt,grant,dispatch,wrapper,payload,ctx
        attempt,grant,dispatch,wrapper,payload,ctx=scenario('synthetic-success')
        response=wrapper(ctx,payload,threading.Event());returned=time.monotonic()
        if response.response.status!=200:raise RuntimeError('structured_success_missing:'+response.response.body.decode()[:300])
        answer=json.loads(response.response.body)
        if answer['choices'][0]['message']['content']!='synthetic supervised okay':raise RuntimeError('wrong_structured_result')
        if not response.outer_cleanup_confirmed or not wrapper.last_receipt.stopped or wrapper.last_receipt.cancelled:raise RuntimeError('success_without_cleanup_or_stop')
        with store._connect() as db:success_row=dict(db.execute('SELECT * FROM provider_dispatch WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone())
        if not cleanup_observations or cleanup_observations[-1]['observed_monotonic']>returned:raise RuntimeError('cleanup_not_before_return')
        if any(thread.name=='cwb-cancel-supervisor' for thread in threading.enumerate()):raise RuntimeError('observer_thread_remaining')
        receipt['success']={'status':response.response.status,'body_sha256':hashlib.sha256(response.response.body).hexdigest(),'supervisor':asdict(wrapper.last_receipt),
            'request_id':success_row['request_id'],'dispatch_state':success_row['state'],'return_monotonic':returned,'cleanup':cleanup_observations[-1]}
        receipt['checks']['success_after_cleanup_and_supervisor_stop']=True
        again=wrapper(ctx,payload,threading.Event())
        if again.response.body!=response.response.body or again.response.status!=200:raise RuntimeError('replay_response_mismatch')
        with store._connect() as db:
            counts={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('provider_dispatch','inference_budget_requests')}
            used=db.execute('SELECT SUM(used) FROM inference_budget_roots').fetchone()[0]
        if counts!={'provider_dispatch':1,'inference_budget_requests':1} or used!=1:raise RuntimeError('replay_double_charged')
        if len(cleanup_observations)!=1:raise RuntimeError('replay_launched_again')
        receipt['checks']['same_nonce_replay_charged_once']=True
        # Finish only the synthetic private attempt so Store may claim the next.
        for state in ('running','verifying'):
            store.transition(attempt['attempt_id'],state,expected_generation=attempt['generation'])
        store.transition(attempt['attempt_id'],'completed',expected_generation=attempt['generation'],outcome='unverified')
        # A different private attempt under the same account uses a fresh grant.
        attempt,grant,dispatch,wrapper,payload,ctx=scenario('wait-for-cancel')
        cancel=threading.Event();observed={}
        def cancel_when_real_cli_runs():
            try:
                end=time.monotonic()+25
                while not watch_stop.is_set() and time.monotonic()<end:
                    with store._connect() as db:row=db.execute('SELECT provider_id FROM provider_dispatch WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone()
                    if row and row[0]:
                        top=docker('top',row[0],'-eo','pid,args',timeout=3,check=False)
                        text=top.stdout.decode(errors='replace')
                        if top.returncode==0 and '/opt/probe/fake-cli' in text:
                            observed.update(provider_id=row[0],process_listing=text,at_monotonic=time.monotonic())
                            cancel.set();return
                    watch_stop.wait(.1)
                observed['error']='synthetic_cli_not_observed'
                cancel.set()
            except Exception as exc:observed['error']=type(exc).__name__;cancel.set()
        watcher=threading.Thread(target=cancel_when_real_cli_runs,name='qualification-cancel-observer',daemon=False);watcher.start()
        output=wrapper(ctx,payload,cancel);returned=time.monotonic();watch_stop.set();watcher.join(5)
        if watcher.is_alive():raise RuntimeError('qualification_observer_unstopped')
        if 'error' in observed or not observed.get('provider_id'):raise RuntimeError('no_actual_active_cli')
        receipt['checks']['actual_cli_running_before_cancel']=True
        with store._connect() as db:cancel_row=dict(db.execute('SELECT * FROM provider_dispatch WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone())
        receipt['cancel']={'raw_status':output.response.status,'raw_outer_cleanup_confirmed':output.outer_cleanup_confirmed,
            'body':json.loads(output.response.body),'supervisor':asdict(wrapper.last_receipt),'request_id':cancel_row['request_id'],
            'raw_dispatch_state':cancel_row['state'],'raw_uncertain':cancel_row['uncertain'],'observed_active_cli':observed,
            'seconds_from_cancel_to_wrapper_return':returned-observed['at_monotonic']}
        if output.response.status==200 or not wrapper.last_receipt.stopped or not wrapper.last_receipt.cancelled:raise RuntimeError('cancel_not_fenced')
        receipt['checks']['cancel_refused_success']=True
        request_id=cancel_row['request_id']
        if not cancel_row['uncertain']:raise RuntimeError('expected_actual_start_uncertainty_missing')
        # Explicit injected missing resolver evidence: no mutation RPC is forged.
        original_resolve=runtime.resolve;runtime.resolve=lambda *args,**kwargs:None
        try:
            try:dispatch.reconcile(request_id);raise RuntimeError('unknown_resolution_accepted')
            except DispatchError as exc:
                if exc.code!='operation_still_unknown':raise
            row=dispatch.read(request_id)
            if row['state']!='quarantined' or not row['uncertain']:raise RuntimeError('unknown_not_quarantined')
            receipt['checks']['unknown_resolution_retained_quarantine']=True
        finally:runtime.resolve=original_resolve
        dispatch.reconcile(request_id)
        receipt['cancel']['resolved_operation']=json.loads(dispatch.read(request_id)['operation_receipt'])
        dispatch.cleanup(request_id)
        final=dispatch.read(request_id)
        ids={key:final[key] for key in ('provider_id','gateway_id','internal_network_id','external_network_id')}
        if final['state']!='cleaned' or not absent(ids):raise RuntimeError('cancel_scope_not_cleaned')
        receipt['cancel']['controller_reconciliation_sequence']=['reconcile with missing evidence refused','reconcile with actual adapter journal','cleanup exact request']
        receipt['cancel']['final_dispatch_state']=final['state'];receipt['cancel']['cleanup']=cleanup_observations[-1]
        receipt['checks']['cancel_scope_physically_cleaned']=True
        with store._connect() as db:
            receipt['budget_counts']={'roots_used':db.execute('SELECT SUM(used) FROM inference_budget_roots').fetchone()[0],
                'attempts_used':db.execute('SELECT SUM(used) FROM inference_budget_attempts').fetchone()[0],
                'requests':db.execute('SELECT COUNT(*) FROM inference_budget_requests').fetchone()[0],
                'dispatches':db.execute('SELECT COUNT(*) FROM provider_dispatch').fetchone()[0]}
        receipt['passed']=True
    except Exception as exc:
        receipt['error']={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc)[:500])}
    finally:
        watch_stop.set()
        if watcher is not None:watcher.join(5)
        cleanup=[];remaining=[]
        for reservation_id in reservations:
            try:
                owned=objects(reservation_id)
                for kind in ('containers','networks'):
                    for identity in owned[kind]:
                        args=('inspect',identity) if kind=='containers' else ('network','inspect',identity)
                        obj=json.loads(docker(*args).stdout)[0]
                        labels=obj['Config']['Labels'] if kind=='containers' else obj['Labels']
                        if labels.get('io.cloudworkbench.provider-reservation')!=reservation_id:raise RuntimeError('fallback_owner_mismatch')
                        result=docker(*(('rm','-f',identity) if kind=='containers' else ('network','rm',identity)),check=False)
                        cleanup.append({'kind':kind,'id':identity,'fallback_remove_exit':result.returncode})
                final_owned=objects(reservation_id)
                remaining+=final_owned['containers']+final_owned['networks']
            except Exception as exc:cleanup.append({'reservation':reservation_id,'error':type(exc).__name__});remaining.append('unconfirmed:'+reservation_id)
        if token is not None:
            try:token.unlink();receipt['synthetic_token_removed']=True
            except Exception:receipt['synthetic_token_removed']=False
        if tag is not None:
            try:
                inspected=docker('image','inspect',tag,check=False)
                if inspected.returncode==0:
                    obj=json.loads(inspected.stdout)[0]
                    if obj['Config']['Labels'].get('io.cloudworkbench.qualification')!=run_id:raise RuntimeError('image_owner_mismatch')
                    result=docker('image','rm',tag,check=False)
                    cleanup.append({'kind':'image','id':obj['Id'],'remove_exit':result.returncode})
                    if result.returncode:remaining.append(obj['Id'])
            except Exception as exc:cleanup.append({'kind':'image','error':type(exc).__name__});remaining.append('image_unconfirmed')
        try:receipt['services_after']=service_snapshot()
        except Exception:receipt['services_after']=['unconfirmed']
        receipt['cleanup']={'actions':cleanup,'remaining_objects':remaining,'complete':not remaining and all('error' not in action and action.get('fallback_remove_exit',action.get('remove_exit',0))==0 for action in cleanup)}
        if not receipt['cleanup']['complete'] or receipt.get('services_before')!=receipt['services_after']:receipt['passed']=False
        local_receipt.write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt))
    return receipt


def local_main():
    root=Path(__file__).resolve().parents[2]
    sources={name:(root/'src/cloudworkbench'/name).read_text() for name in MODULES}
    bundle={'sources':sources,'hashes':{name:hashlib.sha256(source.encode()).hexdigest() for name,source in sources.items()},
            'candidate':json.loads((root/'evidence/provider-image-candidate.json').read_text())}
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    base=root/'evidence'/('supervised-provider-linux-'+stamp)
    script=Path(__file__).read_text()
    bundle['harness_sha256']=hashlib.sha256(script.encode()).hexdigest()
    base.with_suffix('.source-snapshot.json').write_text(json.dumps(bundle,indent=2)+'\n')
    base.with_suffix('.harness.py').write_text(script)
    program='QUALIFICATION_BUNDLE='+repr(bundle)+'\n'+script
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','operator@worker.example.invalid',
        'sudo -n /opt/cloud-workbench/.venv/bin/python - --remote'],input=program,capture_output=True,text=True,timeout=300)
    base.with_suffix('.stderr.txt').write_text(result.stderr)
    base.with_suffix('.json' if result.stdout.strip() else '.failed.txt').write_text(result.stdout)
    if result.returncode:raise RuntimeError('remote_qualification_failed: '+str(result.returncode))
    receipt=json.loads(result.stdout)
    print(json.dumps({'receipt':str(base.relative_to(root))+'.json','passed':receipt.get('passed'),
                      'error':receipt.get('error'),'cleanup':receipt.get('cleanup')},indent=2))
    validate_archive(receipt,bundle,script.encode())


if __name__=='__main__':
    if sys.argv[1:]==['--remote']:
        remote_main(QUALIFICATION_BUNDLE)
    else:
        local_main()
