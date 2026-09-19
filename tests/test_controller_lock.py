import os
import subprocess
import sys
import pytest
from cloudworkbench.controller_lock import ControllerLock,ControllerLockError


def test_second_controller_refused_until_first_closes(tmp_path):
    with ControllerLock(tmp_path) as first:
        first.assert_held()
        with pytest.raises(ControllerLockError,match='controller_already_running'):
            ControllerLock(tmp_path).acquire()
    with ControllerLock(tmp_path) as second:
        second.assert_held()


def test_process_exit_releases_lock_without_deleting_file(tmp_path):
    script="from cloudworkbench.controller_lock import ControllerLock;import os,sys;lock=ControllerLock(sys.argv[1]).acquire();os._exit(77)"
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],timeout=5)
    assert result.returncode==77
    assert (tmp_path/'controller.lock').exists()
    with ControllerLock(tmp_path) as lock:
        lock.assert_held()


@pytest.mark.parametrize('kind',['symlink','hardlink','mode'])
def test_unsafe_lock_rejected(tmp_path,kind):
    target=tmp_path/'controller.lock'
    other=tmp_path/'other';other.write_text('');other.chmod(0o600)
    if kind=='symlink':target.symlink_to(other)
    elif kind=='hardlink':os.link(other,target)
    else:target.write_text('');target.chmod(0o644)
    with pytest.raises(ControllerLockError):
        ControllerLock(tmp_path).acquire()


def test_unlinked_lock_fences_owner(tmp_path):
    with ControllerLock(tmp_path) as lock:
        (tmp_path/'controller.lock').unlink()
        with pytest.raises(ControllerLockError):
            lock.assert_held()


def test_separate_process_cannot_take_over_live_controller(tmp_path):
    script='''import sys
from cloudworkbench.controller_lock import ControllerLock,ControllerLockError
try:
 with ControllerLock(sys.argv[1]) as lock:lock.assert_held()
except ControllerLockError as exc:
 assert str(exc)=='controller_already_running'
 raise SystemExit(23)
'''
    with ControllerLock(tmp_path):
        blocked=subprocess.run([sys.executable,'-c',script,str(tmp_path)],capture_output=True,text=True,timeout=5)
        assert blocked.returncode==23,blocked.stderr
    accepted=subprocess.run([sys.executable,'-c',script,str(tmp_path)],capture_output=True,text=True,timeout=5)
    assert accepted.returncode==0,accepted.stderr


def test_forked_child_cannot_claim_parent_lock_authority(tmp_path):
    with ControllerLock(tmp_path) as lock:
        pid=os.fork()
        if pid==0:
            try:
                lock.assert_held()
            except ControllerLockError:
                os._exit(23)
            os._exit(1)
        _,status=os.waitpid(pid,0)
        assert os.waitstatus_to_exitcode(status)==23
        lock.assert_held()


@pytest.mark.parametrize('kind',['writable','symlink','file'])
def test_unsafe_directory_refused(tmp_path,kind):
    directory=tmp_path/'state'
    if kind=='file':directory.write_text('')
    elif kind=='symlink':directory.symlink_to(tmp_path,target_is_directory=True)
    else:directory.mkdir(mode=0o770);directory.chmod(0o770)
    with pytest.raises(ControllerLockError,match='unsafe_controller_directory'):
        ControllerLock(directory).acquire()


def test_exec_does_not_inherit_descriptor_even_with_close_fds_false(tmp_path):
    with ControllerLock(tmp_path) as lock:
        script="import fcntl,sys,errno\ntry:fcntl.fcntl(int(sys.argv[1]),fcntl.F_GETFD)\nexcept OSError as e:assert e.errno==errno.EBADF\nelse:raise AssertionError('fd inherited')"
        result=subprocess.run([sys.executable,'-c',script,str(lock.fd)],close_fds=False,capture_output=True,text=True,timeout=5)
        assert result.returncode==0,result.stderr


def test_relative_directory_survives_chdir(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory=tmp_path/'state';directory.mkdir(mode=0o700)
    with ControllerLock('state') as lock:
        monkeypatch.chdir('/')
        lock.assert_held()
