#!/usr/bin/env python3
"""Actual native Hermes -> distinct-UID relay -> supervised synthetic Docker proof."""
from pathlib import Path
import argparse, datetime, hashlib, json, subprocess, sys

MODULES=('__init__.py','models.py','retention.py','store.py','provider_leases.py','provider_dispatch.py',
    'provider_docker.py','provider_executor.py','supervised_executor.py','provider_protocol.py','native_responses.py','provider_recovery.py',
    'cancellation_supervisor.py','budget_authority.py','egress.py','profile_binding.py','inference_transport.py',
    'inference_budget.py','inference_relay.py','inference_service.py','hermes_inference_protocol.py')
RESOURCE_KEYS=('provider_id','gateway_id','internal_network_id','external_network_id')


def full_ids(value):
    if (set(value)!=set(RESOURCE_KEYS) or len(set(value.values()))!=4
            or any(not isinstance(v,str) or len(v)!=64 or set(v)-set('0123456789abcdef') for v in value.values())):
        raise ValueError('invalid_resource_ids')
    return value


def _validate(receipt,bundle):
    if (not receipt.get('passed') or receipt.get('host')!='omarchy' or receipt['source_hashes']!=bundle['hashes']
            or receipt['harness_sha256']!=bundle['harness_sha256'] or receipt['run_id']!=bundle['run_id']
            or receipt['caller_image']!=bundle['candidate']['base'] or not receipt['cleanup']['complete']
            or receipt['cleanup']['remaining'] or receipt['services_before']!=receipt['services_after']):
        raise ValueError('incomplete_native_proof')
    actions=receipt['cleanup']['actions']
    expected_actions={('containers',receipt['caller_id']),('volume','cwb2-'+receipt['run_id']+'-source'),('image',receipt['provider_image'])}
    if (len(actions)!=3 or {(a['kind'],a.get('id',a.get('name'))) for a in actions}!=expected_actions
            or any(a['exit']!=0 or a.get('fallback_provider_cleanup',False) is not False for a in actions)):
        raise ValueError('cleanup_identity_mismatch')
    if receipt['budget']!={'root_used':2,'attempt_used':2,'requests':2,'dispatches':2}:raise ValueError('budget_mismatch')
    calls=receipt['inferences']
    if len(calls)!=2 or len({call['nonce'] for call in calls})!=2:raise ValueError('inference_count_mismatch')
    ids=[]
    for call in calls:
        if call['status']!=200 or call['content_type']!='text/event-stream' or not call['cleaned'] or not call['supervisor']['stopped'] or call['supervisor']['cancelled']:
            raise ValueError('unclean_inference_success')
        cleanup=call['cleanup'];ids.extend(full_ids(cleanup['resources']).values())
        if not cleanup['absent'] or cleanup['at']>call['returned_at']:raise ValueError('response_before_cleanup')
    if len(set(ids))!=8 or calls[0]['actual_tool_result'] is not False or calls[1]['actual_tool_result'] is not True:raise ValueError('native_tool_correlation_missing')
    if not receipt['native']['passed'] or len(receipt['native']['isolation_checks'])!=8:raise ValueError('native_or_isolation_failed')
    if receipt['synthetic_token_removed'] is not True:raise ValueError('synthetic_token_remaining')
    expected_denials={'worker_capability_read':13,'worker_socket_connect':13,'relay_journal_read':13,'relay_signal':1,
        'init_signal':1,'relay_environ_read':13,'source_readonly':30,'docker_socket_absent':2}
    if {item['check']:item['errno'] for item in receipt['native']['isolation_checks']}!=expected_denials:
        raise ValueError('isolation_denial_mismatch')
    native=receipt['native'];events=native['events'];terminal=[event for event in events if event.get('type')=='result']
    if (native['cli_exit']!=0 or len(terminal)!=1 or terminal[0].get('exit_code')!=0
            or terminal[0].get('text')!='SYNTHETIC_NATIVE_SUPERVISED_OK'
            or not any(event.get('type')=='tool_use' and event.get('name')=='read_file'
                and event.get('input',{}).get('path')=='/run/tool/fixture.txt' for event in events)
            or not any(event.get('type')=='tool_result' and event.get('name')=='read_file'
                and 'SYNTHETIC_NATIVE_SUPERVISED_FILE' in event.get('output','') and event.get('is_error') is False for event in events)):
        raise ValueError('native_events_missing')
    expected={(call['nonce'],call['payload_digest']) for call in calls}
    if ({(row[0],row[1]) for row in receipt['relay_journal']['rows']}!=expected
            or any(row[2]!='http_write_completed' for row in receipt['relay_journal']['rows'])
            or {(row[0],row[1]) for row in receipt['worker_rows']}!=expected
            or any(row[2]!='cached' for row in receipt['worker_rows'])):raise ValueError('journal_identity_mismatch')
    if calls[0]['binding']!=calls[1]['binding']:raise ValueError('attempt_binding_changed')
    process=native['process'];relay=receipt['relay_process'];boundary=receipt['caller_boundary'];host=boundary['host_config']
    if process['uid']!=1000 or relay['uid']!=1001 or boundary['init_user']!='1002:1002':raise ValueError('uid_boundary_changed')
    for observed in (process,relay):
        if int(observed['status']['CapEff'],16)!=0 or observed['status']['NoNewPrivs']!='1':raise ValueError('privileged_caller')
    members={int(value) for value in process['cgroup']['cgroup.procs'].split()}
    if relay['pid'] not in members or process['hermes_pid'] not in members:raise ValueError('shared_cgroup_not_observed')
    if host['NetworkMode']!='none' or not host['ReadonlyRootfs'] or host['CapDrop']!=['ALL']:raise ValueError('caller_config_changed')
    if (host['NanoCpus']!=1400000000 or host['Memory']!=3154116608 or host['PidsLimit']!=352
            or process['cgroup']['cpu.max'].strip()!='140000 100000'
            or int(process['cgroup']['memory.max'])!=3154116608 or int(process['cgroup']['pids.max'])!=352):raise ValueError('caller_limits_changed')
    mounts=boundary['mounts']
    if len(mounts)!=2 or {m['Destination'] for m in mounts}!={'/proof','/run/worker-inference'} or any(m['RW'] for m in mounts):raise ValueError('caller_mount_changed')
    return True


