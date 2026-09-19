"""Controller composition of protected checks over one immutable routed candidate."""
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

from .artifacts import _open_directory, _remove_staging
from .inference_budget import _stamp
from .routed_candidate import CandidateRevision
from .routed_cleanup import RoutedChildCleanup, _publish_exclusive
from .routed_driver import _exclusive_driver
from .routed_observation_store import validate_forbidden_values, scan_output_secrets
from .routed_publication import (authorize_result, result_authority, read_cleanup_evidence,
                                 validate_cleanup_membership, _read)
from .routed_results import ChildResults
from .routed_verification_policy import (VerificationPlan, VerificationLimits,
    build_verification_plan, CheckExecution, assess_verification)
from .routed_verifier_runtime import VerifierRuntime, CheckRuntimeSpec
from .scheduler import _child_control_tx
from .store import encode
from .workflow_revisions import (RevisionBinding, StageMaterialization, materialize_stage,
    verify_unlaunched_materialization, verify_revision, _write_tree, _verify_group)


_CLEANUP_ACTIONS = frozenset({'reconcile','cleanup_inspect','cleanup_logs','stop','remove','status'})
_EXECUTION_ACTIONS = frozenset({'prepare','create','start','inspect','publish','load'})

class VerificationPhaseError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value:
        raise VerificationPhaseError(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _scan(raw, secrets):
    _require(not any(value in raw for value in secrets), 'verification_known_secret_refused')
    scan_output_secrets(b'{}', _json({'text': raw.decode('utf-8', errors='replace')}),
                        forbidden_values=secrets)


def _identity(path):
    fd = _open_directory(path)
    try:
        info = os.fstat(fd)
        _require(info.st_uid == os.geteuid(), 'verification_directory_owner')
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _private_root(path):
    path = Path(os.path.abspath(path))
    _identity(path)
    _require(stat.S_IMODE(path.stat().st_mode) == 0o700, 'verification_private_storage_required')
    return path


def _disjoint(path, sources):
    for source in sources:
        source = Path(source)
        _require(path != source and path not in source.parents and source not in path.parents,
                 'verification_storage_overlap')


def _immutable(path, raw, *, mode=0o440):
    fd, name = tempfile.mkstemp(prefix='.verification-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        try:
            _publish_exclusive(temporary, path)
        except FileExistsError:
            _require(_read(path, len(raw)) == raw, 'verification_file_conflict')
        fd = _open_directory(path.parent)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _material_dict(material):
    value = asdict(material)
    value['source'], value['scratch'] = str(material.source), str(material.scratch)
    return value


def _material_load(value):
    value = dict(value)
    value['source'], value['scratch'] = Path(value['source']), Path(value['scratch'])
    value['consumer'] = RevisionBinding(**value['consumer'])
    for name in ('publication_identity', 'source_identity', 'scratch_identity'):
        value[name] = tuple(value[name])
    return StageMaterialization(**value)


@dataclass(frozen=True)
class PreparedVerification:
    plan: VerificationPlan
    materialization: StageMaterialization
    tasks: tuple[tuple[str, Path, tuple[int, int]], ...]
    storage: Path
    preparation_sha256: str


@dataclass(frozen=True)
class PublishedVerification:
    artifact_id: str
    plan_sha256: str
    receipt_sha256: str
    outcome: str
    remaining_criteria: tuple[str, ...]
    event_sequence: int


def prepare_qualified_verification(scheduler, routed, caller, results, *,
                                   allowed_versions, script_sources, forbidden_values, **options):
    """Use the root's frozen qualified environment; never an ambient active version."""
    from .routed_environment import load_routed_environment
    _require(not {'environment', 'scripts'} & set(options), 'verification_environment_override')
    authorize_result(scheduler, caller, results.quiesced)
    assignment = scheduler.child_assignment(caller.attempt_id, expected_generation=caller.generation)
    request = assignment['attempt']['request']
    snapshot = assignment['workflow_snapshot']['provenance'].get('environment')
    _require(snapshot is not None, 'verification_qualified_environment_missing')
    resolved = load_routed_environment(snapshot, project_id=request['project_id'],
        allowed_versions=allowed_versions, script_sources=script_sources,
        forbidden_values=forbidden_values)
    _require(resolved.manifest['version'] == request['environment_version'],
             'verification_environment_version_changed')
    return prepare_verification(scheduler, routed, caller, results, environment=resolved.manifest,
        scripts=resolved.scripts, forbidden_values=forbidden_values, **options)


def _qualified_environment_binding(authority, secrets, *, environment=None, plan=None):
    from .environments import validate_manifest
    from .routed_environment import validate_routed_environment_snapshot
    provenance = json.loads(authority['root']['frozen'])['provenance']
    if 'environment' not in provenance:
        return
    request = authority['child']['request']
    snapshot = validate_routed_environment_snapshot(provenance['environment'],
        project_id=request['project_id'], allowed_versions=(request['environment_version'],),
        forbidden_values=secrets)
    manifest = snapshot['manifest']
    if environment is not None:
        try: normalized, digest = validate_manifest(environment)
        except ValueError: raise VerificationPhaseError('verification_environment_invalid') from None
        _require(normalized == manifest and digest == snapshot['manifest_sha256'],
                 'verification_qualified_environment_changed')
    if plan is not None:
        _require(plan.environment_sha256 == snapshot['manifest_sha256']
                 and plan.image == manifest['image_digest']
                 and [check.to_dict() for check in plan.checks] == manifest['checks'],
                 'verification_qualified_environment_changed')


def _context(scheduler, routed, caller, results, services):
    _require(type(results) is ChildResults and type(results.candidate) is CandidateRevision,
             'verification_candidate_required')
    collector = RoutedChildCleanup(scheduler, routed, caller, results.quiesced, services=services)
    evidence = collector.collect()
    _require(evidence == results.cleanup, 'verification_cleanup_changed')
    proof = read_cleanup_evidence(collector, evidence)
    candidate = results.candidate
    verify_revision(scheduler.store, candidate.revision)
    with _child_control_tx(scheduler.store, write=False) as db:
        authority = result_authority(scheduler, db, caller, results.quiesced)
        validate_cleanup_membership(db, proof, authority['binding'], authority['root'],
                                   authority['accounts'], scheduler)
        row = db.execute('SELECT metadata FROM artifacts WHERE id=? AND attempt_id=? AND session_id=?',
                         (candidate.artifact_id, caller.attempt_id, caller.session_id)).fetchone()
        metadata = json.loads(row['metadata']) if row else {}
        expected = {'generation': caller.generation, 'provenance': 'controller_captured_candidate',
            'sha256': candidate.revision.sha256, 'output_revision_sha256': candidate.revision.sha256,
            'input_revision_sha256': authority['assignment']['input_revision_sha256'],
            'cleanup_sha256': evidence.evidence_sha256,
            'export_receipt_sha256': candidate.export_receipt_sha256,
            'launch_binding_sha256': results.quiesced.binding_digest,
            'storage_path': str(candidate.revision.path / 'manifest.json')}
        _require(all(metadata.get(k) == v for k, v in expected.items()), 'verification_candidate_binding')
        _require(candidate.input_revision_sha256 == expected['input_revision_sha256']
                 and candidate.cleanup_sha256 == evidence.evidence_sha256
                 and candidate.revision.binding.attempt_id == caller.attempt_id
                 and candidate.revision.binding.generation == caller.generation,
                 'verification_candidate_binding')
    return collector, evidence, proof, authority


def _task_metadata(tasks):
    return [{'check_id': key, 'path': str(path), 'identity': identity}
            for key, path, identity in tasks]


def _preparation_bytes(plan, material, tasks):
    return _json({'schema_version': 1, 'plan': plan.to_dict(),
                  'materialization': _material_dict(material), 'tasks': _task_metadata(tasks)})


def _preparation_identity(prepared):
    _require(type(prepared) is PreparedVerification, 'verification_preparation_invalid')
    _private_root(prepared.storage)
    raw = _read(prepared.storage / 'preparation.json', 1024 * 1024)
    _require(_sha(raw) == prepared.preparation_sha256
             and raw == _preparation_bytes(prepared.plan, prepared.materialization, prepared.tasks),
             'verification_preparation_changed')
    return prepared


def _verify_preparation(prepared):
    _preparation_identity(prepared)
    _tool_group(prepared.materialization.required_tool_gid)
    _require(prepared.materialization.consumer == prepared.plan.binding
             and prepared.materialization.revision_sha256 == prepared.plan.candidate_revision_sha256
             and prepared.materialization.readonly is True, 'verification_material_binding')
    verify_unlaunched_materialization(prepared.materialization)
    _require(tuple(key for key, _, _ in prepared.tasks) == tuple(c.id for c in prepared.plan.checks),
             'verification_task_set_changed')
    _check_tasks(prepared.plan, prepared.tasks)
    for _, path, _ in prepared.tasks:
        _verify_group(path, prepared.materialization.required_tool_gid)
    return prepared



def _fsync_directory(path):
    fd = _open_directory(path)
    try: os.fsync(fd)
    finally: os.close(fd)


def _unpublished_material(root, identity, prefix):
    matches=[]
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            _require(index < 1024, 'verification_material_inventory_limit')
            if entry.name.startswith(prefix) and entry.is_dir(follow_symlinks=False):
                if _identity(Path(entry.path)) == tuple(identity): matches.append(Path(entry.path))
    _require(len(matches) == 1, 'verification_material_publication_unresolved')
    return matches[0]


def _check_tasks(plan, tasks):
    for check, (_, path, identity) in zip(plan.checks, tasks):
        _require(_identity(path) == identity and set(os.listdir(path)) == {check.script_name},
                 'verification_task_identity_changed')
        raw = _read(path / check.script_name, 1024 * 1024, private=False)
        _require(raw == check.script_bytes and _sha(raw) == check.script_sha256,
                 'verification_script_changed')
        info = (path / check.script_name).lstat()
        _require(stat.S_IMODE(info.st_mode) == 0o550, 'verification_script_mode')


def _tool_group(value):
    _require(value is None or type(value) is int and value == 1000,
             'verification_tool_identity_unsupported')

def prepare_verification(scheduler, routed, caller, results, *, services, environment,
                         scripts, storage_root, material_root, forbidden_values,
                         required_tool_gid=None, limits=VerificationLimits()):
    """Trusted frozen operator environment only; never caller-supplied check policy.

    Material parents are separately provisioned (setgid for a different tool GID).
    This does not qualify/activate an environment or change attempt lifecycle.
    """
    secrets = validate_forbidden_values(forbidden_values)
    _tool_group(required_tool_gid)
    with _exclusive_driver(routed, caller):
        collector, evidence, proof, authority = _context(scheduler, routed, caller, results, services)
        _qualified_environment_binding(authority, secrets, environment=environment)
        candidate = results.candidate
        plan = build_verification_plan(binding=candidate.revision.binding,
            input_revision_sha256=candidate.input_revision_sha256,
            candidate_revision_sha256=candidate.revision.sha256,
            launch_binding_sha256=results.quiesced.binding_digest, cleanup_sha256=evidence.evidence_sha256,
            environment=environment, scripts=scripts,
            acceptance=authority['child']['request'].get('acceptance', []), limits=limits)
        _scan(plan.canonical_json.encode(), secrets)
        for check in plan.checks:
            _scan(check.script_bytes, secrets)
        root = _private_root(storage_root)
        material_root = Path(os.path.abspath(material_root)); _identity(material_root)
        sources = (caller.workspace, caller.scratch, caller.task_dir, caller.worker_socket_dir,
                   candidate.revision.path, routed.root)
        _disjoint(root, sources); _disjoint(material_root, sources)
        name = 'verification-' + _sha(f'{caller.attempt_id}:{caller.generation}'.encode())
        storage = root / name
        _disjoint(root, (material_root,))
        material_destination = material_root / name
        destination = material_root / (name + '-checks')
        intent = _json({'schema_version':1, 'plan':plan.to_dict(),
            'material_root':str(material_root), 'material_root_identity':_identity(material_root),
            'required_tool_gid':required_tool_gid})
        if not os.path.lexists(storage):
            _require(not os.path.lexists(material_destination) and not os.path.lexists(destination),
                     'verification_unowned_material_exists')
            staging = Path(tempfile.mkdtemp(prefix='.verification-intent-', dir=root))
            try:
                staging.chmod(0o700)
                _immutable(staging / 'intent.json', intent)
                _publish_exclusive(staging, storage)
                _fsync_directory(root)
            finally:
                if staging.exists(): _remove_staging(staging)
        _private_root(storage)
        _require(_read(storage / 'intent.json', 1024*1024) == intent,
                 'verification_plan_changed')
        if os.path.lexists(storage / 'preparation.json'):
            raw = _read(storage / 'preparation.json', 1024 * 1024)
            value = json.loads(raw)
            _require(value['plan'] == plan.to_dict(), 'verification_plan_changed')
            material = _material_load(value['materialization'])
            tasks = tuple((v['check_id'], Path(v['path']), tuple(v['identity'])) for v in value['tasks'])
            _require(material.source.parent == material_destination
                     and all(path.parent == destination / 'files' for _, path, _ in tasks),
                     'verification_material_location_changed')
            return _verify_preparation(PreparedVerification(plan, material, tasks, storage, _sha(raw)))
        material_intent = storage / 'material-intent.json'
        if os.path.lexists(material_intent):
            material = _material_load(json.loads(_read(material_intent, 65536)))
            _require(material.source.parent == material_destination
                     and material.scratch == material_destination / 'scratch'
                     and material.consumer == plan.binding
                     and material.revision_sha256 == plan.candidate_revision_sha256
                     and material.readonly is True and material.required_tool_gid == required_tool_gid,
                     'verification_material_intent_changed')
            if not os.path.lexists(material_destination):
                staged = _unpublished_material(material_root, material.publication_identity, '.materialize-')
                temporary_material = replace(material, source=staged/'source', scratch=staged/'scratch')
                verify_unlaunched_materialization(temporary_material)
                authorize_result(scheduler, caller, results.quiesced)
                _publish_exclusive(staged, material_destination)
                _fsync_directory(material_root)
            verify_unlaunched_materialization(material)
        else:
            _require(not os.path.lexists(material_destination), 'verification_unowned_material_exists')
            def pin_material(value):
                authorize_result(scheduler, caller, results.quiesced)
                _immutable(material_intent, _json(_material_dict(value)))
                return True
            material = materialize_stage(scheduler.store, candidate.revision, candidate.revision.binding,
                material_destination, readonly=True, required_tool_gid=required_tool_gid,
                before_publish=lambda _: bool(authorize_result(scheduler, caller, results.quiesced)),
                before_publish_material=pin_material)
        checks_intent = storage / 'checks-intent.json'
        if os.path.lexists(checks_intent):
            pinned = json.loads(_read(checks_intent, 65536))
            _require(pinned['plan_sha256'] == plan.digest, 'verification_checks_intent_changed')
            tasks = tuple((v['check_id'], Path(v['path']), tuple(v['identity'])) for v in pinned['tasks'])
            _require(tuple(key for key, _, _ in tasks) == tuple(c.id for c in plan.checks)
                     and all(path == destination/'files'/key for key,path,_ in tasks),
                     'verification_checks_intent_changed')
            if not os.path.lexists(destination):
                staged = _unpublished_material(material_root, tuple(pinned['identity']), '.verification-checks-')
                _check_tasks(plan, tuple((key, staged/'files'/key, identity) for key,_,identity in tasks))
                authorize_result(scheduler, caller, results.quiesced)
                _publish_exclusive(staged, destination)
                _fsync_directory(material_root)
            _require(_identity(destination) == tuple(pinned['identity']), 'verification_task_identity_changed')
            _check_tasks(plan, tasks)
        else:
            _require(not os.path.lexists(destination), 'verification_unowned_material_exists')
            temporary = Path(tempfile.mkdtemp(prefix='.verification-checks-', dir=material_root))
            try:
                _write_tree(temporary / 'files',
                    {c.id + '/' + c.script_name: (c.script_bytes, True) for c in plan.checks},
                    [c.id for c in plan.checks], readonly=True)
                _verify_group(temporary, required_tool_gid)
                authorize_result(scheduler, caller, results.quiesced)
                temporary.chmod(0o550)
                tasks = tuple((c.id, destination/'files'/c.id,
                               _identity(temporary/'files'/c.id)) for c in plan.checks)
                _immutable(checks_intent, _json({'plan_sha256':plan.digest,
                    'identity':_identity(temporary), 'tasks':_task_metadata(tasks)}))
                _publish_exclusive(temporary, destination)
                temporary = None
                _fsync_directory(material_root)
            finally:
                # A pinned unfinished tree belongs to durable reconciliation.
                if temporary is not None and temporary.exists() and not checks_intent.exists():
                    _remove_staging(temporary)
        raw = _preparation_bytes(plan, material, tasks)
        _immutable(storage / 'preparation.json', raw)
        return _verify_preparation(PreparedVerification(plan, material, tasks, storage, _sha(raw)))


def _runtime_specs(routed, prepared):
    plan = prepared.plan
    scope = plan.binding
    return tuple(CheckRuntimeSpec(owner=routed.runtime.owner, attempt_id=scope.attempt_id,
        generation=scope.generation, root_id=scope.root_attempt_id,
        root_generation=scope.root_generation, plan_sha256=plan.digest,
        check_id=check.id, image=plan.image, candidate_path=prepared.materialization.source,
        task_path=path, candidate_identity=prepared.materialization.source_identity,
        task_identity=identity, argv=check.argv, timeout_seconds=check.timeout_seconds,
        uid=1000, gid=1000,
        cpus=plan.limits.cpus, memory_mib=plan.limits.memory_mib, pids=plan.limits.pids,
        max_log_bytes=plan.limits.max_log_bytes)
        for check, (_, path, identity) in zip(plan.checks, prepared.tasks))


def _reconcile_checks(scheduler, routed, caller, results, prepared, verifier, services, secrets):
    _require(type(verifier) is VerifierRuntime and type(results) is ChildResults,
             'verification_runtime_required')
    _preparation_identity(prepared)
    _require(prepared.plan.binding.attempt_id == caller.attempt_id
             and prepared.plan.binding.generation == caller.generation
             and prepared.plan.launch_binding_sha256 == results.quiesced.binding_digest,
             'verification_context_changed')
    collector = RoutedChildCleanup(scheduler, routed, caller, results.quiesced, services=services)
    def cleanup_authority(action):
        _require(type(action) is str and action in _CLEANUP_ACTIONS,
                 'verification_cleanup_only')
        collector._snapshot()
        return True
    return tuple(verifier.reconcile(spec, authorize=cleanup_authority, forbidden_values=secrets)
                 for spec in _runtime_specs(routed, prepared))


def reconcile_verification(scheduler, routed, caller, results, prepared, verifier, *, services,
                           forbidden_values):
    """Stop exact retained verifier operations even after publication is revoked."""
    secrets = validate_forbidden_values(forbidden_values)
    with _exclusive_driver(routed, caller):
        return _reconcile_checks(scheduler, routed, caller, results, prepared, verifier, services, secrets)



def _wall_time():
    return time.time()


def _monotonic():
    return time.monotonic()


def _replace_budget(path, value):
    # Callers hold the child driver lock; existing state is always read/validated.
    raw = _json(value)
    fd, name = tempfile.mkstemp(prefix='.verification-budget-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class _PhaseBudget:
    def __init__(self, prepared, *, replay_only):
        self.path = prepared.storage / 'phase-budget.json'
        self.replay_only = replay_only
        now = self._clock()
        expected = {'schema_version':1, 'plan_sha256':prepared.plan.digest,
                    'preparation_sha256':prepared.preparation_sha256,
                    'max_seconds':prepared.plan.limits.max_seconds}
        if not os.path.lexists(self.path):
            _require(not replay_only, 'verification_budget_missing')
            value = {**expected, 'started_at':now, 'deadline_at':now+prepared.plan.limits.max_seconds,
                     'high_water':now, 'remaining_seconds':float(prepared.plan.limits.max_seconds), 'blocked':False}
            _immutable(self.path, _json(value), mode=0o600)
        self.value = json.loads(_read(self.path, 16384))
        value = self.value
        _require(type(value) is dict and set(value) == set(expected) | {
            'started_at','deadline_at','high_water','remaining_seconds','blocked'}
            and all(value.get(k) == v and type(value.get(k)) is type(v) for k,v in expected.items())
            and type(value['blocked']) is bool
            and all(type(value[k]) in (int,float) and math.isfinite(value[k]) and value[k] > 0
                    for k in ('started_at','deadline_at','high_water'))
            and value['deadline_at'] == value['started_at'] + prepared.plan.limits.max_seconds
            and value['high_water'] >= value['started_at']
            and type(value['remaining_seconds']) in (int,float) and math.isfinite(value['remaining_seconds'])
            and 0 <= value['remaining_seconds'] <= prepared.plan.limits.max_seconds,
            'verification_budget_invalid')
        self.last_monotonic = _monotonic()
        self.monotonic_deadline = self.last_monotonic + min(value['remaining_seconds'],
            max(0, value['deadline_at']-max(now,value['high_water'])))
        self.check()

    @staticmethod
    def _clock():
        now = _wall_time()
        _require(type(now) in (int,float) and math.isfinite(now) and now > 0,
                 'verification_clock_invalid')
        return now

    def check(self):
        # Exact committed publication may be read after phase expiry. No run/start
        # is permitted in this mode; current root/controller authority still applies.
        if self.replay_only:
            return
        value = self.value
        now = self._clock()
        if now < value['high_water']:
            value['blocked'] = True
            _replace_budget(self.path, value)
        _require(not value['blocked'], 'verification_clock_rollback')
        monotonic = _monotonic()
        _require(monotonic >= self.last_monotonic, 'verification_monotonic_clock_changed')
        elapsed = max(now-value['high_water'], monotonic-self.last_monotonic)
        if elapsed > 0:
            value['remaining_seconds'] = max(0.0, value['remaining_seconds']-elapsed)
            value['high_water'] = now
            _replace_budget(self.path, value)
        self.last_monotonic = monotonic
        _require(value['remaining_seconds'] > 0 and now < value['deadline_at']
                 and _monotonic() < self.monotonic_deadline,
                 'verification_phase_deadline')


def _verification_phase(scheduler, routed, caller, results, prepared, verifier, *, services,
                        forbidden_values, require_published):
    """Run/load each isolated check and publish its controller-observed receipt.

    No gate, terminal outcome, account reservation, child seat or session pointer
    is changed. Root orchestration must still commit the protected decision.
    """
    secrets = validate_forbidden_values(forbidden_values)
    _require(type(verifier) is VerifierRuntime, 'verification_runtime_required')
    with _exclusive_driver(routed, caller):
        _reconcile_checks(scheduler, routed, caller, results, prepared, verifier, services, secrets)
        collector, evidence, proof, authority = _context(scheduler, routed, caller, results, services)
        _verify_preparation(prepared)
        plan = prepared.plan
        _qualified_environment_binding(authority, secrets, plan=plan)
        _require(plan.binding == results.candidate.revision.binding
                 and plan.candidate_revision_sha256 == results.candidate.revision.sha256
                 and plan.input_revision_sha256 == results.candidate.input_revision_sha256
                 and plan.launch_binding_sha256 == results.quiesced.binding_digest
                 and plan.cleanup_sha256 == evidence.evidence_sha256,
                 'verification_context_changed')
        _require([asdict(c) for c in plan.acceptance]
                 == authority['child']['request'].get('acceptance', []),
                 'verification_acceptance_changed')
        _scan(plan.canonical_json.encode(), secrets)
        for check in plan.checks:
            _scan(check.script_bytes, secrets)
        artifact_id = 'verification-' + _sha(f'{caller.attempt_id}:{caller.generation}'.encode())
        with _child_control_tx(scheduler.store, write=False) as db:
            replay_only = db.execute('SELECT 1 FROM artifacts WHERE id=? AND attempt_id=?',
                (artifact_id,caller.attempt_id)).fetchone() is not None
        _require(not require_published or replay_only, 'verification_publication_missing')
        budget = _PhaseBudget(prepared, replay_only=replay_only)

        def authorize(action):
            _require(type(action) is str and action in _CLEANUP_ACTIONS | _EXECUTION_ACTIONS,
                     'verification_action_unsupported')
            if action in _CLEANUP_ACTIONS:
                collector._snapshot()
            else:
                budget.check()
                _require(not replay_only or action not in {'prepare','create','start'},
                         'verification_replay_only')
                authorize_result(scheduler, caller, results.quiesced)
                if action in {'prepare', 'create', 'start', 'publish', 'load'}:
                    _verify_preparation(prepared)
                    verify_revision(scheduler.store, results.candidate.revision)
            return True

        records = []
        for check, spec in zip(plan.checks, _runtime_specs(routed, prepared)):
            value = (verifier.load if replay_only else verifier.run)(
                spec, authorize=authorize, forbidden_values=secrets)
            recovered = value if replay_only else verifier.load(
                spec, authorize=authorize, forbidden_values=secrets)
            _require(value == recovered, 'verification_runtime_receipt_changed')
            records.append(CheckExecution(check.id, value.runtime_id, value.exit_code, value.oom,
                value.timed_out, value.cleanup_confirmed, value.logs_sha256))
        decision = assess_verification(plan, records)
        authorize('publish')
        _require(collector.collect() == evidence, 'verification_cleanup_changed')
        raw = _json({'schema_version': 1, 'plan': plan.to_dict(), 'assessment': decision.to_dict()})
        _scan(raw, secrets)
        digest = _sha(raw)
        destination = prepared.storage / ('receipt-' + digest + '.json')
        if require_published:
            _require(_read(destination, len(raw)) == raw, 'verification_receipt_changed')
        else:
            _immutable(destination, raw)
        artifact_id = 'verification-' + _sha(f'{caller.attempt_id}:{caller.generation}'.encode())
        metadata = {'id': artifact_id, 'session_id': caller.session_id, 'attempt_id': caller.attempt_id,
            'generation': caller.generation, 'path': f'routed/{caller.attempt_id}/{caller.generation}/verification.json',
            'storage_path': str(destination), 'bytes': len(raw), 'sha256': digest, 'mime': 'application/json',
            'provenance': 'controller_protected_checks', 'plan_sha256': plan.digest,
            'input_revision_sha256': plan.input_revision_sha256,
            'verified_revision_sha256': plan.candidate_revision_sha256,
            'launch_binding_sha256': plan.launch_binding_sha256, 'cleanup_sha256': evidence.evidence_sha256,
            'outcome': decision.outcome, 'protected_checks_passed': decision.outcome == 'passed'}
        event = {'artifact_id': artifact_id, 'plan_sha256': plan.digest, 'receipt_sha256': digest,
                 'outcome': decision.outcome, 'remaining_criteria': list(decision.remaining_criteria)}
        with _child_control_tx(scheduler.store) as db:
            budget.check()
            current = result_authority(scheduler, db, caller, results.quiesced)
            _qualified_environment_binding(current, secrets, plan=plan)
            _require(current['assignment'] == authority['assignment'], 'verification_assignment_changed')
            _require(current['child']['request'].get('acceptance', [])
                     == [asdict(c) for c in plan.acceptance], 'verification_acceptance_changed')
            validate_cleanup_membership(db, proof, current['binding'], current['root'], current['accounts'], scheduler)
            rows = db.execute('SELECT id,metadata FROM artifacts WHERE id=? OR (attempt_id=? AND json_extract(metadata,\'$.path\')=?)',
                (artifact_id, caller.attempt_id, metadata['path'])).fetchall()
            _require(not require_published or bool(rows), 'verification_publication_missing')
            if rows:
                _require(len(rows) == 1 and rows[0]['id'] == artifact_id
                         and json.loads(rows[0]['metadata']) == metadata, 'verification_publication_conflict')
                found = db.execute("SELECT sequence,payload FROM events WHERE attempt_id=? AND type='workflow.protected_verification' AND json_extract(payload,'$.artifact_id')=?",
                    (caller.attempt_id, artifact_id)).fetchall()
                _require(len(found) == 1 and json.loads(found[0]['payload']) == event,
                         'verification_publication_conflict')
                sequence = found[0]['sequence']
            else:
                db.execute('INSERT INTO artifacts(id,session_id,attempt_id,metadata) VALUES(?,?,?,?)',
                           (artifact_id, caller.session_id, caller.attempt_id, encode(metadata)))
                scheduler.store._event(db, caller.session_id, caller.attempt_id, 'artifact.created',
                    {key:value for key,value in metadata.items() if key != 'storage_path'})
                sequence = scheduler.store._event(db, caller.session_id, caller.attempt_id,
                                                  'workflow.protected_verification', event)
            db.execute('UPDATE inference_budget_roots SET high_water_us=max(high_water_us,?) WHERE root_id=?',
                       (_stamp(current['observed_at']), current['root']['root_id']))
        return PublishedVerification(artifact_id, plan.digest, digest, decision.outcome,
                                     decision.remaining_criteria, sequence)


def run_verification(scheduler, routed, caller, results, prepared, verifier, *, services,
                     forbidden_values):
    return _verification_phase(scheduler, routed, caller, results, prepared, verifier,
        services=services, forbidden_values=forbidden_values, require_published=False)


def load_published_verification(scheduler, routed, caller, results, prepared, verifier, *, services,
                                forbidden_values):
    """Revalidate a committed receipt without launching checks or repairing publication.

    Exact retained runtime cleanup can still occur. This requires current live
    result authority; terminal decision replay has its own durable authority.
    """
    return _verification_phase(scheduler, routed, caller, results, prepared, verifier,
        services=services, forbidden_values=forbidden_values, require_published=True)
