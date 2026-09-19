"""Read-only qualified environment binding for trusted routed-root provenance."""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
import time
from uuid import UUID

from .environments import EnvironmentRegistry, QualificationReceipt, canonical, validate_manifest
from .routed_observation_store import validate_forbidden_values
from .routed_verification_policy import ProtectedCheck, VerificationLimits
from .routed_verifier_runtime import _script, VerifierRuntimeError

MAX_SNAPSHOT_BYTES = 32768
MAX_SCRIPT_BYTES = 1024 * 1024
MAX_TOTAL_SCRIPT_BYTES = 4 * 1024 * 1024
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


class RoutedEnvironmentError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value: raise RoutedEnvironmentError(code)


def _sha(raw): return hashlib.sha256(raw).hexdigest()


def _encode(value):
    try: return canonical(value).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise RoutedEnvironmentError('environment_snapshot_invalid') from None


def _policy(values):
    try: return validate_forbidden_values(values)
    except ValueError: raise RoutedEnvironmentError('environment_secret_policy_invalid') from None


def _scan(raw, forbidden):
    _require(not any(secret in raw for secret in forbidden), 'environment_known_secret_refused')
    if not forbidden:
        return
    try: pending = [json.loads(raw)]
    except (ValueError, UnicodeError, RecursionError): return
    while pending:
        item = pending.pop()
        if type(item) is str:
            try: encoded = item.encode()
            except UnicodeError: raise RoutedEnvironmentError('environment_snapshot_invalid') from None
            _require(not any(secret in encoded for secret in forbidden), 'environment_known_secret_refused')
        elif type(item) is dict:
            pending.extend(item.keys()); pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)


def _selection(project, version, allowed):
    _require(type(project) is str and _IDENTIFIER.fullmatch(project), 'environment_project_invalid')
    _require(type(version) is str and _IDENTIFIER.fullmatch(version), 'environment_explicit_version_required')
    _require(type(allowed) in (tuple, list, set, frozenset) and 0 < len(allowed) <= 128
        and all(type(v) is str and _IDENTIFIER.fullmatch(v) for v in allowed), 'environment_allowed_versions_invalid')
    _require(version in allowed, 'environment_version_not_allowed')


def _date(value):
    _require(type(value) is str and len(value) <= 64, 'environment_qualification_metadata_invalid')
    try: parsed = datetime.fromisoformat(value)
    except ValueError: raise RoutedEnvironmentError('environment_qualification_metadata_invalid') from None
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'environment_qualification_metadata_invalid')
    return parsed


def _qualification(value, manifest, manifest_sha256, started_at):
    _require(type(value) is dict and set(value) == set(QualificationReceipt.model_fields) | {'qualification_id','qualified_at'},
        'environment_qualification_invalid')
    identity = value['qualification_id']
    try: parsed = UUID(identity) if type(identity) is str else None
    except ValueError: parsed = None
    _require(parsed is not None and parsed.version == 4 and str(parsed) == identity, 'environment_qualification_metadata_invalid')
    _require(_date(started_at) <= _date(value['qualified_at']), 'environment_qualification_metadata_invalid')
    try:
        receipt = QualificationReceipt.model_validate({k:v for k,v in value.items() if k in QualificationReceipt.model_fields}).model_dump()
    except ValueError: raise RoutedEnvironmentError('environment_qualification_invalid') from None
    expected = {'manifest_sha256':manifest_sha256, 'image_digest':manifest['image_digest'],
        'os':manifest['os'], 'architecture':manifest['architecture'], 'cli_versions':manifest['cli_versions']}
    _require(all(receipt[k] == v for k,v in expected.items()), 'environment_qualification_identity_mismatch')
    _require(sorted(p['id'] for p in receipt['probes']) == sorted(p['id'] for p in manifest['readiness_probes'])
        and all(p['exit_code'] == 0 for p in receipt['probes']), 'environment_qualification_probes_failed')
    return receipt


