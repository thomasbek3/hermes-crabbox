#!/usr/bin/env python3
"""Send reviewed source via stdin only into one disposable, isolated Linux container."""
from pathlib import Path
import datetime,hashlib,json,shlex,subprocess,uuid
root=Path(__file__).resolve().parents[1]

paths=['src/cloudworkbench/inference_transport.py','src/cloudworkbench/hermes_inference_protocol.py','tests/fixtures/hermes/base-tool-schemas.json','scripts/qualify-hermes-inference-codec.py']
sources={path:(root/path).read_text() for path in paths}
hashes={path:hashlib.sha256(source.encode()).hexdigest() for path,source in sources.items()}
assert hashes[paths[0]]=='6752df1df4f655b477a514a7712e13b70e5f1237aa33a07cdd9837baecce941f'

image='sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
owner='h1codec-'+uuid.uuid4().hex[:12];name='cwb-'+owner
ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','thomas@100.83.74.92']
program='''import hashlib,json,os,pathlib,runpy,sys,tempfile
sources=%r
hashes=%r
stage=pathlib.Path(tempfile.mkdtemp(prefix='codec-',dir='/tmp'))
for relative,source in sources.items():
 path=stage/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(source)
 assert hashlib.sha256(path.read_bytes()).hexdigest()==hashes[relative]
(stage/'src/cloudworkbench/__init__.py').write_text('')
(stage/'source-manifest.json').write_text(json.dumps(hashes))
sys.path.insert(0,str(stage/'src'))
assert os.getpid()==1 and os.getuid()==1000
runpy.run_path(str(stage/'scripts/qualify-hermes-inference-codec.py'),run_name='__main__')
'''%(sources,hashes)
argv=['/usr/bin/docker','run','--name',name,'--label','io.cloudworkbench.probe-owner='+owner,'--network','none','--read-only',
 '--user','1000:1000','--cpus','2','--memory','512m','--memory-swap','512m','--pids-limit','64','--cap-drop','ALL','--security-opt','no-new-privileges',
 '--tmpfs','/tmp:rw,exec,nosuid,nodev,size=32m,mode=1777','--workdir','/tmp','-e','PYTHONDONTWRITEBYTECODE=1',
 '-e','PYTHONPATH=/opt/hermes/source','-e','HOME=/tmp','-e','HERMES_HOME=/tmp/hermes','-e','CODEX_HOME=/tmp/codex','-e','CLAUDE_CONFIG_DIR=/tmp/claude','--entrypoint','/opt/hermes/venv/bin/python','-i',image,'-']
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ');base=root/'evidence'/('hermes-inference-codec-native-'+stamp)
receipt={'owner':owner,'name':name,'host':'omarchy','image':image,'source_sha256':hashes,'container_argv':argv,'source_deployed':False,'services_changed':False,'provider_calls':False,'real_credentials_used':False}
try:
 result=subprocess.run(ssh+[shlex.join(argv)],input=program,capture_output=True,text=True,timeout=50)
 base.with_suffix('.stdout.json').write_text(result.stdout);base.with_suffix('.stderr.txt').write_text(result.stderr)
 receipt.update(transport_exit_code=result.returncode,stdout_sha256=hashlib.sha256(result.stdout.encode()).hexdigest())
 if result.stdout:
  parsed=json.loads(result.stdout);assert parsed['source_sha256']==hashes
  receipt.update(passed=parsed['passed'],failed=parsed['failed'],suite=parsed['suite'])
finally:
 try:
  inspected=subprocess.run(ssh+[shlex.join(['/usr/bin/docker','inspect',name,'--format','{"id":{{json .Id}},"image":{{json .Image}},"owner":{{json (index .Config.Labels "io.cloudworkbench.probe-owner")}},"state":{{json .State}},"limits":{{json .HostConfig}},"mounts":{{json .Mounts}}}'])],capture_output=True,text=True,timeout=15)
  metadata=json.loads(inspected.stdout);receipt['container']=metadata
  if metadata['owner']!=owner:raise RuntimeError('probe ownership mismatch; refusing removal')
  # Remove the verified owned container before validating its reported constraints.
  removed=subprocess.run(ssh+[shlex.join(['/usr/bin/docker','rm','-f',name])],capture_output=True,text=True,timeout=15)
  readback_argv=['/usr/bin/docker','ps','-aq','--filter','label=io.cloudworkbench.probe-owner='+owner]
  readback=subprocess.run(ssh+[shlex.join(readback_argv)],capture_output=True,text=True,timeout=15)
  receipt['cleanup']={'remove_exit':removed.returncode,'readback_argv':readback_argv,
   'readback_exit':readback.returncode,'readback_stdout':readback.stdout,
   'owned_containers_remaining':readback.stdout.splitlines(),
   'removed':removed.returncode==0 and readback.returncode==0 and not readback.stdout.strip(),
   'docker_init_stopped_observed':not metadata['state']['Running'] and metadata['state']['Pid']==0,
   'inference':'Docker private PID namespace and tmpfs lifecycle; escaped PID and tmpfs not separately measured after removal'}
  assert receipt['cleanup']['removed']
  assert not metadata['mounts'] and metadata['limits']['NetworkMode']=='none' and metadata['limits']['ReadonlyRootfs'] is True
  assert metadata['limits']['PidMode']=='' and metadata['limits']['PidsLimit']==64
  assert metadata['limits']['Memory']==536870912 and metadata['limits']['NanoCpus']==2000000000
 except Exception as exc:
  receipt['cleanup_error_type']=type(exc).__name__
  raise
 finally:
  base.with_suffix('.json').write_text(json.dumps(receipt,indent=2)+'\n')
  base.with_suffix('.script.py').write_text(sources[paths[-1]])

print(json.dumps({'receipt':str(base.relative_to(root))+'.json','passed':receipt.get('passed'),'failed':receipt.get('failed'),'exit':receipt.get('transport_exit_code'),'cleanup':receipt['cleanup']},indent=2))
raise SystemExit(0 if receipt.get('transport_exit_code')==0 and receipt.get('failed')==0 else 1)
