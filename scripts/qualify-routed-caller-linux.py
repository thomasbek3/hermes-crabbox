#!/usr/bin/env python3
"""Prepare/validate offline. --remote is an explicit, separately approved Linux run.

Actual Hermes + separate UID relay; synthetic host responses only, no provider auth.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

MODULES = ('__init__.py','runtime.py','routed_runtime.py','routed_caller.py','hermes_adapter.py',
    'pstack_routing.py','workflow_instructions.py','workflow_routing.py','native_responses.py',
    'inference_transport.py','inference_relay.py','inference_service.py','provider_protocol.py',
    'profile_binding.py','hermes_inference_protocol.py','adapters.py')
PROFILES = (('openai-codex','gpt-6-astra','high'),('openai-codex','gpt-5.6-sol','max'),('xai-oauth','grok-4.6','xhigh'))
SOURCE = ('__init__.py','routed_caller.py','hermes_adapter.py','pstack_routing.py','inference_relay.py','inference_service.py','adapters.py')
MARKER = 'SYNTHETIC_ROUTED_CALLER_FILE'
FINAL = 'SYNTHETIC_ROUTED_CALLER_OK'
SERVICES = ('cloud-workbench-api','cloud-workbench-worker','cloudd')


def sha(data):return hashlib.sha256(data).hexdigest()
def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()


def validate_bundle(bundle, harness):
    if (set(bundle)!={'version','run_id','image','profiles','sources','hashes','harness_sha256','launch_receipts'}
            or bundle['version']!=1 or not re.fullmatch('routed-[0-9a-f]{12}',bundle['run_id'])
            or not re.fullmatch('sha256:[0-9a-f]{64}',bundle['image'])
            or set(bundle['sources'])!=set(MODULES) or set(bundle['hashes'])!=set(MODULES)
            or bundle['profiles'] not in ([list(PROFILES[0])],[list(p) for p in PROFILES])
            or len(bundle['launch_receipts'])!=len(bundle['profiles'])
            or any(not isinstance(r,str) for r in bundle['launch_receipts'])
            or sha(harness)!=bundle['harness_sha256']
            or any(sha(text.encode())!=bundle['hashes'][name] for name,text in bundle['sources'].items())):
        raise ValueError('qualification_bundle_mismatch')


def validate_receipt(receipt,bundle):
    if (receipt.get('passed') is not True or receipt.get('run_id')!=bundle['run_id']
            or receipt.get('image')!=bundle['image'] or receipt.get('host')!='omarchy'
            or receipt.get('source_hashes')!=bundle['hashes'] or receipt.get('harness_sha256')!=bundle['harness_sha256']
            or receipt.get('real_provider_calls') is not False or receipt.get('real_credentials_used') is not False
            or receipt.get('services_before')!=receipt.get('services_after')
            or receipt.get('remaining')!=[] or not receipt.get('cleanup_complete')
            or len(receipt.get('cases',[]))!=len(bundle['profiles'])):
        raise ValueError('incomplete_routed_caller_receipt')
    for key in ('services_before','services_after'):
        observed=receipt.get(key,{})
        if set(observed)!=set(SERVICES) or any(v.get('ActiveState')!='active' or not str(v.get('MainPID','')).isdigit() or int(v['MainPID'])<1 for v in observed.values()):raise ValueError('service_readback_missing')
    for key in ('headroom_before','headroom_after'):
        if any(type(receipt.get(key,{}).get(field)) is not int or receipt[key][field]<0 for field in ('memory_available_kib','docker_available_bytes')):raise ValueError('headroom_readback_missing')
    if receipt['headroom_before']['memory_available_kib']<6*1024**2 or receipt['headroom_before']['docker_available_bytes']<5*1024**3:raise ValueError('insufficient_initial_headroom')
    if datetime.datetime.fromisoformat(receipt['finished_at'])<datetime.datetime.fromisoformat(receipt['started_at']):raise ValueError('invalid_receipt_time')
    ids=[]
    for ordinal,(case,profile,expected_launch) in enumerate(zip(receipt['cases'],bundle['profiles'],bundle['launch_receipts'])):
        launch=json.loads(expected_launch)
        expected_binding={'attempt_id':bundle['run_id']+'-'+str(ordinal),'generation':1,'profile_digest':launch['profile_digest']}
        if (case.get('binding')!=expected_binding or launch['model']!=profile[1] or launch['effort']!=profile[2]
                or launch['provider']!=profile[0] or case.get('launch_receipt_json')!=expected_launch
                or case.get('result',{}).get('launch_receipt_sha256')!=sha(expected_launch.encode())):
            raise ValueError('launch_profile_binding_mismatch')
        if any(r.get('model')!=profile[1] or r.get('effort')!=profile[2] or r.get('binding')!=expected_binding
               or not re.fullmatch('[0-9a-f]{64}',r.get('payload_sha256','')) for r in case.get('requests',[])):
            raise ValueError('admitted_request_binding_mismatch')
        if (case.get('profile')!=profile or case.get('passed') is not True
                or case.get('create_state')!='created' or case.get('durable_binding_before_start') is not True
                or case.get('caller_state_before_cleanup')!='running'
                or case.get('result',{}).get('status')!='completed'
                or case.get('result',{}).get('verification_pass') is not False
                or case.get('result',{}).get('outer_cleanup_required') is not True
                or len(case.get('requests',[]))!=2 or case['requests'][0]['tool_result_seen'] is not False
                or case['requests'][1]['tool_result_seen'] is not True
                or len({r['nonce'] for r in case['requests']})!=2
                or case.get('worker_states')!=['cached','cached']
                or case.get('removed') is not True):
            raise ValueError('routed_case_incomplete')
        stop=case.get('stop_receipt',{});removed=case.get('remove_receipt',{})
        if (stop.get('caller_stopped') is not True or removed.get('caller_removed') is not True
                or any(r.get('runtime_id')!=case['runtime_id'] or r.get('attempt_id')!=expected_binding['attempt_id']
                       or r.get('generation')!=1 or r.get('provider_cleanup_qualified') is not False for r in (stop,removed))):
            raise ValueError('caller_cleanup_receipt_missing')
        if case.get('denials')!={'workspace_write':30,'worker_config_read':13,'worker_socket_connect':13,'relay_state_read':13,'init_signal':1,'source_write':30,'docker_socket':2}:
            raise ValueError('caller_permission_proof_missing')
        process=case['process']
        if process['uid']!=1000 or process['gid']!=1000 or process['no_new_privs']!='1' or int(process['cap_eff'],16)!=0:
            raise ValueError('caller_process_identity_mismatch')
        if (process['cpu_max'].strip()!='140000 100000' or int(process['memory_max'])!=3008*1024**2
                or int(process['pids_max'])!=352):raise ValueError('caller_cgroup_limits_mismatch')
        if case['relay_ready']['uid']!=1001 or case['relay_ready']['binding']!=case['binding']:raise ValueError('relay_binding_mismatch')
        if not re.fullmatch('[0-9a-f]{64}',case['runtime_id']):raise ValueError('caller_id_mismatch')
        ids.append(case['runtime_id'])
        events=case['events']
        if not any(e.get('type')=='tool.started' and e.get('payload',{}).get('name')=='read_file' for e in events):raise ValueError('native_tool_execution_missing')
        if not any(e.get('type')=='adapter.result' and e.get('payload',{}).get('summary')==FINAL for e in events):raise ValueError('native_final_missing')
    if len(set(ids))!=len(ids):raise ValueError('caller_reused')
    return True


def synthetic_sse(profile,ordinal):
    if ordinal==1:
        items=[{'type':'function_call','id':'fc_routed_fixture','call_id':'call_routed_fixture','name':'read_file',
                'arguments':'{"path":"/workspace/fixture.txt"}','status':'completed'}]
    else:
        items=[{'type':'message','id':'msg_routed_fixture','role':'assistant','status':'completed',
                'content':[{'type':'output_text','text':FINAL,'annotations':[]}]}]
    events=[{'type':'response.output_item.done','output_index':index,'item':item} for index,item in enumerate(items)]
    events.append({'type':'response.completed','response':{'id':'resp_routed_'+str(ordinal),'status':'completed','model':profile[1],'output':items,'usage':None}})
    return b''.join(b'event: '+e['type'].encode()+b'\ndata: '+json.dumps(e).encode()+b'\n\n' for e in events)


def safe_diagnostic(exc):
    allowed={'routed container policy mismatch','routed tmpfs policy mismatch','routed mounts changed',
        'routed working directory changed','routed log policy mismatch','invalid routed image build metadata',
        'new routed caller is not stopped','routed caller start unconfirmed','qualification_docker_failure',
        'caller_json_not_complete_before_deadline','synthetic_tool_sequence_changed',
        'routed caller stop unconfirmed','routed caller removal unconfirmed'}
    message=str(exc)
    return {'type':type(exc).__name__,'code':message if message in allowed else 'unclassified_qualification_failure'}


def safe_inspection(obj):
    config=obj.get('Config',{});host=obj.get('HostConfig',{})
    labels=config.get('Labels',{}) or {}
    return {'Id':obj.get('Id'),'Image':obj.get('Image'),'Name':obj.get('Name'),
        'State':{k:obj.get('State',{}).get(k) for k in ('Status','Running','ExitCode','OOMKilled')},
        'Config':{'User':config.get('User'),'WorkingDir':config.get('WorkingDir'),
                  'LabelKeys':sorted(labels),'Labels':{k:v for k,v in labels.items() if k in
                      ('io.cloudworkbench.owner','io.cloudworkbench.attempt','io.cloudworkbench.generation',
                       'io.cloudworkbench.role','io.cloudworkbench.caller-spec','io.cloudworkbench.build-owner','io.cloudworkbench.candidate')}},
        'HostConfig':{k:host.get(k) for k in ('NetworkMode','ReadonlyRootfs','Privileged','CapDrop','CapAdd',
                    'SecurityOpt','IpcMode','PidMode','Dns','RestartPolicy','Memory','MemorySwap','PidsLimit','NanoCpus','Tmpfs','LogConfig')},
        'Mounts':[{k:m.get(k) for k in ('Type','Source','Destination','RW')} for m in obj.get('Mounts',[])],
        'Networks':sorted(obj.get('NetworkSettings',{}).get('Networks',{}))}


def expected_plan(identity):
    from cloudworkbench.hermes_adapter import build_routed_launch
    from cloudworkbench.native_responses import NativeProfile
    from cloudworkbench.workflow_instructions import StageInstructions,STAGE_BOUNDARY
    text='Inspect the supplied file. Do not modify workspace or delegate.'
    instructions=StageInstructions('review','plan_review','synthetic-stage.md',sha(text.encode()),text,STAGE_BOUNDARY)
    return build_routed_launch(NativeProfile(*identity),instructions,
        task='Read /workspace/fixture.txt using read_file, then report '+FINAL+'.',
        input_revision_sha256=sha((MARKER+'\n').encode()),workspace_readonly=True,max_turns=3,run_budget_seconds=90)


def wait_json(read, deadline, *, clock, sleep):
    # Presence is not atomic publication in the frozen caller; retry only parse-incomplete data.
    while clock()<deadline:
        value=read()
        if value['present']:
            try:return json.loads(value['data'])
            except (ValueError,UnicodeDecodeError):pass
        sleep(.1)
    raise RuntimeError('caller_json_not_complete_before_deadline')


def prepare(output,image,all_profiles):
    import os,uuid
    root=Path(__file__).resolve().parents[1]
    output=Path(output).resolve();output.mkdir(mode=0o700,parents=False,exist_ok=False)
    body=Path(__file__).read_bytes()
    sources={name:(root/'src/cloudworkbench'/name).read_text() for name in MODULES}
    bundle={'version':1,'run_id':'routed-'+uuid.uuid4().hex[:12],'image':image,
            'profiles':[list(p) for p in (PROFILES if all_profiles else PROFILES[:1])],
            'launch_receipts':[expected_plan(p).receipt_json for p in (PROFILES if all_profiles else PROFILES[:1])],
            'sources':sources,'hashes':{name:sha(text.encode()) for name,text in sources.items()},'harness_sha256':sha(body)}
    validate_bundle(bundle,body)
    (output/'bundle.json').write_text(json.dumps(bundle,indent=2)+'\n');(output/'bundle.json').chmod(0o600)
    (output/'harness.py').write_bytes(body);(output/'harness.py').chmod(0o600)
    print(json.dumps({'prepared_only':True,'folder':str(output),'run_id':bundle['run_id'],'image':image,'bundle_sha256':sha((output/'bundle.json').read_bytes())}))


def remote(bundle_path,receipt_path):
    import os,socket,subprocess,tempfile,threading,time,secrets
    from dataclasses import asdict
    bundle=json.loads(Path(bundle_path).read_text());validate_bundle(bundle,Path(__file__).read_bytes())
    if socket.gethostname()!='omarchy' or os.geteuid()!=0:raise RuntimeError('wrong_qualification_host')
    folder=Path(tempfile.mkdtemp(prefix='cwrc-'+bundle['run_id'][-12:]+'-')).resolve();folder.chmod(0o700)
    receipt={'run_id':bundle['run_id'],'image':bundle['image'],'host':socket.gethostname(),'started_at':stamp(),'source_hashes':bundle['hashes'],
             'harness_sha256':bundle['harness_sha256'],'folder':str(folder),'passed':False,'cases':[],
             'real_provider_calls':False,'real_credentials_used':False,'scheduler_binding_qualified':False,'filesystem_quota_qualified':False,
             'scope':'actual routed caller with synthetic host responses, no provider containers or authority'}
    receipt_path=Path(receipt_path)
    if receipt_path.exists():raise RuntimeError('refusing_existing_receipt')
    def save():receipt_path.write_text(json.dumps(receipt,indent=2)+'\n');receipt_path.chmod(0o600)
    save()
    package=folder/'host-source'/'cloudworkbench';package.mkdir(parents=True)
    for name,text in bundle['sources'].items():(package/name).write_text(text)
    sys.path.insert(0,str(package.parent))
    from cloudworkbench.runtime import Runtime
    from cloudworkbench.routed_runtime import RoutedRuntime
    from cloudworkbench.hermes_adapter import build_routed_launch
    from cloudworkbench.native_responses import NativeProfile,validate_request
    from cloudworkbench.workflow_instructions import StageInstructions,STAGE_BOUNDARY
    from cloudworkbench.inference_relay import AttemptBinding,WorkerDispatcher,WorkerSocketServer,DispatchResult
    from cloudworkbench.inference_service import ServiceResponse
    docker_config=folder/'docker-config';docker_config.mkdir(mode=0o700)
    def command(args,timeout=15):
        proc=subprocess.run(['/usr/bin/docker','--host','unix:///var/run/docker.sock','--config',str(docker_config),*args],
            capture_output=True,text=True,timeout=min(timeout,30),env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
        if len(proc.stdout)+len(proc.stderr)>2*1024**2:raise RuntimeError('qualification_command_output_limit')
        if proc.returncode:raise RuntimeError('qualification_docker_failure')
        return proc.stdout.strip()
    def services():
        result={}
        for unit in SERVICES:
            proc=subprocess.run(['systemctl','show',unit,'--property=ActiveState,MainPID'],capture_output=True,text=True,timeout=5,check=True)
            result[unit]=dict(line.split('=',1) for line in proc.stdout.splitlines())
            if result[unit]['ActiveState']!='active':raise RuntimeError('qualification_service_unavailable')
        return result
    def headroom():
        available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
        disk=os.statvfs('/var/lib/docker')
        return {'memory_available_kib':available,'docker_available_bytes':disk.f_bavail*disk.f_frsize}
    def inventory():return command(['container','ls','--all','--no-trunc','--filter','label=io.cloudworkbench.owner='+bundle['run_id'],'--format','{{.ID}}']).splitlines()
    def write(path,body,gid=1000,mode=0o440):
        path.write_text(body);os.chown(path,0,gid);path.chmod(mode)
    try:
        receipt['services_before']=services();receipt['headroom_before']=headroom()
        if receipt['headroom_before']['memory_available_kib']<6*1024**2 or receipt['headroom_before']['docker_available_bytes']<5*1024**3:raise RuntimeError('qualification_headroom_unavailable')
        if command(['image','inspect',bundle['image'],'--format','{{.Id}}'])!=bundle['image']:raise RuntimeError('qualification_image_changed')
        if inventory():raise RuntimeError('qualification_owner_collision')
        for ordinal,identity in enumerate(bundle['profiles']):
            case={'profile':identity,'passed':False,'requests':[],'removed':False};receipt['cases'].append(case);save()
            case_root=folder/str(ordinal);case_root.mkdir(mode=0o700)
            task=case_root/'task';task.mkdir(mode=0o751);os.chown(task,0,1000)
            source=task/'source'/'cloudworkbench';source.mkdir(parents=True)
            for directory in (source.parent,source):os.chown(directory,0,1000);directory.chmod(0o555)
            for name in SOURCE:write(source/name,bundle['sources'][name],mode=0o444)
            workspace=case_root/'workspace';workspace.mkdir(mode=0o770);os.chown(workspace,0,1000)
            write(workspace/'fixture.txt',MARKER+'\n')
            scratch=case_root/'scratch';scratch.mkdir(mode=0o770);os.chown(scratch,1000,1000)
            worker_dir=case_root/'worker';worker_dir.mkdir(mode=0o750);os.chown(worker_dir,0,1001)
            profile=NativeProfile(*identity);attempt=bundle['run_id']+'-'+str(ordinal)
            binding=AttemptBinding(attempt,1,profile.digest);case['binding']=asdict(binding)
            plan=expected_plan(identity)
            if plan.receipt_json!=bundle['launch_receipts'][ordinal]:raise RuntimeError('frozen_launch_changed')
            case['launch_receipt_json']=plan.receipt_json
            write(task/'prompt.txt',plan.prompt);write(task/'config.yaml',plan.config_json);write(task/'launch.json',json.dumps(asdict(plan)))
            http_cap=secrets.token_hex(32);worker_cap=secrets.token_hex(32);write(task/'http-capability',http_cap)
            write(worker_dir/'client.json',json.dumps({'binding':asdict(binding),'worker_capability':worker_cap,'http_capability':http_cap,'request_path':plan.request_path,'port':9876}),gid=1001)
            def execute(context,payload,cancel):
                validate_request(profile,payload)
                seen=any(item.get('type')=='function_call_output' and item.get('call_id')=='call_routed_fixture' and MARKER in str(item.get('output','')) for item in payload.get('input',[]) if isinstance(item,dict))
                number=len(case['requests'])+1
                if number>2 or seen!=(number==2):raise RuntimeError('synthetic_tool_sequence_changed')
                case['requests'].append({'nonce':context.request_nonce,'payload_sha256':context.payload_digest,'tool_result_seen':seen,'model':payload['model'],'effort':payload['reasoning']['effort'],'binding':asdict(context.binding)})
                return DispatchResult(ServiceResponse(200,'text/event-stream',synthetic_sse(identity,number)),True)
            worker=WorkerDispatcher(journal_path=case_root/'worker.db',binding=binding,capability_sha256=sha(worker_cap.encode()),authorize=lambda b:b==binding,execute_request=execute,max_requests=2)
            uds=WorkerSocketServer(worker_dir/'socket',worker,socket_gid=1001,deadline_seconds=100)
            thread=threading.Thread(target=uds.serve_forever,daemon=False)
            runtime=Runtime({'root':case_root,'image':bundle['image'],'owner':bundle['run_id'],'cpus':1.4,'memory_mib':3008,'pids':352,
                             'approved_mount_roots':[case_root],'approved_writable_mount_roots':[case_root]})
            runtime._run=command
            routed=RoutedRuntime(runtime,journal_root=case_root/'journal');runtime_id=None;spec=None
            try:
                case['phase']='prepare';thread.start()
                spec=routed.prepare_caller(attempt,attempt,generation=1,plan=plan,workspace=workspace,scratch=scratch,task_dir=task,worker_socket_dir=worker_dir)
                case['phase']='create';save()
                runtime_id=routed.create_caller(spec);case['runtime_id']=runtime_id
                case['create_state']=routed.inspect_caller(spec,runtime_id)['Status']
                durable=case_root/'controller-binding.json'
                with durable.open('x') as out:json.dump({'runtime_id':runtime_id,'attempt_id':attempt,'generation':1,'spec_digest':spec.digest},out);out.flush();os.fsync(out.fileno())
                case['durable_binding_before_start']=True;save()
                case['phase']='start_relay';save()
                routed.start_caller(spec,runtime_id);routed.start_caller_process(spec,runtime_id,role='relay')
                case['relay_ready']=wait_json(lambda:routed.read_caller_file(spec,runtime_id,name='relay-ready',max_bytes=4096),time.monotonic()+12,clock=time.monotonic,sleep=time.sleep)
                if case['relay_ready']['binding']!=asdict(binding):raise RuntimeError('relay_binding_changed')
                case['phase']='permission_probe';save()
                probe="""import os,json,signal,socket, pathlib