def _check_shapes(manifest):
    _require(len(manifest['checks']) <= 32, 'environment_check_limit')
    names = {}
    for check in manifest['checks']:
        _require(all(type(check[k]) is str and check[k] for k in ('script_id','script_name','script_sha256')),
            'environment_protected_script_required')
        try: name = _script(tuple(check['argv']))
        except (ValueError, VerifierRuntimeError): raise RoutedEnvironmentError('environment_check_argv_unsupported') from None
        _require(name == check['script_name'], 'environment_check_argv_unsupported')
        _require(name not in names or names[name] == check['script_sha256'], 'environment_script_name_conflict')
        names[name] = check['script_sha256']
    # Verifier limits remain independent of manifest caller/workspace resources.
    VerificationLimits()


def _validated_snapshot(snapshot, project_id, allowed_versions, forbidden):
    fields = {'schema_version','project_id','version','manifest','manifest_sha256','qualification',
        'qualification_started_at','qualification_sha256','snapshot_sha256'}
    _require(type(snapshot) is dict and set(snapshot) == fields and type(snapshot['schema_version']) is int
        and snapshot['schema_version'] == 1, 'environment_snapshot_invalid')
    _selection(project_id, snapshot['version'], allowed_versions)
    _require(snapshot['project_id'] == project_id, 'environment_project_mismatch')
    raw = _encode(snapshot)
    _require(len(raw) <= MAX_SNAPSHOT_BYTES, 'environment_snapshot_too_large')
    _scan(raw, forbidden)
    _require(snapshot['snapshot_sha256'] == _sha(_encode({k:v for k,v in snapshot.items() if k != 'snapshot_sha256'})),
        'environment_snapshot_digest_mismatch')
    try: manifest, digest = validate_manifest(snapshot['manifest'])
    except ValueError: raise RoutedEnvironmentError('environment_manifest_invalid') from None
    _require(manifest == snapshot['manifest'] and digest == snapshot['manifest_sha256']
        and manifest['project_id'] == project_id and manifest['version'] == snapshot['version'], 'environment_manifest_binding_mismatch')
    _qualification(snapshot['qualification'], manifest, digest, snapshot['qualification_started_at'])
    _require(snapshot['qualification_sha256'] == _sha(_encode(snapshot['qualification'])), 'environment_qualification_digest_mismatch')
    _check_shapes(manifest)
    return json.loads(raw)


def validate_routed_environment_snapshot(snapshot, *, project_id, allowed_versions, forbidden_values=()):
    """Validate trusted controller provenance; a self-hash is not user authority."""
    return _validated_snapshot(snapshot, project_id, allowed_versions, _policy(forbidden_values))


def _identity(info):
    return info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode


def _trusted(info, *, directory):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    sticky_root_directory = directory and info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    _require(kind(info.st_mode) and info.st_uid in (0, os.geteuid())
        and (not info.st_mode & 0o022 or sticky_root_directory),
        'environment_script_source_untrusted')
    if not directory:
        _require(info.st_nlink == 1 and 0 < info.st_size <= MAX_SCRIPT_BYTES, 'environment_script_size_or_links_invalid')


def _read_script(source, deadline):
    _require(type(source) is str or isinstance(source, Path), 'environment_script_path_invalid')
    path = Path(source)
    _require(path.is_absolute() and '..' not in path.parts and len(path.parts) <= 64
        and len(str(path).encode()) <= 4096 and '\x00' not in str(path), 'environment_script_path_invalid')
    descriptors = []; links = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW); descriptors.append(fd)
        _trusted(os.fstat(fd), directory=True)
        for index, name in enumerate(path.parts[1:]):
            _require(time.monotonic() < deadline, 'environment_script_read_deadline')
            directory = index < len(path.parts) - 2
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0)
            parent = fd; fd = os.open(name, flags, dir_fd=parent); descriptors.append(fd)
            info = os.fstat(fd); _trusted(info, directory=directory)
            links.append((parent, name, _identity(info)))
        _require(len(descriptors) > 1, 'environment_script_path_invalid')
        before = os.fstat(fd); output = bytearray()
        while True:
            _require(time.monotonic() < deadline, 'environment_script_read_deadline')
            part = os.read(fd, min(65536, MAX_SCRIPT_BYTES + 1 - len(output)))
            if not part: break
            output.extend(part)
            _require(len(output) <= MAX_SCRIPT_BYTES, 'environment_script_size_or_links_invalid')
        after = os.fstat(fd)
        _require(_identity(before) == _identity(after) and before.st_size == after.st_size == len(output)
            and before.st_mtime_ns == after.st_mtime_ns and before.st_ctime_ns == after.st_ctime_ns,
            'environment_script_changed')
        for parent, name, identity in links:
            _require(_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == identity,
                'environment_script_changed')
        return bytes(output)
    except OSError: raise RoutedEnvironmentError('environment_script_unavailable') from None
    finally:
        for fd in reversed(descriptors): os.close(fd)


