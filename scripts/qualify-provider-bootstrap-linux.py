"""Synthetic-token proof inside disposable network-none provider boundary."""
import hashlib,json,os,sys
from pathlib import Path
from cloudworkbench.provider_bootstrap import run_token_request
from cloudworkbench.inference_transport import PinnedCLI
ROOT=Path(__file__).resolve().parents[1]
TOKEN='sk-ant-oat-synthetic-only-not-a-real-credential'
REQUEST={'messages':[{'role':'user','content':'synthetic'}],'tools':[]}
assert TOKEN=='sk-ant-oat-synthetic-only-not-a-real-credential'
checks=[]
for name,mode in [('normal','normal'),('literal_exposure','expose'),('auth_failure','auth'),('private_state','persist')]:
 folder=ROOT/name;folder.mkdir(mode=0o700)
 home=folder/'home';home.mkdir(mode=0o700)
 scratch=folder/'scratch';scratch.mkdir(mode=0o700)
 token=folder/'token';token.write_text(TOKEN);token.chmod(0o400)
 binary=folder/'fake-cli'
 code='#!'+sys.executable+'\nimport os,sys,json\nsys.stdin.read()\n'
 code+="assert os.environ['DISABLE_AUTOUPDATER']=='1' and os.environ['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']=='1'\n"
 code+="assert os.environ['CLAUDE_CODE_OAUTH_TOKEN']=="+repr(TOKEN)+"\nassert "+repr(TOKEN)+" not in str(sys.argv)\n"
 if mode=='persist':code+="open(os.path.join(os.environ['HOME'],'private-state'),'w').write(os.environ['CLAUDE_CODE_OAUTH_TOKEN'])\n"
 if mode=='auth':code+="print(json.dumps({'type':'result','is_error':True,'api_error_status':401}))\n"
 else:code+="print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':{'kind':'final','text':"+("os.environ['CLAUDE_CODE_OAUTH_TOKEN']" if mode=='expose' else "'synthetic okay'")+",'tool_calls':[]}}))\n"
 binary.write_text(code);binary.chmod(0o700)
 profile=PinnedCLI(binary,hashlib.sha256(binary.read_bytes()).hexdigest(),'2.1.274','claude-fable-5-1','high')
 result=run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=token)
 expected={'normal':None,'expose':'credential_exposure_detected','auth':'provider_auth_rejected','persist':None}[mode]
 assert result['error']==expected and result['status']==('error' if expected else 'ok')
 assert TOKEN not in json.dumps(result) and result['container_cleanup_required'] and not result['credential_reuse_authorized']
 assert token.read_text()==TOKEN
 if mode=='persist':assert (home/'private-state').read_text()==TOKEN
 checks.append({'name':name,'passed':True,'error':result['error'],'token_mode':oct(token.stat().st_mode&0o777),'token_uid':token.stat().st_uid,'private_tmpfs_persistence_permitted':mode=='persist'})

for invalid in ['world_read','group_write','symlink','hardlink','missing']:
 path=ROOT/('invalid-'+invalid)
 if invalid!='missing':
  if invalid=='symlink':path.symlink_to(token)
  else:
   path.write_text(TOKEN);path.chmod(0o644 if invalid=='world_read' else 0o660 if invalid=='group_write' else 0o400)
   if invalid=='hardlink':os.link(path,ROOT/'second-hardlink')
 result=run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=path)
 assert result['error']==('auth_missing' if invalid=='missing' else 'auth_invalid')
 checks.append({'name':invalid,'passed':True,'error':result['error']})

print(json.dumps({'passed':len(checks),'failed':0,'checks':checks,'suite':'Linux synthetic setup-token bootstrap','source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in json.loads((ROOT/'source-manifest.json').read_text())},'uid':os.getuid(),'real_credentials':False,'provider_calls':False,'real_cli':False,'read_only_bind_mount_proven':False,'fake_cli_requires_executable_tmpfs':True,'production_should_use_readonly_image_binary':True}))
