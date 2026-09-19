"""Bounded local text-workspace staging. No network, task creation, or legacy mutation."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata

MAX_FILES = 128
MAX_FILE_BYTES = 8 * 1024**2
MAX_TOTAL_BYTES = 64 * 1024**2
BLOCKED = frozenset({'.git','.ssh','.aws','.azure','.config','.env','.claude','.codex','.credentials',
    'credentials','credential-state','native','state','cache','.cache','node_modules','__pycache__',
    '.venv','venv','log','logs','server','handoff.md','handoff.txt','token','tokens','secrets'})
SECRET_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
    r'\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})',
    r'\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|token)[\s"\']*[:=][\s"\']*[^\s"\',;}{]{4,}',
    r'\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/-]{12,}',
    r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}',
    r'\b[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@',
))

class ImportRefused(ValueError):
    """Safe fixed diagnostics; never include candidate bytes or secret-like path text."""


def _digest(data):return hashlib.sha256(data).hexdigest()
def _signature(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_uid,info.st_gid,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def _absolute(path):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts or str(path)!=os.path.normpath(path):
        raise ImportRefused('An exact absolute path is required')
    return path

def _directory(path, stack):
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    stack.callback(os.close,fd)
    for part in path.parts[1:]:
        fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        stack.callback(os.close,fd)
    return fd

def _paths(values):
    if not isinstance(values,list) or not 1<=len(values)<=MAX_FILES:
        raise ImportRefused('A bounded nonempty explicit file allowlist is required')
    result=[];seen=set()
    for value in values:
        if not isinstance(value,str) or len(value.encode())>1024:
            raise ImportRefused('Invalid allowlist entry')
        p=PurePosixPath(value)
        if p.is_absolute() or p.as_posix()!=value or not p.parts or any(x in ('.','..') for x in p.parts):
            raise ImportRefused('Invalid allowlist entry')
        normalized=unicodedata.normalize('NFC',value).casefold()
        if normalized in seen or any(ord(x)<32 or ord(x)==127 for x in value) or '\\' in value:
            raise ImportRefused('Duplicate or unsafe allowlist entry')
        for part in p.parts:
            lowered=unicodedata.normalize('NFC',part).casefold()
            if lowered in BLOCKED or lowered.startswith(('.env.','handoff','.credential')) or lowered.endswith(('.pem','.key','.p12','.pfx','.token')):
                raise ImportRefused('Sensitive workspace paths are excluded')
        seen.add(normalized);result.append(value)
    return result

def _regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>MAX_FILE_BYTES:
        raise ImportRefused('Only bounded singly linked regular files may be staged')

def _read(fd):
    info=os.fstat(fd);_regular(info);os.lseek(fd,0,os.SEEK_SET)
    chunks=[];size=0
    while True:
        chunk=os.read(fd,min(65536,MAX_FILE_BYTES+1-size))
        if not chunk:break
        chunks.append(chunk);size+=len(chunk)
        if size>MAX_FILE_BYTES:raise ImportRefused('Selected file exceeds its bound')
    if _signature(info)!=_signature(os.fstat(fd)) or size!=info.st_size:
        raise ImportRefused('Selected source changed while reading')
    return b''.join(chunks)

def _screen(data):
    try:text=data.decode('utf-8')
    except UnicodeError:raise ImportRefused('Only reviewable UTF-8 text is supported') from None
    if any(ord(c)<32 and c not in '\n\r\t' for c in text):
        raise ImportRefused('Binary or control-bearing content is unsupported')
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise ImportRefused('Secret-like content detected; staging refused')

def _selected(rootfd, relative, stack, directories):
    fd=rootfd
    for part in PurePosixPath(relative).parts[:-1]:
        fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        stack.callback(os.close,fd);directories.append((fd,_signature(os.fstat(fd))))
    leaf=PurePosixPath(relative).name
    fd=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    stack.callback(os.close,fd);_regular(os.fstat(fd));return fd

def _write(directory, name, data):
    fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb',closefd=False) as stream:stream.write(data);stream.flush();os.fsync(fd)
    finally:os.close(fd)

def stage(workspace: Path, job_id: str, allowlist: list[str], destination: Path, *, quiescence_confirmed: bool):
    """Stage one explicitly selected, operator-quiesced workspace; never upload or execute."""
    workspace=_absolute(workspace);destination=_absolute(destination);paths=_paths(allowlist)
    if not re.fullmatch(r'[0-9]{4}-[0-9]{6}-[a-f0-9]{4}',job_id) or workspace.name!='work' or workspace.parent.name!=job_id or workspace.parent.parent.name!='jobs':
        raise ImportRefused('Exact legacy jobs/job-ID/work root and matching ID required')
    if not quiescence_confirmed:raise ImportRefused('Operator quiescence confirmation is required')
    if destination==workspace or workspace in destination.parents or destination in workspace.parents:
        raise ImportRefused('Destination must be outside the selected workspace')
    created=False;written={};destfd=None;dest_identity=None
    try:
        with ExitStack() as stack:
            rootfd=_directory(workspace,stack);root_signature=_signature(os.fstat(rootfd));directories=[(rootfd,root_signature)]
            parentfd=_directory(destination.parent,stack);parentstat=os.fstat(parentfd)
            if parentstat.st_uid!=os.geteuid() or stat.S_IMODE(parentstat.st_mode)&0o077:
                raise ImportRefused('Destination parent must be private and owned by the operator')
            selected=[];total=0
            for path in paths:
                fd=_selected(rootfd,path,stack,directories);info=os.fstat(fd);data=_read(fd);_screen(data)
                total+=len(data)
                if total>MAX_TOTAL_BYTES:raise ImportRefused('Selection exceeds total byte bound')
                selected.append((path,fd,_signature(info),len(data),_digest(data)))
            os.mkdir(destination.name,0o700,dir_fd=parentfd);created=True
            destfd=os.open(destination.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parentfd);stack.callback(os.close,destfd);os.fchmod(destfd,0o700)
            created_stat=os.fstat(destfd);dest_identity=(created_stat.st_dev,created_stat.st_ino)
            records=[]
            for index,(path,fd,signature,size,digest) in enumerate(selected):
                data=_read(fd)
                if _signature(os.fstat(fd))!=signature or _digest(data)!=digest:raise ImportRefused('Selected source changed before copying')
                _screen(data);object_name=f'object-{index:04d}.txt';_write(destfd,object_name,data)
                copied_stat=os.stat(object_name,dir_fd=destfd,follow_symlinks=False);written[object_name]=(copied_stat.st_dev,copied_stat.st_ino)
                copied=os.open(object_name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=destfd)
                try:
                    if _digest(_read(copied))!=digest:raise ImportRefused('Staged bytes failed integrity verification')
                finally:os.close(copied)
                records.append({'relative_path':path,'object':object_name,'bytes':size,'sha256':digest,'source_snapshot':dict(zip(('device','inode','mode','links','uid','gid','bytes','mtime_ns','ctime_ns'),signature))})
            # Reopen through the exact original root and paths; an old FD alone would accept renamed entries.
            freshroot=_directory(workspace,stack)
            if _signature(os.fstat(freshroot))!=root_signature:raise ImportRefused('Workspace identity changed')
            for path,fd,signature,size,digest in selected:
                fresh=_selected(freshroot,path,stack,[])
                if _signature(os.fstat(fresh))!=signature or _digest(_read(fresh))!=digest or _signature(os.fstat(fd))!=signature:
                    raise ImportRefused('Selected source snapshot changed')
            if any(_signature(os.fstat(fd))!=signature for fd,signature in directories):raise ImportRefused('Workspace directory changed')
            result={'schema_version':1,'legacy_job_id':job_id,'workspace_root':str(workspace),'staged_at':datetime.now(timezone.utc).isoformat(),
                    'file_count':len(records),'total_bytes':total,'files':records,'source_snapshot_sha256':_digest(json.dumps(records,sort_keys=True,separators=(',',':')).encode()),
                    'scope':'explicit_selected_text_files','source_quiescence':'operator_confirmed_not_enforced','secret_screen':'heuristic_patterns_not_a_secret_free_guarantee',
                    'human_review_required_before_upload':True,'trusted_verification':False,'uploaded':False,'task_created':False}
            _write(destfd,'manifest.json',(json.dumps(result,indent=2)+'\n').encode())
            manifest_stat=os.stat('manifest.json',dir_fd=destfd,follow_symlinks=False);written['manifest.json']=(manifest_stat.st_dev,manifest_stat.st_ino)
            os.fsync(destfd);os.fsync(parentfd)
            return result
    except (OSError,ImportRefused):
        if created:
            # Reopen only the private destination we created. Never traverse/delete the source.
            try:
                with ExitStack() as cleanup:
                    parent=_directory(destination.parent,cleanup)
                    d=os.open(destination.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);cleanup.callback(os.close,d)
                    current=os.fstat(d)
                    if (current.st_dev,current.st_ino)!=dest_identity:raise OSError('Destination identity changed')
                    for name,identity in written.items():
                        current=os.stat(name,dir_fd=d,follow_symlinks=False)
                        if (current.st_dev,current.st_ino)!=identity:raise OSError('Staged object identity changed')
                        os.unlink(name,dir_fd=d)
                    os.rmdir(destination.name,dir_fd=parent)
            except OSError:raise ImportRefused('Staging refused; partial private destination requires operator cleanup') from None
        raise ImportRefused('Staging refused: unsafe, secret-like, unavailable, or changed source/destination') from None

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True,type=Path);parser.add_argument('--job-id',required=True)
    parser.add_argument('--file',action='append',required=True,dest='allowlist');parser.add_argument('--destination',required=True,type=Path)
    parser.add_argument('--quiescence-confirmed',action='store_true')
    args=parser.parse_args(argv)
    try:stage(args.workspace,args.job_id,args.allowlist,args.destination,quiescence_confirmed=args.quiescence_confirmed)
    except ImportRefused:
        print('Staging refused. No candidate content printed; inspect the selected paths and private destination.');return 1
    print('Selected files staged privately. Review manifest and contents before any separate upload.');return 0

if __name__=='__main__':raise SystemExit(main())
