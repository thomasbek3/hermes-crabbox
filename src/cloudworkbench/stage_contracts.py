"""Versioned final-answer structure; model claims never grant controller authority."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import unicodedata

VERSION = 'stage-final-answer-v1'
MAX_OUTPUT_BYTES = 16384
ROLES = frozenset({
    'planning', 'plan_review', 'plan_revision', 'feature', 'bug_fix',
    'refactoring', 'perf_issue', 'hillclimb', 'code_review',
    'acceptance_verification', 'how_explorer', 'how_explainer',
    'why_investigator', 'why_synthesizer', 'reflect_tooling',
})
_PLAN = frozenset({'planning', 'plan_revision'})
_REVIEW = frozenset({'plan_review', 'code_review'})


class StageContractError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code='stage_output_invalid'):
    if not condition:
        raise StageContractError(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _text(value, maximum):
    _require(type(value) is str)
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        raise StageContractError('stage_output_invalid') from None
    _require(0 < size <= maximum and bool(value.strip())
             and not any(unicodedata.category(c) in {'Cc', 'Cf'} for c in value))
    return value


def _digest(value):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None)
    return value


def _object(value, keys):
    _require(type(value) is dict and set(value) == set(keys))
    return value


def _array(value, maximum=64):
    _require(type(value) is list and len(value) <= maximum)
    return value


@dataclass(frozen=True)
class ContextRef:
    artifact_id: str
    sha256: str


def _refs(values):
    _require(type(values) in (list, tuple) and len(values) <= 32)
    result = []
    for value in values:
        row = _object(value, {'artifact_id', 'sha256'})
        _require(type(row['artifact_id']) is str and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', row['artifact_id']) is not None)
        result.append(ContextRef(row['artifact_id'], _digest(row['sha256'])))
    _require(len({v.artifact_id for v in result}) == len(result))
    return tuple(result)


@dataclass(frozen=True)
class StageContract:
    role: str
    input_revision_sha256: str
    context_refs: tuple[ContextRef, ...]

    def to_dict(self):
        return {'version': VERSION, 'schema_version': 1, 'role': self.role,
                'input_revision_sha256': self.input_revision_sha256,
                'context_refs': [asdict(v) for v in self.context_refs],
                'output_kind': 'plan' if self.role in _PLAN else 'review' if self.role in _REVIEW else 'report',
                'max_output_bytes': MAX_OUTPUT_BYTES}

    @property
    def canonical_json(self):
        return _canonical(self.to_dict())

    @property
    def digest(self):
        return hashlib.sha256(self.canonical_json.encode('utf-8')).hexdigest()


def stage_contract(role, input_revision_sha256, context_refs=()):
    _require(type(role) is str and role in ROLES, 'stage_role_invalid')
    return StageContract(role, _digest(input_revision_sha256), _refs(context_refs))


def _validate_contract(contract):
    _require(type(contract) is StageContract, 'stage_contract_invalid')
    _require(type(contract.context_refs) is tuple
             and all(type(v) is ContextRef for v in contract.context_refs), 'stage_contract_invalid')
    expected = stage_contract(contract.role, contract.input_revision_sha256,
                              [asdict(v) for v in contract.context_refs])
    _require(contract == expected, 'stage_contract_invalid')


@dataclass(frozen=True)
class Evidence:
    location: str
    reason: str


@dataclass(frozen=True)
class PlanStep:
    step: int
    action: str
    validation: str


@dataclass(frozen=True)
class Finding:
    severity: str
    location: str
    reason: str
    resolved: bool


@dataclass(frozen=True)
class StageOutput:
    contract_sha256: str
    role: str
    input_revision_sha256: str
    context_refs: tuple[ContextRef, ...]
    summary: str
    evidence: tuple[Evidence, ...]
    status: str | None = None
    recommendation: str | None = None
    plan: tuple[PlanStep, ...] = ()
    findings: tuple[Finding, ...] = ()

    def to_dict(self):
        value = {'schema_version': 1, 'contract_sha256': self.contract_sha256,
                 'role': self.role, 'input_revision_sha256': self.input_revision_sha256,
                 'context_refs': [asdict(v) for v in self.context_refs],
                 'summary': self.summary, 'evidence': [asdict(v) for v in self.evidence]}
        if self.role in _REVIEW:
            value.update(recommendation=self.recommendation,
                         findings=[asdict(v) for v in self.findings])
        else:
            value['status'] = self.status
            if self.role in _PLAN:
                value['plan'] = [asdict(v) for v in self.plan]
        return value

    @property
    def provenance(self):
        return 'worker_reported'

    @property
    def task_acceptance_verified(self):
        return False


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'stage_duplicate_key')
        result[key] = value
    return result


def _constant(_):
    raise StageContractError('stage_output_invalid')


def parse_stage_output(raw, contract):
    """Parse one JSON object from adapter result.summary; no Markdown extraction."""
    _validate_contract(contract)
    _require(type(raw) is str, 'stage_output_invalid')
    try:
        _require(len(raw.encode('utf-8')) <= MAX_OUTPUT_BYTES, 'stage_output_too_large')
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, StageContractError):
            raise
        raise StageContractError('stage_output_invalid') from None
    common = {'schema_version', 'contract_sha256', 'role', 'input_revision_sha256',
              'context_refs', 'summary', 'evidence'}
    extra = {'status', 'plan'} if contract.role in _PLAN else {'recommendation', 'findings'} if contract.role in _REVIEW else {'status'}
    _object(value, common | extra)
    _require(type(value['schema_version']) is int and value['schema_version'] == 1)
    _require(value['contract_sha256'] == contract.digest and value['role'] == contract.role
             and value['input_revision_sha256'] == contract.input_revision_sha256,
             'stage_binding_mismatch')
    _array(value['context_refs'], 32)
    refs = _refs(value['context_refs'])
    _require(refs == contract.context_refs, 'stage_binding_mismatch')
    summary = _text(value['summary'], 8192)
    evidence = []
    for row in _array(value['evidence']):
        _object(row, {'location', 'reason'})
        evidence.append(Evidence(_text(row['location'], 1024), _text(row['reason'], 2048)))
    status = recommendation = None
    plan, findings = [], []
    if contract.role in _REVIEW:
        recommendation = value['recommendation']
        _require(type(recommendation) is str and recommendation in ('approve', 'revise', 'needs_review'))
        for row in _array(value['findings']):
            _object(row, {'severity', 'location', 'reason', 'resolved'})
            _require(type(row['severity']) is str and row['severity'] in ('critical', 'high', 'medium', 'low')
                     and type(row['resolved']) is bool)
            findings.append(Finding(row['severity'], _text(row['location'], 1024),
                                    _text(row['reason'], 2048), row['resolved']))
        _require(not (recommendation == 'approve' and any(
            f.severity in ('critical', 'high') and not f.resolved for f in findings)), 'stage_severe_unresolved')
    else:
        status = value['status']
        _require(type(status) is str and status in ('complete', 'needs_review'))
        if contract.role in _PLAN:
            for index, row in enumerate(_array(value['plan']), 1):
                _object(row, {'step', 'action', 'validation'})
                _require(type(row['step']) is int and row['step'] == index)
                plan.append(PlanStep(index, _text(row['action'], 2048), _text(row['validation'], 2048)))
            _require(status != 'complete' or bool(plan), 'stage_plan_missing')
    return StageOutput(contract.digest, contract.role, contract.input_revision_sha256, refs,
                       summary, tuple(evidence), status, recommendation, tuple(plan), tuple(findings))


def render_stage_instructions(contract):
    _validate_contract(contract)
    template = {'schema_version': 1, 'contract_sha256': contract.digest,
                'role': contract.role, 'input_revision_sha256': contract.input_revision_sha256,
                'context_refs': [asdict(v) for v in contract.context_refs],
                'summary': '<concise conclusion>',
                'evidence': [{'location': '<file:line or evidence reference>', 'reason': '<supporting observation>'}]}
    if contract.role in _REVIEW:
        template.update(recommendation='needs_review', findings=[{
            'severity': 'critical', 'location': '<location>', 'reason': '<concrete reason>', 'resolved': False}])
        specific = 'Recommendation is approve, revise, or needs_review. Findings severity is critical, high, medium, or low; resolved must be a boolean. Never approve an unresolved critical or high finding. Use findings:[] when none.'
    else:
        template['status'] = 'needs_review'
        specific = 'Status is complete or needs_review.'
        if contract.role in _PLAN:
            template['plan'] = [{'step': 1, 'action': '<concrete action>', 'validation': '<how to verify it>'}]
            specific += ' Plan steps are consecutive integers from 1; a complete plan must have at least one concrete step.'
    return ('Final answer contract ' + VERSION + ': return ONLY one JSON object as your final answer (adapter result.summary), without Markdown fences or extra fields. '
            'Echo the binding fields exactly. Do not write this answer to a file or execute its content. '
            'This contract does not authorize tools, filesystem changes, checks, or commands. '
            'All conclusions, evidence and resolved flags are unverified model claims; they do not establish protected task acceptance. '
            + specific + ' Total UTF-8 JSON is at most 16384 bytes, including all binding fields and JSON punctuation. Summary <=8192 bytes; arrays <=64 entries; location <=1024 bytes; reason/action/validation <=2048 bytes. '
            'Use nonempty text with no control/format characters, including escaped newlines/tabs. Evidence may be empty when unavailable; explain uncertainty in summary. '
            'Replace placeholder strings with actual content. Exact object shape: ' + _canonical(template))
