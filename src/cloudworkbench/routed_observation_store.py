"""Private durable worker bytes; neither a workflow outcome nor ownership recovery."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .routed_collection import ExecutionObservation, FileEvidence, collect_execution
from .routed_runtime import CallerSpec
from .scheduler import RoleScheduler

_FILES = frozenset({'manifest.json', 'result.json', 'events.jsonl', 'launch.json'})
_LIMITS = {'manifest.json': 65536, 'result.json': 65536,
           'events.jsonl': 16 * 1024**2, 'launch.json': 512 * 1024}


class ObservationStoreError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value:
        raise ObservationStoreError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ObservationStoreError('observation_snapshot_invalid')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ObservationStoreError('observation_snapshot_invalid') from None


def validate_forbidden_values(values):
    """Freeze a bounded controller policy without including its contents in errors."""
    _require(type(values) in (tuple, list) and len(values) <= 64,
             'observation_secret_policy_invalid')
    policy = tuple(values)
    _require(all(type(value) is bytes and 1 <= len(value) <= 4096 for value in policy),
             'observation_secret_policy_invalid')
    _require(sum(map(len, policy)) <= 65536, 'observation_secret_policy_invalid')
    return policy


def scan_output_secrets(result, events, *, forbidden_values=()):
    """Reject known bytes in raw output or any decoded JSON string/key."""
    policy = validate_forbidden_values(forbidden_values)
    _require(type(result) is bytes and len(result) <= _LIMITS['result.json']
             and type(events) is bytes and len(events) <= _LIMITS['events.jsonl'],
             'observation_bytes_invalid')
    if not policy:
        return
    def scan(raw):
        _require(not any(value in raw for value in policy), 'observation_secret_refused')
    def structured(value):
        pending = [value]
        while pending:
            item = pending.pop()
            if type(item) is str:
                try:
                    raw = item.encode('utf-8')
                except UnicodeError:
                    raise ObservationStoreError('observation_snapshot_invalid') from None
                scan(raw)
            elif type(item) is dict:
                pending.extend(item.keys())
                pending.extend(item.values())
            elif type(item) is list:
                pending.extend(item)
    scan(result)
    scan(events)
    structured(_json(result))
    for line in events.splitlines():
        structured(_json(line))


@dataclass(frozen=True)
class ObservationSnapshot:
    path: Path
    manifest_sha256: str
    attempt_id: str
    generation: int
    runtime_id: str
    binding_digest: str


def _directory(path, *, private):
    path = Path(path)
    _require(path.is_absolute() and '..' not in path.parts, 'observation_path_invalid')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if private:
            _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) in (0o700, 0o2700),
                     'observation_directory_untrusted')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(directory, name, maximum, *, private=True):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_uid == os.geteuid() and before.st_size <= maximum
                 and (stat.S_IMODE(before.st_mode) == 0o600 if private else not before.st_mode & 0o022),
                 'observation_file_untrusted')
        raw = bytearray()
        while len(raw) <= maximum:
            piece = os.read(fd, min(65536, maximum + 1 - len(raw)))
            if not piece:
                break
            raw.extend(piece)
        _require(len(raw) == before.st_size and _signature(before) == _signature(os.fstat(fd))
                 == _signature(os.stat(name, dir_fd=directory, follow_symlinks=False)),
                 'observation_file_changed')
        return bytes(raw)
    finally:
        os.close(fd)


def _location(storage_root, spec):
    _require(type(spec) is CallerSpec and type(spec.generation) is int and spec.generation > 0,
             'observation_binding_invalid')
    root = Path(storage_root)
    fd = _directory(root, private=True)
    os.close(fd)
    for name in (spec.workspace, spec.scratch, spec.task_dir, spec.worker_socket_dir):
        other = Path(name)
        _require(root != other and root not in other.parents and other not in root.parents,
                 'observation_storage_overlap')
    return root / ('observation-' + _sha((spec.attempt_id + ':' + str(spec.generation)).encode()))


def _bound(value):
    return {key: item for key, item in asdict(value).items() if key not in ('state', 'may_start')}


def _launch_receipt(raw, spec):
    _require(_sha(raw) == dict(spec.task_files)['launch.json'], 'observation_launch_changed')
    launch = _json(raw)
    _require(type(launch) is dict and type(launch.get('receipt_json')) is str,
             'observation_launch_invalid')
    return _sha(launch['receipt_json'].encode())


def _parse(result, events, result_evidence, events_evidence, receipt):
    for evidence, raw, limit in ((result_evidence, result, 65536),
                                  (events_evidence, events, 16 * 1024**2)):
        _require(type(evidence) is FileEvidence and type(raw) is bytes
                 and len(raw) <= limit and evidence.size == len(raw)
                 and evidence.sha256 == _sha(raw), 'observation_bytes_changed')
    def reader(*, name, offset, max_bytes):
        raw, evidence = ((result, result_evidence) if name == 'result' else (events, events_evidence))
        data = raw[offset:offset + max_bytes]
        return {'present': True, 'offset': offset, 'next_offset': offset + len(data),
                'inode': evidence.inode, 'size': len(raw), 'mtime_ns': evidence.mtime_ns,
                'data': data, 'provenance': 'worker_reported'}
    return collect_execution(reader, launch_receipt_sha256=receipt)


def _load(path, spec, bound):
    directory = _directory(path, private=True)
    try:
        before = os.fstat(directory)
        _require(set(os.listdir(directory)) == _FILES, 'observation_snapshot_layout_changed')
        files = {name: _read(directory, name, maximum) for name, maximum in _LIMITS.items()}
        _require(_signature(before) == _signature(os.fstat(directory)), 'observation_directory_changed')
    finally:
        os.close(directory)
    manifest = _json(files['manifest.json'])
    _require(type(manifest) is dict and set(manifest) == {'version', 'spec', 'binding', 'files',
             'launch_receipt_sha256', 'result_evidence', 'events_evidence', 'provenance', 'verification_pass'}
             and type(manifest['version']) is int and manifest['version'] == 1
             and _encoded(manifest['spec']) == _encoded(asdict(spec))
             and _encoded(manifest['binding']) == _encoded(_bound(bound))
             and manifest['provenance'] == 'worker_reported' and manifest['verification_pass'] is False
             and manifest['files'] == {name: {'bytes': len(raw), 'sha256': _sha(raw)}
                 for name, raw in files.items() if name != 'manifest.json'}, 'observation_manifest_binding_changed')
    receipt = _launch_receipt(files['launch.json'], spec)
    _require(receipt == manifest['launch_receipt_sha256'], 'observation_launch_changed')
    try:
        result_evidence = FileEvidence(**manifest['result_evidence'])
        events_evidence = FileEvidence(**manifest['events_evidence'])
    except (TypeError, KeyError):
        raise ObservationStoreError('observation_manifest_invalid') from None
    observation = _parse(files['result.json'], files['events.jsonl'], result_evidence, events_evidence, receipt)
    snapshot = ObservationSnapshot(path, _sha(files['manifest.json']), spec.attempt_id,
        spec.generation, bound.runtime_id, bound.binding_digest)
    return observation, snapshot, files


def save_observation(scheduler, storage_root, spec, runtime_id, binding_digest, observation, *,
                     forbidden_values=()):
    """Persist validated bytes before fencing/removing the caller; controller-only."""
    policy = validate_forbidden_values(forbidden_values)
    _require(type(scheduler) is RoleScheduler and type(observation) is ExecutionObservation,
             'observation_context_invalid')
    path = _location(storage_root, spec)
    def authority():
        bound = scheduler.check_child_start_authority(spec.attempt_id,
            expected_generation=spec.generation, binding_digest=binding_digest)
        _require(bound.state == 'started' and bound.runtime_id == runtime_id
                 and bound.caller_spec_digest == spec.digest, 'observation_binding_changed')
        return bound
    bound = authority()
    task = _directory(Path(spec.task_dir), private=False)
    try:
        launch_raw = _read(task, 'launch.json', _LIMITS['launch.json'], private=False)
    finally:
        os.close(task)
    receipt = _launch_receipt(launch_raw, spec)
    result, events = observation.result_json.encode(), observation.events_jsonl.encode()
    parsed = _parse(result, events, observation.result_evidence, observation.events_evidence, receipt)
    _require(_encoded(asdict(parsed)) == _encoded(asdict(observation)), 'observation_changed')
    scan_output_secrets(result, events, forbidden_values=policy)
    files = {'result.json': result, 'events.jsonl': events, 'launch.json': launch_raw}
    manifest = {'version': 1, 'spec': asdict(spec), 'binding': _bound(bound),
        'files': {name: {'bytes': len(raw), 'sha256': _sha(raw)} for name, raw in files.items()},
        'launch_receipt_sha256': receipt, 'result_evidence': asdict(observation.result_evidence),
        'events_evidence': asdict(observation.events_evidence), 'provenance': 'worker_reported',
        'verification_pass': False}
    files['manifest.json'] = _encoded(manifest)
    _require(len(files['manifest.json']) <= _LIMITS['manifest.json'], 'observation_manifest_limit')
    temporary = Path(tempfile.mkdtemp(prefix='.observation-', suffix='.pending', dir=path.parent))
    os.chmod(temporary, 0o700)
    try:
        for name, raw in files.items():
            fd = os.open(temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        directory = _directory(temporary, private=True)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _require(authority() == bound, 'observation_authority_changed')
        from .routed_cleanup import _publish_exclusive
        try:
            _publish_exclusive(temporary, path)
        except FileExistsError:
            _, _, existing = _load(path, spec, bound)
            _require(existing == files, 'observation_snapshot_conflict')
        directory = _directory(path.parent, private=True)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        loaded, snapshot, _ = _load(path, spec, bound)
        _require(_encoded(asdict(loaded)) == _encoded(asdict(observation)), 'observation_changed')
        _require(authority() == bound, 'observation_authority_changed')
        return snapshot
    finally:
        # Only this invocation's unpublished private staging directory is removed.
        if temporary.exists():
            for name in _FILES:
                (temporary / name).unlink(missing_ok=True)
            temporary.rmdir()


def load_observation(scheduler, storage_root, spec, quiesced, *, forbidden_values=()):
    """Read durable bytes under current authority; never adopt a previous controller."""
    policy = validate_forbidden_values(forbidden_values)
    from .routed_driver import QuiescedStage
    from .routed_publication import authorize_result
    _require(type(scheduler) is RoleScheduler and type(quiesced) is QuiescedStage,
             'observation_recovery_context_invalid')
    expected_cleanup = {'runtime_id': quiesced.runtime_id, 'attempt_id': spec.attempt_id,
        'generation': spec.generation, 'spec_digest': spec.digest, 'caller_stopped': True,
        'caller_removed': True, 'authority': 'controller_observed_caller_only',
        'provider_cleanup_qualified': False}
    _require(quiesced.grant_fence_confirmed is True and quiesced.cleanup_error is None
             and quiesced.caller_cleanup == expected_cleanup
             and quiesced.verification_pass is False, 'observation_recovery_cleanup_unconfirmed')
    bound = authorize_result(scheduler, spec, quiesced)
    path = _location(storage_root, spec)
    observation, _, files = _load(path, spec, bound)
    scan_output_secrets(files['result.json'], files['events.jsonl'], forbidden_values=policy)
    _require(authorize_result(scheduler, spec, quiesced) == bound, 'observation_authority_changed')
    return observation