@dataclass(frozen=True)
class ResolvedRoutedEnvironment:
    _snapshot_json: str = field(repr=False)
    _scripts: tuple[tuple[str, bytes], ...] = field(repr=False)

    @property
    def snapshot(self): return json.loads(self._snapshot_json)
    @property
    def manifest(self): return self.snapshot['manifest']
    @property
    def scripts(self): return dict(self._scripts)


def load_routed_environment(snapshot, *, project_id, allowed_versions, script_sources, forbidden_values):
    """Reload the frozen version and exact scripts without consulting any registry/default."""
    forbidden = _policy(forbidden_values)
    value = _validated_snapshot(snapshot, project_id, allowed_versions, forbidden)
    _require(type(script_sources) is dict and len(script_sources) <= 4096, 'environment_script_mapping_invalid')
    scripts = {}; total = 0; deadline = time.monotonic() + 5
    for check in value['manifest']['checks']:
        identity = check['script_id']
        _require(identity in script_sources, 'environment_script_mapping_missing')
        if identity not in scripts:
            raw = _read_script(script_sources[identity], deadline); total += len(raw)
            _require(total <= MAX_TOTAL_SCRIPT_BYTES, 'environment_script_total_limit')
            _scan(raw, forbidden); scripts[identity] = raw
        try: ProtectedCheck(**{**check, 'argv':tuple(check['argv'])}, script_bytes=scripts[identity])
        except ValueError: raise RoutedEnvironmentError('environment_script_digest_mismatch') from None
    return ResolvedRoutedEnvironment(canonical(value), tuple(sorted(scripts.items())))


def resolve_routed_environment(registry, *, project_id, version, allowed_versions, script_sources, forbidden_values):
    """Resolve one explicit operator-qualified version through a read-only registry view."""
    _selection(project_id, version, allowed_versions); forbidden = _policy(forbidden_values)
    _require(type(registry) is EnvironmentRegistry, 'environment_registry_invalid')
    try:
        reader = EnvironmentRegistry(registry.path, read_only=True)
        record = reader.get(project_id, version)
    except (ValueError, OSError, sqlite3.Error): raise RoutedEnvironmentError('environment_registry_unavailable') from None
    qualification = record.get('qualification')
    _require(type(qualification) is dict, 'environment_not_qualified')
    qualification_id = qualification.get('qualification_id')
    _require(type(qualification_id) is str and len(qualification_id) <= 64, 'environment_qualification_metadata_invalid')
    try:
        with reader.db() as db:
            row = db.execute('SELECT project,version,started_at,finished_at,status,receipt,error_code FROM qualification_attempts WHERE id=?',
                (qualification_id,)).fetchone()
    except sqlite3.Error: raise RoutedEnvironmentError('environment_registry_unavailable') from None
    _require(row is not None and row['project'] == project_id and row['version'] == version
        and row['status'] == 'passed' and row['error_code'] is None and row['finished_at'] == qualification.get('qualified_at'),
        'environment_qualification_record_mismatch')
    receipt = _qualification(qualification, record['manifest'], record['manifest_sha256'], row['started_at'])
    _require(row['receipt'] == canonical(receipt), 'environment_qualification_record_mismatch')
    body = {'schema_version':1, 'project_id':project_id, 'version':version, 'manifest':record['manifest'],
        'manifest_sha256':record['manifest_sha256'], 'qualification':qualification,
        'qualification_started_at':row['started_at'], 'qualification_sha256':_sha(_encode(qualification))}
    body['snapshot_sha256'] = _sha(_encode(body))
    return load_routed_environment(body, project_id=project_id, allowed_versions=allowed_versions,
        script_sources=script_sources, forbidden_values=forbidden)
