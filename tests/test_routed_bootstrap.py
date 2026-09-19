from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace

import pytest

from cloudworkbench import routed_bootstrap as boot
from cloudworkbench.hermes_adapter import build_routed_launch
from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.workflow_instructions import StageInstructions,STAGE_BOUNDARY


@pytest.fixture
def bootstrap_request():
    text='Synthetic bootstrap review.'
    instructions=StageInstructions('review','code_review','fixture',hashlib.sha256(text.encode()).hexdigest(),text,STAGE_BOUNDARY)
    profile=NativeProfile('openai-codex','gpt-6-astra','high')
    plan=build_routed_launch(profile,instructions,task='Read fixture',input_revision_sha256='a'*64,workspace_readonly=True)
    return boot.BootstrapRequest('child','session',1,profile.digest,plan)


@pytest.fixture
def privileged(tmp_path,monkeypatch):
    """Real local paths/inodes/bytes; simulated root/chown DAC, not Linux proof."""
    temporary=tempfile.TemporaryDirectory(prefix='cwbboot-',dir='/tmp')
    base=Path(temporary.name).resolve()
    root=base/'b';root.mkdir(mode=0o750)
    source=base/'s';source.mkdir(mode=0o755)
    hashes={}
    for name in boot.SOURCE_FILES:
        path=source/name;path.write_text('# trusted fixture '+name+'\n');path.chmod(0o444)
        hashes[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    original_stat,original_fstat,original_chmod=os.stat,os.fstat,Path.chmod
    modes={}
    ownership={(root.stat().st_dev,root.stat().st_ino):(0,960)}
    class Metadata:
        def __init__(self,value):self.value=value
        def __getattr__(self,key):
            if key=='st_mode':
                mode=modes.get((self.value.st_dev,self.value.st_ino))
                return self.value.st_mode if mode is None else stat.S_IFMT(self.value.st_mode)|mode
            if key in ('st_uid','st_gid'):
                value=ownership.get((self.value.st_dev,self.value.st_ino),(0,0))
                return value[0 if key=='st_uid' else 1]
            return getattr(self.value,key)
    def set_owner(info,uid,gid):
        current=ownership.get((info.st_dev,info.st_ino),(0,0))
        ownership[(info.st_dev,info.st_ino)]=(current[0] if uid==-1 else uid,current[1] if gid==-1 else gid)
    def chmod(path,mode,**kwargs):
        info=original_stat(path,follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            modes[(info.st_dev,info.st_ino)]=mode
            return original_chmod(path,mode|0o700,**kwargs)
        return original_chmod(path,mode,**kwargs)
    monkeypatch.setattr(Path,'chmod',chmod)
    monkeypatch.setattr(os,'geteuid',lambda:0)
    monkeypatch.setattr(os,'stat',lambda *a,**k:Metadata(original_stat(*a,**k)))
    monkeypatch.setattr(os,'fstat',lambda fd:Metadata(original_fstat(fd)))
    monkeypatch.setattr(os,'chown',lambda path,uid,gid,**kw:set_owner(original_stat(path,follow_symlinks=False),uid,gid))
    monkeypatch.setattr(os,'fchown',lambda fd,uid,gid:set_owner(original_fstat(fd),uid,gid))
    helper=boot.RoutedBootstrap(destination_root=root,source_root=source,source_hashes=hashes,
        authorize=lambda _:True,authorize_discard=lambda _:True)
    yield helper,root,source,ownership
    temporary.cleanup()


def test_exact_material_modes_distinct_caps_no_public_secret(privileged,bootstrap_request):
    helper,root,source,_=privileged
    value=helper.provision(bootstrap_request)
    assert value.root.parent==root and value.binding_digest==bootstrap_request.digest
    client=json.loads((value.worker_socket_dir/'client.json').read_text())
    assert client['binding']=={'attempt_id':'child','generation':1,'profile_digest':bootstrap_request.profile_digest}
    http=(value.task_dir/'http-capability').read_text()
    assert http==client['http_capability'] and http!=client['worker_capability']
    assert hashlib.sha256(http.encode()).hexdigest()==value.http_capability_sha256
    assert hashlib.sha256(client['worker_capability'].encode()).hexdigest()==value.worker_capability_sha256
    assert http not in repr(value) and client['worker_capability'] not in repr(value)
    for path,uid,gid,mode in ((value.root,0,960,0o750),(value.task_dir,959,1000,0o751),
            (value.worker_socket_dir,959,1001,0o2750),(value.materialization_parent,959,1000,0o2750),
            (value.task_dir/'http-capability',959,1000,0o440),(value.worker_socket_dir/'client.json',959,1001,0o440)):
        info=path.stat();assert (info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))==(uid,gid,mode)
    assert json.loads((value.task_dir/'launch.json').read_text())['prompt']==bootstrap_request.plan.prompt
    assert {p.name for p in value.worker_socket_dir.iterdir()}=={'client.json'}
    assert list(value.materialization_parent.iterdir())==[]


def test_no_root_or_authority_refuses_before_material(privileged,bootstrap_request,monkeypatch):
    helper,root,*_=privileged
    monkeypatch.setattr(os,'geteuid',lambda:959)
    with pytest.raises(boot.BootstrapError,match='root_request'):helper.provision(bootstrap_request)
    monkeypatch.setattr(os,'geteuid',lambda:0);helper.authorize=lambda _:False
    with pytest.raises(boot.BootstrapError,match='authority'):helper.provision(bootstrap_request)
    assert list(root.iterdir())==[]


@pytest.mark.parametrize('case',['digest','symlink','hardlink','writable'])
def test_source_policy_refuses_tampered_code(privileged,bootstrap_request,case):
    helper,root,source,_=privileged
    path=source/'routed_caller.py'
    if case=='digest':path.chmod(0o644);path.write_text('changed')
    elif case=='symlink':path.unlink();path.symlink_to(source/'adapters.py')
    elif case=='hardlink':os.link(path,source/'other.py')
    else:path.chmod(0o666)
    with pytest.raises((boot.BootstrapError,OSError)):helper.provision(bootstrap_request)
    assert list(root.iterdir())==[]


def test_same_attempt_refused_and_other_generation_preserved(privileged,bootstrap_request):
    helper,*_=privileged
    first=helper.provision(bootstrap_request);second=helper.provision(replace(bootstrap_request,generation=2))
    with pytest.raises(boot.BootstrapError,match='attempt_exists'):helper.provision(bootstrap_request)
    helper.discard_unlaunched(first)
    assert not first.root.exists() and second.root.exists()
    with pytest.raises(boot.BootstrapError):helper.provision(bootstrap_request)  # permanent claim prevents replay


def test_revocation_before_publish_rolls_back_only_private_owned_paths(privileged,bootstrap_request):
    helper,root,*_=privileged;count=[0]
    def authorize(_):count[0]+=1;return count[0]==1
    helper.authorize=authorize
    with pytest.raises(boot.BootstrapError,match='authority'):helper.provision(bootstrap_request)
    assert list(root.iterdir())==[]


def test_mid_write_failure_cleans_private_staging(privileged,bootstrap_request,monkeypatch):
    helper,root,*_=privileged;original=os.fchown;count=[0]
    def failure(fd,uid,gid):
        count[0]+=1
        if count[0]==3:raise OSError('synthetic failure')
        return original(fd,uid,gid)
    monkeypatch.setattr(os,'fchown',failure)
    with pytest.raises(boot.BootstrapError,match='filesystem_failure'):helper.provision(bootstrap_request)
    assert list(root.iterdir())==[]


@pytest.mark.parametrize('case',['extra','changed','symlink','denied','wrong_binding'])
def test_discard_rejects_in_use_or_changed_material(privileged,bootstrap_request,case):
    helper,*_=privileged;value=helper.provision(bootstrap_request)
    if case=='extra':(value.worker_socket_dir/'socket').write_text('not actually a socket')
    elif case=='changed':(value.task_dir/'prompt.txt').chmod(0o640);(value.task_dir/'prompt.txt').write_text('changed')
    elif case=='symlink':
        target=value.task_dir/'prompt.txt';target.unlink();target.symlink_to(value.task_dir/'config.yaml')
    elif case=='denied':helper.authorize_discard=lambda _:False
    else:value=replace(value,binding_digest='b'*64)
    with pytest.raises(boot.BootstrapError):helper.discard_unlaunched(value)
    assert value.root.exists()


@pytest.mark.parametrize('field,value',[('attempt_id','../escape'),('generation',True),('generation',0),('profile_digest','bad')])
def test_request_fixed_identity_validation(bootstrap_request,field,value):
    with pytest.raises(boot.BootstrapError):replace(bootstrap_request,**{field:value})


def test_mismatched_plan_refused(bootstrap_request):
    with pytest.raises(boot.BootstrapError,match='plan_mismatch'):
        replace(bootstrap_request,plan=replace(bootstrap_request.plan,prompt='changed'))


def test_lost_claim_prevents_replay_without_deleting_claim(privileged,bootstrap_request):
    helper,root,*_=privileged;claim=root/(bootstrap_request.directory_name+'.claim');claim.write_text(bootstrap_request.digest)
    with pytest.raises(boot.BootstrapError):helper.provision(bootstrap_request)
    assert list(root.iterdir())==[claim] and claim.read_text()==bootstrap_request.digest


def test_concurrent_same_attempt_publishes_only_one_material(privileged,bootstrap_request):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    helper,root,*_=privileged
    barrier=threading.Barrier(2)
    lock=threading.Lock();calls=[0]
    def authorize(_):
        with lock:calls[0]+=1;ordinal=calls[0]
        if ordinal<=2:barrier.wait(timeout=3)
        return True
    helper.authorize=authorize
    def provision():
        try:return helper.provision(bootstrap_request)
        except boot.BootstrapError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(lambda _:provision(),range(2)))
    assert sum(v is not None for v in values)==1
    assert sorted(p.name for p in root.iterdir())==sorted([bootstrap_request.directory_name,bootstrap_request.directory_name+'.claim'])


