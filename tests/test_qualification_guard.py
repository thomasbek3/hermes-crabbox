import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest
from cloudworkbench.qualification_guard import LegacyRunnerFence,QualificationRefused


def fixture(tmp_path):
    root=tmp_path/'worker';root.mkdir(mode=0o750)
    lock=root/'runner.lock';lock.write_text('existing-content');lock.chmod(0o660)
    database=tmp_path/'state.db'
    with sqlite3.connect(database) as db:db.execute('CREATE TABLE attempts(state TEXT)')
    config=tmp_path/'worker.json';config.write_text(json.dumps({'state_root':str(root),'database':str(database)}));config.chmod(0o640)
    info=lock.stat()
    kwargs=dict(config_path=config,config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),state_root=root,database=database,
        lock_device=info.st_dev,lock_inode=info.st_ino,worker_uid=os.geteuid(),worker_gid=os.getegid(),service_inactive=lambda:True)
    return kwargs,lock,database


def contender(path,connection):
    with open(path,'a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:connection.send(False)
        else:connection.send(True)


def test_exact_runner_inode_excludes_an_actual_other_process(tmp_path):
    args,path,database=fixture(tmp_path);original=path.read_bytes()
    with LegacyRunnerFence(**args):
        ctx=multiprocessing.get_context('spawn');a,b=ctx.Pipe();p=ctx.Process(target=contender,args=(path,b));p.start()
        assert a.poll(3) and a.recv() is False;p.join(3);assert p.exitcode==0
    assert path.read_bytes()==original and path.stat().st_ino==args['lock_inode']


def test_active_runner_lock_refuses_before_service_or_database_probe(tmp_path):
    args,path,database=fixture(tmp_path)
    with path.open('a') as held:
        fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
        args['service_inactive']=lambda:(_ for _ in ()).throw(AssertionError('must not probe'))
        with pytest.raises(QualificationRefused,match='legacy_runner_owns_lock'):LegacyRunnerFence(**args).acquire()


@pytest.mark.parametrize('case',['inode','missing','symlink','hardlink','world','root_shared','configuration'])
def test_changed_or_unsafe_lock_refused_without_replacement(tmp_path,case):
    args,path,database=fixture(tmp_path)
    if case=='inode':args['lock_inode']+=1
    elif case=='missing':path.unlink()
    elif case=='symlink':path.rename(path.with_suffix('.real'));path.symlink_to(path.with_suffix('.real'))
    elif case=='hardlink':os.link(path,tmp_path/'second')
    elif case=='world':path.chmod(0o666)
    elif case=='root_shared':path.parent.chmod(0o770)
    else:args['config_sha256']='f'*64
    with pytest.raises((QualificationRefused,OSError)):LegacyRunnerFence(**args).acquire()
    if case=='missing':assert not path.exists()


@pytest.mark.parametrize('state',['queued','running','held','unknown'])
def test_live_or_queued_attempts_refuse_readonly(tmp_path,state):
    args,path,database=fixture(tmp_path)
    with sqlite3.connect(database) as db:db.execute('INSERT INTO attempts VALUES(?)',(state,))
    before=database.read_bytes()
    with pytest.raises(QualificationRefused,match='legacy_attempts_not_idle'):LegacyRunnerFence(**args).acquire()
    assert database.read_bytes()==before


def test_active_service_refuses_even_when_lock_is_free(tmp_path):
    args,path,database=fixture(tmp_path);args['service_inactive']=lambda:False
    with pytest.raises(QualificationRefused,match='worker_quiescence_unconfirmed'):LegacyRunnerFence(**args).acquire()


def test_uncertain_execution_retains_same_lock_until_trusted_settlement(tmp_path):
    args,path,database=fixture(tmp_path);guard=LegacyRunnerFence(**args).acquire();guard.begin_execution()
    with pytest.raises(QualificationRefused,match='fence_retained_cleanup_unconfirmed'):guard.close()
    with path.open('a') as other:
        with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with pytest.raises(QualificationRefused):guard.confirm_settled(False)
    with pytest.raises(QualificationRefused):guard.confirm_settled(lambda:False)
    guard.confirm_settled(lambda:True);guard.close();assert guard.fd is None


def test_lock_replacement_is_detected_while_held(tmp_path):
    args,path,database=fixture(tmp_path);guard=LegacyRunnerFence(**args).acquire()
    path.rename(path.with_suffix('.old'));path.write_text('different inode');path.chmod(0o660)
    try:
        with pytest.raises(QualificationRefused,match='runner_lock_replaced'):guard.check()
        with pytest.raises(QualificationRefused,match='runner_lock_replaced'):guard.close()
    finally:os.close(guard.fd);guard.fd=None


def test_cleanup_can_settle_after_service_state_changes(tmp_path):
    args,path,database=fixture(tmp_path);guard=LegacyRunnerFence(**args).acquire();guard.begin_execution()
    guard.service_inactive=lambda:False
    with pytest.raises(QualificationRefused,match='quiescence'):guard.check()
    guard.confirm_settled(lambda:True);guard.close();assert guard.fd is None


def test_wal_reader_closes_and_does_not_leave_new_directory_entries(tmp_path):
    args,path,database=fixture(tmp_path)
    db=sqlite3.connect(database);db.execute('PRAGMA journal_mode=WAL');db.execute("INSERT INTO attempts VALUES('completed')");db.commit()
    before={p.name for p in tmp_path.iterdir()}
    try:
        with LegacyRunnerFence(**args):pass
        assert {p.name for p in tmp_path.iterdir()}==before
    finally:db.close()
