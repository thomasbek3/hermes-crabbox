#!/usr/bin/env python3
"""One authorized analysis-only Grok review; no provider/runtime activation."""
import hashlib,json,os,signal,socket,subprocess,time
from pathlib import Path

assert socket.gethostname()=='omarchy' and os.geteuid()==0
root=Path('/var/lib/cloud-workbench/auth/native-login-20260918/review-native-integration')
auth=Path('/var/lib/cloud-workbench/auth/native-login-20260918/grok')
exe=Path('/var/lib/cloud-workbench/auth/native-login-20260918/bin/grok')
assert hashlib.sha256(exe.read_bytes()).hexdigest()=='be5905e107d2b8b5f3c142d21ecfe4c8fd32a913d2fd551b788707930c4dc80d'
prompt=root/'prompt.txt'
assert hashlib.sha256(prompt.read_bytes()).hexdigest()=='4446d599369eaaea2814da121d7c22a60c6beb781d23bc623f27bc2529271588'
args=[str(exe),'--prompt-file',str(prompt),'-m','grok-4.6','--reasoning-effort','xhigh',
      '--output-format','json','--sandbox','read-only','--permission-mode','default',
      '--no-subagents','--no-memory','--disable-web-search','--max-turns','30',
      '--tools','','--deny','*','--no-leader']
env={'HOME':str(auth),'GROK_HOME':str(auth/'.grok'),'XDG_CONFIG_HOME':str(auth/'.config'),
     'PATH':'/usr/bin:/bin','TERM':'dumb','LANG':'C.UTF-8'}
os.umask(0o077)
receipt={'host':'omarchy','uid':959,'gid':960,'model_requested':'grok-4.6','effort_requested':'xhigh',
    'started_at':time.time(),'wall_limit_seconds':900,'argv':args,'prompt_sha256':hashlib.sha256(prompt.read_bytes()).hexdigest(),
    'source_binding':'evidence/native-integration-grok-source.json','code_changes_permitted':False,'raw_credentials_read_by_driver':False}
process=None
try:
    with (root/'stdout.json').open('xb') as out,(root/'stderr.txt').open('xb') as err:
        process=subprocess.Popen(args,cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,
            start_new_session=True,user=959,group=960,extra_groups=[])
        deadline=time.monotonic()+900
        while process.poll() is None:
            if time.monotonic()>=deadline or out.tell()+err.tell()>16*1024*1024:
                receipt['interrupted']='wall_timeout' if time.monotonic()>=deadline else 'output_limit'
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
                break
            time.sleep(.2)
        receipt['exit_code']=process.returncode
    raw=(root/'stdout.json').read_bytes();stderr=(root/'stderr.txt').read_bytes()
    receipt['stdout_bytes']=len(raw);receipt['stderr_bytes']=len(stderr)
    receipt['stdout_sha256']=hashlib.sha256(raw).hexdigest();receipt['stderr_sha256']=hashlib.sha256(stderr).hexdigest()
    receipt['sandbox_warning_present']=any(x in stderr.lower() for x in (b'sandbox warning',b'sandbox failed',b'failed to apply sandbox',b'continuing without sandbox'))
    try:
        parsed=json.loads(raw);receipt['parsed_json']=True
        receipt['top_level_type']=type(parsed).__name__
        receipt['top_level_keys']=list(parsed) if isinstance(parsed,dict) else []
    except (ValueError,UnicodeError):receipt['parsed_json']=False
finally:
    if process is not None and process.poll() is None:
        os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
    prompt.unlink(missing_ok=True)
    receipt['prompt_deleted']=not prompt.exists();receipt['finished_at']=time.time()
    (root/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt),flush=True)
