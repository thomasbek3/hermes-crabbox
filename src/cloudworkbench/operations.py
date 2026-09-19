"""Offline v2 backup and isolated restore. Never starts services or imports auth."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid

from .artifacts import DEPENDENCY_EXCLUSIONS, ExportError, _open_directory, _remove_staging, export_workspace


FORMAT = 'cloud-workbench-offline-backup-v1'
COMPONENTS = frozenset({'artifacts', 'inputs', 'results', 'workspaces', 'native', 'baselines'})
CLOSED_STATES = frozenset({'completed', 'failed', 'cancelled', 'interrupted', 'paused'})
EXCLUDED_NAMES = tuple(sorted((set(DEPENDENCY_EXCLUSIONS) - {'.git'}) | {
    '.volumes', '.credentials', 'credential-state', 'credential_state', '.credentials.json', 'credentials.json', 'auth.json',
    '.env', 'claude-token', 'token', 'tokens', 'credentials', 'auth', 'secrets',
    '.ssh', '.aws', '.gnupg', 'cookies', 'Cookies',
}))


class OperationsError(ValueError):
    pass


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(char) < 32 for char in value):
        raise OperationsError('unsafe bundle path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'.', '..'} for part in path.parts) or path.as_posix() != value:
        raise OperationsError('unsafe bundle path')
    return Path(*path.parts)


def _directory(path):
    fd = _open_directory(Path(path))
    os.close(fd)


def _digest(path, forbidden=(), deadline=None):
    digest = hashlib.sha256()
    size, tail = 0, b''
    overlap = max((len(value) for value in forbidden), default=1) - 1
    with path.open('rb') as handle:
        while chunk := handle.read(128 * 1024):
            if deadline is not None and time.monotonic() > deadline:
                raise OperationsError('operation time limit exceeded')
            scan = tail + chunk
            if any(value in scan for value in forbidden):
                raise OperationsError('protected credential material found in backup')
            tail = scan[-overlap:] if overlap else b''
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validate_database(db):
    db.row_factory = sqlite3.Row
    tables = {row['name']: row['type'] for row in db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    required = {'schema_version', 'clients', 'sessions', 'turns', 'attempts', 'events', 'idempotency', 'artifacts', 'inputs'}
    if any(tables.get(name) != 'table' for name in required):
        raise OperationsError('not a supported v2 database')
    if [row[0] for row in db.execute('SELECT version FROM schema_version')] != [1]:
        raise OperationsError('unsupported backup schema version')
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise OperationsError('database integrity check failed')
    states = {row[0] for row in db.execute('SELECT DISTINCT state FROM attempts')}
    if not states <= CLOSED_STATES:
        raise OperationsError('backup/restore requires all attempts closed; queued or live work must be resolved explicitly')


def _sqlite_budget(db, byte_budget):
    page_size = db.execute('PRAGMA page_size').fetchone()[0]
    pages = byte_budget // page_size
    if pages < db.execute('PRAGMA page_count').fetchone()[0]:
        raise OperationsError('database exceeds remaining operation byte budget')
    db.execute(f'PRAGMA max_page_count={pages}')


def _validate_references(db, included):
    def validate(kind, path, expected_hash, expected_bytes):
        record = included.get(path)
        if record is None:
            raise OperationsError('referenced input or artifact was omitted or missing')
        if type(expected_bytes) is not int or expected_bytes != record['bytes'] or expected_hash != record['sha256']:
            raise OperationsError(f'referenced {kind} content does not match database integrity metadata')
    for row in db.execute('SELECT metadata FROM artifacts'):
        metadata = json.loads(row['metadata'])
        validate('artifact', metadata['storage_path'], metadata.get('sha256'), metadata.get('bytes'))
    for row in db.execute('SELECT state,storage_path,sha256,bytes FROM inputs'):
        if row['storage_path']:
            if row['state'] != 'ready':
                raise OperationsError('stored input is not finalized')
            validate('input', row['storage_path'], row['sha256'], row['bytes'])
        elif row['state'] == 'ready':
            raise OperationsError('ready input has no stored content')


def _disable_clients(db):
    count = 0
    for (client_id,) in db.execute('SELECT id FROM clients').fetchall():
        db.execute('UPDATE clients SET token_hash=?, revoked_at=? WHERE id=?', ('backup-disabled:' + str(uuid.uuid4()), _stamp(), client_id))
        count += 1
    return count


def _relocate_references(db, transform):
    for row in db.execute('SELECT id,metadata FROM artifacts').fetchall():
        metadata = json.loads(row['metadata'])
        metadata['storage_path'] = transform('artifacts', metadata['storage_path'])
        db.execute('UPDATE artifacts SET metadata=? WHERE id=?', (json.dumps(metadata, sort_keys=True), row['id']))
    for row in db.execute('SELECT id,state,storage_path FROM inputs').fetchall():
        if row['storage_path']:
            db.execute('UPDATE inputs SET storage_path=? WHERE id=?', (transform('inputs', row['storage_path']), row['id']))
        elif row['state'] == 'ready':
            raise OperationsError('ready input has no stored content')


def _write(path, content):
    with path.open('wb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path):
    fd = _open_directory(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _freeze(root):
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            os.chmod(Path(directory) / name, 0o400)
        _sync_directory(directory)
        os.chmod(directory, 0o500)


def _limits(max_bytes, max_files, max_seconds, reserve_bytes):
    if any(type(value) is not int or value < 0 for value in (max_bytes, max_files, reserve_bytes)) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise OperationsError('invalid operation limits')


def backup_state(database: Path, components: dict[str, Path], destination: Path, *,
                 quiesced: bool = False, forbidden_values: tuple[bytes, ...] = (),
                 max_bytes: int = 4 * 1024**3, max_files: int = 100000,
                 max_seconds: float = 120, reserve_bytes: int = 1024**3) -> dict:
    """Create a private plaintext rehearsal bundle from explicitly quiesced v2.

    Caller must stop API/worker/external filesystem writers before acknowledging
    quiescence. A write transaction fences new DB changes during the copy, but
    cannot stop arbitrary filesystem writers. No service or job is stopped here.
    Provider credentials/configuration/logs/tasks/raw loop images are excluded.
    The resulting bundle is NOT encrypted and is NOT a live deployment restore.
    """
    _limits(max_bytes, max_files, max_seconds, reserve_bytes)
    if quiesced is not True:
        raise OperationsError('explicit offline quiescence acknowledgement required')
    if not isinstance(components, dict) or not components or not set(components) <= COMPONENTS:
        raise OperationsError('components must be explicit supported v2 storage roots')
    if any(not isinstance(value, bytes) or not value for value in forbidden_values):
        raise OperationsError('secret scan values must be nonempty bytes')
    database, destination = Path(database).absolute(), Path(destination).absolute()
    roots = {name: Path(path).absolute() for name, path in components.items()}
    _directory(database.parent)
    info = database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OperationsError('database must be a regular unlinked file')
    for root in roots.values():
        _directory(root)
        if database == root or root in database.parents or destination == root or root in destination.parents:
            raise OperationsError('database and backup destination must be outside copied component roots')
    for name, root in roots.items():
        if any(root == other or root in other.parents or other in root.parents for key, other in roots.items() if key != name):
            raise OperationsError('overlapping backup components are not supported')
    _directory(destination.parent)
    if os.path.lexists(destination):
        raise OperationsError('backup destination already exists')
    if shutil.disk_usage(destination.parent).free < 3 * max_bytes + reserve_bytes:
        raise OperationsError('backup disk budget would violate free-space reserve')
    stage = Path(tempfile.mkdtemp(prefix='.cwb-backup-', dir=destination.parent))
    deadline = time.monotonic() + max_seconds
    manifest = {'format': FORMAT, 'created_at': _stamp(), 'schema_version': 1,
                'scope': 'offline-selected-v2-logical-state', 'encrypted': False,
                'credentials': 'excluded; copied client credentials replaced and revoked',
                'execution_policy': 'closed attempts only; no service configuration or auto-start',
                'components': {}, 'files': []}
    try:
        with closing(sqlite3.connect(database, timeout=1)) as fence:
            fence.execute('PRAGMA foreign_keys=ON')
            fence.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            fence.execute('BEGIN IMMEDIATE')
            try:
                _validate_database(fence)
                source_size = fence.execute('PRAGMA page_count').fetchone()[0] * fence.execute('PRAGMA page_size').fetchone()[0]
                if source_size > max_bytes:
                    raise OperationsError('database exceeds backup byte limit')
                copied_database = stage / 'database.sqlite'
                def progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise OperationsError('database backup time limit exceeded')
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as source, closing(sqlite3.connect(copied_database)) as copied:
                    source.backup(copied, pages=64, progress=progress)
                    copied.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                    copied.execute('PRAGMA journal_mode=DELETE')
                    _validate_database(copied)
                    _sqlite_budget(copied, max_bytes)
                    manifest['clients_disabled'] = _disable_clients(copied)
                    def portable(component, value):
                        root = roots.get(component)
                        if root is None:
                            raise OperationsError('referenced storage component is absent')
                        path = Path(value)
                        if not path.is_absolute():
                            path = root / path
                        try:
                            relative = _safe_relative(path.relative_to(root).as_posix())
                        except ValueError as exc:
                            raise OperationsError('storage reference is outside configured component') from exc
                        return (Path('components') / component / relative).as_posix()
                    _relocate_references(copied, portable)
                    copied.commit()
                    copied.execute('VACUUM')
                digest, consumed = _digest(copied_database, forbidden_values, deadline)
                manifest['files'].append({'path': 'database.sqlite', 'sha256': digest, 'bytes': consumed})
                (stage / 'components').mkdir(mode=0o700)
                remaining_files = max_files - 4
                if remaining_files < len(roots):
                    raise OperationsError('backup entry limit too small')
                for name, root in sorted(roots.items()):
                    report = {}
                    records = export_workspace(root, stage / 'components' / name, max_bytes=max_bytes - consumed,
                                               max_files=remaining_files-1, forbidden_values=forbidden_values,
                                               max_seconds=max(0.001, deadline-time.monotonic()),
                                               excluded_names=EXCLUDED_NAMES, export_report=report)
                    manifest['components'][name] = report
                    for record in records:
                        manifest['files'].append({'path': (Path('components') / name / record['path']).as_posix(), 'sha256': record['sha256'], 'bytes': record['bytes']})
                        consumed += record['bytes']
                    remaining_files -= 1 + sum(1 for _ in (stage / 'components' / name).rglob('*'))
                with closing(sqlite3.connect(copied_database)) as copied:
                    copied.row_factory = sqlite3.Row
                    included = {item['path']: item for item in manifest['files']}
                    _validate_references(copied, included)
                manifest['bytes'] = consumed
                manifest['file_count'] = len(manifest['files'])
                encoded = json.dumps(manifest, indent=2, sort_keys=True).encode()
                if len(encoded) > 16 * 1024**2 or consumed + len(encoded) > max_bytes:
                    raise OperationsError('backup manifest exceeds operation limit')
                _write(stage / 'manifest.json', encoded)
                _digest(stage / 'manifest.json', forbidden_values, deadline)
                _freeze(stage)
                if os.path.lexists(destination):
                    raise OperationsError('backup destination already exists')
                os.rename(stage, destination)
                stage = None
                _sync_directory(destination.parent)
                return manifest
            finally:
                fence.rollback()
    except (sqlite3.Error, OSError, ExportError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise OperationsError('backup failed; source state was not changed') from exc
    finally:
        if stage is not None:
            _remove_staging(stage)


def restore_state(backup: Path, target: Path, *, max_bytes: int = 4 * 1024**3,
                  max_files: int = 100000, max_seconds: float = 120,
                  reserve_bytes: int = 1024**3) -> dict:
    """Verify and restore into an existing empty directory. No daemon promotion."""
    _limits(max_bytes, max_files, max_seconds, reserve_bytes)
    backup, target = Path(backup).absolute(), Path(target).absolute()
    _directory(backup)
    _directory(target)
    _directory(target.parent)
    if target == backup or backup in target.parents or target in backup.parents:
        raise OperationsError('restore target must be separate from backup')
    info = target.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022 or any(target.iterdir()):
        raise OperationsError('restore target must be empty, owned, and not group/world writable')
    if shutil.disk_usage(target.parent).free < 3 * max_bytes + reserve_bytes:
        raise OperationsError('restore disk budget would violate free-space reserve')
    deadline = time.monotonic() + max_seconds
    staging_parent = Path(tempfile.mkdtemp(prefix='.cwb-restore-', dir=target.parent))
    stage = staging_parent / 'payload'
    promoted = False
    try:
        records = export_workspace(backup, stage, max_bytes=max_bytes, max_files=max_files-1, max_seconds=max_seconds)
        entries = {record['path']: record for record in records}
        if 'manifest.json' not in entries or entries['manifest.json']['bytes'] > 16 * 1024**2:
            raise OperationsError('backup manifest missing or too large')
        manifest = json.loads((stage / 'manifest.json').read_text())
        if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('schema_version') != 1:
            raise OperationsError('unsupported backup format')
        if not isinstance(manifest.get('files'), list) or not isinstance(manifest.get('components'), dict) or not set(manifest['components']) <= COMPONENTS:
            raise OperationsError('invalid backup manifest schema')
        expected = {}
        for record in manifest['files']:
            path = _safe_relative(record['path']).as_posix()
            parts = PurePosixPath(path).parts
            if path != 'database.sqlite' and (len(parts) < 3 or parts[0] != 'components' or parts[1] not in manifest['components']):
                raise OperationsError('unexpected backup manifest path')
            if path in expected or path in {'manifest.json', 'restore-receipt.json'}:
                raise OperationsError('duplicate or reserved manifest path')
            expected[path] = record
        if set(entries) != set(expected) | {'manifest.json'} or 'database.sqlite' not in expected:
            raise OperationsError('backup contents do not match manifest')
        for name, expected_record in expected.items():
            actual = entries[name]
            if expected_record['bytes'] != actual['bytes'] or expected_record['sha256'] != actual['sha256']:
                raise OperationsError('backup file integrity mismatch')
        if manifest.get('file_count') != len(expected) or manifest.get('bytes') != sum(record['bytes'] for record in expected.values()):
            raise OperationsError('backup totals do not match manifest')
        os.chmod(stage, 0o700)
        database = stage / 'database.sqlite'
        database.chmod(0o600)
        with closing(sqlite3.connect(database)) as db:
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            _validate_database(db)
            _validate_references(db, expected)
            other_bytes = sum(item['bytes'] for name, item in entries.items() if name != 'database.sqlite')
            _sqlite_budget(db, max_bytes - other_bytes - 8192)
            disabled = _disable_clients(db)
            def relocate(component, value):
                relative = _safe_relative(value)
                required_prefix = ('components', component)
                if relative.parts[:2] != required_prefix or value not in expected:
                    raise OperationsError('restored storage reference is not backed up')
                return str(target / relative)
            _relocate_references(db, relocate)
            db.commit()
            db.execute('VACUUM')
        digest, size = _digest(database)
        receipt = {'format': FORMAT, 'restored_at': _stamp(), 'source_manifest_sha256': entries['manifest.json']['sha256'],
                   'source_file_count': len(expected), 'integrity_verified': True, 'clients_revoked': disabled,
                   'credentials_restored': False, 'services_started': False, 'attempt_states_changed': False,
                   'storage_paths_remapped': True, 'restored_database_sha256': digest,
                   'restored_database_bytes': size,
                   'promotion': 'manual operator review, new credentials, configuration and bounded-volume provisioning required'}
        _write(stage / 'restore-receipt.json', json.dumps(receipt, indent=2, sort_keys=True).encode())
        _freeze(stage)
        if any(target.iterdir()):
            raise OperationsError('restore target changed and is no longer empty')
        # Darwin requires write access while moving a directory across parents.
        os.chmod(stage, 0o700)
        os.rename(stage, target)
        promoted = True
        os.chmod(target, 0o500)
        _sync_directory(target.parent)
        return receipt
    except (sqlite3.Error, OSError, ExportError, KeyError, TypeError, json.JSONDecodeError) as exc:
        message = 'restore published but final durability step failed; inspect target' if promoted else 'restore failed; target was not promoted'
        raise OperationsError(message) from exc
    finally:
        if staging_parent.exists():
            _remove_staging(staging_parent)
