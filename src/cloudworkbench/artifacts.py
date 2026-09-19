"""Bounded, fail-closed export from a stopped workspace into private storage."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time


# Controller-selected names, never imported from worker .gitignore or prompts.
# Build outputs (build/dist/target) may be deliverables and remain included.
DEPENDENCY_EXCLUSIONS = (
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
)


class ExportError(ValueError):
    @property
    def code(self):
        message = str(self)
        if 'credential' in message: return 'artifact_secret_rejected'
        if 'byte' in message or 'export limit' in message: return 'artifact_byte_limit'
        if 'count' in message: return 'artifact_entry_limit'
        if 'depth' in message: return 'artifact_depth_limit'
        if 'time limit' in message: return 'artifact_time_limit'
        if 'links' in message or 'special' in message: return 'artifact_unsafe_entry'
        if 'changed' in message: return 'artifact_changed'
        if 'filename' in message: return 'artifact_unsafe_name'
        return 'artifact_export_failed'



def _open_directory(path: Path) -> int:
    """Resolve every component through descriptors without following symlinks."""
    absolute = Path(os.path.abspath(path))
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _remove_staging(path: Path) -> None:
    for directory, _, _ in os.walk(path):
        os.chmod(directory, 0o700)
    shutil.rmtree(path)


def export_workspace(
    workspace: Path,
    destination: Path,
    max_bytes: int = 2 * 1024**3,
    max_files: int = 10000,
    forbidden_values: tuple[bytes, ...] = (),
    *,
    max_seconds: float = 120,
    max_depth: int = 64,
    shared_group: bool = False,
    excluded_names: tuple[str, ...] = (),
    export_report: dict | None = None,
    receipt_path: Path | None = None,
    receipt_context: dict | None = None,
) -> list[dict]:
    """Publish immutable selected files, or leave no destination on failure.

    The caller must quiesce all workspace writers first. Destination's parent is
    a controller-owned directory; workers must never be able to write there.
    The strict default includes every entry. A controller may explicitly select
    exact excluded entry names at any depth, including untraversed links with
    those names, but must then provide export_report and persist it with results.
    Report counts refer to excluded roots, not their untraversed descendants.
    Every included link, special file, mutation, secret, or limit breach rejects
    the entire export. The source is never modified. Storage paths are internal.
    export_report is populated only after publication succeeds.
    """
    if max_bytes < 0 or max_files < 0 or max_seconds <= 0 or max_depth < 0:
        raise ExportError("invalid export limits")
    if any(not isinstance(secret, bytes) or not secret for secret in forbidden_values):
        raise ExportError("secret scan values must be nonempty bytes")
    if not isinstance(excluded_names, tuple) or len(excluded_names) > 64:
        raise ExportError("exclusion policy must be a bounded tuple of exact names")
    if any(not isinstance(name, str) or not 1 <= len(name) <= 80
           or name in {".", ".."}
           or any(not (char.isascii() and (char.isalnum() or char in "._-")) for char in name)
           for name in excluded_names):
        raise ExportError("exclusion policy must contain safe exact entry names")
    if export_report is not None and not isinstance(export_report, dict):
        raise ExportError("export report must be a dictionary")
    if excluded_names and export_report is None:
        raise ExportError("exclusions require a report sink for result provenance")
    if any(secret in name.encode() for name in excluded_names for secret in forbidden_values):
        raise ExportError("exclusion policy contains protected credential material")
    excluded_set = frozenset(excluded_names)
    report = {
        "schema_version": 1,
        "scope": "selected_workspace_files" if excluded_set else "complete_workspace_files",
        "complete_workspace": not bool(excluded_set),
        "excluded_names": sorted(excluded_set),
        "omitted_count": 0,
        "omitted_by_name": {},
        "omissions": [],
        "omissions_truncated": False,
        "omitted_descendants": "not traversed; descendant counts and bytes unknown",
    }
    report_bytes = 0

    def record_omission(path: Path, info: os.stat_result, name: str) -> None:
        nonlocal report_bytes
        report["omitted_count"] += 1
        report["omitted_by_name"][name] = report["omitted_by_name"].get(name, 0) + 1
        relative = path.as_posix()
        entry_type = "directory" if stat.S_ISDIR(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "special"
        item = {"path": relative[:512], "type": entry_type, "reason": "configured_exclusion"}
        if len(relative) > 512:
            item.update(path_truncated=True, path_sha256=hashlib.sha256(os.fsencode(path)).hexdigest())
        encoded_length = len(json.dumps(item, ensure_ascii=True).encode())
        if len(report["omissions"]) < 128 and report_bytes + encoded_length <= 24576:
            report["omissions"].append(item)
            report_bytes += encoded_length
        else:
            report["omissions_truncated"] = True

    workspace = Path(workspace)
    destination = Path(os.path.abspath(destination))
    if destination == Path(destination.anchor):
        raise ExportError("invalid destination")
    source_fd = None
    parent_fd = None
    stage = None
    records: list[dict] = []
    consumed = 0
    entries = 0
    deadline = time.monotonic() + max_seconds
    overlap = max((len(value) for value in forbidden_values), default=1) - 1

    def check_time() -> None:
        if time.monotonic() > deadline:
            raise ExportError("artifact export time limit exceeded")

    def walk(directory_fd: int, relative: Path, depth: int) -> None:
        nonlocal consumed, entries
        check_time()
        if depth > max_depth:
            raise ExportError("artifact directory depth exceeded")
        # scandir stays streaming even for directories with millions of entries.
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                check_time()
                entries += 1
                if entries > max_files:
                    raise ExportError("artifact entry count exceeded")
                name = entry.name
                if name in (".", "..") or "\\" in name or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in name):
                    raise ExportError("unsafe artifact filename")
                path = relative / name
                if any(secret in os.fsencode(path) for secret in forbidden_values):
                    raise ExportError("artifact name contains protected credential material")
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if name in excluded_set:
                    record_omission(path, before, name)
                    continue
                if stat.S_ISDIR(before.st_mode):
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    try:
                        actual = os.fstat(child_fd)
                        if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
                            raise ExportError("workspace changed during export")
                        (stage / path).mkdir(mode=0o700)
                        walk(child_fd, path, depth + 1)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                    if len(records) >= max_files or before.st_size > max_bytes - consumed:
                        raise ExportError("artifact export limit exceeded")
                    read_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                    try:
                        opened = os.fstat(read_fd)
                        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                            raise ExportError("workspace changed during export")
                        digest = hashlib.sha256()
                        size = 0
                        tail = b""
                        output = stage / path
                        with output.open("xb") as target:
                            os.chmod(output, 0o600)
                            while True:
                                check_time()
                                chunk = os.read(read_fd, min(128 * 1024, max_bytes - consumed + 1))
                                if not chunk:
                                    break
                                size += len(chunk)
                                consumed += len(chunk)
                                if consumed > max_bytes:
                                    raise ExportError("artifact byte limit exceeded")
                                scan = tail + chunk
                                if any(secret in scan for secret in forbidden_values):
                                    raise ExportError("artifact contains protected credential material")
                                tail = scan[-overlap:] if overlap else b""
                                digest.update(chunk)
                                target.write(chunk)
                            target.flush()
                            os.fsync(target.fileno())
                        after = os.fstat(read_fd)
                        signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
                        if signature(opened) != signature(after) or size != opened.st_size:
                            raise ExportError("workspace changed during export")
                        os.chmod(output, 0o440 if shared_group else 0o400)
                        records.append({"path": path.as_posix(), "storage_path": str(destination / path), "sha256": digest.hexdigest(), "bytes": size, "executable": bool(opened.st_mode & 0o100), "mime": mimetypes.guess_type(name)[0] or "application/octet-stream"})
                    finally:
                        os.close(read_fd)
                else:
                    raise ExportError("links and special files cannot be exported")

    try:
        source_fd = _open_directory(workspace)
        parent_fd = _open_directory(destination.parent)
        if os.path.lexists(destination):
            raise ExportError("artifact destination already exists")
        source_real = Path(os.path.realpath(workspace))
        if destination == source_real or source_real in destination.parents:
            raise ExportError("artifact destination cannot be inside workspace")
        stage = Path(tempfile.mkdtemp(prefix=".artifact-", dir=destination.parent))
        walk(source_fd, Path(), 0)
        for directory, _, _ in os.walk(stage, topdown=False):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(directory, 0o550 if shared_group else 0o500)
        check_time()
        if os.path.lexists(destination):
            raise ExportError("artifact destination already exists")
        report.update(exported_file_count=len(records), exported_bytes=consumed)
        if receipt_path is not None:
            receipt = {'schema_version': 1, 'destination': str(destination), 'context': receipt_context,
                       'records': sorted(records, key=lambda record: record['path']), 'report': report}
            fd, temporary = tempfile.mkstemp(prefix='.export-receipt-', dir=receipt_path.parent)
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(receipt, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, receipt_path)
                receipt_dir = _open_directory(receipt_path.parent)
                try: os.fsync(receipt_dir)
                finally: os.close(receipt_dir)
            finally:
                if os.path.lexists(temporary): os.unlink(temporary)
        os.rename(stage.name, destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        stage = None
        os.fsync(parent_fd)
        report.update(exported_file_count=len(records), exported_bytes=consumed)
        if export_report is not None:
            export_report.clear()
            export_report.update(report)
        return sorted(records, key=lambda record: record["path"])
    except OSError as exc:
        raise ExportError("artifact filesystem operation failed") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        if stage is not None:
            _remove_staging(stage)


def adopt_export(destination: Path, receipt_path: Path, *, expected_context: dict | None = None) -> tuple[list[dict], dict]:
    """Validate a previously published controller-owned export after interruption."""
    fd = os.open(receipt_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024**2:
            raise ExportError('invalid export receipt')
        with os.fdopen(fd, 'rb', closefd=False) as source: receipt = json.load(source)
    finally: os.close(fd)
    if receipt.get('schema_version') != 1 or receipt.get('destination') != str(destination):
        raise ExportError('invalid export receipt destination')
    if receipt.get('context') != expected_context:
        raise ExportError('export receipt generation or identity mismatch')
    records = receipt.get('records')
    if not isinstance(records, list) or len(records) > 10000:
        raise ExportError('invalid export receipt records')
    expected = {}
    for record in records:
        name = record['path']
        if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                or Path(name).as_posix() != name or name in expected
                or record.get('storage_path') != str(destination/name)):
            raise ExportError('invalid export receipt path')
        expected[name] = record
    actual = set()
    def walk(directory_fd, prefix, depth=0):
        if depth > 64: raise ExportError('artifact directory depth exceeded')
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                before = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                name = str(prefix/entry.name)
                if stat.S_ISDIR(before.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    try: walk(child, prefix/entry.name, depth+1)
                    finally: os.close(child)
                elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and name in expected:
                    record = expected[name]
                    if before.st_size != record['bytes']: raise ExportError('artifact changed after publication')
                    child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                            raise ExportError('artifact changed after publication')
                        digest, count = hashlib.sha256(), 0
                        while chunk := os.read(child, 128*1024):
                            count += len(chunk)
                            if count > record['bytes']: raise ExportError('artifact changed after publication')
                            digest.update(chunk)
                        after = os.fstat(child)
                        if (count != record['bytes'] or digest.hexdigest() != record['sha256']
                                or opened.st_mtime_ns != after.st_mtime_ns or after.st_nlink != 1):
                            raise ExportError('artifact changed after publication')
                    finally: os.close(child)
                    actual.add(name)
                else: raise ExportError('links, special or unexpected files in published export')
    root = _open_directory(destination)
    try: walk(root, Path())
    finally: os.close(root)
    if actual != set(expected): raise ExportError('artifact changed after publication')
    return records, receipt['report']
