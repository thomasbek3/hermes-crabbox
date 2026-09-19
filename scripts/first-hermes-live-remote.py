"""Operator-run first real Hermes job; no service install or shared configuration edits.

Run as root on Omarchy. Only the trusted coordinator sees the dedicated access
token and Docker socket. Agent terminal/file tools use a separate offline Docker
workspace with no credentials or host administration mounts.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

IMAGE = 'sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
AUTH = Path('/var/lib/cloud-workbench/auth/native-login-20260918/grok/.grok/auth.json')
SERVICES = ['cloud-workbench-api.service', 'cloud-workbench-worker.service', 'cloudd.service']

def docker(*args, **kw):
    kw.setdefault('timeout',30)
    return subprocess.run(['/usr/bin/docker', *args], check=True, capture_output=True, **kw)

def services():
    return {s: subprocess.check_output(['systemctl', 'show', s, '--property=MainPID,ActiveState', '--value'], text=True,timeout=10).splitlines() for s in SERVICES}

def write(path, value, uid=959, gid=960, mode=0o600):
    path.write_text(value); os.chown(path, uid, gid); path.chmod(mode)

assert os.geteuid() == 0 and os.uname().nodename == 'omarchy'
base = Path('/var/lib/cloud-workbench/first-hermes-live')
base.mkdir(mode=0o750, exist_ok=True)
lock = os.open(base/'account.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
mode = sys.argv[1] if len(sys.argv) > 1 else 'first'
if mode == 'first':
    root = base/('job-'+uuid.uuid4().hex[:12])
    root.mkdir(mode=0o750); os.chown(root, 959, 960)
    for name in ('profile', 'home', 'workspace', 'empty-codex', 'empty-claude', 'xdg', 'plugin-source'):
        p=root/name;p.mkdir(mode=0o700);os.chown(p,959,960)
    os.chown(root/'workspace',1000,1000);(root/'workspace').chmod(0o750)
    copy_name='cwb-pstack-copy-'+root.name
    docker('create','--name',copy_name,IMAGE)
    try:docker('cp',copy_name+':/opt/hermes/trusted-plugins/pstack/.',str(root/'plugin-source'))
    finally:docker('rm',copy_name)
    (root/'plugin-source').chmod(0o755)
    (root/'profile/plugins').mkdir();os.chown(root/'profile/plugins',959,960)
    (root/'profile/plugins/pstack').symlink_to(root/'plugin-source')
    config={
        'model':{'provider':'xai','default':'grok-4.6','api_mode':'codex_responses'},
        'agent':{'reasoning_effort':'xhigh','max_turns':20,'api_max_retries':0},
        'plugins':{'enabled':['pstack']},'security':{'tirith_enabled':False},
        'terminal':{'backend':'docker','cwd':'/workspace','timeout':60,
            'docker_image':IMAGE,'docker_volumes':[str(root/'workspace')+':/workspace:rw'],
            'docker_network':False,'docker_forward_env':[], 'docker_env':{},
            'docker_extra_args':['--label','io.cloudworkbench.first-live='+root.name],
            'container_persistent':False,'docker_auto_mount_cwd':False,'docker_run_as_host_user':False,
            'container_cpu':2,'container_memory':1536},
        'memory':{'memory_enabled':False,'user_profile_enabled':False},
        'mcp_servers':{},'hooks':{},'fallback_model':[],
        'auxiliary':{'transient_retries':0,'title_generation':{'enabled':False,'model_upgrade_enabled':False},'background_review':{'enabled':False}},
        'approvals':{'single_query_mode':'allow'},
    }
    write(root/'profile/config.yaml',json.dumps(config,indent=2)+'\n')
    prompt='''Use the loaded pstack:tdd skill to implement a small useful cloud-job status utility in /workspace. This is an authorized isolated coding task. Work only inside /workspace. Do not access credentials, network services, host paths, or install dependencies. Use Python standard library only. Implement status_summary.py with summarize(records): input is a list of objects with unique nonempty string id and state in queued, preparing, running, verifying, completed, failed, cancelled, interrupted. Return {"total":N,"active":count_of_first_four_states,"terminal":count_of_last_four_states,"by_state":all_eight_state_counts}. Reject malformed records, unknown states, or duplicate IDs with ValueError. CLI: python3 status_summary.py FILE --json reads a JSON list and prints the summary; invalid data prints a concise error to stderr and exits 2. Add unittest tests, README with commands and run the tests. First demonstrate failing tests, then implement and get tests passing. Do not merely describe the code. Finish with file names and actual test results.'''
    write(root/'first-prompt.txt',prompt)
    write(base/'latest',str(root)+'\n',uid=0,gid=0)
    resume=[]
elif mode=='followup':
    root=Path((base/'latest').read_text().strip())
    assert root.parent==base and root.name.startswith('job-')
    events=[json.loads(line) for line in (root/'first.stdout.jsonl').read_text().splitlines() if line.startswith('{')]
    result=next(v for v in reversed(events) if v.get('type')=='result' and v.get('exit_code')==0)
    session=result['session_id'];assert isinstance(session,str) and len(session)<128
    resume=['--resume',session]
    config=json.loads((root/'profile/config.yaml').read_text())
    config['terminal'].update(container_cpu=2,container_memory=1536)
    config['terminal'].pop('cpu',None);config['terminal'].pop('memory',None)
    write(root/'profile/config.yaml',json.dumps(config,indent=2)+'\n')
    prompt='''Continue the utility you implemented in this same session and /workspace. Use pstack:tdd. Add a --strict CLI flag: valid input still prints its normal summary, but exit with status 1 if active > 0 or failed > 0 or interrupted > 0; otherwise exit 0. Without --strict preserve the prior successful exit behavior. cancelled alone is not a strict failure. Invalid input remains exit 2. Add regression tests for both old behavior and all strict cases, update README, and actually run all tests. Do not access network, credentials, or any paths outside /workspace. Finish with actual results.'''
    write(root/'followup-prompt.txt',prompt)
else:raise SystemExit('unknown mode')

name='cwb-hermes-'+root.name+'-'+mode
entry='''import json,os,sys
from pathlib import Path
from datetime import datetime,timezone
p=Path('/run/dedicated-grok-auth.json');v=json.loads(p.read_bytes())
assert len(v)==1
a=next(iter(v.values()))
assert a['auth_mode']=='oidc' and a['oidc_issuer']=='https://auth.x.ai'
assert datetime.fromisoformat(a['expires_at'].replace('Z','+00:00')).timestamp()>datetime.now(timezone.utc).timestamp()+300
os.environ['XAI_API_KEY']=a['key']
os.execv('/opt/hermes/venv/bin/hermes',['/opt/hermes/venv/bin/hermes',*sys.argv[1:]])
'''
write(root/'entry.py',entry)
env={'HOME':str(root/'home'),'HERMES_HOME':str(root/'profile'),
     'CODEX_HOME':str(root/'empty-codex'),'CLAUDE_CONFIG_DIR':str(root/'empty-claude'),
     'XDG_CONFIG_HOME':str(root/'xdg'),'XDG_CACHE_HOME':'/tmp/cache','XDG_DATA_HOME':'/tmp/data',
     'HERMES_BUNDLED_PLUGINS':'/opt/hermes/empty-bundled','HERMES_ENABLE_PROJECT_PLUGINS':'0',
     'HERMES_INTERACTIVE':'0','HERMES_REDACT_SECRETS':'1','PYTHONDONTWRITEBYTECODE':'1',
     'HERMES_DOCKER_BINARY':'/usr/bin/docker','OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1'}
argv=['/usr/bin/docker','run','--init','--name',name,'--label','io.cloudworkbench.first-live-coordinator='+root.name,
      '--user','959:960','--group-add','966','--group-add','1000','--read-only',
      '--cpus','2','--memory','1536m','--memory-swap','1536m','--pids-limit','128',
      '--cap-drop','ALL','--security-opt','no-new-privileges',
      '--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777',
      '--mount','type=bind,src='+str(root)+',dst='+str(root),
      '--mount','type=bind,src='+str(AUTH)+',dst=/run/dedicated-grok-auth.json,readonly',
      '--mount','type=bind,src=/usr/bin/docker,dst=/usr/bin/docker,readonly',
      '--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
      '--workdir',str(root/'home')]
for k,v in env.items():argv+=['-e',k+'='+v]
argv += ['--entrypoint','/opt/hermes/venv/bin/python',IMAGE,str(root/'entry.py'),
         'chat','--query-file',str(root/(mode+'-prompt.txt')),'--format','stream-json','--oneshot',
         '--ignore-rules','--provider','xai','--model','grok-4.6','--reasoning','xhigh',
         '--skills','pstack:tdd','--toolsets','terminal,file','--max-turns','20','--run-budget','420',*resume]
before=services();started=time.time()
record={'root':str(root),'mode':mode,'image':IMAGE,'requested_model':'grok-4.6','requested_effort':'xhigh',
        'provider':'xai via dedicated OAuth access token','services_before':before,'started_at':started,
        'coordinator_docker_authority':True,'tool_network':'none','full_platform_complete':False}
try:
    try:result=subprocess.run(argv,capture_output=True,timeout=480)
    except subprocess.TimeoutExpired as exc:
        record['failure']='coordinator_timeout'
        result=subprocess.CompletedProcess(argv,124,exc.stdout or b'',exc.stderr or b'')
    # Exact-value redaction precedes any persisted CLI output.
    auth=json.loads(AUTH.read_bytes());a=next(iter(auth.values()))
    for channel,raw in [('stdout.jsonl',result.stdout),('stderr.txt',result.stderr)]:
        for k in ('key','refresh_token'):
            value=a.get(k)
            if value:raw=raw.replace(value.encode(),b'[REDACTED]')
        write(root/(mode+'.'+channel),raw.decode('utf-8',errors='replace'))
    record['exit_code']=result.returncode
finally:
    # Stop the authority that could create tools before taking a tool inventory.
    inspected=subprocess.run(['/usr/bin/docker','inspect',name],capture_output=True,timeout=15)
    if inspected.returncode==0:
        data=json.loads(inspected.stdout)[0]
        assert data['Config']['Labels']['io.cloudworkbench.first-live-coordinator']==root.name
        record['coordinator_exit']=data['State']['ExitCode'];docker('rm','-f',name)
    ids=docker('ps','-aq','--filter','label=io.cloudworkbench.first-live='+root.name).stdout.decode().split()
    record['tool_containers']=[]
    for ident in ids:
        data=json.loads(docker('inspect',ident).stdout)[0]
        assert data['Config']['Labels']['io.cloudworkbench.first-live']==root.name
        record['tool_containers'].append({'id':ident,'image':data['Image'],'network':data['HostConfig']['NetworkMode'],
            'mounts':[{'source':m['Source'],'destination':m['Destination'],'rw':m['RW']} for m in data['Mounts']]})
        docker('rm','-f',ident)
    record['remaining_tool_ids']=docker('ps','-aq','--filter','label=io.cloudworkbench.first-live='+root.name).stdout.decode().split()
    record['remaining_coordinator_ids']=docker('ps','-aq','--filter','label=io.cloudworkbench.first-live-coordinator='+root.name).stdout.decode().split()
    assert not record['remaining_tool_ids'] and not record['remaining_coordinator_ids']
    record.update(finished_at=time.time(),services_after=services())
    record['services_unchanged']=record['services_after']==before
    write(root/(mode+'.receipt.json'),json.dumps(record,indent=2)+'\n')
print(json.dumps(record))
