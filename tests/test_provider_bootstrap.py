import hashlib
import json
from pathlib import Path
import sys

from cloudworkbench.inference_transport import PinnedCLI
from cloudworkbench.provider_bootstrap import run_token_request

TOKEN='sk-ant-oat-synthetic-only-never-real'
REQUEST={'messages':[{'role':'user','content':'synthetic request'}],'tools':[]}


def setup(tmp_path,body):
    binary=tmp_path/'fake-cli'
    binary.write_text('#!'+sys.executable+'\nimport os,json,sys\nsys.stdin.read()\n'+body)
    binary.chmod(0o700)
    home=tmp_path/'home';home.mkdir(mode=0o700)
    scratch=tmp_path/'scratch';scratch.mkdir(mode=0o700)
    credential=tmp_path/'token';credential.write_text(TOKEN);credential.chmod(0o600)
    profile=PinnedCLI(binary,hashlib.sha256(binary.read_bytes()).hexdigest(),'2.1.274','claude-fable-5-1','high')
    return profile,home,scratch,credential


def output(text):
    return "print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':{'kind':'final','text':"+text+",'tool_calls':[]}}))\n"


def test_token_only_in_child_env_not_argv_and_no_copyback(tmp_path):
    profile,home,scratch,credential=setup(tmp_path,"assert os.environ['DISABLE_AUTOUPDATER']=='1'\nassert os.environ['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']=='1'\nassert os.environ['CLAUDE_CODE_OAUTH_TOKEN']=="+repr(TOKEN)+"\nassert "+repr(TOKEN)+" not in str(sys.argv)\n"+output(repr('synthetic okay')))
    before=credential.read_bytes()
    result=run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=credential)
    assert result['result']['status']=='ok'
    assert TOKEN not in json.dumps(result)
    assert result['credential_reuse_authorized'] is False
    assert credential.read_bytes()==before


def test_exact_secret_in_decision_never_returns(tmp_path):
    profile,home,scratch,credential=setup(tmp_path,output("'prefix '+os.environ['CLAUDE_CODE_OAUTH_TOKEN']"))
    result=run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=credential)
    assert result['error']=='credential_exposure_detected'
    assert TOKEN not in json.dumps(result)


def test_bad_credential_no_cli_start(tmp_path):
    profile,home,scratch,credential=setup(tmp_path,"open('started','w').write('yes')\n"+output(repr('okay')))
    credential.chmod(0o644)
    result=run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=credential)
    assert result['error']=='auth_invalid'
    assert not (scratch/'started').exists()


def test_missing_and_symlinked_credential_refused(tmp_path):
    profile,home,scratch,credential=setup(tmp_path,output(repr('okay')))
    link=tmp_path/'link';link.symlink_to(credential)
    for path,code in [(link,'auth_invalid'),(tmp_path/'missing','auth_missing')]:
        result=run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=path)
        assert result['error']==code


def test_runtime_auth_failure_has_same_top_level_contract(tmp_path):
    profile,home,scratch,credential=setup(tmp_path,"print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'api_error_status':401}))\n")
    result=run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=credential)
    assert result['status']=='error' and result['error']=='provider_auth_rejected'
    assert result['result']['error_code']==result['error']


def test_group_read_is_supported_but_group_write_is_not(tmp_path):
    profile,home,scratch,credential=setup(tmp_path,output(repr('okay')))
    credential.chmod(0o640)
    assert run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=credential)['status']=='ok'
    credential.chmod(0o660)
    assert run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=credential)['error']=='auth_invalid'


def test_private_tmpfs_state_is_not_exported_or_claimed_absent(tmp_path):
    body="open(os.path.join(os.environ['HOME'],'private-state'),'w').write(os.environ['CLAUDE_CODE_OAUTH_TOKEN'])\n"+output(repr('okay'))
    profile,home,scratch,credential=setup(tmp_path,body)
    result=run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=credential)
    assert result['status']=='ok' and (home/'private-state').read_text()==TOKEN
    assert TOKEN not in json.dumps(result) and result['container_cleanup_required']


def test_shared_scratch_refused(tmp_path):
    profile,home,scratch,credential=setup(tmp_path,output(repr('okay')))
    scratch.chmod(0o1777)
    assert run_token_request(profile,REQUEST,home=home,scratch=scratch,credential_path=credential)['error']=='unsafe_private_directory'