def test_private_root_policy_rechecked_before_provision(privileged,bootstrap_request):
    helper,root,*_=privileged;root.chmod(0o751)
    with pytest.raises(boot.BootstrapError,match='root_changed'):helper.provision(bootstrap_request)
    assert list(root.iterdir())==[]


def test_capability_collision_fails_without_published_material(privileged,bootstrap_request,monkeypatch):
    helper,root,*_=privileged;original=boot.secrets.token_hex
    monkeypatch.setattr(boot.secrets,'token_hex',lambda n: 'a'*64 if n==32 else original(n))
    with pytest.raises(boot.BootstrapError,match='capability_collision'):helper.provision(bootstrap_request)
    assert list(root.iterdir())==[]


def test_constructor_refuses_unprivileged_and_unpinned_policy(privileged,monkeypatch):
    helper,root,source,_=privileged
    kwargs=dict(destination_root=root,source_root=source,source_hashes=helper.hashes,authorize=lambda _:True,authorize_discard=lambda _:True)
    with pytest.raises(boot.BootstrapError,match='manifest'):boot.RoutedBootstrap(**{**kwargs,'source_hashes':{}})
    monkeypatch.setattr(os,'geteuid',lambda:959)
    with pytest.raises(boot.BootstrapError,match='root_helper'):boot.RoutedBootstrap(**kwargs)


