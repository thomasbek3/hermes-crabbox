"""Resolve pstack seats to configured Hermes profiles; qualification is external."""
from dataclasses import dataclass
import hashlib
import json
import re
from collections.abc import Mapping

ALIASES = frozenset({'auto', 'inherit-parent'})
PANELS = frozenset({'arena_runner', 'architect_runner', 'interrogate_reviewer'})
ROLES = frozenset({'coordinator', 'synthesis', 'feature', 'refactoring', 'bug_fix',
    'perf_issue', 'hillclimb', 'how_explorer', 'how_explainer', 'why_investigator',
    'why_synthesizer', 'reflect_tooling', 'reflect_judgment', 'reflect_divergent',
    'reflect_synthesizer', 'swarm_worker', 'judgment', 'prose', 'hardest',
    'arena_cross_judge', 'planning', 'plan_revision', 'plan_review',
    'code_review', 'acceptance_verification'}) | PANELS
DISPLAY_ROLES = {
    'bug-fix': 'bug_fix', 'perf-issue': 'perf_issue',
    'hardest tasks': 'hardest',
    'how explorer': 'how_explorer', 'how explainer': 'how_explainer',
    'why investigators': 'why_investigator', 'why synthesizer': 'why_synthesizer',
    'reflect tooling': 'reflect_tooling', 'reflect judgment': 'reflect_judgment',
    'reflect divergent': 'reflect_divergent', 'reflect synthesizer': 'reflect_synthesizer',
    'swarm workers': 'swarm_worker', 'arena runners': 'arena_runner',
    'architect runners': 'architect_runner', 'interrogate reviewers': 'interrogate_reviewer',
    'arena cross-judge pool': 'arena_cross_judge',
}
EFFORTS = frozenset({'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'})


def requested_policy():
    """Desired aliases only; never an activated or qualified backend configuration."""
    groups = {
        'grok-xhigh': {'feature', 'refactoring', 'bug_fix', 'perf_issue', 'hillclimb',
                      'how_explorer', 'why_investigator', 'swarm_worker'},
        'astra-high': {'plan_review', 'code_review'},
        'sol-max': {'acceptance_verification', 'reflect_tooling'},
        'fable-max': {'planning', 'plan_revision', 'coordinator', 'synthesis',
                      'how_explainer', 'why_synthesizer', 'reflect_judgment',
                      'reflect_divergent', 'reflect_synthesizer', 'judgment', 'prose', 'hardest'},
    }
    return {role: (profile,) for profile, roles in groups.items() for role in sorted(roles)}



def canonical_role(role):
    if not isinstance(role, str):
        raise RoutingError('invalid_role_route')
    role = DISPLAY_ROLES.get(role, role)
    if role not in ROLES:
        raise RoutingError('invalid_role_route')
    return role

_SAFE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}\Z')

class RoutingError(ValueError):
    def __init__(self, code, *, seat=None):
        super().__init__(code)
        self.receipt = seat.provenance() if seat is not None else None