checks={}
def deny(name,fn):
 try:fn()
 except OSError as e:checks[name]=e.errno
 else:raise RuntimeError(name+'_allowed')
deny('workspace_write',lambda:pathlib.Path('/workspace/forbidden').write_text('x'))
deny('worker_config_read',lambda:pathlib.Path('/run/worker-inference/client.json').read_bytes())
def connect():
 with socket.socket(socket.AF_UNIX) as s:s.connect('/run/worker-inference/socket')
deny('worker_socket_connect',connect)
deny('relay_state_read',lambda:pathlib.Path('/run/relay/relay.db').read_bytes())
deny('init_signal',lambda:os.kill(1,signal.SIGTERM))
deny('source_write',lambda:pathlib.Path('/run/task/new-file').write_text('x'))
deny('docker_socket',lambda:pathlib.Path('/var/run/docker.sock').stat())
status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
print(json.dumps({'denials':checks,'process':{'uid':os.getuid(),'gid':os.getgid(),'no_new_privs':status['NoNewPrivs'].strip(),'cap_eff':status['CapEff'].strip(),'cpu_max':pathlib.Path('/sys/fs/cgroup/cpu.max').read_text(),'memory_max':pathlib.Path('/sys/fs/cgroup/memory.max').read_text(),'pids_max':pathlib.Path('/sys/fs/cgroup/pids.max').read_text()}}))
"""
                case.update(json.loads(command(['exec','--user','1000:1000',runtime_id,'/opt/hermes/venv/bin/python','-I','-c',probe])))
                case['phase']='hermes_execution';save()
                routed.start_caller_process(spec,runtime_id,role='hermes')
                case['result']=wait_json(lambda:routed.read_caller_file(spec,runtime_id,name='result'),time.monotonic()+110,clock=time.monotonic,sleep=time.sleep)
                case['phase']='collect';save()
                events=bytearray();offset=0;inode=None
                while True:
                    value=routed.read_caller_file(spec,runtime_id,name='events',offset=offset)
                    if not value['present']:raise RuntimeError('caller_events_missing')
                    if inode is not None and value['inode']!=inode:raise RuntimeError('caller_events_replaced')
                    inode=value['inode'];events.extend(value['data']);offset=value['next_offset']
                    if len(events)>1024**2:raise RuntimeError('qualification_event_limit')
                    if offset==value['size']:break
                case['events']=[json.loads(line) for line in events.splitlines()];case['events_sha256']=sha(events)
                case['worker_states']=[row[0] for row in worker.journal.db.execute('SELECT state FROM requests ORDER BY rowid')]
                case['caller_state_before_cleanup']=routed.inspect_caller(spec,runtime_id)['Status']
                case['passed']=True
            except Exception as exc:
                case['diagnostic']=safe_diagnostic(exc);save();raise
            finally:
                cleanup_error=None
                try:
                    # Exact run namespace only; inspect every object before stop/remove.
                    for found in inventory():
                        obj=json.loads(command(['inspect',found]))[0]
                        if obj['Config']['Labels'].get('io.cloudworkbench.owner')!=bundle['run_id'] or obj['Config']['Labels'].get('io.cloudworkbench.attempt')!=attempt:raise RuntimeError('cleanup_identity_changed')
                        case['inspection_before_cleanup']=safe_inspection(obj)
                        if spec is not None and runtime_id==found:
                            began=time.monotonic()
                            case['stop_receipt']=routed.stop_caller(spec,found)
                            case['remove_receipt']=routed.remove_caller(spec,found)
                            case['cleanup_seconds']=round(time.monotonic()-began,3)
                            case['removed']=found not in inventory()
                            continue
                        command(['stop','--time','3',found],timeout=10)
                        stopped=json.loads(command(['inspect',found]))[0]['State']
                        if stopped['Running']:raise RuntimeError('caller_stop_unconfirmed')
                        command(['rm',found]);case['removed']=found not in inventory()
                except Exception as exc:cleanup_error=safe_diagnostic(exc)
                finally:
                    uds.shutdown();uds.server_close();thread.join(3);worker.close()
                    if thread.is_alive():cleanup_error='worker_thread_not_stopped'
                    if cleanup_error:case['cleanup_error']=cleanup_error
                    save()
                if cleanup_error:raise RuntimeError('caller_cleanup_unconfirmed')
        receipt['passed']=True
    except Exception as exc:
        receipt['error']=safe_diagnostic(exc)
    finally:
        try:
            receipt['remaining']=inventory();receipt['cleanup_complete']=not receipt['remaining']
            receipt['services_after']=services();receipt['headroom_after']=headroom()
        except Exception:
            receipt['passed']=False;receipt['cleanup_complete']=False;receipt['readback_error']='qualification_readback_failed'
        receipt['finished_at']=stamp();save()
    try:validate_receipt(receipt,bundle)
    except Exception:
        receipt['passed']=False;receipt['validation_error']='qualification_receipt_incomplete';save();raise

    print(json.dumps({'receipt':str(receipt_path),'passed':True,'profiles':len(receipt['cases']),'remaining':receipt['remaining']}))


def main():
    parser=argparse.ArgumentParser();modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--prepare',type=Path);modes.add_argument('--remote',type=Path);modes.add_argument('--validate',type=Path)
    parser.add_argument('--image');parser.add_argument('--all-profiles',action='store_true');parser.add_argument('--bundle',type=Path);parser.add_argument('--receipt',type=Path)
    args=parser.parse_args()
    if args.prepare:prepare(args.prepare,args.image,args.all_profiles)
    elif args.remote:
        if not args.receipt:parser.error('--remote requires a fresh --receipt path')
        remote(args.remote,args.receipt)
    else:
        if not args.bundle:parser.error('--validate requires --bundle')
        bundle=json.loads(args.bundle.read_text());validate_bundle(bundle,Path(__file__).read_bytes())
        validate_receipt(json.loads(args.validate.read_text()),bundle);print('receipt valid')


if __name__=='__main__':main()
