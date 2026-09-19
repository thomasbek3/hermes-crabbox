"""Explicit immutable revision copy between controller-authorized workflow roots.

This validates identities and terminal/released source state, not stop/delivery
proof or permission to admit a remediation. Those belong to the caller.
"""
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from .artifacts import _open_directory, _remove_staging
from .workflow_revisions import (RevisionBinding, RevisionError, RevisionLimits,
    WorkspaceRevision, _canonical, _load_verified, _metadata, _publish, _safe_path,
    _trusted_directory, _write_tree, validate_binding)


@dataclass(frozen=True)
class ReboundRevision:
    revision: WorkspaceRevision
    source_sha256: str
    content_sha256: str


def _scope(store, source, target):
    validate_binding(store, source.binding)
    validate_binding(store, target)
    old=source.binding
    if ((old.owner_id,old.project_id,old.session_id)!=(target.owner_id,target.project_id,target.session_id)
            or old.root_attempt_id==target.root_attempt_id or old.turn_id==target.turn_id
            or target.attempt_id!=target.root_attempt_id or target.generation!=target.root_generation):
        raise RevisionError('rebind_scope_mismatch')
    with closing(store._connect()) as db:
        rows={r['id']:r for r in db.execute('SELECT id,state,cancel_requested FROM attempts WHERE id IN (?,?,?)',
            (old.attempt_id,old.root_attempt_id,target.attempt_id))}
        roots={r['root_id']:r for r in db.execute('SELECT * FROM workflow_roots WHERE root_id IN (?,?)',
            (old.root_attempt_id,target.root_attempt_id))}
        if (any(identity not in rows for identity in (old.attempt_id,old.root_attempt_id,target.attempt_id))
                or rows[old.attempt_id]['state']!='completed' or rows[old.root_attempt_id]['state']!='completed'
                or old.root_attempt_id not in roots or roots[old.root_attempt_id]['state']!='released'):
            raise RevisionError('source_not_completed_released')
        if (rows[target.attempt_id]['state'] not in ('queued','preparing','running')
                or rows[target.attempt_id]['cancel_requested'] or target.root_attempt_id not in roots
                or roots[target.root_attempt_id]['state'] not in ('queued','held')
                or roots[target.root_attempt_id]['cancel_requested']
                or db.execute("SELECT 1 FROM attempts WHERE workflow_root_id=? AND id!=? LIMIT 1",
                    (target.root_attempt_id,target.attempt_id)).fetchone()):
            raise RevisionError('target_not_fresh')


def _private(path, *, directory=True, readonly=False):
    fd=_open_directory(path) if directory else os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if (info.st_uid!=os.geteuid() or info.st_mode & (0o222 if readonly else 0o022)
                or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1))):
            raise RevisionError('untrusted_revision_storage')
        return info.st_dev,info.st_ino
    finally:os.close(fd)


def _secret_policy(values):
    if (type(values) is not tuple or len(values)>64 or any(type(v) is not bytes or not 1<=len(v)<=4096 for v in values)
            or sum(map(len,values))>65536):
        raise RevisionError('invalid_secret_scan')


def _manifest(manifest, files, directories, forbidden):
    if (type(manifest) is not dict or set(manifest)!= {'schema_version','binding','selected_paths','directories','files','scope'}
            or type(manifest['schema_version']) is not int or manifest['schema_version']!=1
            or manifest['scope']!='controller_selected_deliverables'):
        raise RevisionError('invalid_manifest')
    selected=manifest['selected_paths']
    if (type(selected) is not list or not selected or any(type(p) is not str for p in selected)
            or selected!=sorted(set(selected))):raise RevisionError('invalid_selection')
    for p in selected:_safe_path(p)
    if any(b.startswith(a+'/') for i,a in enumerate(selected) for b in selected[i+1:]):
        raise RevisionError('overlapping_selection')
    for p in (*files,*directories):
        if not any(p==s or p.startswith(s+'/') or (p in directories and s.startswith(p+'/')) for s in selected):
            raise RevisionError('selection_mismatch')
    if any(s not in files and s not in directories for s in selected):raise RevisionError('selection_mismatch')
    # Scan decoded JSON too: canonical JSON escapes Unicode, while policy values
    # refer to the original strings, including filenames and binding fields.
    def scan(value):
        if isinstance(value,str):
            if any(v in value.encode() for v in forbidden):raise RevisionError('secret_refused')
        elif isinstance(value,dict):
            for k,v in value.items():scan(k);scan(v)
        elif isinstance(value,list):
            for v in value:scan(v)
    scan(manifest)
    if any(v in _canonical(manifest) for v in forbidden) or any(v in data for data,_ in files.values() for v in forbidden):
        raise RevisionError('secret_refused')
    return hashlib.sha256(_canonical({'files':_metadata(files),'directories':directories,'selected_paths':selected})).hexdigest()


