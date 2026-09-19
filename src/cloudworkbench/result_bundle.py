"""Bounded result downloads from an authorized, terminal-attempt DB snapshot."""
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile
import unicodedata

from .artifacts import _open_directory
from .models import TERMINAL
from .resource_monitor import attempt_resources, ResourceHistoryUnavailable

MAX_BYTES = 16 * 1024**2
MAX_FILES = 1000


class BundleError(ValueError):
    def __init__(self, detail, status_code=409):
        self.detail, self.status_code = detail, status_code
        super().__init__(detail)


def _json(value):
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BundleError('Invalid result metadata') from exc


def _object(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BundleError('Invalid result metadata')
    return value


def _elapsed(start, end):
    if not start or not end:
        return {'seconds': None, 'reason': 'timestamp_unavailable'}
    try:
        a, b = (datetime.fromisoformat(value.replace('Z', '+00:00')) for value in (start, end))
        if a.tzinfo is None or b.tzinfo is None:
            raise ValueError
        seconds = (b - a).total_seconds()
        if seconds < 0 or not math.isfinite(seconds):
            raise ValueError
        return {'seconds': seconds, 'reason': None}
    except (ValueError, TypeError, AttributeError, OverflowError):
        return {'seconds': None, 'reason': 'invalid_timestamp'}


def _select(value, schema):
    """Project public receipt fields; never carry an unknown metadata subtree."""
    if value is None:
        return None
    if schema is None:
        if not isinstance(value, (str, int, float, bool)):
            raise BundleError('Invalid public metadata')
        return value
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise BundleError('Invalid public metadata')
        return [_select(item, schema[0]) for item in value]
    return {key: _select(item, schema[key]) for key, item in _object(value).items() if key in schema}


def _fields(*names):
    return dict.fromkeys(names)


RESOURCES = _fields('cpus', 'memory_mib', 'pids', 'workspace_mib')
REPOSITORY = _fields('repository_id', 'base_commit', 'baseline_sha256')
CHANGED_FILE = _fields('path', 'status', 'before_sha256', 'after_sha256', 'before_mode', 'after_mode')
OMISSION = _fields('path', 'type', 'reason', 'path_truncated', 'path_sha256')
DELIVERY = {**_fields('base_commit', 'patch_sha256', 'mode_tracking', 'complete_text_patch'),
            'changed_files': [CHANGED_FILE], 'excluded_from_text_patch': [OMISSION],
            'baseline_paths_not_exported': [OMISSION]}
EXPORT = {**_fields('schema_version', 'scope', 'complete_workspace', 'omitted_count',
                    'omissions_truncated', 'omitted_descendants'),
          'excluded_names': [None], 'omissions': [OMISSION]}
ENVIRONMENT = {**_fields('manifest_sha256', 'qualified', 'created_at'),
               'manifest': {**_fields('schema_version', 'project_id', 'version', 'os', 'architecture',
                                      'base_image_digest', 'image_digest', 'network_profile', 'legacy_template_id'),
                            'resources': RESOURCES,
                            'repositories': [_fields('repository_id', 'commit', 'destination')]}}
CHECK = _fields('id', 'result', 'passed', 'exit_code', 'reason')


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise BundleError('Invalid result identity')
    return value


def latest_root_attempt(attempts):
    """Default results follow session roots, never a delegated child's generation."""
    kinds = {'legacy', 'hermes_root', 'hermes_child'}
    if (not isinstance(attempts, (list, tuple)) or any(not isinstance(a, dict)
            or type(a.get('execution_kind', 'legacy')) is not str
            or a.get('execution_kind', 'legacy') not in kinds for a in attempts)):
        raise BundleError('Invalid attempt metadata')
    roots = [a for a in attempts if a.get('execution_kind', 'legacy') != 'hermes_child']
    if not roots:
        return None
    if not all(type(a.get('generation')) is int and a['generation'] > 0 for a in roots):
        raise BundleError('Invalid attempt generation')
    ordering = 'root_sequence' if any('root_sequence' in a for a in roots) else 'generation'
    if not all(type(a.get(ordering)) is int and a[ordering] > 0 for a in roots):
        raise BundleError('Invalid root attempt order')
    if len({a[ordering] for a in roots}) != len(roots):
        raise BundleError('Ambiguous root attempt order')
    return max(roots, key=lambda a: a[ordering])


def authorized_snapshot(store, principal, session_id):
    """Pin latest session root, provenance and artifacts in one authorized snapshot."""
    store._scope(principal, 'observe')
    store._scope(principal, 'retrieve')
    with store._connect() as db:
        db.execute('BEGIN')
        session = store._session(db, principal, session_id)
        selection = ("AND execution_kind IN ('legacy','hermes_root') ORDER BY root_sequence DESC"
                     if store._routed_schema(db) else 'ORDER BY generation DESC')
        row = db.execute('SELECT id FROM attempts WHERE session_id=? ' + selection + ' LIMIT 1', (session_id,)).fetchone()
        if not row:
            raise BundleError('No attempt available')
        attempt = store._attempt(db, row['id'])
        if attempt['state'] not in TERMINAL:
            raise BundleError('Latest attempt is not terminal')
        rows = db.execute('SELECT id,session_id,attempt_id,metadata FROM artifacts WHERE session_id=? AND attempt_id=? ORDER BY id LIMIT ?',
                          (session_id, attempt['id'], MAX_FILES + 1)).fetchall()
        if len(rows) + 4 > MAX_FILES:
            raise BundleError('Bundle file limit exceeded; use individual artifact downloads', 413)
        provenance = db.execute("SELECT sequence,payload FROM events WHERE session_id=? AND attempt_id=? AND type='adapter.provenance' ORDER BY sequence DESC LIMIT 1",
                                (session_id, attempt['id'])).fetchone()
        try:
            artifacts = [{**_object(json.loads(row['metadata'])), 'id': row['id'], 'session_id': row['session_id'], 'attempt_id': row['attempt_id']} for row in rows]
            session['bundle_provenance'] = {**_object(json.loads(provenance['payload'])), 'event_sequence': provenance['sequence']} if provenance else {}
        except (ValueError, TypeError) as exc:
            raise BundleError('Invalid result metadata') from exc
        running = db.execute("SELECT created_at FROM events WHERE session_id=? AND attempt_id=? AND type='attempt.state' AND json_extract(payload,'$.state')='running' ORDER BY sequence LIMIT 1",
                             (session_id, attempt['id'])).fetchone()
        session['bundle_lifecycle'] = {'running_at': running[0] if running else None}
        try:
            session['bundle_resources'] = attempt_resources(db, attempt)
        except ResourceHistoryUnavailable as exc:
            raise BundleError('Resource history temporarily unavailable; retry bundle download',503) from exc
        session['attempts'] = [attempt]
        return session, artifacts


def _read(artifact, root, limit):
    size = artifact['bytes']
    if type(size) is not int or not 0 <= size <= limit:
        raise BundleError('Bundle byte limit exceeded; use individual artifact downloads', 413)
    try:
        path = Path(artifact['storage_path'])
        relative = path.relative_to(root) if path.is_absolute() else path
        if not relative.parts or '..' in relative.parts:
            raise ValueError
        parent = _open_directory(root)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
                raise ValueError
            content = source.read(size + 1)
            after = os.fstat(source.fileno())
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_nlink)
        if identity(before) != identity(after) or len(content) != size or hashlib.sha256(content).hexdigest() != artifact['sha256']:
            raise ValueError
        return content
    except (OSError, ValueError, TypeError) as exc:
        raise BundleError('Artifact integrity check failed') from exc


