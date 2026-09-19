#!/usr/bin/env python3
"""Check baked source and negative startup using no profile or credentials."""
from pathlib import Path
import json,subprocess,shlex,uuid,datetime
root=Path(__file__).resolve().parents[1]
candidate=json.loads((root/'evidence/provider-image-candidate.json').read_text())
image=candidate['image_id'];owner='provider-image-'+uuid.uuid4().hex[:12]
ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','thomas@100.83.74.92']
assert subprocess.check_output(ssh+['hostname'],text=True).strip()=='omarchy'
checks=[]
for case in ['baked_sources','root_boundary','missing_profile']:
 name='cwb-'+owner+'-'+case
 args=['docker','run','--name',name,'--label','io.cloudworkbench.probe-owner='+owner,'--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user',('0:0' if case=='root_boundary' else '958:959'),'--memory','128m','--pids-limit','32','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777']
 if case=='baked_sources':
  program="import pathlib,hashlib,json,os\nm="+repr(candidate['sources'])+"\nfor f,h in m.items():\n p=pathlib.Path('/opt/cloud-provider/cloudworkbench')/f\n assert hashlib.sha256(p.read_bytes()).hexdigest()==h\n assert p.stat().st_uid==0 and not p.stat().st_mode&0o222\nprint(json.dumps({'files':len(m),'uid':os.getuid()}))"
  program+="\nactual={str(p.relative_to('/opt/cloud-provider/cloudworkbench')) for p in pathlib.Path('/opt/cloud-provider/cloudworkbench').rglob('*') if p.is_file()}\nassert actual==set(m)"
  args+=['--entrypoint','/opt/hermes/venv/bin/python',image,'-c',program]
 elif case=='root_boundary':
  program="import os,errno,pathlib,json,subprocess\np=pathlib.Path('/opt/cloud-provider/cloudworkbench/provider_main.py')\ntry:\n os.chmod(p,0o644)\nexcept OSError as e:\n assert e.errno==errno.EROFS\nelse:\n raise AssertionError('rootfs mutable')\np=pathlib.Path('/tmp/noexec');p.write_bytes(bytes([35,33,47,98,105,110,47,115,104,10,101,120,105,116,32,48,10]));p.chmod(0o755)\ntry:\n subprocess.run([str(p)],check=True)\nexcept PermissionError:\n pass\nelse:\n raise AssertionError('tmp executable')\nprint(json.dumps({'rootfs_readonly':True,'tmp_noexec':True}))"
  args+=['--entrypoint','/opt/hermes/venv/bin/python',image,'-c',program]
 else:args+=['-i',image]
 try:
  result=subprocess.run(ssh+[shlex.join(args)],input='{}',text=True,capture_output=True,timeout=20)
  output=json.loads(result.stdout)
  if case=='baked_sources':assert result.returncode==0 and output['files']==6
  elif case=='root_boundary':assert result.returncode==0 and output['tmp_noexec']
  else:assert result.returncode==1 and output['status']=='error' and output['container_cleanup_required'] and not output['credential_reuse_authorized']
  checks.append({'case':case,'passed':True,'exit':result.returncode,'output':output})
 finally:
  meta=json.loads(subprocess.check_output(ssh+[shlex.join(['docker','inspect',name])],text=True))[0]
  limits=meta['HostConfig']
  assert limits['ReadonlyRootfs'] and limits['NetworkMode']=='none' and limits['CapDrop']==['ALL']
  assert 'no-new-privileges' in limits['SecurityOpt'] and limits['Memory']==134217728 and limits['PidsLimit']==32
  if checks and checks[-1]['case']==case:checks[-1]['host_config']=limits
  assert meta['Config']['Labels']['io.cloudworkbench.probe-owner']==owner and meta['Image']==image
  subprocess.run(ssh+[shlex.join(['docker','rm','-f',name])],check=True,capture_output=True)
remaining=subprocess.check_output(ssh+[shlex.join(['docker','ps','-aq','--filter','label=io.cloudworkbench.probe-owner='+owner])],text=True)
assert not remaining.strip()
receipt={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'image':image,'checks':checks,'remaining':[],'activated':False,'real_credentials':False,'provider_calls':False,'positive_provider_request_qualified':False}
(root/'evidence/provider-image-qualification.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