def validate(receipt,bundle):
    try:return _validate(receipt,bundle)
    except (KeyError,TypeError,AttributeError,IndexError) as exc:raise ValueError('invalid_receipt_shape') from exc


def validate_archive(receipt,bundle,harness):
    validate(receipt,bundle)
    if set(bundle['sources'])!=set(bundle['hashes']):raise ValueError('source_inventory_mismatch')
    if any(hashlib.sha256(value.encode()).hexdigest()!=bundle['hashes'][name] for name,value in bundle['sources'].items()):raise ValueError('source_snapshot_mismatch')
    if hashlib.sha256(harness).hexdigest()!=bundle['harness_sha256']:raise ValueError('archived_harness_mismatch')
    if hashlib.sha256(bundle['caller_script'].encode()).hexdigest()!=bundle['caller_script_sha256']:raise ValueError('caller_helper_mismatch')
    return True


def remote_main(bundle):
    import io,os,socket,tarfile,tempfile,threading,time,uuid
    from dataclasses import asdict
    if socket.gethostname()!='omarchy' or os.geteuid()!=0:raise RuntimeError('wrong_target')
    run_id=bundle['run_id'];folder=Path(tempfile.mkdtemp(prefix='cwb2-'+run_id+'-')).resolve();folder.chmod(0o700)
    receipt={'host':socket.gethostname(),'run_id':run_id,'folder':str(folder),'passed':False,
        'source_hashes':bundle['hashes'],'harness_sha256':bundle['harness_sha256'],'inferences':[],
        'qualification':'synthetic provider CLI/token; actual native Hermes and Docker; no production activation'}
    journal=folder/'qualification.json'
    def save():journal.write_text(json.dumps(receipt,indent=2)+'\n')
    save()
    package=folder/'source'/'cloudworkbench';package.mkdir(parents=True)
    for name,source in bundle['sources'].items():
        if name not in MODULES or hashlib.sha256(source.encode()).hexdigest()!=bundle['hashes'][name]:raise RuntimeError('source_binding_mismatch')
        (package/name).write_text(source)
    sys.path.insert(0,str(package.parent))
    from cloudworkbench.store import Store
    from cloudworkbench.inference_budget import InferenceBudget,RootScope,AttemptScope,ensure_schema
    from cloudworkbench.provider_leases import ProviderLeases
    from cloudworkbench.provider_dispatch import ProviderDispatch
    from cloudworkbench.provider_docker import DockerConfig,ProviderDocker
    from cloudworkbench.provider_executor import ProviderExecutor
    from cloudworkbench.supervised_executor import SupervisedProviderExecutor
    from cloudworkbench.inference_transport import PinnedCLI
    from cloudworkbench.inference_relay import AttemptBinding,WorkerDispatcher,WorkerSocketServer
    from cloudworkbench.profile_binding import canonical_pinned_profile_digest
    config_dir=folder/'docker-cli';config_dir.mkdir(mode=0o700)
    def docker(*args,data=None,timeout=15,check=True):
        result=subprocess.run(['/usr/bin/docker','--host','unix:///var/run/docker.sock','--config',str(config_dir),*args],
            input=data,capture_output=True,timeout=timeout,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
        if check and result.returncode:raise RuntimeError('docker_command_failed:'+str(args[:2]))
        return result
    def inventory(reservation=None):
        selector='label=io.cloudworkbench.provider-reservation='+reservation if reservation else 'label=io.cloudworkbench.qualification='+run_id
        return {'containers':docker('ps','-aq','--no-trunc','--filter',selector).stdout.decode().splitlines(),
            'networks':docker('network','ls','-q','--no-trunc','--filter',selector).stdout.decode().splitlines(),
            'volumes':docker('volume','ls','-q','--filter','label=io.cloudworkbench.qualification='+run_id).stdout.decode().splitlines()}
    def physically_absent(resources):
        full_ids(resources)
        containers=set(docker('ps','-aq','--no-trunc').stdout.decode().splitlines())
        networks=set(docker('network','ls','-q','--no-trunc').stdout.decode().splitlines())
        return not any(identity in (networks if 'network' in key else containers) for key,identity in resources.items())
    def services():
        value=subprocess.run(['systemctl','is-active','cloud-workbench-api','cloud-workbench-worker','cloudd'],capture_output=True,text=True,timeout=5)
        if value.returncode:raise RuntimeError('services_unavailable')
        return value.stdout.splitlines()
    tag=None;volume=None;token=None;reservation=None;uds=None;uds_thread=None;worker=None;caller=None;wrapper=None
    try:
        receipt['services_before']=services()
        available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
        disk=os.statvfs('/var/lib/docker')
        if available<6*1024*1024 or disk.f_bavail*disk.f_frsize<5*1024**3:raise RuntimeError('headroom_unavailable')
        receipt['headroom']={'available_memory_kib':available,'docker_free_bytes':disk.f_bavail*disk.f_frsize}
        candidate=bundle['candidate'];caller_image=candidate['base']
        if docker('image','inspect',candidate['tag'],'--format','{{.Id}}').stdout.decode().strip()!=candidate['image_id']:raise RuntimeError('provider_image_mismatch')
        if docker('image','inspect',caller_image,'--format','{{.Id}}').stdout.decode().strip()!=caller_image:raise RuntimeError('caller_image_mismatch')
        fake=b'''#!/opt/hermes/venv/bin/python
import json,sys,os
request=json.load(sys.stdin)
assert os.environ['CLAUDE_CODE_OAUTH_TOKEN']=='sk-ant-oat-synthetic-native-only'
assert os.getuid()==958 and sys.argv[sys.argv.index('--tools')+1]==''
seen=any(m.get('role')=='tool' and 'SYNTHETIC_NATIVE_SUPERVISED_FILE' in str(m.get('content','')) for m in request['messages'])
if seen:decision={'kind':'final','text':'SYNTHETIC_NATIVE_SUPERVISED_OK','tool_calls':[]}
else:decision={'kind':'tool_calls','text':None,'tool_calls':[{'id':'call_native_supervised_1','name':'read_file','arguments':{'path':'/run/tool/fixture.txt'}}]}
print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':decision}))
'''
        tag='cwb2-native-supervised:'+run_id;context=io.BytesIO()
        with tarfile.open(fileobj=context,mode='w') as archive:
            files={'Dockerfile':('FROM '+candidate['tag']+'\nLABEL io.cloudworkbench.qualification='+run_id+'\nCOPY --chmod=0555 fake-cli /opt/probe/fake-cli\n').encode(),'fake-cli':fake}
            for name,data in files.items():
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o555 if name=='fake-cli' else 0o644;archive.addfile(info,io.BytesIO(data))
        build=docker('build','--pull=false','--network','none','-t',tag,'-',data=context.getvalue(),timeout=75,check=False)
        (folder/'build.log').write_bytes(build.stdout+build.stderr)
        if build.returncode:raise RuntimeError('synthetic_image_build_failed')
        image=docker('image','inspect',tag,'--format','{{.Id}}').stdout.decode().strip();receipt.update(provider_image=image,caller_image=caller_image)
        profile=PinnedCLI(Path('/opt/probe/fake-cli'),hashlib.sha256(fake).hexdigest(),'2.1.274','claude-fable-5-1','high')
        profile_path=folder/'profile.json';value=asdict(profile);value['path']=str(profile.path);profile_path.write_text(json.dumps(value));profile_path.chmod(0o444)
        token=folder/'synthetic-token';token.write_text('sk-ant-oat-synthetic-native-only');os.chown(token,0,959);token.chmod(0o640)
        staging=folder/'staging';staging.mkdir(mode=0o700);digest=canonical_pinned_profile_digest(profile)
        store=Store(folder/'state.db');principal=store.add_client('synthetic-native','SYNTHETIC-'+uuid.uuid4().hex*2,['submit','observe','cancel'],['synthetic-native'])
        attempt=store.create_session(principal,{'project_id':'synthetic-native','agent':'hermes','goal':'actual native synthetic file loop'},'native-proof');claimed=store.claim_next()
        if claimed['attempt_id']!=attempt['attempt_id']:raise RuntimeError('wrong_attempt')
        holder={};cleanups=[]
        runtime=ProviderDocker(DockerConfig(image,profile_path,hashlib.sha256(profile_path.read_bytes()).hexdigest(),digest,token,staging,('example.com',)),
            cancel_check=lambda spec:holder['wrapper'].cancel_check(spec) if holder else True)
        def verify(target):
            result=runtime.cleanup(target)
            for items in target.dispatches:
                row=dict(items);resources={key:row[key] for key in RESOURCE_KEYS}
                if not physically_absent(resources):raise RuntimeError('cleanup_not_physical')
                cleanups.append({'request_id':row['request_id'],'at':time.monotonic(),'resources':resources,'absent':True,'evidence_sha256':result.evidence_sha256})
            return result
        leases=ProviderLeases(store,cleanup_verifier=verify,inspector_id=runtime.config.inspector_id)
        leases.register_account(run_id,legacy_agent='claude',persistent_owner_id=run_id);reservation=leases.reserve(run_id,persistent_owner_id=run_id)
        receipt['reservation']=asdict(reservation);save()
        grant=leases.issue_grant(reservation,attempt_id=attempt['attempt_id'],generation=attempt['generation'])
        with store._connect() as db:row=db.execute('SELECT a.session_id,a.turn_id,s.owner_id,s.project_id FROM attempts a JOIN sessions s ON s.id=a.session_id WHERE a.id=?',(attempt['attempt_id'],)).fetchone()
        root=RootScope(row['owner_id'],row['project_id'],row['session_id'],row['turn_id'],attempt['attempt_id'],attempt['generation']);scope=AttemptScope(root,attempt['attempt_id'],attempt['generation']);budget=InferenceBudget()
        with store._tx() as db:ensure_schema(db);budget.register_root(db,root,admitted_at=time.time());budget.register_attempt(db,scope)
        dispatch=ProviderDispatch(leases,runtime,budget=budget,budget_scope=scope);binding=AttemptBinding(attempt['attempt_id'],attempt['generation'],digest)
        wrapper=SupervisedProviderExecutor(ProviderExecutor(dispatch,reservation=reservation,grant_id=grant,binding=binding,profile=profile,remaining_seconds=lambda:600));holder['wrapper']=wrapper
        def execute_request(context,payload,cancel):
            output=wrapper(context,payload,cancel);returned=time.monotonic()
            with store._connect() as db:row=db.execute('SELECT request_id FROM provider_dispatch WHERE attempt_id=? AND generation=? AND request_nonce=?',(binding.attempt_id,binding.generation,context.request_nonce)).fetchone()
            cleanup=next((record for record in cleanups if row and record['request_id']==row[0]),None)
            receipt['inferences'].append({'nonce':context.request_nonce,'binding':asdict(context.binding),'payload_digest':context.payload_digest,
                'status':output.response.status,'content_type':output.response.content_type,'cleaned':output.outer_cleanup_confirmed,
                'returned_at':returned,'cleanup':cleanup,'supervisor':asdict(wrapper.last_receipt) if wrapper.last_receipt else None,
                'actual_tool_result':any(m.get('role')=='tool' and 'SYNTHETIC_NATIVE_SUPERVISED_FILE' in str(m.get('content','')) for m in payload.get('messages',[]))})
            save();return output
        worker_cap=uuid.uuid4().hex+uuid.uuid4().hex;http_cap=uuid.uuid4().hex+uuid.uuid4().hex
        socket_dir=folder/'worker-socket';socket_dir.mkdir(mode=0o750);os.chown(socket_dir,0,1001)
        (socket_dir/'client.json').write_text(json.dumps({'binding':asdict(binding),'worker_capability':worker_cap,'http_capability':http_cap}));os.chown(socket_dir/'client.json',0,1001);(socket_dir/'client.json').chmod(0o440)
        worker=WorkerDispatcher(journal_path=folder/'worker.db',binding=binding,capability_sha256=hashlib.sha256(worker_cap.encode()).hexdigest(),authorize=wrapper.authorize,execute_request=execute_request)
        uds=WorkerSocketServer(socket_dir/'socket',worker,socket_gid=1001,deadline_seconds=180)
        uds_thread=threading.Thread(target=uds.serve_forever,name='qualification-worker-uds',daemon=False);uds_thread.start()
        volume='cwb2-'+run_id+'-source';docker('volume','create','--label','io.cloudworkbench.qualification='+run_id,volume)
        files={'cloudworkbench/'+name:bundle['sources'][name] for name in ('__init__.py','inference_relay.py','inference_service.py')}
        files['caller.py']=bundle['caller_script'];files['native-config.json']=json.dumps({'model':profile.native_model,'http_capability':http_cap})
        stage="import pathlib\nroot=pathlib.Path('/proof')\nfiles="+repr(files)+"\nfor name,source in files.items():\n p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.parent.chmod(0o755);p.write_text(source);p.chmod(0o644)\n(root/'writable-probe').mkdir();(root/'writable-probe').chmod(0o777)\n"
        prep='cwb2-'+run_id+'-prep'
        docker('run','--rm','--name',prep,'--label','io.cloudworkbench.qualification='+run_id,'--network','none','--read-only','--user','0:0','--cpus','.25','--memory','128m','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--mount','type=volume,src='+volume+',dst=/proof','--entrypoint','/opt/hermes/venv/bin/python','-i',caller_image,'-',data=stage.encode(),timeout=30)
        caller='cwb2-'+run_id+'-caller';init="import signal,time,sys;signal.signal(signal.SIGTERM,lambda *a:sys.exit(0));exec('while True: time.sleep(1)')"
        caller_id=docker('run','-d','--name',caller,'--label','io.cloudworkbench.qualification='+run_id,'--network','none','--read-only','--user','1002:1002',
            '--cpus','1.4','--memory','3008m','--memory-swap','3008m','--pids-limit','352','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777','--tmpfs','/run/relay:rw,nosuid,nodev,noexec,size=32m,uid=1001,gid=1001,mode=0700',
            '--tmpfs','/run/tool:rw,nosuid,nodev,size=128m,uid=1000,gid=1000,mode=0700',
            '--mount','type=volume,src='+volume+',dst=/proof,readonly','--mount','type=bind,src='+str(socket_dir)+',dst=/run/worker-inference,readonly',
            '--entrypoint','/opt/hermes/venv/bin/python',caller_image,'-c',init).stdout.decode().strip()
        receipt['caller_id']=caller_id;save()
        docker('exec','-d','--user','1001:1001','-e','PYTHONPATH=/proof','-e','PYTHONDONTWRITEBYTECODE=1',caller_id,'/opt/hermes/venv/bin/python','/proof/caller.py','relay')
        ready_code="import pathlib,time\np=pathlib.Path('/run/relay/ready.json')\nfor _ in range(100):\n if p.exists():break\n time.sleep(.1)\nprint(p.read_text())"
        ready=json.loads(docker('exec','--user','1001:1001',caller_id,'/opt/hermes/venv/bin/python','-c',ready_code,timeout=15).stdout)
        receipt['relay_process']=ready
        output=docker('exec','--user','1000:1000','-e','PYTHONPATH=/proof:/opt/hermes/source','-e','PYTHONDONTWRITEBYTECODE=1',caller_id,'/opt/hermes/venv/bin/python','/proof/caller.py','tool',str(ready['pid']),timeout=125,check=False)
        (folder/'native.stdout').write_bytes(output.stdout);(folder/'native.stderr').write_bytes(output.stderr)
        receipt['native']=json.loads(output.stdout)
        if output.returncode or not receipt['native']['passed']:raise RuntimeError('native_loop_failed')
        relay_read="import pathlib,json,sqlite3\nr=pathlib.Path('/run/relay');assert not (r/'fenced.json').exists();db=sqlite3.connect('file:/run/relay/relay.db?mode=ro',uri=True);print(json.dumps({'rows':db.execute('SELECT nonce,payload_digest,state FROM requests').fetchall(),'journal_mode':oct((r/'relay.db').stat().st_mode&0o777)}))"
        receipt['relay_journal']=json.loads(docker('exec','--user','1001:1001',caller_id,'/opt/hermes/venv/bin/python','-c',relay_read).stdout)
        receipt['worker_rows']=worker.journal.db.execute('SELECT nonce,payload_digest,state FROM requests').fetchall()
        metadata=json.loads(docker('inspect',caller_id).stdout)[0]
        receipt['caller_boundary']={'id':metadata['Id'],'mounts':metadata['Mounts'],'host_config':metadata['HostConfig'],'init_user':metadata['Config']['User']}
        if metadata['Id']!=caller_id or any(m.get('Source')=='/var/run/docker.sock' for m in metadata['Mounts']):raise RuntimeError('caller_boundary_mismatch')
        with store._connect() as db:
            receipt['budget']={'root_used':db.execute('SELECT SUM(used) FROM inference_budget_roots').fetchone()[0],
                'attempt_used':db.execute('SELECT SUM(used) FROM inference_budget_attempts').fetchone()[0],
                'requests':db.execute('SELECT COUNT(*) FROM inference_budget_requests').fetchone()[0],
                'dispatches':db.execute('SELECT COUNT(*) FROM provider_dispatch').fetchone()[0]}
        if any(thread.name=='cwb-cancel-supervisor' for thread in threading.enumerate()):raise RuntimeError('supervisor_left_running')
        receipt['passed']=True
    except Exception as exc:
        receipt['error']={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc)[:500])}
    finally:
        actions=[];remaining=[]
        if uds is not None:
            try:uds.shutdown();uds.server_close();uds_thread.join(2)
            except Exception as exc:actions.append({'error':'uds_shutdown:'+type(exc).__name__})
            if uds_thread.is_alive():actions.append({'error':'uds_thread_alive'})
        if worker is not None:
            try:worker.close()
            except Exception as exc:actions.append({'error':'worker_close:'+type(exc).__name__})
        for reservation_id in ([reservation.reservation_id] if reservation else [])+[None]:
            try:
                owned=inventory(reservation_id)
                for kind in ('containers','networks'):
                    for identity in owned[kind]:
                        obj=json.loads(docker(*(('inspect',identity) if kind=='containers' else ('network','inspect',identity))).stdout)[0]
                        labels=obj['Config']['Labels'] if kind=='containers' else obj['Labels']
                        key='io.cloudworkbench.provider-reservation' if reservation_id else 'io.cloudworkbench.qualification'
                        if labels.get(key)!=(reservation_id or run_id):raise RuntimeError('cleanup_owner_mismatch')
                        removed=docker(*(('rm','-f',identity) if kind=='containers' else ('network','rm',identity)),check=False)
                        actions.append({'kind':kind,'id':identity,'exit':removed.returncode,'fallback_provider_cleanup':reservation_id is not None})
                if reservation_id is None:
                    for name in owned['volumes']:
                        obj=json.loads(docker('volume','inspect',name).stdout)[0]
                        if obj['Labels'].get('io.cloudworkbench.qualification')!=run_id:raise RuntimeError('volume_owner_mismatch')
                        removed=docker('volume','rm',name,check=False);actions.append({'kind':'volume','name':name,'exit':removed.returncode})
                final=inventory(reservation_id);remaining+=final['containers']+final['networks']+(final['volumes'] if reservation_id is None else [])
            except Exception as exc:actions.append({'error':type(exc).__name__});remaining.append('unconfirmed')
        if token is not None:
            try:token.unlink();receipt['synthetic_token_removed']=True
            except Exception:receipt['synthetic_token_removed']=False;remaining.append('synthetic_token')
        if tag is not None:
            try:
                obj=json.loads(docker('image','inspect',tag).stdout)[0]
                if obj['Config']['Labels'].get('io.cloudworkbench.qualification')!=run_id:raise RuntimeError('image_owner_mismatch')
                removed=docker('image','rm',tag,check=False);actions.append({'kind':'image','id':obj['Id'],'exit':removed.returncode})
            except Exception as exc:actions.append({'error':'image_cleanup:'+type(exc).__name__})
        try:receipt['services_after']=services()
        except Exception:receipt['services_after']=['unconfirmed']
        receipt['cleanup']={'actions':actions,'remaining':remaining,'complete':not remaining and all('error' not in action and action.get('exit')==0 for action in actions)}
        if not receipt['cleanup']['complete'] or receipt.get('services_before')!=receipt['services_after']:receipt['passed']=False
        save();print(json.dumps(receipt))
    return receipt


