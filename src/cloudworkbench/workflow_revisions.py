"""Controller-owned routed revisions; no lifecycle state or runtime launch authority."""
from dataclasses import asdict, dataclass, replace
from contextlib import closing
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time

from .artifacts import _open_directory, _remove_staging


class RevisionError(ValueError):
    pass


@dataclass(frozen=True)
class RevisionBinding:
    owner_id: str
    project_id: str
    session_id: str
    turn_id: str
    root_attempt_id: str
    root_generation: int
    attempt_id: str
    generation: int

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name in {'generation', 'root_generation'}:
                if type(value) is not int or not 1 <= value <= 2**31:
                    raise RevisionError('invalid_binding')
            elif not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value):
                raise RevisionError('invalid_binding')


@dataclass(frozen=True)
class RevisionLimits:
    max_bytes: int = 32 * 1024**2
    max_entries: int = 2000
    max_seconds: float = 30
    max_depth: int = 32

    def __post_init__(self):
        if (type(self.max_bytes) is not int or not 0 <= self.max_bytes <= 128 * 1024**2
                or type(self.max_entries) is not int or not 1 <= self.max_entries <= 10000
                or type(self.max_depth) is not int or not 1 <= self.max_depth <= 64
                or type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds)
                or not 0 < self.max_seconds <= 120):
            raise RevisionError('invalid_limits')


@dataclass(frozen=True)
class WorkspaceRevision:
    path: Path
    sha256: str
    binding: RevisionBinding


@dataclass(frozen=True)
class StageMaterialization:
    source: Path
    scratch: Path
    revision_sha256: str
    consumer: RevisionBinding
    readonly: bool
    required_tool_gid: int | None
    publication_id: str
    publication_identity: tuple[int, int]
    source_identity: tuple[int, int]
    scratch_identity: tuple[int, int]
    content_sha256: str
    metadata_sha256: str

    @property
    def mounts(self):
        return [{'source': str(self.source), 'target': '/workspace', 'readonly': self.readonly},
                {'source': str(self.scratch), 'target': '/scratch', 'readonly': False}]


_DENIED = frozenset({'.git', '.hermes', '.codex', '.claude', '.ssh', '.aws', '.azure',
                     '.config', '.local', '.cache', '.credentials', 'credential_state',
                     '.npmrc', '.pypirc', '.netrc', '.docker', '.kube', '.gnupg', '.envrc',
                     'credentials', 'auth', 'native', 'node_modules', '.venv', '__pycache__'})