def _reported(value, reason):
    return {'value': value if isinstance(value, str) and value else None,
            'reason': None if isinstance(value, str) and value else reason}


def _result(session, attempt):
    raw = _object(attempt.get('result'))
    request = _object(attempt.get('request'))
    environment = _object(raw.get('environment'))
    manifest = _object(environment.get('manifest'))
    provenance = _object(session.get('bundle_provenance'))
    provider = _object(raw.get('provider_result'))
    usage = raw.get('usage')
    known_usage = isinstance(usage, dict) and bool(usage)
    usage_reason = _object(raw.get('usage_status')).get('reason') or 'provider_did_not_report'
    checks = raw.get('checks', [])
    if not isinstance(checks, list) or not all(isinstance(check, dict) for check in checks):
        raise BundleError('Invalid verification metadata')
    summary = raw.get('summary', '')
    if not isinstance(summary, str):
        raise BundleError('Invalid summary metadata')
    expected_versions = _object(manifest.get('cli_versions'))
    cost = provider.get('reported_cost_usd')
    cost_known = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
    verification_start = raw.get('verification_started_at')
    running_at = _object(session.get('bundle_lifecycle')).get('running_at')
    result = {'schema_version': 1, 'session_id': session['id'], 'attempt_id': attempt['id'],
              'generation': attempt['generation'], 'state': attempt['state'], 'outcome': attempt.get('outcome'),
              'reason': attempt.get('reason'), 'exit_code': attempt.get('exit_code'), 'summary': summary,
              'verification': _select(checks, [CHECK]), 'delivery': _select(raw.get('delivery'), DELIVERY),
              'repository': _select(raw.get('repository'), REPOSITORY),
              'environment': _select(raw.get('environment'), ENVIRONMENT), 'environment_version': request.get('environment_version') or raw.get('environment_version'),
              'image_digest': raw.get('image_digest'), 'agent': attempt.get('agent'),
              'continuation': raw.get('continuation') or attempt.get('resume_mode'),
              'provider_provenance': {
                  'requested_model': _reported(request.get('model'), 'configured_default_requested'),
                  'resolved_model': _reported(provenance.get('model'), 'adapter_did_not_report_model'),
                  'cli_version': _reported(provenance.get('cli_version'), 'adapter_did_not_report_cli_version'),
                  'expected_cli_version': _reported(expected_versions.get(attempt.get('agent')), 'environment_cli_version_unavailable'),
                  'source': 'worker_reported_adapter_event' if provenance else 'unavailable',
                  'event_sequence': provenance.get('event_sequence')},
              'timing': {'created_at': attempt.get('created_at'), 'started_at': attempt.get('started_at'),
                         'started_at_basis': 'controller_preparing_transition', 'running_at': running_at,
                         'running_at_basis': 'controller_running_transition' if running_at else 'running_transition_unavailable',
                         'verification_started_at': verification_start, 'finished_at': attempt.get('updated_at'),
                         'created_to_terminal': _elapsed(attempt.get('created_at'), attempt.get('updated_at')),
                         'queue': _elapsed(attempt.get('created_at'), attempt.get('started_at')),
                         'preparation': _elapsed(attempt.get('started_at'), running_at),
                         'execution_and_verification': _elapsed(running_at, attempt.get('updated_at')),
                         'execution': _elapsed(running_at, verification_start),
                         'verification': _elapsed(verification_start, attempt.get('updated_at'))},
              'resources': {'limits': _select(manifest.get('resources'), RESOURCES), 'limits_reason': None if manifest.get('resources') else 'environment_resource_limits_unavailable',
                            **session.get('bundle_resources', {'measured_usage':None,'reason':'no_controller_resource_samples'})},
              'usage': usage if known_usage else None,
              'usage_status': {'state': 'reported', 'reason': None} if known_usage else {'state': 'unknown', 'reason': usage_reason},
              'cost': {'value': None, 'currency': None, 'reason': 'provider_billing_not_available',
                       'provider_reported_usd': cost if cost_known else None,
                       'provider_reported_basis': 'provider_reported_not_verified_billing' if cost_known else 'provider_did_not_report_cost'},
              'remaining_criteria': raw.get('remaining_criteria', []), 'export_report': _select(raw.get('export_report'), EXPORT),
              'partial': raw.get('partial', False), 'unresolved_issues': []}
    issues = result['unresolved_issues']
    if attempt.get('outcome') != 'verified':
        issues.append('Outcome is not independently verified: ' + str(attempt.get('outcome') or attempt['state']))
    if _object(raw.get('delivery')).get('complete_text_patch') is False:
        issues.append('Patch is incomplete; inspect delivery omissions and exported files.')
    if raw.get('artifact_error'):
        issues.append('Artifact export failed; consult authorized event diagnostics.')
    if raw.get('verification_error'):
        issues.append('Verification infrastructure failed; consult authorized event diagnostics.')
    if raw.get('remaining_criteria'):
        issues.append('Some acceptance criteria remain unverified; see remaining_criteria.')
    if not checks:
        issues.append('No verification check receipts were recorded for this attempt.')
    if request.get('model') and provenance.get('model') and request['model'] != provenance['model']:
        issues.append('Requested and reported model differ; provider alias resolution has not been verified.')
    expected_cli = expected_versions.get(attempt.get('agent'))
    if expected_cli and provenance.get('cli_version') and expected_cli != provenance['cli_version']:
        issues.append('Reported CLI version differs from the environment expected version.')
    return result


