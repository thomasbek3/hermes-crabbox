#!/usr/bin/env python3
"""Isolated adapter proof. Synthetic token/CLI only; no live service/schema changes."""
from pathlib import Path
import datetime,hashlib,json,subprocess
root=Path(__file__).resolve().parents[1]
candidate=json.loads((root/'evidence/provider-image-candidate.json').read_text())
files=['__init__.py','models.py','provider_leases.py','provider_dispatch.py','provider_docker.py','egress.py','profile_binding.py','inference_transport.py','inference_budget.py']
sources={name:(root/'src/cloudworkbench'/name).read_text() for name in files}
program=r'''
import pathlib,tempfile,subprocess,os,json,hashlib,socket,datetime,time,uuid,sys,io,tarfile,shlex
assert socket.gethostname()=='omarchy'
owner='adapterqual-'+uuid.uuid4().hex
folder=pathlib.Path(tempfile.mkdtemp(prefix='cwb2-'+owner+'-')).resolve()
package=folder/'source'/'cloudworkbench';package.mkdir(parents=True)
for name,source in SOURCES.items():(package/name).write_text(source)
sys.path.insert(0,str(package.parent))
from cloudworkbench.provider_docker import DockerConfig,ProviderDocker,DockerError
from cloudworkbench.provider_dispatch import DispatchSpec
from cloudworkbench.provider_leases import Reservation,RequestLease,CleanupTarget
service_names=['cloud-workbench-api','cloud-workbench-worker','cloudd']
before=subprocess.check_output(['systemctl','is-active',*service_names],text=True).splitlines()
assert before==['active']*3
base_id=subprocess.check_output(['docker','image','inspect',CANDIDATE['tag'],'--format','{{.Id}}'],text=True).strip()
assert base_id==CANDIDATE['image_id']
cli=b"#!/opt/hermes/venv/bin/python\nimport sys,json,os\nrequest=sys.stdin.read()\nif 'wait-for-cancel' in request:\n import time\n time.sleep(20)\nassert os.environ['CLAUDE_CODE_OAUTH_TOKEN']=='sk-ant-oat-synthetic-adapter-only'\nassert os.environ['HTTPS_PROXY'].startswith('http://172.')\nassert sys.argv[sys.argv.index('--tools')+1]==''\nassert sys.argv[sys.argv.index('--disallowedTools')+1]=='*'\nassert os.getuid()==958 and os.getgid()==959\nimport socket,urllib.parse\nfor destination in [('100.83.74.92',7777),('1.1.1.1',443)]:\n try: stream=socket.create_connection(destination,.5)\n except OSError: pass\n else: stream.close();raise AssertionError('direct egress unexpectedly allowed')\nproxy=urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])\nfor host,status in [('127.0.0.1',b'403'),('example.com',b'200')]:\n with socket.create_connection((proxy.hostname,proxy.port),3) as stream:\n  stream.settimeout(12)\n  stream.sendall(('CONNECT '+host+':443 HTTP/1.1\\r\\nHost: '+host+':443\\r\\n\\r\\n').encode())\n  assert stream.recv(1024).split(b' ')[1]==status\nprint(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':{'kind':'final','text':'synthetic adapter okay','tool_calls':[]}}))\n"
context=io.BytesIO()
with tarfile.open(fileobj=context,mode='w') as archive:
 for name,data,mode in [('Dockerfile',('FROM '+CANDIDATE['tag']+'\nUSER root\nCOPY fake-cli /opt/probe/fake-cli\nRUN chmod 0555 /opt/probe/fake-cli\nUSER 958:959\n').encode(),0o644),('fake-cli',cli,0o555)]:
  info=tarfile.TarInfo(name);info.size=len(data);info.mode=mode;archive.addfile(info,io.BytesIO(data))
tag='cwb2-provider-adapter-qual:'+owner
build=subprocess.run(['docker','build','--network','none','-t',tag,'-'],input=context.getvalue(),capture_output=True,timeout=60)
assert build.returncode==0,'synthetic image build failed'
image=subprocess.check_output(['docker','image','inspect',tag,'--format','{{.Id}}'],text=True).strip()
profile=folder/'profile.json';profile.write_text(json.dumps({'path':'/opt/probe/fake-cli','sha256':hashlib.sha256(cli).hexdigest(),'version':'2.1.274','native_model':'claude-fable-5-1','effort':'high'}));profile.chmod(0o444)
token=folder/'synthetic-token';token.write_text('sk-ant-oat-synthetic-adapter-only');os.chown(token,0,959);token.chmod(0o640)
staging=folder/'staging';staging.mkdir(mode=0o700)
digest=hashlib.sha256(json.dumps(json.loads(profile.read_bytes()),sort_keys=True,separators=(',',':')).encode()).hexdigest()
config=DockerConfig(image,profile,hashlib.sha256(profile.read_bytes()).hexdigest(),digest,token,staging,('example.com',))
clock=time.time();reservation=Reservation(owner,owner,str(uuid.uuid4()),1,str(uuid.uuid4()))
lease=RequestLease(reservation,str(uuid.uuid4()),str(uuid.uuid4()),'inference',clock+120)
payload=json.dumps({'messages':[{'role':'user','content':'synthetic local transport'}],'tools':[]},separators=(',',':')).encode()
launch=uuid.uuid4().hex;prefix='cwb2-e1-'+launch
spec=DispatchSpec(lease,str(uuid.uuid4()),1,uuid.uuid4().hex+uuid.uuid4().hex,hashlib.sha256(payload).hexdigest(),digest,launch,prefix+'-provider',prefix+'-gateway',prefix+'-internal',prefix+'-external')
runtime=ProviderDocker(config,cancel_check=lambda _:False)
receipt={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'host':socket.gethostname(),'owner':owner,'source_sha256':{name:hashlib.sha256(source.encode()).hexdigest() for name,source in SOURCES.items()},'base_image':base_id,'candidate_manifest':CANDIDATE,'synthetic_image':image,'synthetic_tag':tag,'real_credentials':False,'provider_calls':False,'live_services_changed':False,'checks':{},'passed':False}
error=None
try:
 resources=runtime.create(spec,payload,deadline=time.monotonic()+30)
 observations=runtime.inspect(spec,resources,deadline=time.monotonic()+5)
 assert [x.state for x in observations[:2]]==['created','created']
 receipt['checks']['four_resources_created_stopped']=True
 receipt['resources']=vars(resources)
 receipt['engine_versions']=runtime._load(spec)['engine_versions']
 # Re-open same private staging/config after acknowledged create, as a new controller instance.
 runtime=ProviderDocker(config,cancel_check=lambda _:False)
 assert runtime.inspect(spec,resources,deadline=time.monotonic()+5)==observations
 receipt['checks']['restart_rebind_exact_resources']=True
 phase={};original_command=runtime.command
 def timed_command(args,**kwargs):
  result=original_command(args,**kwargs)
  if args==['start',resources.gateway_id]:phase['gateway_started']=time.monotonic()
  if args[0]=='exec' and 'gateway_ready_seconds' not in phase:phase['gateway_ready_seconds']=time.monotonic()-phase['gateway_started']
  return result
 runtime.command=timed_command
 provider=json.loads(subprocess.check_output(['docker','inspect',resources.provider_id],text=True))[0]
 gateway=json.loads(subprocess.check_output(['docker','inspect',resources.gateway_id],text=True))[0]
 receipt['config']={'provider_user':provider['Config']['User'],'provider_memory':provider['HostConfig']['Memory'],'provider_pids':provider['HostConfig']['PidsLimit'],'provider_nano_cpus':provider['HostConfig']['NanoCpus'],'readonly_root':provider['HostConfig']['ReadonlyRootfs'],'tmpfs':provider['HostConfig']['Tmpfs'],'cap_drop':provider['HostConfig']['CapDrop'],'security_opt':provider['HostConfig']['SecurityOpt'],'gateway_mounts':gateway['Mounts'],'provider_mounts':[{k:m.get(k) for k in ('Type','Destination','RW')} for m in provider['Mounts']]}
 runtime.start(spec,resources,deadline=time.monotonic()+30)
 response=runtime.collect(spec,resources,limit=256*1024,deadline=time.monotonic()+5)
 assert b'sk-ant-oat-' not in response
 result=json.loads(response)
 assert result['status']=='ok' and result['result']['decision']['text']=='synthetic adapter okay'
 assert result['container_cleanup_required'] and not result['credential_reuse_authorized']
 receipt['gateway_ready_seconds']=phase['gateway_ready_seconds']
 receipt['checks']['bounded_stdin_proxy_and_response']=True
 receipt['checks']['host_and_direct_egress_denied_private_connect_denied_public_connect_allowed']=True
 row={key:getattr(spec,key) for key in ('attempt_id','generation','payload_digest','profile_digest','launch_nonce','provider_name','gateway_name','internal_network_name','external_network_name')};row.update(vars(resources));row['request_id']=lease.request_id
 target=CleanupTarget('request',reservation,lease.request_id,lease.grant_id,spec.attempt_id,1,time.time(),str(uuid.uuid4()),(tuple(row.items()),))
 clean=runtime.cleanup(target)
 assert clean.target==target and clean.outcome=='terminated'
 receipt['checks']['exact_cleanup_receipt']=True
 receipt['cleanup_evidence_sha256']=clean.evidence_sha256
 # A second sequential synthetic request demonstrates active cancellation and
 # whole-scope cleanup. The fake CLI sleeps; no provider request is issued.
 from dataclasses import replace
 lease=replace(lease,request_id=str(uuid.uuid4()),expires_at=time.time()+120)
 launch=uuid.uuid4().hex;prefix='cwb2-e1-'+launch
 payload=json.dumps({'messages':[{'role':'user','content':'wait-for-cancel'}],'tools':[]},separators=(',',':')).encode()
 spec=replace(spec,lease=lease,request_nonce=uuid.uuid4().hex+uuid.uuid4().hex,payload_digest=hashlib.sha256(payload).hexdigest(),launch_nonce=launch,provider_name=prefix+'-provider',gateway_name=prefix+'-gateway',internal_network_name=prefix+'-internal',external_network_name=prefix+'-external')
 resources=runtime.create(spec,payload,deadline=time.monotonic()+30)
 cancel_at=[None];original=runtime.command
 def cancellation_command(args,**kwargs):
  output=original(args,**kwargs)
  if args==['start',resources.provider_id]:cancel_at[0]=time.monotonic()+.5
  return output
 runtime.command=cancellation_command;runtime.cancel_check=lambda _:cancel_at[0] is not None and time.monotonic()>=cancel_at[0]
 started=time.monotonic()
 try:runtime.start(spec,resources,deadline=time.monotonic()+30);raise AssertionError('expected cancellation')
 except DockerError as failure:assert failure.code=='provider_cancelled'
 resolution=runtime.resolve(spec,{'operation':'start','version':1},deadline=time.monotonic()+20)
 assert resolution.resources==resources and resolution.outcome=='completed'
 row={key:getattr(spec,key) for key in ('attempt_id','generation','payload_digest','profile_digest','launch_nonce','provider_name','gateway_name','internal_network_name','external_network_name')};row.update(vars(resources));row['request_id']=lease.request_id
 target=CleanupTarget('request',reservation,lease.request_id,lease.grant_id,spec.attempt_id,1,time.time(),str(uuid.uuid4()),(tuple(row.items()),))
 runtime.cleanup(target)
 receipt['checks']['active_synthetic_execution_cancelled_and_exact_scope_cleaned']=True
 receipt['cancel_total_seconds']=time.monotonic()-started
 # Third sequential scope proves execution deadline kills a known-running process.
 runtime=ProviderDocker(config,cancel_check=lambda _:False)
 lease=replace(lease,request_id=str(uuid.uuid4()),expires_at=time.time()+120)
 launch=uuid.uuid4().hex;prefix='cwb2-e1-'+launch
 spec=replace(spec,lease=lease,request_nonce=uuid.uuid4().hex+uuid.uuid4().hex,launch_nonce=launch,provider_name=prefix+'-provider',gateway_name=prefix+'-gateway',internal_network_name=prefix+'-internal',external_network_name=prefix+'-external')
 resources=runtime.create(spec,payload,deadline=time.monotonic()+30)
 started=time.monotonic()
 try:runtime.start(spec,resources,deadline=started+8);raise AssertionError('expected execution deadline')
 except DockerError as failure:assert failure.code=='provider_execution_timeout',failure.code
 state=json.loads(subprocess.check_output(['docker','inspect',resources.provider_id],text=True))[0]['State']
 assert state['Status']=='exited'
 assert runtime._load(spec)['start_complete'] and runtime._load(spec)['pending'] is None
 receipt['execution_deadline_seconds']=time.monotonic()-started
 receipt['checks']['execution_deadline_stopped_provider_before_return']=True
 row={key:getattr(spec,key) for key in ('attempt_id','generation','payload_digest','profile_digest','launch_nonce','provider_name','gateway_name','internal_network_name','external_network_name')};row.update(vars(resources));row['request_id']=lease.request_id
 target=CleanupTarget('request',reservation,lease.request_id,lease.grant_id,spec.attempt_id,1,time.time(),str(uuid.uuid4()),(tuple(row.items()),))
 runtime.cleanup(target)
 receipt['checks']['deadline_scope_exact_cleanup']=True
 receipt['passed']=True
except Exception as exc:
 error=getattr(exc,'code',type(exc).__name__);receipt['error']=error
 try:
  state=json.loads(subprocess.check_output(['docker','inspect',spec.gateway_name],text=True))[0]['State']
  logs=subprocess.run(['docker','logs','--tail','20',spec.gateway_name],capture_output=True,text=True,timeout=3)
  receipt['gateway_failure']={'state':state,'diagnostic':(logs.stdout+logs.stderr)[:2000]}
 except Exception:receipt['gateway_failure']={'status':'unavailable'}
finally:
 # Qualification-only fallback, exact deterministic names and immutable labels.
 # No other runtime is stopped, no network/global prune is used.
 cleanup=[]
 for kind,name,labels in [('container',spec.provider_name,spec.labels('provider')),('container',spec.gateway_name,spec.labels('gateway')),('network',spec.internal_network_name,spec.network_labels('internal')),('network',spec.external_network_name,spec.network_labels('external'))]:
  args=['docker','inspect',name] if kind=='container' else ['docker','network','inspect',name]
  found=subprocess.run(args,capture_output=True,text=True,timeout=5)
  if found.returncode==0:
   obj=json.loads(found.stdout)[0]
   if kind=='container':
    base=json.loads(subprocess.check_output(['docker','image','inspect',obj['Image']],text=True))[0]
    labels={**(base['Config'].get('Labels') or {}),**labels}
   assert (obj['Config']['Labels'] if kind=='container' else obj['Labels'])==labels
   if kind=='container':subprocess.run(['docker','rm','-f',obj['Id']],check=True,capture_output=True,timeout=10)
   else:subprocess.run(['docker','network','rm',obj['Id']],check=True,capture_output=True,timeout=10)
   cleanup.append({'kind':kind,'id':obj['Id'],'fallback':True})
 receipt['fallback_cleanup']=cleanup
 receipt['services_after']=subprocess.check_output(['systemctl','is-active',*service_names],text=True).splitlines()
 assert receipt['services_after']==before
 receipt['remaining_objects']=subprocess.check_output(['docker','ps','-aq','--filter','label=io.cloudworkbench.provider-reservation='+reservation.reservation_id],text=True).splitlines()+subprocess.check_output(['docker','network','ls','-q','--filter','label=io.cloudworkbench.provider-reservation='+reservation.reservation_id],text=True).splitlines()
 assert not receipt['remaining_objects']
 # Remove only this created synthetic credential; retain nonsensitive journals/source as evidence.
 token.unlink();receipt['synthetic_token_removed']=True
 receipt['qualification_folder']=str(folder)
print(json.dumps(receipt))
'''.replace('SOURCES',repr(sources)).replace('CANDIDATE',repr(candidate))
result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','thomas@100.83.74.92','sudo /opt/cloud-workbench/.venv/bin/python -'],input=program,capture_output=True,text=True,timeout=150)
base=root/'evidence'/('provider-docker-linux-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
base.with_suffix('.stderr.txt').write_text(result.stderr)
base.with_suffix('.json' if result.stdout.strip() else '.failed.txt').write_text(result.stdout)
assert result.returncode==0,result.stderr[-1500:]
receipt=json.loads(result.stdout)
print(json.dumps({'receipt':str(base.with_suffix('.json')),'passed':receipt['passed'],'error':receipt.get('error'),'remaining_objects':receipt['remaining_objects']}))
