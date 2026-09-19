from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import pytest

from cloudworkbench.remediation_revision import rebind_revision
from cloudworkbench.workflow_revisions import RevisionBinding,RevisionError,RevisionLimits,capture_revision,verify_revision,materialize_stage
from cloudworkbench.store import encode
from tests.test_root_cleanup import setup,release


@pytest.fixture
def fixture(setup,tmp_path):
    store,owner,leases,scheduler,root,_=setup
    binding=RevisionBinding(owner['id'],'project',root['session_id'],root['turn_id'],root['attempt_id'],1,root['attempt_id'],1)
    work=tmp_path/'work';work.mkdir();(work/'src').mkdir();(work/'src/empty').mkdir()
    (work/'src/code.py').write_bytes(b'print("safe")\n');(work/'src/code.py').chmod(0o755)
    revisions=tmp_path/'original';revisions.mkdir()
    source=capture_revision(store,binding,work,revisions,selected_paths=('src',),controller_attests_quiesced=True)
    scheduler.transition(root['attempt_id'],'verifying',expected_generation=1)
    scheduler.transition(root['attempt_id'],'completed',expected_generation=1,outcome='needs_review')
    release(setup)
    target=replace(binding,turn_id='second-turn',root_attempt_id='second-root',attempt_id='second-root')
    with store._tx() as db:
        old=dict(db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(binding.root_attempt_id,)).fetchone())
        request=db.execute('SELECT request FROM turns WHERE id=?',(binding.turn_id,)).fetchone()[0]
        db.execute('INSERT INTO turns VALUES(?,?,?,?,?)',(target.turn_id,target.session_id,2,request,'now'))
        db.execute("""INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at,
          execution_kind,workflow_root_id,root_sequence) VALUES(?,?,?,'hermes',1,'queued','now','now','hermes_root',?,2)""",
            (target.attempt_id,target.session_id,target.turn_id,target.root_attempt_id))
        db.execute("""INSERT INTO workflow_roots(root_id,generation,owner_id,project_id,session_id,turn_id,frozen,frozen_digest,state)
          VALUES(?,1,?,?,?,?,?,?,'queued')""",(target.root_attempt_id,target.owner_id,target.project_id,target.session_id,
            target.turn_id,old['frozen'],old['frozen_digest']))
    destination=tmp_path/'rebound';destination.mkdir()
    return store,source,target,destination,scheduler,work


def call(f,**kwargs):return rebind_revision(*f[:4],**kwargs)


def test_copy_exact_content_new_identity_replay_no_database_changes(fixture):
    store,source,target,destination,*_=fixture
    with store._connect() as db:before=tuple(db.iterdump())
    result=call(fixture)
    assert result.source_sha256==source.sha256 and result.revision.binding==target
    assert result.revision.sha256!=source.sha256 and call(fixture)==result
    assert call(fixture,expected_content_sha256=result.content_sha256)==result
    old=verify_revision(store,source);new=verify_revision(store,result.revision)
    assert new=={**old,'binding':target.__dict__}
    for path in ('src/code.py','src/empty'):
        assert (source.path/'files'/path).stat().st_ino!=(result.revision.path/'files'/path).stat().st_ino
        assert (source.path/'files'/path).stat().st_mode==(result.revision.path/'files'/path).stat().st_mode
    assert (result.revision.path/'files/src/code.py').read_bytes()==b'print("safe")\n'
    with store._connect() as db:assert tuple(db.iterdump())==before
    with pytest.raises(RevisionError,match='cross_root'):materialize_stage(store,source,target,destination/'forbidden')


@pytest.mark.parametrize('field,value',[('owner_id','other'),('project_id','other'),('session_id','other'),('turn_id','other'),('generation',2)])
def test_wrong_target_identity(fixture,field,value):
    with pytest.raises(RevisionError):rebind_revision(fixture[0],fixture[1],replace(fixture[2],**{field:value}),fixture[3])
    assert not list(fixture[3].iterdir())


def test_same_root_refused(fixture):
    with pytest.raises(RevisionError,match='scope'):rebind_revision(fixture[0],fixture[1],fixture[1].binding,fixture[3])


@pytest.mark.parametrize('state',['failed','cancelled','interrupted'])
def test_source_must_be_completed(fixture,state):
    with fixture[0]._tx() as db:db.execute('UPDATE attempts SET state=? WHERE id=?',(state,fixture[1].binding.attempt_id))
    with pytest.raises(RevisionError,match='completed_released'):call(fixture)


def test_source_must_be_released(fixture):
    with fixture[0]._tx() as db:db.execute("UPDATE workflow_roots SET state='releasing' WHERE root_id=?",(fixture[1].binding.root_attempt_id,))
    with pytest.raises(RevisionError,match='completed_released'):call(fixture)


def test_cancelled_target_refused(fixture):
    with fixture[0]._tx() as db:db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(fixture[2].attempt_id,))
    with pytest.raises(RevisionError,match='fresh'):call(fixture)


def test_any_existing_child_refused(fixture):
    store,_,target,*_=fixture
    with store._tx() as db:
        db.execute("INSERT INTO workflow_requests VALUES('request','pending',?,1,'feature','{}','[]',1)",(target.attempt_id,))
        db.execute("INSERT INTO workflow_seats VALUES('seat','request',0,'{}')")
        db.execute("""INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at,
         execution_kind,workflow_root_id,workflow_parent_id,workflow_parent_generation,role_seat_id)
         VALUES('child',?,?,'hermes',1,'queued','now','now','hermes_child',?,?,1,'seat')""",
         (target.session_id,target.turn_id,target.attempt_id,target.attempt_id))
    with pytest.raises(RevisionError,match='fresh'):call(fixture)


