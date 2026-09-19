"""Read-only root/ancestry fence for an already active provider operation.

An allowed result is only a budget/lifecycle predicate. The supervisor must also
check the immutable grant/account/profile binding and retain sticky cancellation.
The caller supplies the bounded SQLite connection and wall-clock observation.
"""
from dataclasses import dataclass
import json
import sqlite3

from .inference_budget import AttemptScope, BudgetError, BudgetPolicy, _attempt, _identity as _budget_identity, _policy, _root, _stamp
from .models import LIVE


@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    reason: str | None
    deadline_at: float | None = None
    policy_digest: str | None = None


# A single statement observes the whole chain under one SQLite snapshot. Fixed
# depth and selected text lengths bound returned data even for corrupted records.
_QUERY = """WITH RECURSIVE chain AS (
    SELECT attempt_id,root_id,parent_id,parent_generation,latest_generation,deadline_us,0 AS depth
    FROM inference_budget_attempts WHERE attempt_id=?
    UNION ALL
    SELECT p.attempt_id,p.root_id,p.parent_id,p.parent_generation,p.latest_generation,p.deadline_us,c.depth+1
    FROM inference_budget_attempts p JOIN chain c ON p.attempt_id=c.parent_id
    WHERE c.depth<64
)
SELECT substr(c.attempt_id,1,129) AS attempt_id,substr(c.root_id,1,129) AS root_id,
    substr(c.parent_id,1,129) AS parent_id,c.parent_generation,c.latest_generation,c.deadline_us,c.depth,
    substr(a.id,1,129) AS actual_id,substr(a.session_id,1,129) AS session_id,
    substr(a.turn_id,1,129) AS turn_id,a.generation AS actual_generation,
    substr(a.state,1,64) AS state,a.cancel_requested,
    substr(s.owner_id,1,129) AS owner_id,substr(s.project_id,1,129) AS project_id,
    substr(t.session_id,1,129) AS turn_session_id,
    substr(client.id,1,129) AS client_id,client.revoked_at IS NOT NULL AS client_revoked,
    substr(r.scope_json,1,4097) AS scope_json,substr(r.policy_json,1,4097) AS policy_json,
    substr(r.policy_digest,1,65) AS policy_digest,r.admitted_us,r.deadline_us AS root_deadline_us,r.high_water_us,
    g.generation AS registered_generation,original.generation AS original_root_generation,
    (SELECT count(*) FROM inference_budget_version) AS version_count,
    (SELECT version FROM inference_budget_version LIMIT 1) AS schema_version
FROM chain c
LEFT JOIN attempts a ON a.id=c.attempt_id
LEFT JOIN sessions s ON s.id=a.session_id
LEFT JOIN turns t ON t.id=a.turn_id
LEFT JOIN clients client ON client.id=s.owner_id
LEFT JOIN inference_budget_roots r ON r.root_id=?
LEFT JOIN inference_budget_generations g ON g.attempt_id=c.attempt_id AND g.generation=c.latest_generation
LEFT JOIN inference_budget_generations original ON original.attempt_id=r.root_id AND original.generation=?
ORDER BY c.depth LIMIT 65"""


def _integer(value, *, minimum=0, maximum=10**17):
    return type(value) is int and minimum <= value <= maximum


def _identity(value):
    try:
        _budget_identity(value)
        return True
    except BudgetError:
        return False