def local_main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');parser.add_argument('--expected-budget-authority',required=True)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    sources={name:(root/'src/cloudworkbench'/name).read_text() for name in MODULES}
    hashes={name:hashlib.sha256(source.encode()).hexdigest() for name,source in sources.items()}
    if hashes['budget_authority.py']!=args.expected_budget_authority:raise ValueError('budget_authority_not_frozen')
    import uuid
    script=Path(__file__).read_text();caller=(root/'scripts/qualify-native-supervised-caller.py').read_text()
    bundle={'run_id':'nativequal-'+uuid.uuid4().hex,'sources':sources,'hashes':hashes,'harness_sha256':hashlib.sha256(script.encode()).hexdigest(),
        'caller_script':caller,'caller_script_sha256':hashlib.sha256(caller.encode()).hexdigest(),
        'candidate':json.loads((root/'evidence/provider-image-candidate.json').read_text())}
    base=root/'evidence'/('native-supervised-linux-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    base.with_suffix('.source-snapshot.json').write_text(json.dumps(bundle,indent=2)+'\n');base.with_suffix('.harness.py').write_text(script)
    if not args.execute:
        print(json.dumps({'prepared':str(base),'executed':False}));return
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','thomas@100.83.74.92',
        'sudo -n /opt/cloud-workbench/.venv/bin/python - --remote'],input='QUALIFICATION_BUNDLE='+repr(bundle)+'\n'+script,capture_output=True,text=True,timeout=320)
    base.with_suffix('.stderr.txt').write_text(result.stderr);base.with_suffix('.json' if result.stdout.strip() else '.failed.txt').write_text(result.stdout)
    if result.returncode:raise RuntimeError('remote_exit_'+str(result.returncode))
    receipt=json.loads(result.stdout)
    print(json.dumps({'receipt':str(base.relative_to(root))+'.json','passed':receipt.get('passed'),'error':receipt.get('error'),'cleanup':receipt.get('cleanup')},indent=2))
    validate_archive(receipt,bundle,script.encode())


if __name__=='__main__':
    if sys.argv[1:]==['--remote']:remote_main(QUALIFICATION_BUNDLE)
    else:local_main()
