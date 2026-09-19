#!/usr/bin/env python3
"""One bounded official-CLI authentication probe; never emit credentials."""
import json
from pathlib import Path
import sys
import time
import uuid

from cloudworkbench.runtime import Runtime

PROGRAM = r'''
import json,os,pathlib,subprocess
token=pathlib.Path('/run/secrets/claude-token').read_text().strip()
env=os.environ.copy()
env.update(HOME='/home/agent',CLAUDE_CONFIG_DIR='/home/agent/claude',CLAUDE_CODE_OAUTH_TOKEN=token,DISABLE_AUTOUPDATER='1',CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
args=['claude','--print','--output-format','json','--safe-mode','--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--tools','','--no-session-persistence','--max-turns','1']
p=subprocess.run(args,input=b'Reply with exactly CLOUD_AUTH_OK. Do not use any tools.',stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=120)
try:result=json.loads(p.stdout)
except ValueError:result={}
message=str(result.get('result',''))
report={'returncode':p.returncode,'passed':p.returncode==0 and result.get('is_error') is False and message.strip()=='CLOUD_AUTH_OK','is_error':result.get('is_error'),'subtype':result.get('subtype'),'model_usage':result.get('modelUsage'),'usage':result.get('usage')}
if not report['passed']:
    import re
    error=(message+' '+p.stderr.decode(errors='replace')).replace(token,'[REDACTED]')
    report['diagnostic']=re.sub(r'sk-ant-\S+','[REDACTED]',error)[:700]
print(json.dumps(report),flush=True)
'''

config = json.loads(Path('/etc/cloud-workbench/worker.json').read_text())['runtime']
config.update(owner='authprobe', workspace_mib=64, network_enabled=True,
              allowed_domains=['api.anthropic.com','claude.ai','platform.claude.com'],
              approved_mount_roots=['/var/lib/cloud-workbench/auth'])
runtime = Runtime(config)
attempt = 'auth-' + uuid.uuid4().hex
report = {'attempt':attempt,'scope':'official Claude CLI, one no-tools response'}
try:
    rid = runtime.launch(attempt,attempt,['python3','-c',PROGRAM],{},
        [{'source':'/var/lib/cloud-workbench/auth/claude-token','target':'/run/secrets/claude-token','readonly':True}],generation=1)
    deadline=time.monotonic()+150
    while time.monotonic()<deadline:
        status=runtime.status(rid,expected_generation=1)
        if status['state']!='running':break
        time.sleep(.5)
    if status['state']=='running':
        report.update(passed=False,error='probe_timeout')
    else:
        report['container_exit_code']=status.get('exit_code')
        logs=runtime.logs(rid,max_bytes=32768)
        try:report.update(json.loads(logs))
        except ValueError:report.update(passed=False,error='no_structured_probe_result')
finally:
    runtime.cleanup_attempt(attempt,expected_generation=1)
Path(sys.argv[1]).write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
sys.exit(0 if report.get('passed') else 1)
