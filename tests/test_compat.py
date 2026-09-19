import hashlib
import json
import pytest
from cloudworkbench.compat import main
from test_cli import server, token_file


def configured(tmp_path, token_file, url, *args):
    path=tmp_path/'compat.json'
    path.write_text(json.dumps({'server':url,'token_file':str(token_file),
       'default':{'project_id':'sample','environment_version':'v1'},
       'repositories':{'https://example.invalid/registered.git':{'main':{'project_id':'repo','environment_version':'pinned'}}}}))
    return ['--config',str(path),*args]


def test_run_preserves_prompt_and_maps_registered_repo(tmp_path,token_file,capsys):
    with server(b'{"session_id":"session-one"}') as (url,calls):
        assert main(configured(tmp_path,token_file,url,'run','claude','literal $(touch NO)',
          '--repo','https://example.invalid/registered.git','--branch','main','--idempotency-key','stable'))==0
    assert json.loads(calls[0][3])=={'project_id':'repo','environment_version':'pinned','agent':'claude','goal':'literal $(touch NO)'}
    assert calls[0][2]['Idempotency-Key']=='stable'
    assert capsys.readouterr().out.strip()=='session-one'


@pytest.mark.parametrize('extra', [['--repo','https://evil.invalid/a'],['--branch','main'],['--provider','x'],['--name','n']])
def test_unsupported_flags_fail_before_mutation(tmp_path,token_file,extra):
    with server() as (url,calls):
        assert main(configured(tmp_path,token_file,url,'run','claude','task',*extra))==1
        assert not calls


def test_rm_never_silently_cancels(tmp_path,token_file):
    with server(status=501) as (url,calls):
        args=configured(tmp_path,token_file,url,'rm','s')
        assert main(args)==1 and not calls
        assert main(args+['--confirm'])==1
    assert [(c[0],c[1]) for c in calls]==[('DELETE','/v1/sessions/s')]


@pytest.mark.parametrize('state,outcome,expected',[('completed','verified',0),('completed','rejected',1),('failed',None,1),('nonsense',None,1)])
def test_status_truthful(tmp_path,token_file,state,outcome,expected):
    body=json.dumps({'attempts':[{'id':'a','generation':1,'state':state,'outcome':outcome}]}).encode()
    with server(body) as (url,_):
        assert main(configured(tmp_path,token_file,url,'status','s'))==expected


def test_wait_deadline_does_not_cancel(tmp_path,token_file):
    body=b'{"attempts":[{"id":"a","generation":1,"state":"queued"}]}'
    with server(body) as (url,calls):
        assert main(configured(tmp_path,token_file,url,'wait','s','--max-seconds','0.01'))==1
    assert all(call[0]=='GET' for call in calls)


def test_log_paginates_and_redacts(tmp_path,token_file,capsys):
    def body(path):
        if path.endswith('after=0'): return b'data: {"sequence":1,"payload":"SECRET_TEST_TOKEN"}\n\n'
        return b''
    with server(body,content_type='text/event-stream') as (url,calls):
        assert main(configured(tmp_path,token_file,url,'log','s'))==0
    assert len(calls)==2
    assert all('follow=false' in call[1] for call in calls)
    output=capsys.readouterr().out
    assert '[REDACTED]' in output and 'SECRET_TEST_TOKEN' not in output


def test_file_chooses_latest_and_verifies_binary(tmp_path,token_file,capfdbinary):
    blob=b'\x00\xff\x80binary'
    def body(path):
        if path.endswith('/artifacts'): return json.dumps({'artifacts':[
            {'id':'old','attempt_id':'a1','path':'out.bin'}, {'id':'new','attempt_id':'a2','path':'out.bin','sha256':hashlib.sha256(blob).hexdigest()}]}).encode()
        if path.endswith('/content'): return blob
        return b'{"attempts":[{"id":"a1","generation":1,"state":"completed"},{"id":"a2","generation":2,"state":"completed"}]}'
    with server(body,extra_headers={'ETag':'"'+hashlib.sha256(blob).hexdigest()+'"'}) as (url,calls):
        assert main(configured(tmp_path,token_file,url,'file','s','out.bin'))==0
    assert calls[-1][1]=='/v1/artifacts/new/content'
    assert capfdbinary.readouterr().out==blob


def test_http_error_nonzero_and_credential_redacted(tmp_path,token_file,capsys):
    with server(b'SECRET_TEST_TOKEN',status=403) as (url,_):
        assert main(configured(tmp_path,token_file,url,'ls'))==1
    assert 'SECRET_TEST_TOKEN' not in capsys.readouterr().err


def test_config_writable_by_others_rejected(tmp_path,token_file):
    with server() as (url,calls):
        args=configured(tmp_path,token_file,url,'ls');(tmp_path/'compat.json').chmod(0o666)
        assert main(args)==1 and not calls


def test_unfinished_latest_never_downloads_previous_file(tmp_path,token_file):
    def body(path):
        if path.endswith('/artifacts'): return b'{"artifacts":[{"id":"old","attempt_id":"a1","path":"x"}]}'
        return b'{"attempts":[{"id":"a1","generation":1,"state":"completed"},{"id":"a2","generation":2,"state":"running"}]}'
    with server(body) as (url,calls):
        assert main(configured(tmp_path,token_file,url,'file','s','x'))==1
    assert not any(c[1].endswith('/content') for c in calls)


def test_file_missing_etag_still_checks_listing_hash(tmp_path,token_file,capfdbinary):
    def body(path):
        if path.endswith('/content'): return b'wrong'
        if path.endswith('/artifacts'): return json.dumps({'artifacts':[{'id':'a','attempt_id':'a1','path':'x','sha256':'0'*64}]}).encode()
        return b'{"attempts":[{"id":"a1","generation":1,"state":"completed"}]}'
    with server(body) as (url,_):
        assert main(configured(tmp_path,token_file,url,'file','s','x'))==1
    assert capfdbinary.readouterr().out==b''


@pytest.mark.parametrize('state,outcome,code',[('paused',None,1),('completed','unverified',0),('completed','needs_review',0)])
def test_exit_semantics(tmp_path,token_file,state,outcome,code):
    with server(json.dumps({'attempts':[{'generation':1,'state':state,'outcome':outcome}]}).encode()) as (url,_):
        assert main(configured(tmp_path,token_file,url,'status','s'))==code


def test_wait_transitions_then_reads_log(tmp_path,token_file,monkeypatch):
    import cloudworkbench.compat as compat
    monkeypatch.setattr(compat.time,'sleep',lambda seconds:None)
    states=iter(['running','completed'])
    def body(path):
        if '/events?' in path:return b''
        return json.dumps({'attempts':[{'generation':1,'state':next(states),'outcome':'verified'}]}).encode()
    with server(body,content_type='text/event-stream') as (url,calls):
        assert main(configured(tmp_path,token_file,url,'wait','s'))==0
    assert len(calls)==3
