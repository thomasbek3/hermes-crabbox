#!/usr/bin/env python3
"""Positive entrypoint proof using a readonly fake CLI and synthetic credential."""
from pathlib import Path
import json,subprocess,uuid,datetime,hashlib,argparse
parser=argparse.ArgumentParser()
parser.add_argument('--case',choices=['success','missing_token','unsafe_token','hash_mismatch','invalid_profile'],default='success')
parser.add_argument('--inject-before-create',action='store_true')
options=parser.parse_args();mode=options.case
root=Path(__file__).resolve().parents[1]
candidate=json.loads((root/'evidence/provider-image-candidate.json').read_text())
image=candidate['image_id'];owner='provider-main-'+uuid.uuid4().hex[:12]
program=r'''
import pathlib,tempfile,subprocess,os,json,hashlib,socket,datetime
assert socket.gethostname()=='omarchy'
folder=pathlib.Path(tempfile.mkdtemp(prefix='cwb-'+OWNER+'-'))
name='cwb-'+OWNER
receipt={'case':MODE,'host':socket.gethostname(),'image':IMAGE,'passed':False,'provider_calls':False,'real_credentials':False}
cleanup_errors=[]
try:
 if INJECT:raise RuntimeError('synthetic precreate failure')
 cli=folder/'fake-cli'
 cli.write_text("#!/opt/hermes/venv/bin/python\nimport sys,json,os\nsys.stdin.read()\nassert os.environ['CLAUDE_CODE_OAUTH_TOKEN']=='sk-ant-oat-synthetic-main-proof-only'\nassert sys.argv[sys.argv.index('--tools')+1]==''\nassert sys.argv[sys.argv.index('--disallowedTools')+1]=='*'\nassert os.getuid()==958 and os.getgid()==959\nprint(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':{'kind':'final','text':'synthetic entrypoint okay','tool_calls':[]}}))\n")
 cli.chmod(0o555)
 token=folder/'synthetic-token';token.write_text('sk-ant-oat-synthetic-main-proof-only');os.chown(token,959,959);token.chmod(0o640)
 profile=folder/'profile.json';profile.write_text(json.dumps({'path':'/opt/probe/fake-cli','sha256':hashlib.sha256(cli.read_bytes()).hexdigest(),'version':'2.1.274','native_model':'claude-fable-5-1','effort':'high'}));profile.chmod(0o444)
 if MODE=='unsafe_token':token.chmod(0o644)
 if MODE=='hash_mismatch':
  value=json.loads(profile.read_text());value['sha256']='f'*64;profile.write_text(json.dumps(value))
 if MODE=='invalid_profile':profile.write_text('{}')
 args=['docker','run','--name',name,'--label','io.cloudworkbench.probe-owner='+OWNER,'--user','958:959','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','128m','--pids-limit','32','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777']
 for file,dest in [(cli,'/opt/probe/fake-cli'),(token,'/run/secrets/claude-token'),(profile,'/run/provider/profile.json')]:
  if MODE=='missing_token' and file==token:continue
  args+=['--mount','type=bind,src='+str(file)+',dst='+dest+',readonly']
 args+=['-i',IMAGE]
 result=subprocess.run(args,input=json.dumps({'messages':[{'role':'user','content':'synthetic'}],'tools':[]}),capture_output=True,text=True,timeout=25)
 output=json.loads(result.stdout)
 if MODE=='success':
  assert result.returncode==0 and output['status']=='ok' and output['result']['decision']['text']=='synthetic entrypoint okay'
 else:
  expected={'missing_token':'auth_missing','unsafe_token':'auth_invalid','hash_mismatch':'binary_digest_mismatch','invalid_profile':'invalid_profile_file'}[MODE]
  assert result.returncode==1 and output['status']=='error' and output['error']==expected,output
 assert 'sk-ant-oat' not in result.stdout+result.stderr
 assert output['container_cleanup_required'] and not output['credential_reuse_authorized']
 meta=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
 assert meta['Image']==IMAGE and all(not m['RW'] for m in meta['Mounts'])
 limits=meta['HostConfig']
 assert limits['ReadonlyRootfs'] and limits['NetworkMode']=='none' and limits['CapDrop']==['ALL']
 assert 'no-new-privileges' in limits['SecurityOpt'] and limits['Memory']==134217728 and limits['PidsLimit']==32
 receipt={'case':MODE,'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'synthetic_cli':True,'host_config':limits,'host':socket.gethostname(),'image':IMAGE,'passed':True,'output':output,'mounts':meta['Mounts'],'user':meta['Config']['User'],'tmpfs':meta['HostConfig']['Tmpfs'],'provider_calls':False,'real_credentials':False}
except Exception as exc:
 receipt['failure_type']=type(exc).__name__
finally:
 inspection=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
 if inspection.returncode==0:
  meta=json.loads(inspection.stdout)[0]
  assert meta['Config']['Labels']['io.cloudworkbench.probe-owner']==OWNER
  removed=subprocess.run(['docker','rm','-f',name],capture_output=True)
  if removed.returncode:cleanup_errors.append('container_remove_failed')
 try:
  for file in folder.iterdir():file.unlink()
  folder.rmdir()
 except OSError:
  cleanup_errors.append('tempfile_remove_failed')
remaining=subprocess.check_output(['docker','ps','-aq','--filter','label=io.cloudworkbench.probe-owner='+OWNER],text=True).strip()
receipt['cleanup']={'containers_remaining':remaining.splitlines(),'tempfile_removed':not folder.exists(),'errors':cleanup_errors}
if remaining or folder.exists() or cleanup_errors:receipt['passed']=False
print(json.dumps(receipt))
raise SystemExit(0 if receipt['passed'] else 1)
'''.replace('OWNER',repr(owner)).replace('IMAGE',repr(image)).replace('MODE',repr(mode)).replace('INJECT',repr(options.inject_before_create))
result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','thomas@100.83.74.92','sudo python3 -'],input=program,capture_output=True,text=True,timeout=50)
base=root/'evidence'/('provider-main-linux-'+mode+'-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
base.with_suffix('.stderr.txt').write_text(result.stderr)
base.with_suffix('.json' if result.stdout.strip() else '.failed.txt').write_text(result.stdout)
assert result.returncode==0, result.stderr[-2000:]
receipt=json.loads(result.stdout);assert receipt['passed'] and receipt['cleanup']['tempfile_removed']
print(json.dumps({'receipt':str(base.with_suffix('.json')),'image':image,'passed':True,'cleanup':receipt['cleanup']}))
