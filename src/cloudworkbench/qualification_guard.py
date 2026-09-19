"""Qualification-only fence using the deployed legacy Runner's existing lock.

No credential reads, service mutations or production database migrations.
The root operator must keep the worker stopped throughout a qualification window.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import stat


class QualificationRefused(RuntimeError):
    pass


def read_bounded(path,limit=65536):
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>limit:
            raise QualificationRefused('unsafe_configuration_file')
        with os.fdopen(fd,'rb',closefd=False) as stream:body=stream.read(limit+1)
        if len(body)>limit:raise QualificationRefused('configuration_limit')
        return body,info
    finally:os.close(fd)


class LegacyRunnerFence:
    """Exact existing inode flock. No O_CREAT, replacement, unlink or truncation."""
    def __init__(self,config_path,*,config_sha256,state_root,database,lock_device,lock_inode,worker_uid,worker_gid,service_inactive):
        self.config_path=Path(config_path);self.config_sha256=config_sha256
        self.state_root=Path(state_root);self.database=Path(database)
        self.expected=(lock_device,lock_inode);self.worker_uid=worker_uid;self.worker_gid=worker_gid
        if not callable(service_inactive):raise QualificationRefused('service_probe_required')
        self.service_inactive=service_inactive;self.fd=None;self.started=False;self.settled=False
        for path in (self.config_path,self.state_root,self.database):
            if not path.is_absolute() or path.resolve()!=path:raise QualificationRefused('noncanonical_fence_path')
        if any(type(v) is not int or v<0 for v in (*self.expected,worker_uid,worker_gid)):
            raise QualificationRefused('invalid_fence_identity')

    def _configuration(self):
        body,info=read_bounded(self.config_path)
        if info.st_mode&0o022 or hashlib.sha256(body).hexdigest()!=self.config_sha256:
            raise QualificationRefused('worker_configuration_changed')
        config=json.loads(body)
        if config.get('state_root')!=str(self.state_root) or config.get('database')!=str(self.database):
            raise QualificationRefused('worker_path_binding_changed')
        root=self.state_root.lstat()
        if not stat.S_ISDIR(root.st_mode) or root.st_uid!=self.worker_uid or root.st_gid!=self.worker_gid or root.st_mode&0o022:
            raise QualificationRefused('unsafe_worker_state_root')

    def acquire(self):
        if self.fd is not None:raise QualificationRefused('fence_already_acquired')
        self._configuration()
        lock=self.state_root/'runner.lock'
        fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or (info.st_dev,info.st_ino)!=self.expected
                    or info.st_uid!=self.worker_uid or info.st_gid!=self.worker_gid or info.st_mode&0o007):
                raise QualificationRefused('runner_lock_identity_mismatch')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise QualificationRefused('legacy_runner_owns_lock') from None
            self.fd=fd
            self.check()
            self.check_idle()
        except BaseException:
            self.fd=None;os.close(fd);raise
        return self

    def check(self):
        if self.fd is None:raise QualificationRefused('runner_lock_not_held')
        self._configuration()
        self.check_lock()
        if not self.service_inactive():raise QualificationRefused('worker_quiescence_unconfirmed')

    def check_lock(self):
        if self.fd is None:raise QualificationRefused('runner_lock_not_held')
        path=self.state_root/'runner.lock';current=path.lstat();held=os.fstat(self.fd)
        if (current.st_dev,current.st_ino)!=self.expected or (held.st_dev,held.st_ino)!=self.expected or current.st_nlink!=1:
            raise QualificationRefused('runner_lock_replaced')

    def check_idle(self):
        # SQLite read-only WAL readers can create coordination sidefiles. Probe
        # as the existing worker identity, explicitly close, and refuse new entries.
        before={path.name for path in self.database.parent.iterdir()}
        program="""import os,sys,sqlite3,pathlib
uid,gid=int(sys.argv[1]),int(sys.argv[2])
if os.geteuid()==0:os.setgroups([gid]);os.setgid(gid);os.setuid(uid)
if os.geteuid()!=uid or os.getegid()!=gid:raise SystemExit(2)
db=sqlite3.connect(pathlib.Path(sys.argv[3]).as_uri()+'?mode=ro',uri=True,timeout=1)
try:
 db.execute('PRAGMA query_only=ON')
 print(db.execute("SELECT COUNT(*) FROM attempts WHERE state NOT IN ('completed','failed','cancelled','interrupted','paused')").fetchone()[0])
finally:db.close()
"""
        result=subprocess.run([sys.executable,'-c',program,str(self.worker_uid),str(self.worker_gid),str(self.database)],
            capture_output=True,timeout=3,env={'PATH':'/usr/bin:/bin'})
        if {path.name for path in self.database.parent.iterdir()}!=before:
            raise QualificationRefused('database_directory_changed')
        if result.returncode or not result.stdout.strip().isdigit():raise QualificationRefused('legacy_idle_probe_failed')
        if int(result.stdout):raise QualificationRefused('legacy_attempts_not_idle')

    def begin_execution(self):
        self.check();self.check_idle();self.started=True

    def confirm_settled(self,verifier):
        # Only trusted controller code supplies this callback. No model boolean.
        self.check_lock()
        if not callable(verifier) or verifier() is not True:raise QualificationRefused('qualification_cleanup_unconfirmed')
        self.settled=True

    def close(self):
        if self.fd is None:return
        if self.started and not self.settled:raise QualificationRefused('fence_retained_cleanup_unconfirmed')
        self.check_lock()
        os.close(self.fd);self.fd=None

    def __enter__(self):return self.acquire()
    def __exit__(self,*unused):self.close()