@pytest.mark.parametrize('kind',['symlink','hardlink','bytes','mode','extra'])
def test_source_mutation_refused(fixture,kind):
    p=fixture[1].path/'files/src/code.py';p.parent.chmod(0o750)
    if kind=='symlink':p.unlink();p.symlink_to(fixture[5]/'src/code.py')
    elif kind=='hardlink':os.link(p,p.parent/'second')
    elif kind=='bytes':p.chmod(0o750);p.write_bytes(b'changed');p.chmod(0o550)
    elif kind=='mode':p.chmod(0o440)
    else:(p.parent/'extra').write_text('extra')
    p.parent.chmod(0o550)
    with pytest.raises(RevisionError):call(fixture)
    assert not list(fixture[3].iterdir())


@pytest.mark.parametrize('secret',[b'print',b'code.py',b'controller_selected_deliverables',b'second-root'])
def test_secret_bytes_and_full_manifest_refused(fixture,secret):
    with pytest.raises(RevisionError,match='secret_refused'):call(fixture,forbidden_values=(secret,))
    assert not list(fixture[3].iterdir())


def test_unicode_path_decoded_scan(fixture):
    store,source,target,destination,scheduler,work=fixture
    (work/'src/秘密').write_text('data')
    source=capture_revision(store,source.binding,work,source.path.parent,selected_paths=('src',),controller_attests_quiesced=True)
    with pytest.raises(RevisionError,match='secret_refused'):
        rebind_revision(store,source,target,destination,forbidden_values=('秘密'.encode(),))


@pytest.mark.parametrize('bad',[True,'bad','0'*64])
def test_expected_content_digest_refused(fixture,bad):
    with pytest.raises(RevisionError):call(fixture,expected_content_sha256=bad)


def test_limits_and_overlap(fixture):
    with pytest.raises(RevisionError,match='byte_limit'):call(fixture,limits=RevisionLimits(max_bytes=1))
    for path in (fixture[1].path,fixture[1].path.parent,fixture[1].path/'files'):
        with pytest.raises(RevisionError,match='storage_overlap'):rebind_revision(*fixture[:3],path)


def test_existing_destination_is_fully_revalidated(fixture):
    result=call(fixture);p=result.revision.path/'files/src/code.py';p.chmod(0o750);p.write_text('corrupt');p.chmod(0o550)
    with pytest.raises(RevisionError):call(fixture)


def test_publication_failure_removes_only_owned_stage(fixture,monkeypatch):
    import cloudworkbench.remediation_revision as module
    preserved=fixture[3]/'unrelated';preserved.write_text('keep')
    def fail(*_):raise OSError('fixture')
    monkeypatch.setattr(module,'_publish',fail)
    with pytest.raises(RevisionError,match='filesystem'):call(fixture)
    assert list(fixture[3].iterdir())==[preserved] and preserved.read_text()=='keep'


def test_scope_change_during_copy_refused(fixture,monkeypatch):
    import cloudworkbench.remediation_revision as module
    original=module._write_tree
    def cancel(*args,**kwargs):
        original(*args,**kwargs)
        with fixture[0]._tx() as db:db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(fixture[2].attempt_id,))
    monkeypatch.setattr(module,'_write_tree',cancel)
    with pytest.raises(RevisionError,match='fresh'):call(fixture)
    assert not list(fixture[3].iterdir())


@pytest.mark.parametrize('kind',['selection','mode','directory','bytes'])
def test_content_digest_binds_every_normalized_content_component(fixture,kind):
    first=call(fixture);store,source,target,destination,_,work=fixture
    selected=('src',)
    if kind=='selection':selected=('src/code.py','src/empty')
    elif kind=='mode':(work/'src/code.py').chmod(0o644)
    elif kind=='directory':(work/'src/more').mkdir()
    else:(work/'src/code.py').write_text('new bytes')
    second=capture_revision(store,source.binding,work,source.path.parent,selected_paths=selected,controller_attests_quiesced=True)
    result=rebind_revision(store,second,target,destination)
    assert result.content_sha256!=first.content_sha256
    with pytest.raises(RevisionError,match='content_digest_mismatch'):
        rebind_revision(store,second,target,destination,expected_content_sha256=first.content_sha256)


@pytest.mark.parametrize('change',[
    lambda m:{**m,'selected_paths':None},
    lambda m:{**m,'selected_paths':['outside']},
    lambda m:{**m,'scope':'everything'},
    lambda m:{**m,'extra':'field'},
    lambda m:{**m,'schema_version':True},
    lambda m:{**m,'files':[{**m['files'][0],'executable':1}]},
])
def test_self_hashed_malformed_manifest_refused(fixture,change):
    from cloudworkbench.workflow_revisions import _canonical
    source=fixture[1];path=source.path/'manifest.json'
    raw=_canonical(change(json.loads(path.read_bytes())))
    path.chmod(0o640);path.write_bytes(raw);path.chmod(0o440)
    malformed=replace(source,sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(RevisionError):rebind_revision(fixture[0],malformed,fixture[2],fixture[3])


@pytest.mark.parametrize('target',['source_parent','destination'])
def test_job_writable_storage_refused(fixture,target):
    path=fixture[1].path.parent if target=='source_parent' else fixture[3]
    path.chmod(0o770)
    with pytest.raises(RevisionError,match='untrusted_revision_storage'):call(fixture)


def test_symlink_destination_root_refused(fixture,tmp_path):
    link=tmp_path/'alias';link.symlink_to(fixture[3],target_is_directory=True)
    with pytest.raises(RevisionError):rebind_revision(*fixture[:3],link)
