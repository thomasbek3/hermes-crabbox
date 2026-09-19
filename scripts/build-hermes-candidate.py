#!/usr/bin/env python3
"""One isolated candidate build from the reviewed tracked-source context."""
from pathlib import Path
import datetime,hashlib,json,os,shlex,subprocess,tarfile,uuid
ROOT=Path(__file__).resolve().parents[1]
CONTEXT=ROOT.parent/'work/hermes-pstack-image-context'
BASE='sha256:f1b15bb81917de9b48df7c9b5c2be438e686f692b42bdaa58eb2aeedf5b9ea0b'
HOST='thomas@100.83.74.92'
SSH=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',HOST]
owner='hermesbuild-'+uuid.uuid4().hex[:10]
stage='/tmp/cwb-'+owner
base_tag='cwb-hermes-build-base:'+owner
tag='cwb-hermes-candidate:0.21.3-'+owner
manifest=json.loads((CONTEXT/'source-manifest.json').read_text())
for name,digest in manifest['files_sha256'].items():
 p=CONTEXT/name
 assert p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==digest,name
archive=ROOT.parent/'work'/(owner+'.tar.gz')
with tarfile.open(archive,'w:gz') as tar:
 for p in sorted(CONTEXT.rglob('*')):
  assert not p.is_symlink(),str(p)
  tar.add(p,arcname=str(p.relative_to(CONTEXT)),recursive=False)
sha=hashlib.sha256(archive.read_bytes()).hexdigest()
receipt={'owner':owner,'tag':tag,'base_image':BASE,'base_tag':base_tag,'stage':stage,'context_archive_sha256':sha,
 'manifest_sha256':hashlib.sha256((CONTEXT/'source-manifest.json').read_bytes()).hexdigest(),
 'dockerfile_sha256':hashlib.sha256((CONTEXT/'Dockerfile').read_bytes()).hexdigest(),'services_changed':False,'live_activation':False}
receipt_path=ROOT/'evidence'/(owner+'.json')
receipt_path.write_text(json.dumps(receipt,indent=2)+'\n')
subprocess.run(SSH+['mkdir -m 700 '+shlex.quote(stage)],check=True,timeout=20)
subprocess.run(['scp','-q',str(archive),HOST+':'+stage+'/context.tar.gz'],check=True,timeout=120)
program='''import hashlib,json,pathlib,subprocess,tarfile,sys,socket,time
stage=pathlib.Path(%r);archive=stage/'context.tar.gz'
assert socket.gethostname()=='omarchy'
assert hashlib.sha256(archive.read_bytes()).hexdigest()==%r
base=%r;base_tag=%r;tag=%r
assert subprocess.check_output(['/usr/bin/docker','image','inspect',base,'--format','{{.Id}}'],text=True).strip()==base
assert not subprocess.check_output(['/usr/bin/docker','ps','-q'],text=True).strip(),'another container active before build'
mem=dict(line.split(':',1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
assert int(mem['MemAvailable'].split()[0])*1024>=12*1024**3
import shutil
assert shutil.disk_usage('/var/lib/docker').free>=30*1024**3
context=stage/'context';context.mkdir()
with tarfile.open(archive) as tar:tar.extractall(context,filter='data')
subprocess.run(['/usr/bin/docker','tag',base,base_tag],check=True)
start=time.monotonic()
with (stage/'build.log').open('w') as log:
 result=subprocess.run(['/usr/bin/docker','build','--progress','plain','--pull=false','--build-arg','BASE_IMAGE='+base_tag,'--label','io.cloudworkbench.candidate=true','--label','io.cloudworkbench.build-owner='+%r,'-t',tag,str(context)],stdout=log,stderr=subprocess.STDOUT,timeout=1200)
receipt={'exit_code':result.returncode,'seconds':time.monotonic()-start,'image_id':None}
if result.returncode==0:receipt['image_id']=subprocess.check_output(['/usr/bin/docker','image','inspect',tag,'--format','{{.Id}}'],text=True).strip()
(stage/'build-result.json').write_text(json.dumps(receipt,indent=2)+'\\n')
print(json.dumps(receipt));sys.exit(result.returncode)
'''%(stage,sha,BASE,base_tag,tag,owner)
result=subprocess.run(SSH+['python3 -'],input=program,text=True,capture_output=True,timeout=1240)
receipt['remote_stdout']=result.stdout;receipt['remote_stderr']=result.stderr;receipt['transport_exit']=result.returncode
for remote,suffix in [('build.log','.log'),('build-result.json','.build-result.json')]:
 r=subprocess.run(SSH+['cat '+shlex.quote(stage+'/'+remote)],capture_output=True,timeout=20)
 (ROOT/'evidence'/(owner+suffix)).write_bytes(r.stdout)
 if remote=='build-result.json' and r.returncode==0:receipt['build']=json.loads(r.stdout)
receipt_path.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'receipt':str(receipt_path.relative_to(ROOT)),'tag':tag,'stage':stage,'build':receipt.get('build'),'transport_exit':result.returncode},indent=2))
raise SystemExit(result.returncode)
