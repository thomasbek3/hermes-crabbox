"""Read immutable blobs from operator-registered Git repositories; never run job Git."""
from __future__ import annotations

import base64
import difflib
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import resource
import subprocess
import tempfile
import time


class RepositoryError(ValueError):
    pass


def safe_path(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or len(name) > 512 or path.is_absolute() or path.as_posix() != name
            or any(p in {'..', '.', '.git'} for p in path.parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or '\\' in name):
        raise RepositoryError('repository_path_rejected')
    return name


def _git(repository: Path, args: list[str], limit: int, *, timeout: float = 30) -> bytes:
    """Only metadata/blob reads, with no pager, filters, hooks, or network commands."""
    def bounded_output():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit + 1, limit + 1))
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LC_ALL': 'C',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1'}
    command = ['git', '--no-pager', '-c', 'core.hooksPath=/dev/null', '-c', 'safe.directory='+str(repository), '-C', str(repository), *args]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(command, env=env, stdout=output, stderr=errors,
                                    timeout=timeout, preexec_fn=bounded_output)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepositoryError('repository_read_failed') from exc
        output.seek(0)
        content = output.read(limit + 1)
    if result.returncode or len(content) > limit:
        raise RepositoryError('repository_read_failed_or_limit')
    return content


def snapshot(repository: Path, commit: str, *, max_bytes=32 * 1024**2, max_files=2000) -> dict:
    """The repository path comes only from trusted registration, never a job request.

    Resolve a full commit object and reject symlinks/submodules. The caller stores
    the returned baseline outside the job's mounts. No checkout configuration or
    code from the job workspace is evaluated by the supervisor.
    """
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
        raise RepositoryError('immutable_commit_required')
    deadline = time.monotonic() + 120
    repository = Path(repository)
    if not repository.is_absolute() or repository.is_symlink() or not repository.is_dir():
        raise RepositoryError('registered_repository_required')
    if _git(repository, ['cat-file', '-t', commit], 64).strip() != b'commit':
        raise RepositoryError('commit_object_required')
    tree = _git(repository, ['ls-tree', '-rlz', commit], 2 * 1024**2)
    files, total = {}, 0
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, oid, size = metadata.split()
        try:
            name = safe_path(raw_name.decode('utf-8'))
        except UnicodeError as exc:
            raise RepositoryError('repository_filename_encoding') from exc
        if kind != b'blob' or mode not in {b'100644', b'100755'}:
            raise RepositoryError('repository_links_or_special_entries')
        total += int(size)
        if total > max_bytes or len(files) >= max_files:
            raise RepositoryError('repository_snapshot_limit')
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise RepositoryError('repository_snapshot_timeout')
        content = _git(repository, ['cat-file', 'blob', oid.decode()], int(size), timeout=min(30, remaining))
        if len(content) != int(size):
            raise RepositoryError('repository_blob_changed')
        files[name] = {'content': content, 'executable': mode == b'100755',
                       'sha256': hashlib.sha256(content).hexdigest()}
    return {'commit': commit, 'files': files, 'bytes': total}


