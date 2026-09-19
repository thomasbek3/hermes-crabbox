import json
import os
from pathlib import Path
import stat
import pytest
from cloudworkbench import legacy_import as li

JOB='0917-015824-4707'
@pytest.fixture
def setup(tmp_path):
    root=tmp_path.resolve();workspace=root/'jobs'/JOB/'work';workspace.mkdir(parents=True)
    (workspace/'hello.txt').write_text('Hello from the legacy workspace.\n')
    private=root/'private';private.mkdir(mode=0o700)
    return workspace,private/'staged'

def stage(setup,files=None,**kwargs):
    workspace,destination=setup
    return li.stage(workspace,JOB,files or ['hello.txt'],destination,quiescence_confirmed=kwargs.get('quiescence_confirmed',True))

def test_explicit_stage_is_private_hashed_untrusted_and_source_intact(setup):
    workspace,destination=setup;before=(workspace/'hello.txt').stat();result=stage(setup)
    assert result==json.loads((destination/'manifest.json').read_text())
    assert (destination/'object-0000.txt').read_bytes()==(workspace/'hello.txt').read_bytes()
    assert result['files'][0]['sha256']==li._digest((workspace/'hello.txt').read_bytes())
    assert result['human_review_required_before_upload'] and not result['uploaded'] and not result['task_created'] and not result['trusted_verification']
    assert stat.S_IMODE(destination.stat().st_mode)==0o700
    assert all(stat.S_IMODE(p.stat().st_mode)==0o600 for p in destination.iterdir())
    assert li._signature(before)==li._signature((workspace/'hello.txt').stat())

@pytest.mark.parametrize('entry',['../log','/etc/passwd','hello.txt/../log','./hello.txt','a//b','.git/config','.env','.env.production','HANDOFF.md','handoff-copy.txt','.claude/state','native/session','cache/a','secrets/a','foo.pem','server/token','a\\b','a\nb'])
def test_disallowed_paths(setup,entry):
    with pytest.raises(li.ImportRefused):stage(setup,[entry])
    assert not setup[1].exists()

@pytest.mark.parametrize('mode',['symlink','directory_symlink','hardlink','fifo','directory'])
def test_unsafe_source_types(setup,mode,tmp_path):
    workspace,destination=setup;outside=tmp_path/'outside';outside.write_text('outside')
    if mode=='symlink':(workspace/'chosen').symlink_to(outside);selected='chosen'
    elif mode=='directory_symlink':(workspace/'dir').symlink_to(tmp_path,target_is_directory=True);selected='dir/outside'
    elif mode=='hardlink':os.link(outside,workspace/'chosen');selected='chosen'
    elif mode=='fifo':os.mkfifo(workspace/'chosen');selected='chosen'
    else:(workspace/'chosen').mkdir();selected='chosen'
    with pytest.raises(li.ImportRefused):stage(setup,[selected])
    assert outside.read_text()=='outside' and not destination.exists()

@pytest.mark.parametrize('content',['api_key="super-secret-value"','-----BEGIN PRIVATE KEY-----','sk-ant-oat-'+'a'*35,'Bearer '+'a'*30,'https://user:password@example.com','eyJ'+'a'*12+'.'+'b'*15+'.'+'c'*15,'abc\0def'])
def test_secret_like_content_refused_without_printing(setup,content,capsys):
    (setup[0]/'hello.txt').write_text(content)
    with pytest.raises(li.ImportRefused) as error:stage(setup)
    assert content not in str(error.value) and not setup[1].exists() and capsys.readouterr().out==''

def test_unselected_sensitive_content_never_read(setup,monkeypatch):
    workspace,destination=setup;(workspace/'HANDOFF.md').write_text('password=do-not-read')
    opened=[];original=os.open
    def tracked(path,*args,**kwargs):opened.append(str(path));return original(path,*args,**kwargs)
    monkeypatch.setattr(li.os,'open',tracked);stage(setup)
    assert 'HANDOFF.md' not in opened

def test_bounds_and_binary(setup,monkeypatch):
    monkeypatch.setattr(li,'MAX_FILE_BYTES',5)
    with pytest.raises(li.ImportRefused):stage(setup)
    monkeypatch.setattr(li,'MAX_FILE_BYTES',100)
    monkeypatch.setattr(li,'MAX_TOTAL_BYTES',5)
    with pytest.raises(li.ImportRefused):stage(setup)
    monkeypatch.setattr(li,'MAX_TOTAL_BYTES',100)
    (setup[0]/'hello.txt').write_bytes(b'\xff\xfe')
    with pytest.raises(li.ImportRefused):stage(setup)

@pytest.mark.parametrize('change',['rewrite_same_size','replace_file','replace_parent','add_hardlink'])
def test_source_changes_during_copy_refused_and_stage_removed(setup,monkeypatch,change):
    workspace,destination=setup
    if change=='replace_parent':
        (workspace/'sub').mkdir();(workspace/'hello.txt').rename(workspace/'sub/hello.txt');selected='sub/hello.txt'
    else:selected='hello.txt'
    original=li._write
    def changed(*args):
        original(*args)
        if args[1]=='object-0000.txt':
            path=workspace/selected
            if change=='rewrite_same_size':path.write_bytes(b'X'*path.stat().st_size)
            elif change=='replace_file':path.rename(path.with_name('moved'));path.write_text('replacement')
            elif change=='replace_parent':
                (workspace/'sub').rename(workspace/'old');(workspace/'sub').mkdir();(workspace/'sub/hello.txt').write_text('replacement')
            else:os.link(path,workspace/'linked')
    monkeypatch.setattr(li,'_write',changed)
    with pytest.raises(li.ImportRefused):stage(setup,[selected])
    assert not destination.exists()

def test_existing_destination_and_nonprivate_parent_are_not_modified(setup):
    workspace,destination=setup;destination.mkdir();(destination/'keep').write_text('original')
    with pytest.raises(li.ImportRefused):stage(setup)
    assert (destination/'keep').read_text()=='original'
    destination.parent.chmod(0o755)
    with pytest.raises(li.ImportRefused):li.stage(workspace,JOB,['hello.txt'],destination.parent/'other',quiescence_confirmed=True)

def test_exact_root_job_and_quiescence_required(setup):
    with pytest.raises(li.ImportRefused):stage(setup,quiescence_confirmed=False)
    with pytest.raises(li.ImportRefused):li.stage(setup[0],'0917-000000-aaaa',['hello.txt'],setup[1],quiescence_confirmed=True)
    with pytest.raises(li.ImportRefused):li.stage(setup[0],JOB,[],setup[1],quiescence_confirmed=True)
    with pytest.raises(li.ImportRefused):stage(setup,['hello.txt','HELLO.TXT'])

def test_destination_replacement_not_deleted_on_failure(setup,monkeypatch):
    workspace,destination=setup;original=li._write
    def replacement(*args):
        original(*args)
        if args[1]=='object-0000.txt':
            destination.rename(destination.with_name('original-stage'));destination.mkdir();(destination/'keep').write_text('keep')
            (workspace/'hello.txt').write_text('changed')
    monkeypatch.setattr(li,'_write',replacement)
    with pytest.raises(li.ImportRefused,match='operator cleanup'):stage(setup)
    assert (destination/'keep').read_text()=='keep'