def _read(store, source, limits, forbidden):
    manifest,files,directories=_load_verified(store,source,limits)
    if (hashlib.sha256(_canonical(manifest)).hexdigest()!=source.sha256
            or _canonical(manifest.get('files'))!=_canonical(_metadata(files))
            or _canonical(manifest.get('directories'))!=_canonical(directories)):
        raise RevisionError('invalid_manifest')
    _private(source.path.parent)
    _private(source.path,readonly=True)
    _private(source.path/'manifest.json',directory=False,readonly=True)
    _private(source.path/'files',readonly=True)
    for p in directories:_private(source.path/'files'/p,readonly=True)
    for p in files:_private(source.path/'files'/p,directory=False,readonly=True)
    content=_manifest(manifest,files,directories,forbidden)
    if len(manifest['selected_paths'])>limits.max_entries:raise RevisionError('entry_limit')
    return manifest,files,directories,content


def rebind_revision(store, source:WorkspaceRevision, target_binding:RevisionBinding, revision_root,
                    *, expected_content_sha256=None, forbidden_values=(), limits=RevisionLimits()):
    """Copy verified bytes; never mutate the source or grant workflow authority.

    The content digest includes executable bits, directories and selection. The
    caller must independently authenticate the source stop/delivery decision and
    freeze returned lineage before registering the new root input.
    """
    stage=None
    try:
        if (type(source) is not WorkspaceRevision or not isinstance(source.path,Path)
                or type(limits) is not RevisionLimits):
            raise RevisionError('invalid_revision')
        if expected_content_sha256 is not None and (type(expected_content_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}',expected_content_sha256) is None):
            raise RevisionError('invalid_content_digest')
        _secret_policy(forbidden_values)
        _scope(store,source,target_binding)
        source_path=_trusted_directory(source.path);root=_trusted_directory(revision_root)
        if root==source_path or root in source_path.parents or source_path in root.parents:
            raise RevisionError('storage_overlap')
        root_identity=_private(root)
        manifest,files,directories,content=_read(store,source,limits,forbidden_values)
        if expected_content_sha256 is not None and content!=expected_content_sha256:
            raise RevisionError('content_digest_mismatch')
        rebound={**manifest,'binding':asdict(target_binding)}
        _manifest(rebound,files,directories,forbidden_values)
        raw=_canonical(rebound);digest=hashlib.sha256(raw).hexdigest()
        if len(raw)>2*1024**2:raise RevisionError('manifest_limit')
        result=WorkspaceRevision(root/digest,digest,target_binding)
        if os.path.lexists(result.path):
            existing=_read(store,result,limits,forbidden_values)
            if existing[:3]!=(rebound,files,directories):raise RevisionError('revision_changed')
            _scope(store,source,target_binding)
            return ReboundRevision(result,source.sha256,content)
        stage=Path(tempfile.mkdtemp(prefix='.rebind-',dir=root))
        _write_tree(stage/'files',files,directories,readonly=True)
        with (stage/'manifest.json').open('xb') as output:
            output.write(raw);output.flush();os.fsync(output.fileno())
        (stage/'manifest.json').chmod(0o440)
        stage.chmod(0o550)
        if _private(root)!=root_identity:raise RevisionError('revision_storage_changed')
        if _read(store,source,limits,forbidden_values)!=(manifest,files,directories,content):
            raise RevisionError('revision_changed')
        _scope(store,source,target_binding)
        _publish(stage,result.path);stage=None
        return ReboundRevision(result,source.sha256,content)
    except OSError:
        raise RevisionError('revision_filesystem_error') from None
    finally:
        if stage is not None and stage.exists():_remove_staging(stage)