@dataclass(frozen=True)
class BudgetAuthority:
    scope: AttemptScope

    def __post_init__(self):
        _attempt(self.scope)

    def check(self, db, *, now):
        """No writes, transactions, clock callbacks, retry waits, or durable revocation.

        SQLite errors propagate to the supervisor's fixed fail-closed handler.
        Keep its progress handler/busy deadline installed across this call.
        """
        if not isinstance(db, sqlite3.Connection):
            raise BudgetError('budget_authority_connection_required')
        try:
            observed = _stamp(now)
            scope_json = _root(self.scope.root)
            _attempt(self.scope)
        except BudgetError:
            return AuthorityDecision(False, 'budget_authority_invalid_input')
        cursor = db.execute(_QUERY, (self.scope.attempt_id, self.scope.root.root_attempt_id,
                                     self.scope.root.root_generation))
        names = [field[0] for field in cursor.description]
        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        if not rows:
            return AuthorityDecision(False, 'budget_authority_missing')
        first = rows[0]
        if first['version_count'] != 1 or first['schema_version'] != 1:
            return AuthorityDecision(False, 'budget_authority_schema_invalid')
        if first['scope_json'] != scope_json:
            return AuthorityDecision(False, 'budget_authority_scope_mismatch')
        try:
            body = first['policy_json']
            if type(body) is not str or len(body) > 4096:
                raise ValueError()
            policy = BudgetPolicy(**json.loads(body))
            canonical, digest = _policy(policy)
            if body != canonical or digest != first['policy_digest']:
                raise ValueError()
        except (BudgetError, TypeError, ValueError, RecursionError):
            return AuthorityDecision(False, 'budget_authority_policy_invalid')
        admitted, root_deadline, high_water = (first[key] for key in ('admitted_us','root_deadline_us','high_water_us'))
        if (not all(_integer(value) for value in (admitted, root_deadline, high_water))
                or high_water < admitted or root_deadline != admitted + policy.root_wall_seconds * 1_000_000
                or first['original_root_generation'] != self.scope.root.root_generation):
            return AuthorityDecision(False, 'budget_authority_record_invalid')
        if observed < high_water:
            return AuthorityDecision(False, 'budget_clock_rollback')
        if observed >= root_deadline:
            return AuthorityDecision(False, 'root_budget_exceeded', root_deadline / 1_000_000, digest)
        expected_id, expected_generation = self.scope.attempt_id, self.scope.generation
        seen = set()
        minimum_deadline = root_deadline
        child_deadline = None
        root = self.scope.root
        for index, row in enumerate(rows):
            identity, generation, deadline = row['attempt_id'], row['latest_generation'], row['deadline_us']
            if (not _identity(identity) or identity in seen or row['depth'] != index
                    or row['root_id'] != root.root_attempt_id or identity != expected_id):
                return AuthorityDecision(False, 'budget_authority_ancestry_invalid')
            seen.add(identity)
            if (not _integer(generation, minimum=1, maximum=1_000_000)
                    or generation != expected_generation or row['registered_generation'] != generation
                    or row['actual_generation'] != generation):
                return AuthorityDecision(False, 'budget_authority_generation_changed')
            if (row['actual_id'] != identity or row['session_id'] != root.session_id
                    or row['turn_id'] != root.turn_id or row['turn_session_id'] != root.session_id
                    or row['owner_id'] != root.owner_id or row['client_id'] != root.owner_id
                    or row['project_id'] != root.project_id):
                return AuthorityDecision(False, 'budget_authority_scope_mismatch')
            if row['state'] not in LIVE or row['cancel_requested'] != 0 or row['client_revoked'] != 0:
                return AuthorityDecision(False, 'budget_authority_cancelled')
            if (not _integer(deadline) or not admitted < deadline <= root_deadline
                    or (child_deadline is not None and child_deadline > deadline)):
                return AuthorityDecision(False, 'budget_authority_record_invalid')
            if observed >= deadline:
                return AuthorityDecision(False, 'attempt_budget_exceeded', deadline / 1_000_000, digest)
            child_deadline = deadline
            minimum_deadline = min(minimum_deadline, deadline)
            if row['parent_id'] is None:
                if identity != root.root_attempt_id or row['parent_generation'] is not None or index != len(rows)-1:
                    return AuthorityDecision(False, 'budget_authority_ancestry_invalid')
                return AuthorityDecision(True, None, minimum_deadline / 1_000_000, digest)
            if identity == root.root_attempt_id or not _identity(row['parent_id']) or not _integer(row['parent_generation'], minimum=1, maximum=1_000_000):
                return AuthorityDecision(False, 'budget_authority_ancestry_invalid')
            expected_id, expected_generation = row['parent_id'], row['parent_generation']
        return AuthorityDecision(False, 'budget_authority_ancestry_invalid')