def _safe_path(value):
    if (not isinstance(value, str) or not value or len(value.encode('utf-8', errors='surrogatepass')) > 1024
            or '\\' in value or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/'))):
        raise RevisionError('unsafe_path')
    parts = value.split('/')
    if any(p.lower() in _DENIED or p.lower() == '.env' or p.lower().startswith('.env.')
           or p.lower() in {'auth.json', 'credentials.json', 'token.json', 'tokens.json'} for p in parts):
        raise RevisionError('private_state_refused')
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def validate_binding(store, binding):
    """Identity validation only. Admission/cancellation/writer leases belong to the driver."""
    if type(binding) is not RevisionBinding:
        raise RevisionError('invalid_binding')
    with closing(store._connect()) as db:
        if not store._routed_schema(db):
            raise RevisionError('routed_schema_required')
        row = db.execute('''SELECT a.*,s.owner_id,s.project_id,r.generation root_generation,
          r.session_id root_session,r.turn_id root_turn,r.execution_kind root_kind
          FROM attempts a JOIN sessions s ON s.id=a.session_id
          JOIN attempts r ON r.id=a.workflow_root_id WHERE a.id=?''', (binding.attempt_id,)).fetchone()
    if (row is None or row['execution_kind'] not in {'hermes_root', 'hermes_child'}
            or row['root_kind'] != 'hermes_root'
            or row['root_session'] != binding.session_id or row['root_turn'] != binding.turn_id
            or any(row[key] != value for key, value in asdict(binding).items()
                   if key not in {'root_attempt_id', 'attempt_id'})
            or row['workflow_root_id'] != binding.root_attempt_id):
        raise RevisionError('binding_mismatch')
    if row['execution_kind'] == 'hermes_child' and (
            row['workflow_parent_id'] != binding.root_attempt_id
            or row['workflow_parent_generation'] != binding.root_generation):
        raise RevisionError('binding_mismatch')


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _scan(root, selected, limits, forbidden_values=()):
    """Descriptor-relative bounded reads. Returned bytes are the only bytes later published."""
    if not isinstance(forbidden_values, tuple) or any(type(v) is not bytes or not v for v in forbidden_values):
        raise RevisionError('invalid_secret_scan')
    files, directories = {}, set()
    count = consumed = 0
    deadline = time.monotonic() + limits.max_seconds

    def check():
        if time.monotonic() > deadline:
            raise RevisionError('time_limit')

    def entry(parent, name, relative):
        nonlocal count, consumed
        check()
        _safe_path(relative)
        if len(relative.split('/')) > limits.max_depth:
            raise RevisionError('depth_limit')
        if any(v in relative.encode() for v in forbidden_values):
            raise RevisionError('secret_refused')
        count += 1
        if count > limits.max_entries:
            raise RevisionError('entry_limit')
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        is_dir = stat.S_ISDIR(before.st_mode)
        if not is_dir and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
            raise RevisionError('unsafe_entry')
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_dir else os.O_NONBLOCK)
        fd = os.open(name, flags, dir_fd=parent)
        try:
            opened = os.fstat(fd)
            if _signature(before) != _signature(opened):
                raise RevisionError('source_changed')
            if is_dir:
                directories.add(relative)
                with os.scandir(fd) as children:
                    for child in children:
                        entry(fd, child.name, relative + '/' + child.name)
            else:
                if opened.st_size > limits.max_bytes - consumed:
                    raise RevisionError('byte_limit')
                content = bytearray()
                while True:
                    check()
                    chunk = os.read(fd, min(65536, limits.max_bytes - consumed + 1))
                    if not chunk:
                        break
                    consumed += len(chunk)
                    if consumed > limits.max_bytes:
                        raise RevisionError('byte_limit')
                    content.extend(chunk)
                data = bytes(content)
                if any(v in data for v in forbidden_values):
                    raise RevisionError('secret_refused')
                if len(data) != opened.st_size:
                    raise RevisionError('source_changed')
                files[relative] = (data, bool(opened.st_mode & 0o111))
            if _signature(opened) != _signature(os.fstat(fd)) or _signature(before) != _signature(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                raise RevisionError('source_changed')
        finally:
            os.close(fd)

    root_fd = _open_directory(root)
    try:
        initial = os.fstat(root_fd)
        if selected is None:
            with os.scandir(root_fd) as children:
                for child in children:
                    entry(root_fd, child.name, child.name)
        else:
            def selected_entry(parent, parts, index=0):
                check()
                relative = '/'.join(parts[:index + 1])
                if index == len(parts) - 1:
                    entry(parent, parts[index], relative)
                    return
                before = os.stat(parts[index], dir_fd=parent, follow_symlinks=False)
                child = os.open(parts[index], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    if _signature(before) != _signature(os.fstat(child)):
                        raise RevisionError('source_changed')
                    directories.add(relative)
                    selected_entry(child, parts, index + 1)
                    if (_signature(before) != _signature(os.fstat(child)) or
                            _signature(before) != _signature(os.stat(parts[index], dir_fd=parent, follow_symlinks=False))):
                        raise RevisionError('source_changed')
                finally:
                    os.close(child)
            for relative in selected:
                selected_entry(root_fd, relative.split('/'))
        if _signature(initial) != _signature(os.fstat(root_fd)):
            raise RevisionError('source_changed')
    finally:
        os.close(root_fd)
    if len(files) + len(directories) > limits.max_entries:
        raise RevisionError('entry_limit')
    return files, sorted(directories)


def _metadata(files):
    return [{'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'executable': executable}
            for name, (data, executable) in sorted(files.items())]


def _write_tree(root, files, directories, *, readonly):
    root.mkdir(mode=0o700)
    for name in sorted(directories, key=lambda value: (value.count('/'), value)):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, (data, executable) in files.items():
        path = root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        path.chmod((0o550 if executable else 0o440) if readonly else (0o770 if executable else 0o660))
    for directory, _, _ in os.walk(root, topdown=False):
        fd = _open_directory(Path(directory))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(directory, 0o550 if readonly else 0o770)


def _trusted_directory(path):
    path = Path(os.path.abspath(path))
    try:
        fd = _open_directory(path)
        os.close(fd)
    except OSError as error:
        raise RevisionError('revision_filesystem_error') from error
    return path


def _publish(stage, destination):
    """Serialize cooperating controller publishers; the parent is never job-writable."""
    fd = _open_directory(destination.parent)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(destination):
            raise RevisionError('publication_conflict')
        stage_fd = _open_directory(stage)
        try:
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        os.rename(stage.name, destination.name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def capture_revision(store, binding, workspace, revision_root, *, selected_paths,
                     controller_attests_quiesced=False, limits=RevisionLimits(), forbidden_values=()):
    """Capture explicit controller-selected paths after its physical writer quiescence check."""
    if controller_attests_quiesced is not True:
        raise RevisionError('controller_quiescence_attestation_required')
    validate_binding(store, binding)
    if (not isinstance(selected_paths, tuple) or not 1 <= len(selected_paths) <= limits.max_entries
            or any(not isinstance(p, str) for p in selected_paths)
            or len(set(selected_paths)) != len(selected_paths)):
        raise RevisionError('invalid_selection')
    selected = sorted(_safe_path(p) for p in selected_paths)
    if any(b.startswith(a + '/') for i, a in enumerate(selected) for b in selected[i + 1:]):
        raise RevisionError('overlapping_selection')
    workspace, revision_root = _trusted_directory(workspace), _trusted_directory(revision_root)
    if revision_root == workspace or workspace in revision_root.parents or revision_root in workspace.parents:
        raise RevisionError('storage_overlap')
    stage = None
    try:
        files, directories = _scan(workspace, selected, limits, forbidden_values)
        manifest = {'schema_version': 1, 'binding': asdict(binding), 'selected_paths': selected,
                    'directories': directories, 'files': _metadata(files), 'scope': 'controller_selected_deliverables'}
        raw = _canonical(manifest)
        if len(raw) > 2 * 1024**2:
            raise RevisionError('manifest_limit')
        digest = hashlib.sha256(raw).hexdigest()
        destination = revision_root / digest
        revision = WorkspaceRevision(destination, digest, binding)
        if os.path.lexists(destination):
            verify_revision(store, revision, limits=limits)
            return revision
        stage = Path(tempfile.mkdtemp(prefix='.revision-', dir=revision_root))
        _write_tree(stage / 'files', files, directories, readonly=True)
        with (stage / 'manifest.json').open('xb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        (stage / 'manifest.json').chmod(0o440)
        validate_binding(store, binding)
        stage.chmod(0o550)
        _publish(stage, destination)
        stage = None
        return revision
    except OSError as error:
        raise RevisionError('revision_filesystem_error') from error
    finally:
        if stage is not None and stage.exists():
            _remove_staging(stage)


def _load_verified(store, revision, limits):
    validate_binding(store, revision.binding)
    if not isinstance(revision.sha256, str) or not re.fullmatch('[0-9a-f]{64}', revision.sha256):
        raise RevisionError('invalid_revision')
    root = _trusted_directory(revision.path)
    fd = _open_directory(root)
    try:
        if set(os.listdir(fd)) != {'manifest.json', 'files'}:
            raise RevisionError('revision_changed')
        manifest_fd = os.open('manifest.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(manifest_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 2 * 1024**2:
                raise RevisionError('invalid_manifest')
            raw = bytearray()
            while len(raw) <= 2 * 1024**2:
                chunk = os.read(manifest_fd, 65536)
                if not chunk:
                    break
                raw.extend(chunk)
            if _signature(before) != _signature(os.fstat(manifest_fd)):
                raise RevisionError('revision_changed')
        finally:
            os.close(manifest_fd)
    finally:
        os.close(fd)
    if hashlib.sha256(raw).hexdigest() != revision.sha256:
        raise RevisionError('manifest_digest_mismatch')
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RevisionError('invalid_manifest') from None
    if manifest.get('binding') != asdict(revision.binding) or manifest.get('schema_version') != 1:
        raise RevisionError('binding_mismatch')
    files, directories = _scan(root / 'files', None, limits)
    if manifest.get('files') != _metadata(files) or manifest.get('directories') != directories:
        raise RevisionError('revision_changed')
    return manifest, files, directories


def verify_revision(store, revision, *, limits=RevisionLimits()):
    """Return hash-verified metadata; this does not grant execution or success authority."""
    try:
        return _load_verified(store, revision, limits)[0]
    except OSError as error:
        raise RevisionError('revision_filesystem_error') from error


def _identity(info):
    return info.st_dev, info.st_ino


def _materialization_metadata(value):
    return _canonical({'revision_sha256': value.revision_sha256, 'consumer': asdict(value.consumer),
                       'readonly': value.readonly, 'required_tool_gid': value.required_tool_gid,
                       'publication_id': value.publication_id, 'publication_identity': value.publication_identity,
                       'source_identity': value.source_identity, 'scratch_identity': value.scratch_identity,
                       'content_sha256': value.content_sha256})


def _content_digest(files, directories):
    return hashlib.sha256(_canonical({'files': _metadata(files), 'directories': directories})).hexdigest()


def _verify_group(root, required_gid):
    if required_gid is None:
        return
    for path in (root, *root.rglob('*')):
        info = path.lstat()
        if info.st_gid != required_gid or stat.S_ISLNK(info.st_mode):
            raise RevisionError('materialization_group_mismatch')


def materialize_stage(store, revision, consumer, destination, *, readonly=True, limits=RevisionLimits(),
                      required_tool_gid=None, before_publish=None, before_publish_material=None):
    """Create a fresh isolated stage tree. Driver must enforce the returned bind flags."""
    if type(readonly) is not bool:
        raise RevisionError('invalid_mount_mode')
    if required_tool_gid is not None and (type(required_tool_gid) is not int or not 1 <= required_tool_gid <= 2147483647):
        raise RevisionError('invalid_tool_gid')
    if before_publish is not None and not callable(before_publish):
        raise RevisionError('invalid_publication_guard')
    if before_publish_material is not None and not callable(before_publish_material):
        raise RevisionError('invalid_material_publication_guard')
    validate_binding(store, consumer)
    for name in ('owner_id', 'project_id', 'session_id', 'turn_id', 'root_attempt_id', 'root_generation'):
        if getattr(consumer, name) != getattr(revision.binding, name):
            raise RevisionError('cross_root_revision_refused')
    try:
        _, files, directories = _load_verified(store, revision, limits)
    except OSError as error:
        raise RevisionError('revision_filesystem_error') from error
    destination = Path(os.path.abspath(destination))
    parent = _trusted_directory(destination.parent)
    parent_info = parent.stat()
    if required_tool_gid is not None and (parent_info.st_gid != required_tool_gid or not parent_info.st_mode & stat.S_ISGID):
        raise RevisionError('preprovisioned_setgid_parent_required')
    if revision.path == destination or revision.path in destination.parents or destination in revision.path.parents:
        raise RevisionError('storage_overlap')
    if os.path.lexists(destination):
        raise RevisionError('stage_exists')
    stage = Path(tempfile.mkdtemp(prefix='.materialize-', dir=parent))
    try:
        _write_tree(stage / 'source', files, directories, readonly=readonly)
        (stage / 'scratch').mkdir(mode=0o770)
        (stage / 'scratch').chmod(0o770)
        value = StageMaterialization(destination / 'source', destination / 'scratch', revision.sha256, consumer,
                                     readonly, required_tool_gid, secrets.token_hex(16), _identity(stage.stat()),
                                     _identity((stage / 'source').stat()), _identity((stage / 'scratch').stat()),
                                     _content_digest(files, directories), '')
        binding_bytes = _materialization_metadata(value)
        value = replace(value, metadata_sha256=hashlib.sha256(binding_bytes).hexdigest())
        with (stage / 'materialization.json').open('xb') as output:
            output.write(binding_bytes)
            output.flush()
            os.fsync(output.fileno())
        (stage / 'materialization.json').chmod(0o440)
        validate_binding(store, consumer)
        if before_publish is not None and before_publish(consumer) is not True:
            raise RevisionError('publication_authority_refused')
        _verify_group(stage, required_tool_gid)
        stage.chmod(0o750)
        if before_publish_material is not None and before_publish_material(value) is not True:
            raise RevisionError('material_publication_authority_refused')
        _publish(stage, destination)
        stage = None
        return value
    except OSError as error:
        raise RevisionError('revision_filesystem_error') from error
    finally:
        if stage is not None and stage.exists():
            _remove_staging(stage)


def discard_unlaunched_materialization(value, *, controller_attests_unlaunched=False, limits=RevisionLimits()):
    """Discard only this exact returned, unchanged preparation before any runtime create.

    The caller owns the no-runtime attestation. File observations cannot prove no
    container mounts the directory. Changed work is retained for explicit recovery.
    """
    if controller_attests_unlaunched is not True:
        raise RevisionError('controller_unlaunched_attestation_required')
    return _inspect_unlaunched_materialization(value, limits=limits, discard=True)


def verify_unlaunched_materialization(value, *, limits=RevisionLimits()):
    """Recheck exact publication, metadata and content immediately before launch.

    This observes an unlaunched copy; it cannot prevent another host process from
    modifying the tree later. The controller must retain exclusive writer ownership.
    """
    return _inspect_unlaunched_materialization(value, limits=limits, discard=False)


def _inspect_unlaunched_materialization(value, *, limits, discard):
    if (type(value) is not StageMaterialization or value.source.name != 'source' or value.scratch.name != 'scratch'
            or value.source.parent != value.scratch.parent):
        raise RevisionError('invalid_materialization_handle')
    destination = value.source.parent
    parent = _trusted_directory(destination.parent)
    parent_fd = _open_directory(parent)
    root_fd = None
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root_fd = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        root_info = os.fstat(root_fd)
        if (_identity(root_info) != value.publication_identity or root_info.st_uid != os.geteuid()
                or set(os.listdir(root_fd)) != {'source', 'scratch', 'materialization.json'}):
            raise RevisionError('materialization_identity_changed')
        for name, identity in (('source', value.source_identity), ('scratch', value.scratch_identity)):
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or _identity(info) != identity:
                raise RevisionError('materialization_identity_changed')
        metadata_fd = os.open('materialization.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
        try:
            info = os.fstat(metadata_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_size > 8192:
                raise RevisionError('materialization_metadata_changed')
            raw = os.read(metadata_fd, 8193)
            if (hashlib.sha256(raw).hexdigest() != value.metadata_sha256 or raw != _materialization_metadata(value)
                    or _signature(info) != _signature(os.fstat(metadata_fd))):
                raise RevisionError('materialization_metadata_changed')
        finally:
            os.close(metadata_fd)
        files, directories = _scan(value.source, None, limits)
        if _content_digest(files, directories) != value.content_sha256 or any(value.scratch.iterdir()):
            raise RevisionError('materialization_content_changed')
        if _identity(os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)) != value.publication_identity:
            raise RevisionError('materialization_identity_changed')
        _verify_group(destination, value.required_tool_gid)
        if not discard:
            return {'revision_sha256': value.revision_sha256, 'content_sha256': value.content_sha256,
                    'metadata_sha256': value.metadata_sha256, 'publication_id': value.publication_id}

        def erase(fd):
            # All traversal/deletion remains descriptor-relative and never follows links.
            os.fchmod(fd, 0o700)
            with os.scandir(fd) as entries:
                for entry in entries:
                    info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            if _identity(os.fstat(child_fd)) != _identity(info):
                                raise RevisionError('materialization_identity_changed')
                            erase(child_fd)
                        finally:
                            os.close(child_fd)
                        os.rmdir(entry.name, dir_fd=fd)
                    else:
                        os.unlink(entry.name, dir_fd=fd)
        erase(root_fd)
        os.rmdir(destination.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise RevisionError('materialization_discard_failed') from error
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)
