"""Offline check inside the worker image; no credentials or model requests."""
import hashlib, json, os, subprocess, sys, time
from pathlib import Path
from cloudworkbench import crabbox_guest as guest, pstack_child

expected = Path('/opt/hermes/trusted-plugins/cloud-evidence/SOUL.md').read_text()
guest.STATE = Path('/tmp/soul-main-state')
command, env, prompt = guest.prepare({'prompt':'Offline instruction loading check', 'resume_session_id':None, 'api_key':'synthetic-not-a-credential'})
assert '--ignore-rules' not in command
root = Path('/tmp/soul-children');root.mkdir(mode=0o700)
plans=[('main',env)]
for role in ('feature','code_review'):
    plan=pstack_child.prepare({'role':role,'prompt':'Offline instruction loading check','api_key':'synthetic-not-a-credential','deadline':time.time()+300,'profile_root':str(root/role),'max_turns':2},allowed_root=root)
    assert not plan.cli_kwargs['ignore_rules']
    plans.append((role,plan.environment))
code='''
import hashlib,json,os
from pathlib import Path
from run_agent import AIAgent
from agent.system_prompt import build_system_prompt, invalidate_system_prompt
expected=Path(os.environ['HERMES_HOME'],'SOUL.md').read_text()
a=AIAgent(model='offline-test',provider='custom',api_mode='chat_completions',base_url='http://127.0.0.1:9/v1',api_key='synthetic-not-a-credential',enabled_toolsets=[],quiet_mode=True,skip_context_files=False,skip_memory=True,skip_background_review=True,cwd='/workspace')
first=build_system_prompt(a)
assert expected.strip() in first, 'SOUL not included in initial system prompt'
invalidate_system_prompt(a)
after=build_system_prompt(a)
assert expected.strip() in after, 'SOUL not included after compaction invalidation/rebuild'
print(json.dumps({'soul_sha256':hashlib.sha256(expected.encode()).hexdigest(),'initial_system_prompt_contains_full_soul':True,'post_compaction_rebuild_contains_full_soul':True,'provider_requests':0}))
'''
checks=[]
for role,environment in plans:
    environment=dict(environment,PYTHONPATH='/opt/hermes/source:/opt/cloud-pstack',HERMES_TELEMETRY_ENABLED='false')
    result=subprocess.run(['/opt/hermes/venv/bin/python','-c',code],env=environment,text=True,capture_output=True)
    if result.returncode:
        print(result.stdout);print(result.stderr,file=sys.stderr);raise SystemExit(result.returncode)
    data=json.loads(result.stdout.strip().splitlines()[-1]);data['role']=role;checks.append(data)
print(json.dumps({'checks':checks,'network':'disabled','scope':'native system prompt assembly and post-compaction rebuild; no live provider call'},indent=2))