def build_bundle(session, artifacts, artifact_root, *, max_bytes=MAX_BYTES, max_files=MAX_FILES):
    """Validate every byte before return; budgets include ZIP overhead/metadata.

    Caller authorizes observe + retrieve. No inputs, raw logs, native state,
    credentials or unexported workspace files are read. Archive files are mode600.
    """
    if type(max_bytes) is not int or max_bytes <= 0 or max_bytes > MAX_BYTES:
        raise BundleError('Invalid bundle byte limit', 413)
    if type(max_files) is not int or not 4 <= max_files <= MAX_FILES:
        raise BundleError('Bundle file limit exceeded; use individual artifact downloads', 413)
    try:
        attempts = session.get('attempts', [])
        attempt = latest_root_attempt(attempts)
        if attempt is None:
            raise BundleError('No attempt available')
        if attempt['state'] not in TERMINAL:
            raise BundleError('Latest attempt is not terminal')
        _identity(session['id'])
        _identity(attempt['id'])
        if any(item['session_id'] != session['id'] for item in artifacts):
            raise BundleError('Artifact session mismatch')
        selected = [item for item in artifacts if item['attempt_id'] == attempt['id']]
        if len(selected) + 4 > max_files:
            raise BundleError('Bundle file limit exceeded; use individual artifact downloads', 413)
        result = _result(session, attempt)
        records, files, total, portable_names = [], {}, 0, set()
        root = Path(artifact_root).absolute()
        for item in selected:
            name = item['path']
            if not isinstance(name, str):
                raise BundleError('Unsafe artifact name')
            relative = PurePosixPath(name)
            if (not name or name == '.' or len(name.encode()) > 4096 or relative.is_absolute()
                    or relative.as_posix() != name or '..' in relative.parts or '\\' in name
                    or any(ord(c) < 32 or ord(c) == 127 for c in name)):
                raise BundleError('Unsafe artifact name')
            archive_name = 'files/' + name
            if archive_name in files:
                raise BundleError('Duplicate artifact name')
            if any(other.startswith(archive_name + '/') or archive_name.startswith(other + '/') for other in files):
                raise BundleError('Artifact file/directory name conflict')
            portable_name = unicodedata.normalize('NFC', archive_name).casefold()
            if any(other == portable_name or other.startswith(portable_name + '/') or portable_name.startswith(other + '/') for other in portable_names):
                raise BundleError('Portable artifact name conflict')
            portable_names.add(portable_name)
            content = _read(item, root, max_bytes - total)
            total += len(content)
            files[archive_name] = content
            records.append({key: item.get(key) for key in ('id', 'path', 'bytes', 'sha256', 'mime', 'executable')})
        records.sort(key=lambda item: item['path'])
        manifest_bytes = _json(records)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        result['artifact_manifest_sha256'] = manifest_hash
        result['artifact_count'] = len(records)
        summary = '# Cloud task result\n\nState: ' + attempt['state'] + '\n\nOutcome: ' + str(attempt.get('outcome') or 'not available') + '\n\n'
        summary += result['summary'] + '\n\n'
        if result['unresolved_issues']:
            summary += 'Unresolved issues:\n' + ''.join('- ' + issue + '\n' for issue in result['unresolved_issues'])
        verification = {'attempt_id': attempt['id'], 'generation': attempt['generation'], 'outcome': attempt.get('outcome'),
                        'checks': result['verification'], 'artifact_manifest_sha256': manifest_hash,
                        'basis': 'controller_recorded_verifier_receipts; no new verification performed by download'}
        files.update({'result.json': _json(result), 'summary.md': summary.encode(),
                      'artifact-manifest.json': manifest_bytes, 'verification.json': _json(verification)})
        # ZIP_STORED, no comments/extra fields: 30-byte local + 46-byte central
        # header and the UTF-8 filename twice per entry, plus 22-byte EOCD.
        archive_size = 22 + sum(len(data) + 76 + 2 * len(name.encode()) for name, data in files.items())
        if archive_size > max_bytes:
            raise BundleError('Bundle byte limit exceeded; use individual artifact downloads', 413)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, data)
        blob = output.getvalue()
        if len(blob) != archive_size or len(blob) > max_bytes:
            raise BundleError('Bundle byte limit exceeded; use individual artifact downloads', 413)
        return blob
    except BundleError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BundleError('Invalid result metadata') from exc
