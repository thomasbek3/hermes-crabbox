"""Pure protected-check policy; execution and receipt provenance are external proof."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re

from .environments import Check, validate_manifest
from .models import AcceptanceCriterion
from .workflow_revisions import RevisionBinding


class VerificationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise VerificationError(code)


def _hash(value):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None,
             'verification_digest_invalid')


def _canonical(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                             allow_nan=False)
        encoded.encode('utf-8')
        return encoded
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise VerificationError('verification_encoding_invalid') from None


class CanonicalRecord:
    @property
    def canonical_json(self):
        return _canonical(self.to_dict())

    @property
    def digest(self):
        try:
            return hashlib.sha256(self.canonical_json.encode('utf-8')).hexdigest()
        except UnicodeError:
            raise VerificationError('verification_encoding_invalid') from None


@dataclass(frozen=True)
class VerificationLimits:
    max_seconds: int = 600
    cpus: int = 1
    memory_mib: int = 512
    pids: int = 128
    max_log_bytes: int = 65536

    def __post_init__(self):
        _require(type(self.max_seconds) is int and 1 <= self.max_seconds <= 600
                 and type(self.cpus) is int and 1 <= self.cpus <= 2
                 and type(self.memory_mib) is int and 128 <= self.memory_mib <= 4096
                 and type(self.pids) is int and 16 <= self.pids <= 512
                 and type(self.max_log_bytes) is int and 0 <= self.max_log_bytes <= 1024**2,
                 'verification_limits_invalid')


@dataclass(frozen=True)
class ProtectedCheck:
    id: str
    description: str
    argv: tuple[str, ...]
    timeout_seconds: int
    script_id: str
    script_name: str
    script_sha256: str
    script_bytes: bytes = field(repr=False)

    def __post_init__(self):
        _require(type(self.argv) is tuple, 'verification_check_invalid')
        try:
            Check.model_validate(self.to_dict())
        except Exception:
            raise VerificationError('verification_check_invalid') from None
        _require(all(type(x) is str and x for x in (self.script_id,self.script_name,self.script_sha256))
                 and '/run/task/' + self.script_name in self.argv,
                 'verification_protected_script_required')
        _require(type(self.script_bytes) is bytes and 0 < len(self.script_bytes) <= 1024**2
                 and hashlib.sha256(self.script_bytes).hexdigest() == self.script_sha256,
                 'verification_script_invalid')

    def to_dict(self):
        return {'id':self.id,'description':self.description,'argv':list(self.argv),
                'timeout_seconds':self.timeout_seconds,'script_id':self.script_id,
                'script_name':self.script_name,'script_sha256':self.script_sha256}


@dataclass(frozen=True)
class Criterion:
    id: str
    description: str
    mandatory: bool

    def __post_init__(self):
        _require(type(self.id) is str and type(self.description) is str and type(self.mandatory) is bool,
                 'verification_acceptance_invalid')
        try:
            parsed = AcceptanceCriterion.model_validate(asdict(self)).model_dump()
            _require(parsed == asdict(self), 'verification_acceptance_invalid')
        except Exception:
            raise VerificationError('verification_acceptance_invalid') from None


@dataclass(frozen=True)
class VerificationPlan(CanonicalRecord):
    binding: RevisionBinding
    input_revision_sha256: str
    candidate_revision_sha256: str
    launch_binding_sha256: str
    cleanup_sha256: str
    environment_sha256: str
    image: str
    checks: tuple[ProtectedCheck, ...]
    acceptance: tuple[Criterion, ...]
    limits: VerificationLimits
    schema_version: int = field(default=1, init=False)

    def __post_init__(self):
        _require(type(self.binding) is RevisionBinding and type(self.limits) is VerificationLimits,
                 'verification_plan_invalid')
        for value in (self.input_revision_sha256,self.candidate_revision_sha256,
                      self.launch_binding_sha256,self.cleanup_sha256,self.environment_sha256):
            _hash(value)
        _require(type(self.image) is str and re.fullmatch('sha256:[0-9a-f]{64}',self.image) is not None,
                 'verification_image_invalid')
        _require(type(self.checks) is tuple and len(self.checks) <= 32
                 and all(type(c) is ProtectedCheck for c in self.checks)
                 and len({c.id for c in self.checks}) == len(self.checks)
                 and sum(len(c.script_bytes) for c in self.checks) <= 4*1024**2,
                 'verification_checks_invalid')
        names = {}
        for check in self.checks:
            _require(check.script_name not in names or names[check.script_name] == check.script_sha256,
                     'verification_script_name_conflict')
            names[check.script_name] = check.script_sha256
        _require(type(self.acceptance) is tuple and len(self.acceptance) <= 100
                 and all(type(c) is Criterion for c in self.acceptance)
                 and len({c.id for c in self.acceptance}) == len(self.acceptance),
                 'verification_acceptance_invalid')
        _require(len(self.canonical_json.encode('utf-8')) <= 512*1024, 'verification_plan_limit')

    def to_dict(self):
        return {'schema_version':self.schema_version,'binding':asdict(self.binding),
                'input_revision_sha256':self.input_revision_sha256,
                'candidate_revision_sha256':self.candidate_revision_sha256,
                'launch_binding_sha256':self.launch_binding_sha256,'cleanup_sha256':self.cleanup_sha256,
                'environment_sha256':self.environment_sha256,'image':self.image,
                'checks':[c.to_dict() for c in self.checks],
                'acceptance':[asdict(c) for c in self.acceptance],'limits':asdict(self.limits)}


def build_verification_plan(*, binding, input_revision_sha256, candidate_revision_sha256,
                            launch_binding_sha256, cleanup_sha256, environment, scripts,
                            acceptance, limits=VerificationLimits()):
    _require(type(binding) is RevisionBinding and type(environment) is dict,
             'verification_plan_invalid')
    _require(type(scripts) is dict and len(scripts) <= 32
             and all(type(k) is str and type(v) is bytes and 0 < len(v) <= 1024**2
                     for k,v in scripts.items()) and sum(map(len,scripts.values())) <= 4*1024**2,
             'verification_scripts_invalid')
    _require(type(acceptance) in (list,tuple) and len(acceptance) <= 100,
             'verification_acceptance_invalid')
    criteria = []
    for item in acceptance:
        _require(type(item) is dict and set(item) == {'id','description','mandatory'},
                 'verification_acceptance_invalid')
        criteria.append(Criterion(**item))
    try:
        manifest, environment_sha256 = validate_manifest(environment)
    except Exception:
        raise VerificationError('verification_environment_invalid') from None
    _require(manifest['project_id'] == binding.project_id, 'verification_project_mismatch')
    _require(len(manifest['checks']) <= 32, 'verification_checks_invalid')
    _require(set(scripts) == {c['script_id'] for c in manifest['checks']},
             'verification_protected_script_required')
    checks = tuple(ProtectedCheck(**{**c,'argv':tuple(c['argv'])},script_bytes=scripts[c['script_id']])
                   for c in manifest['checks'])
    return VerificationPlan(binding,input_revision_sha256,candidate_revision_sha256,
                            launch_binding_sha256,cleanup_sha256,environment_sha256,
                            manifest['image_digest'],checks,tuple(criteria),limits)


@dataclass(frozen=True)
class CheckExecution:
    check_id: str
    runtime_id: str
    exit_code: int
    oom: bool
    timed_out: bool
    cleanup_confirmed: bool
    logs_sha256: str

    def __post_init__(self):
        _require(type(self.check_id) is str and re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',self.check_id) is not None,
                 'verification_record_invalid')
        _hash(self.runtime_id)
        _hash(self.logs_sha256)
        _require(type(self.exit_code) is int and 0 <= self.exit_code <= 255
                 and all(type(v) is bool for v in (self.oom,self.timed_out,self.cleanup_confirmed)),
                 'verification_record_invalid')


@dataclass(frozen=True)
class VerificationReceipt(CanonicalRecord):
    plan_sha256: str
    records: tuple[CheckExecution, ...]
    outcome: str
    remaining_criteria: tuple[str, ...]
    schema_version: int = field(default=1, init=False)

    def to_dict(self):
        return {'schema_version':self.schema_version,'plan_sha256':self.plan_sha256,
                'records':[asdict(r) for r in self.records],'outcome':self.outcome,
                'remaining_criteria':list(self.remaining_criteria)}


def assess_verification(plan, records):
    _require(type(plan) is VerificationPlan and type(records) in (list,tuple) and len(records) <= 32,
             'verification_records_invalid')
    parsed=[]
    for value in records:
        try:
            item = CheckExecution(**value) if type(value) is dict else value
        except (TypeError, ValueError):
            raise VerificationError('verification_record_invalid') from None
        _require(type(item) is CheckExecution,'verification_record_invalid')
        parsed.append(item)
    _require([r.check_id for r in parsed] == [c.id for c in plan.checks]
             and len({r.runtime_id for r in parsed}) == len(parsed), 'verification_check_set_mismatch')
    _require(all(r.cleanup_confirmed and not r.oom for r in parsed),
             'verification_infrastructure_unconfirmed')
    passed = {r.check_id for r in parsed if r.exit_code == 0 and not r.timed_out}
    definitions = {c.id:c.description for c in plan.checks}
    remaining=tuple(c.id for c in plan.acceptance if c.mandatory and
                    (c.id not in passed or definitions.get(c.id) != c.description))
    outcome = ('rejected' if len(passed) != len(parsed) else
               'needs_review' if not parsed or remaining else 'passed')
    return VerificationReceipt(plan.digest,tuple(parsed),outcome,remaining)


def validate_receipt(plan, receipt):
    """Recompute policy outcome; caller must separately prove controller provenance."""
    _require(type(receipt) is VerificationReceipt,'verification_receipt_invalid')
    expected=assess_verification(plan,receipt.records)
    _require(receipt.canonical_json == expected.canonical_json,'verification_receipt_conflict')
    return expected


def require_exact_replay(plan, previous, current):
    validate_receipt(plan,previous)
    validate_receipt(plan,current)
    _require(previous.canonical_json == current.canonical_json,'verification_receipt_conflict')
    return previous