@dataclass(frozen=True)
class BackendProfile:
    profile_id: str
    provider: str
    model: str
    transport: str
    effort: str
    family: str
    qualification_sha256: str
    supported_efforts: tuple[str, ...]

    def __post_init__(self):
        for value in (self.profile_id, self.provider, self.model, self.transport, self.family):
            if not isinstance(value, str) or not _SAFE.fullmatch(value):
                raise RoutingError('invalid_backend_profile')
        lineage = {'anthropic': 'anthropic', 'claude-code': 'anthropic',
                   'openai': 'openai', 'openai-codex': 'openai',
                   'xai': 'xai', 'xai-oauth': 'xai'}
        if self.family != self.family.lower() or self.provider != self.provider.lower():
            raise RoutingError('invalid_provider_lineage')
        if self.provider in lineage and self.family != lineage[self.provider]:
            raise RoutingError('invalid_provider_lineage')
        if self.profile_id in ALIASES:
            raise RoutingError('reserved_profile_id')
        if not isinstance(self.qualification_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', self.qualification_sha256):
            raise RoutingError('invalid_qualification_reference')
        if not isinstance(self.supported_efforts, tuple) or not self.supported_efforts or any(
                not isinstance(value, str) or value not in EFFORTS for value in self.supported_efforts):
            raise RoutingError('invalid_effort_capabilities')
        if not isinstance(self.effort, str) or self.effort not in self.supported_efforts:
            raise RoutingError('unsupported_effort')

@dataclass(frozen=True)
class Seat:
    role: str
    ordinal: int
    profile: BackendProfile
    inherited: bool
    routing_sha256: str
    diversity_fallback: bool = False

    def provenance(self):
        return {'runtime':'hermes','role':self.role,'seat':self.ordinal,
                'profile_id':self.profile.profile_id,'provider':self.profile.provider,
                'model':self.profile.model,'transport':self.profile.transport,
                'effort':self.profile.effort,'family':self.profile.family,
                'inherited':self.inherited,'routing_sha256':self.routing_sha256,
                'qualification_sha256':self.profile.qualification_sha256,
                'diversity_fallback':self.diversity_fallback,
                'provenance':'controller_configured',
                'qualification_reference_verified':False}

@dataclass(frozen=True)
class PlannedSeat:
    seat: Seat
    ready: bool
    blocked_reason: str | None


class RoleRouter:
    """Trusted config only; readiness is rechecked for every selected seat."""
    def __init__(self, routes: Mapping, profiles: Mapping[str, BackendProfile], *, ready):
        if not callable(ready) or not isinstance(routes, Mapping) or not isinstance(profiles, Mapping):
            raise RoutingError('invalid_routing_configuration')
        self._profiles = dict(profiles)
        if any(not isinstance(p, BackendProfile) or key != p.profile_id for key,p in self._profiles.items()):
            raise RoutingError('invalid_backend_profile')
        if len(self._profiles)>64 or not routes or len(routes)>len(ROLES):
            raise RoutingError('routing_capacity_exceeded')
        normalized={}
        for role, values in routes.items():
            role = canonical_role(role)
            if role in normalized:
                raise RoutingError('duplicate_role_route')
            if role not in ROLES or not isinstance(values,(list,tuple)) or not 1<=len(values)<=8:
                raise RoutingError('invalid_role_route')
            if role not in PANELS and role!='arena_cross_judge' and len(values)!=1:
                raise RoutingError('single_role_requires_one_seat')
            if any(not isinstance(v,str) or v not in ALIASES and v not in self._profiles for v in values):
                raise RoutingError('unresolved_backend_route')
            normalized[role]=tuple(values)
        self._routes=normalized
        self._ready=ready
        identity={'schema_version':1,'routes':normalized,'profiles':{k:p.__dict__ for k,p in self._profiles.items()}}
        self.sha256=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def _selected_seats(self, role: str, *, parent: BackendProfile):
        role = canonical_role(role)
        if not isinstance(parent, BackendProfile) or self._profiles.get(parent.profile_id) != parent:
            raise RoutingError('invalid_parent_profile')
        if role not in self._routes:
            raise RoutingError('role_not_configured')
        seats=[]
        for ordinal, profile_id in enumerate(self._routes[role],1):
            inherited=profile_id in ALIASES
            profile=parent if inherited else self._profiles[profile_id]
            seats.append(Seat(role,ordinal,profile,inherited,self.sha256))
        if role=='arena_cross_judge':
            candidates=[s for s in seats if s.profile.family!=parent.family]
            selected = (candidates or seats)[0]
            seats = [Seat(selected.role, selected.ordinal, selected.profile,
                          selected.inherited, selected.routing_sha256, not candidates)]
        return tuple(seats)

    def plan(self, role: str, *, parent: BackendProfile):
        """Return every configured seat; readiness is an observation, not a grant."""
        result=[]
        for seat in self._selected_seats(role,parent=parent):
            try:
                available=self._ready(seat.profile) is True
            except Exception:
                available=False
            result.append(PlannedSeat(seat,available,None if available else 'backend_not_ready'))
        return tuple(result)

    def seats(self, role: str, *, parent: BackendProfile):
        seats=self._selected_seats(role,parent=parent)
        for seat in seats:
            try:
                available=self._ready(seat.profile)
            except Exception:
                available=False
            if available is not True:
                raise RoutingError('backend_not_ready', seat=seat)
        return tuple(seats)