def text_patch(baseline: dict, delivered: dict[str, bytes], *, max_bytes=4 * 1024**2,
               delivered_modes=None, excluded_names=()) -> dict:
    """Supplementary LF-delimited Git patch; omitted changes remain explicit."""
    before = baseline['files']
    chunks, changed, excluded, ignored = [], [], [], []
    size = 0
    for name in sorted(set(before) | set(delivered)):
        safe_path(name)
        if any(part in excluded_names for part in PurePosixPath(name).parts):
            ignored.append({'path':name,'reason':'outside_export_policy'})
            continue
        exists_before, exists_after = name in before, name in delivered
        old = before[name]['content'] if exists_before else b''
        new = delivered.get(name, b'')
        old_mode = bool(before[name]['executable']) if exists_before else False
        new_mode = (delivered_modes.get(name) if delivered_modes is not None else old_mode) if exists_after else False
        if old == new and exists_before == exists_after and old_mode == new_mode:
            continue
        changed.append({'path': name, 'status': 'added' if not exists_before else 'deleted' if not exists_after else 'modified',
                        'before_sha256': hashlib.sha256(old).hexdigest() if exists_before else None,
                        'after_sha256': hashlib.sha256(new).hexdigest() if exists_after else None,
                        'before_mode': ('100755' if old_mode else '100644') if exists_before else None,
                        'after_mode': (('100755' if new_mode else '100644') if type(new_mode) is bool else 'unknown') if exists_after else None})
        try:
            left, right = old.decode('utf-8'), new.decode('utf-8')
            if (not re.fullmatch(r'[A-Za-z0-9_./-]+', name) or '\x00' in left + right
                    or type(new_mode) is not bool or (left and not left.endswith('\n')) or (right and not right.endswith('\n'))):
                raise ValueError
        except (UnicodeError, ValueError):
            excluded.append({'path': name, 'reason': 'not_represented_by_safe_text_diff; use delivered file and mode metadata'})
            continue
        header = 'diff --git a/'+name+' b/'+name+'\n'
        if not exists_before:
            header += 'new file mode '+('100755' if new_mode else '100644')+'\n'
        elif not exists_after:
            header += 'deleted file mode '+('100755' if old_mode else '100644')+'\n'
        elif old_mode != new_mode:
            header += 'old mode '+('100755' if old_mode else '100644')+'\nnew mode '+('100755' if new_mode else '100644')+'\n'
        # Git counts LF records; str.splitlines also splits form-feed and NEL.
        lines = lambda text: [line+'\n' for line in text.split('\n')[:-1]]
        chunk = (header + ''.join(difflib.unified_diff(lines(left), lines(right),
                         fromfile='a/'+name if exists_before else '/dev/null',
                         tofile='b/'+name if exists_after else '/dev/null'))).encode()
        if size + len(chunk) > max_bytes:
            excluded.append({'path':name,'reason':'patch_byte_limit; use delivered file and mode metadata'})
            continue
        size += len(chunk)
        chunks.append(chunk)
    patch = b''.join(chunks)
    return {'base_commit': baseline['commit'], 'patch': patch, 'patch_sha256': hashlib.sha256(patch).hexdigest(),
            'changed_files': changed, 'excluded_from_text_patch': excluded,
            'baseline_paths_not_exported':ignored, 'mode_tracking':delivered_modes is not None,
            'complete_text_patch': not excluded and not ignored}


def save_baseline(path: Path, baseline: dict) -> None:
    """Persist a host-only immutable baseline before copying a new job workspace."""
    payload = {'schema_version':1,'commit':baseline['commit'],'files':{
        name:{'base64':base64.b64encode(item['content']).decode(),'sha256':item['sha256'],'executable':item['executable']}
        for name,item in baseline['files'].items()}}
    encoded = json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
    if len(encoded) > 48*1024**2: raise RepositoryError('repository_baseline_limit')
    if path.exists():
        if path.is_symlink() or path.read_bytes() != encoded: raise RepositoryError('repository_baseline_conflict')
        return
    fd, temporary = tempfile.mkstemp(prefix='.baseline-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(encoded);out.flush();os.fsync(out.fileno())
        os.link(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:os.unlink(temporary)


def load_baseline(path: Path) -> dict:
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        import stat
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>48*1024**2:
            raise RepositoryError('repository_baseline_invalid')
        with os.fdopen(fd,'rb',closefd=False) as source:payload=json.load(source)
    finally:os.close(fd)
    if payload.get('schema_version')!=1 or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',payload.get('commit','')):
        raise RepositoryError('repository_baseline_invalid')
    files={};total=0
    for name,item in payload['files'].items():
        safe_path(name)
        content=base64.b64decode(item['base64'],validate=True)
        total+=len(content)
        if total>32*1024**2 or len(files)>=2000 or hashlib.sha256(content).hexdigest()!=item['sha256']:
            raise RepositoryError('repository_baseline_invalid')
        files[name]={'content':content,'sha256':item['sha256'],'executable':bool(item['executable'])}
    return {'commit':payload['commit'],'files':files,'bytes':total}


def initialize_workspace(workspace: Path, baseline: dict) -> None:
    """Called only before first launch, under the session's exclusive reservation.

    A partial initialization can resume only when all existing files exactly
    match the immutable baseline; divergence fails closed without deleting work.
    """
    for path in workspace.rglob('*'):
        name=path.relative_to(workspace).as_posix()
        if path.is_symlink(): raise RepositoryError('workspace_initialization_conflict')
        if path.is_dir(): continue
        if (not path.is_file() or name not in baseline['files']
                or path.read_bytes()!=baseline['files'][name]['content']):
            raise RepositoryError('workspace_initialization_conflict')
    for name,item in baseline['files'].items():
        safe_path(name)
        path=workspace/name
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o2770)
        if path.exists(): continue
        with path.open('xb') as output:output.write(item['content'])
        path.chmod(0o770 if item['executable'] else 0o660)