def test_partial_discard_preserves_receipt_and_refuses_blind_retry(privileged,bootstrap_request,monkeypatch):
    helper,*_=privileged;value=helper.provision(bootstrap_request)
    receipt=(value.root/'receipt.json').read_bytes();original=Path.rmdir
    def fail(path):
        if path==value.task_dir/'source/cloudworkbench':raise OSError('synthetic busy')
        return original(path)
    monkeypatch.setattr(Path,'rmdir',fail)
    with pytest.raises(boot.BootstrapError,match='discard_retained'):helper.discard_unlaunched(value)
    assert (value.root/'receipt.json').read_bytes()==receipt
    monkeypatch.setattr(Path,'rmdir',original)
    with pytest.raises(boot.BootstrapError,match='incomplete_retained'):helper.discard_unlaunched(value)
    assert (value.root/'receipt.json').read_bytes()==receipt


def test_root_removal_failure_restores_receipt(privileged,bootstrap_request,monkeypatch):
    helper,*_=privileged;value=helper.provision(bootstrap_request);original=Path.rmdir
    receipt=(value.root/'receipt.json').read_bytes()
    def fail(path):
        if path==value.root:raise OSError('synthetic busy')
        return original(path)
    monkeypatch.setattr(Path,'rmdir',fail)
    with pytest.raises(boot.BootstrapError,match='discard_retained'):helper.discard_unlaunched(value)
    assert (value.root/'receipt.json').read_bytes()==receipt
    with pytest.raises(boot.BootstrapError,match='incomplete_retained'):helper.discard_unlaunched(value)


def test_publication_chmod_failure_returns_exact_recovery_handle(privileged,bootstrap_request,monkeypatch):
    helper,root,*_=privileged;original=Path.chmod
    def fail(path,mode,**kwargs):
        if path==root/bootstrap_request.directory_name:raise OSError('synthetic denied')
        return original(path,mode,**kwargs)
    monkeypatch.setattr(Path,'chmod',fail)
    with pytest.raises(boot.BootstrapError,match='publication_retained') as caught:helper.provision(bootstrap_request)
    material=caught.value.material
    assert material.binding_digest==bootstrap_request.digest
    assert hashlib.sha256((material.root/'receipt.json').read_bytes()).hexdigest()==material.receipt_sha256
    helper.discard_unlaunched(material)
    assert not material.root.exists()


def test_inventory_never_recurses_into_unknown_directory(privileged,bootstrap_request,monkeypatch):
    helper,*_=privileged;value=helper.provision(bootstrap_request)
    nested=value.materialization_parent/'unknown';nested.mkdir()
    for i in range(50):nested=nested/str(i);nested.mkdir()
    monkeypatch.setattr(Path,'rglob',lambda *a,**k:pytest.fail('unbounded recursive walk'))
    original=os.scandir;count=[0]
    class Scan:
        def __init__(self,fd):self.inner=original(fd)
        def __enter__(self):return self
        def __exit__(self,*args):self.inner.close()
        def __iter__(self):return self
        def __next__(self):
            count[0]+=1
            assert count[0]<40
            return next(self.inner)
    monkeypatch.setattr(os,'scandir',Scan)
    with pytest.raises(boot.BootstrapError,match='in_use_or_changed'):helper.discard_unlaunched(value)
    monkeypatch.setattr(os,'scandir',original)
    assert (value.root/'receipt.json').exists() and nested.exists()
