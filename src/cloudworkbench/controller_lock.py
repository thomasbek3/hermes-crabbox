"""Exclusive process-lifetime controller lock for one trusted state directory."""
import fcntl
import os
from pathlib import Path
import stat
import weakref


class ControllerLockError(RuntimeError):
    pass


class ControllerLock:
    def __init__(self, state_directory):
        self.directory = Path(state_directory).absolute()
        self.fd = None
        self.pid = None
        self.dir_fd = None
        reference = weakref.ref(self)
        def after_fork():
            instance = reference()
            if instance is not None:
                instance.close()
        os.register_at_fork(after_in_child=after_fork)

    def acquire(self):
        try:
            return self._acquire()
        except OSError:
            raise ControllerLockError('controller_lock_io_failed') from None

    def _acquire(self):
        if self.fd is not None:
            raise ControllerLockError('controller_lock_already_held')
        directory = self.directory.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid()
                or stat.S_IMODE(directory.st_mode) & 0o022):
            raise ControllerLockError('unsafe_controller_directory')
        dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fd = None
        try:
            opened = os.fstat(dir_fd)
            if ((opened.st_dev,opened.st_ino)!=(directory.st_dev,directory.st_ino)
                    or not stat.S_ISDIR(opened.st_mode) or opened.st_uid!=os.geteuid()
                    or stat.S_IMODE(opened.st_mode)&0o022):
                raise ControllerLockError('unsafe_controller_directory')
            fd = os.open('controller.lock',os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600,dir_fd=dir_fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):
                raise ControllerLockError('unsafe_controller_lock')
            try:
                fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ControllerLockError('controller_already_running') from None
            current = os.stat('controller.lock',dir_fd=dir_fd,follow_symlinks=False)
            if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):
                raise ControllerLockError('controller_lock_changed')
            self.fd,self.pid,self.dir_fd = fd,os.getpid(),dir_fd
            dir_fd = None
            fd = None
            return self
        finally:
            if fd is not None:
                os.close(fd)
            if dir_fd is not None:
                os.close(dir_fd)

    def assert_held(self):
        if self.fd is None or self.pid != os.getpid():
            raise ControllerLockError('controller_lock_not_held')
        try:
            directory = self.directory.lstat()
            opened = os.fstat(self.dir_fd)
            info = os.fstat(self.fd)
            current = os.stat('controller.lock',dir_fd=self.dir_fd,follow_symlinks=False)
            if ((directory.st_dev,directory.st_ino)!=(opened.st_dev,opened.st_ino)
                    or info.st_nlink!=1 or (info.st_dev,info.st_ino)!=(current.st_dev,current.st_ino)):
                raise ControllerLockError('controller_lock_changed')
        except OSError:
            raise ControllerLockError('controller_lock_changed') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd,self.pid = None,None
        if self.dir_fd is not None:
            os.close(self.dir_fd)
            self.dir_fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *unused):
        self.close()
