"""Runs inside a bounded no-network container with synthetic credentials only."""
import hashlib,json,os,subprocess,sys,uuid
from pathlib import Path
from cloudworkbench.adapters import EventSpoolWriter,read_event_spool
from cloudworkbench.entrypoint import capture_provider
from cloudworkbench.credential_state import CredentialState

checks={}
os.umask(0o007)
for p in ('/state','/workspace','/run/task','/run/secrets'):Path(p).mkdir(exist_ok=True)
for kind in ('missing','invalid'):
 token=Path('/run/secrets/claude-token')
 if kind=='invalid':token.write_text('SYNTHETIC-INVALID');token.chmod(0o640)
 name=str(uuid.uuid4())+'.1.jsonl';task={'adapter':'claude','prompt':'must not call provider','event_spool':name}
 Path('/run/task/task.json').write_text(json.dumps(task))
 completed=subprocess.run([sys.executable,'/opt/auth-unit/cloudworkbench/entrypoint.py','/run/task/task.json'],capture_output=True,timeout=10)
 records=read_event_spool(Path('/state/events')/name,final=True)['events'];result=records[-1]['event']['payload']
 checks['entrypoint_'+kind]=completed.returncode==1 and result['failure_code']=='auth_'+kind and result['usage_status']['reason']=='missing_result' and b'SYNTHETIC-INVALID' not in completed.stdout+completed.stderr

def capture(name,raw):
 writer=EventSpoolWriter(Path('/state')/name)
 script='import json;r='+repr(raw)+';[print(json.dumps(x)) for x in r]'
 # Suppress public output locally; only boolean assertions leave the qualifier.
 with open('/dev/null','w') as output:
  old=sys.stdout;sys.stdout=output
  try:capture_provider([sys.executable,'-c',script],'',os.environ.copy(),writer)
  finally:sys.stdout=old;writer.close()
 return read_event_spool(Path('/state')/name,final=True)['events'][-1]['event']['payload']
failed=capture('auth-events',[{'type':'assistant','error':'authentication_failed','message':{'content':[]}},{'type':'result','is_error':True,'subtype':'error_during_execution'}])
checks['structured_auth_rejection']=failed['failure_code']=='provider_auth_rejected' and failed['provenance']=='worker_reported'
success=capture('success-events',[{'type':'assistant','error':'authentication_failed','message':{'content':[]}},{'type':'result','is_error':False,'usage':{'input_tokens':0,'output_tokens':0}}])
checks['transient_error_then_success']=success['failure_code'] is None and success['usage_status']['state']=='reported'
limited=capture('rate-events',[{'type':'result','is_error':True,'api_error_status':429}])
checks['rate_limit_explicit']=limited['failure_code']=='rate_limited'
prose=capture('prose-events',[{'type':'result','is_error':True,'result':'401 authentication_failed rate_limit'}])
checks['prose_not_classified']=prose['failure_code'] is None
path=Path('/state/startup-token');first=b'sk-ant-oat-synthetic-'+uuid.uuid4().hex.encode();path.write_bytes(first);path.chmod(0o640)
statepath=Path('/state/private/claude.json');state=CredentialState(statepath,first);state.initialize();state.quarantine(failed['failure_code'])
script='from pathlib import Path;from cloudworkbench.credential_state import CredentialState;from cloudworkbench.adapters import read_credential;p=Path("/state/startup-token");s=CredentialState(Path("/state/private/claude.json"),read_credential(p)[0]);print(s.blocked_reason(p))'
checks['quarantine_persists_across_process_restart']=subprocess.check_output([sys.executable,'-c',script],text=True).strip()=='provider_auth_rejected'
replacement=b'sk-ant-oat-replacement-'+uuid.uuid4().hex.encode();path.write_bytes(replacement)
checks['replacement_requires_restart']=state.blocked_reason(path)=='auth_changed_restart_required'
checks['replacement_identity_allowed_after_restart']=subprocess.check_output([sys.executable,'-c',script],text=True).strip()=='None'
checks['private_state_contains_no_token']=first not in statepath.read_bytes() and replacement not in statepath.read_bytes() and statepath.stat().st_mode&0o777==0o600
recovered=capture('recovered-error',[{'type':'assistant','error':'authentication_failed','message':{'content':[]}},{'type':'assistant','message':{'content':[{'type':'text','text':'recovered'}]}},{'type':'result','is_error':True,'subtype':'error_max_turns'}])
checks['unrelated_terminal_failure_not_auth']=recovered['failure_code'] is None
state.clear_quarantine(operator_confirmed=True)
checks['explicit_operator_recovery']=CredentialState(statepath,first).blocked_reason() is None
statepath.unlink()
checks['lost_state_fails_closed']=CredentialState(statepath,first).blocked_reason()=='credential_state_invalid'
checks['unknown_usage_reason']=failed['usage_status']=={'state':'unknown','reason':'provider_did_not_report'}
print(json.dumps({'checks':checks,'passed':all(checks.values()),'network':'none','provider_called':False,'synthetic_only':True,'uid':os.getuid(),'source_sha256':{n:hashlib.sha256(Path(sys.modules['cloudworkbench.'+n].__file__).read_bytes()).hexdigest() for n in ('adapters','entrypoint','credential_state')}},indent=2));raise SystemExit(0 if all(checks.values()) else 1)
