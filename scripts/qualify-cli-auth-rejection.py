#!/usr/bin/env python3
"""One bounded synthetic-invalid credential probe; never opens a real token."""
import datetime
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import time
import uuid
from cloudworkbench.runtime import Runtime

PROGRAM = r'''
import json,os,selectors,subprocess,time,uuid
from pathlib import Path
os.makedirs('/tmp/work');os.makedirs('/tmp/home');os.chdir('/tmp/work')
value='sk-ant-oat01-invalid-qualification-'+uuid.uuid4().hex
env={k:v for k,v in os.environ.items() if not k.startswith(('ANTHROPIC_','CLAUDE_'))}
env.update(HOME='/tmp/home',CLAUDE_CONFIG_DIR='/tmp/home/claude',CLAUDE_CODE_OAUTH_TOKEN=value,DISABLE_AUTOUPDATER='1',CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
version=subprocess.check_output(['claude','--version'],env=env,text=True,timeout=10).strip()
command=['claude','--print','--verbose','--output-format','stream-json','--max-turns','1','--model','sonnet','--dangerously-skip-permissions']
p=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,start_new_session=True)
p.stdin.write(b'Reply with OK.');p.stdin.close();data=bytearray();deadline=time.monotonic()+45;failure=None
try:
 with selectors.DefaultSelector() as selector:
  selector.register(p.stdout,selectors.EVENT_READ)
  while True:
   if time.monotonic()>=deadline:failure='timeout';break
   if not selector.select(.2):continue
   chunk=os.read(p.stdout.fileno(),min(4096,32769-len(data)))
   if not chunk:break
   data.extend(chunk)
   if len(data)>32768:failure='output_limit';break
finally:
 if p.poll() is None:
  import signal
  try:os.killpg(p.pid,signal.SIGKILL)
  except ProcessLookupError:pass
 p.wait(timeout=3)
print(json.dumps({'cli_version':version,'exit_code':p.returncode,'capture_error':failure,'stdout_bytes':len(data),'stdout':data.decode(errors='replace').replace(value,'[SYNTHETIC_TOKEN]'),'credential_source':'generated-invalid-token-inside-container','real_token_read':False}))
'''


def main():
    assert os.uname().nodename=='omarchy'
    # Config supplies existing network/image policy only, never credential file content.
    config=json.loads(Path('/etc/cloud-workbench/worker.json').read_text())['runtime']
    config={**config,'owner':'cliprobe-'+uuid.uuid4().hex[:10]}
    runtime=Runtime(config)
    assert runtime.network_enabled
    aid='synthetic-'+uuid.uuid4().hex[:12];rid=None
    result={'host':os.uname().nodename,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'runtime_module_sha256':hashlib.sha256(Path(sys.modules['cloudworkbench.runtime'].__file__).read_bytes()).hexdigest(),'image':runtime.image,'egress_image':runtime.egress_image,'owner':runtime.owner,'attempt_id':aid,'no_workspace_volume':True,'real_credential_read':False,'passed':False}
    try:
        network,proxy=runtime._network(aid,aid,1)
        labels=['--label','io.cloudworkbench.managed=true','--label','io.cloudworkbench.owner='+runtime.owner,'--label','io.cloudworkbench.attempt='+aid,'--label','io.cloudworkbench.session='+aid,'--label','io.cloudworkbench.generation=1','--label','io.cloudworkbench.role=job']
        rid=runtime._run(['create','--name','cwb2-'+runtime.owner+'-'+aid,*labels,'--network',network,'--dns','127.0.0.1','--user',f'{runtime.uid}:{runtime.gid}','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--pids-limit','32','--memory','256m','--memory-swap','256m','--cpus','0.5','--tmpfs',f'/tmp:rw,nosuid,nodev,size=64m,uid={runtime.uid},gid={runtime.gid},mode=1777','--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2','--env','HTTPS_PROXY='+proxy,'--env','HTTP_PROXY='+proxy,'--env','ALL_PROXY='+proxy,'--entrypoint','python3',runtime.image,'-c',PROGRAM])
        result['runtime_id']=rid
        try:
            runtime._run(['start',rid])
        except Exception:
            result['synthetic_start_error']=runtime._run(['inspect','--format','{{.State.Error}}',rid])[:2048]
            raise
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            state=runtime.status(rid,1)
            if state['state']=='exited':break
            time.sleep(.25)
        else:raise RuntimeError('synthetic CLI probe deadline')
        result['container_status']=state
        result['capture']=json.loads(runtime.logs(rid,131072))
        result['passed']=state['exit_code']==0 and result['capture']['capture_error'] is None
    finally:
        if rid:
            runtime.stop(rid,expected_generation=1)
            runtime.cleanup(rid,expected_generation=1)
        else:
            runtime.cleanup_infrastructure(aid,expected_generation=1)
        filters=runtime._attempt_filters(aid,1)
        result['remaining_containers']=runtime._run(['container','ls','--all',*filters,'--format','{{.ID}}']).splitlines()
        result['remaining_networks']=runtime._run(['network','ls',*filters,'--format','{{.ID}}']).splitlines()
        result['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        result['passed']=result['passed'] and not result['remaining_containers'] and not result['remaining_networks']
        print(json.dumps(result,indent=2))
    assert result['passed']

if __name__=='__main__':main()
