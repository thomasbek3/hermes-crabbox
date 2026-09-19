"""Durable D9 dispatch state; runtime operations are injected trusted interfaces.

No Docker implementation, token access or HTTP service. Runtime adapters must bound
calls by the supplied deadline and must never start by name rather than bound ID.
"""
from dataclasses import asdict, dataclass
from contextlib import contextmanager
import hashlib
import json
import re
import time
import uuid

from .provider_leases import LeaseError, RequestLease, _HEX, _id
from .inference_budget import InferenceBudget, AttemptScope

_NONCE = re.compile(r'[0-9a-f]{64}\Z')
_STATES = frozenset({'admitted','create_intent','created','start_intent','running','collected','cleaning','cleaned','delivered','failed','quarantined'})


class DispatchError(LeaseError):
    pass


@dataclass(frozen=True)
class BudgetRefusal:
    reason: str
    retry_after_seconds: float | None


class BudgetAdmissionRefused(DispatchError):
    def __init__(self,refusal):
        super().__init__(refusal.reason)
        self.retry_after_seconds=refusal.retry_after_seconds


class _Denied(DispatchError):
    def __init__(self,code,grant_id,reservation_id,stamp,request_id=None):
        super().__init__(code)
        self.binding=(grant_id,reservation_id,stamp,request_id)


@dataclass(frozen=True)
class Resources:
    provider_id: str
    gateway_id: str
    internal_network_id: str
    external_network_id: str

    def __post_init__(self):
        values=(self.provider_id,self.gateway_id,self.internal_network_id,self.external_network_id)
        if any(not isinstance(value,str) or not _HEX.fullmatch(value) for value in values) or len(set(values))!=4:
            raise DispatchError('invalid_runtime_identity')


@dataclass(frozen=True)
class ObservedResource:
    runtime_id: str
    name: str
    labels: dict
    state: str


@dataclass(frozen=True)
class DispatchSpec:
    lease: RequestLease
    attempt_id: str
    generation: int
    request_nonce: str
    payload_digest: str
    profile_digest: str
    launch_nonce: str
    provider_name: str
    gateway_name: str
    internal_network_name: str
    external_network_name: str

    def labels(self, component):
        if component not in ('provider','gateway'):raise DispatchError('invalid_component')
        return {'io.cloudworkbench.provider-reservation':self.lease.reservation.reservation_id,
                'io.cloudworkbench.provider-epoch':str(self.lease.reservation.epoch),
                'io.cloudworkbench.provider-request':self.lease.request_id,
                'io.cloudworkbench.provider-launch':self.launch_nonce,
                'io.cloudworkbench.provider-component':component}

    def network_labels(self, role):
        if role not in ('internal','external'):raise DispatchError('invalid_network_role')
        return {**self.labels('gateway'),'io.cloudworkbench.provider-network':role}


@dataclass(frozen=True)
class OperationResolution:
    """Trusted runtime's definitive operation evidence, not an absence boolean."""
    request_id: str
    launch_nonce: str
    dispatch_version: int
    operation: str
    outcome: str
    resources: Resources | None
    evidence_sha256: str


class ProviderDispatch:
    def __init__(self, leases, runtime, *, response_bytes=256*1024, budget=None, budget_scope=None):
        if type(response_bytes) is not int or not 1<=response_bytes<=2*1024*1024:raise DispatchError('invalid_response_limit')
        for method in ('create','inspect','start','collect','resolve'):
            if not callable(getattr(runtime,method,None)):raise DispatchError('trusted_runtime_required')
        validate_leases=getattr(runtime,'validate_leases',None)
        if validate_leases is not None:
            if not callable(validate_leases):raise DispatchError('trusted_runtime_required')
            validate_leases(leases)
        self.leases,self.store,self.runtime=leases,leases.store,runtime
        self.response_bytes=response_bytes
        if (budget is None)!=(budget_scope is None) or (budget is not None and (type(budget) is not InferenceBudget or type(budget_scope) is not AttemptScope)):
            raise DispatchError("invalid_budget_binding")
        self.budget,self.budget_scope=budget,budget_scope

    @contextmanager
    def _tx(self):
        # Failed state transitions roll back, but an observed expiry/revocation
        # remains durable and cannot be revived by a later clock rollback.
        try:
            with self.store._tx() as db:yield db
        except _Denied as error:
            grant,reservation,stamp,request=error.binding
            with self.store._tx() as db:
                if request is None:
                    db.execute("UPDATE provider_execution_grants SET revoked_at=COALESCE(revoked_at,?),reason=COALESCE(reason,?) WHERE id=? AND reservation_id=?",(stamp,error.code,grant,reservation))
                    db.execute("UPDATE provider_request_leases SET state='quarantined',reason=? WHERE grant_id=? AND reservation_id=? AND state='held'",(error.code,grant,reservation))
                else:
                    db.execute("UPDATE provider_request_leases SET state='quarantined',reason=? WHERE id=? AND grant_id=? AND reservation_id=? AND state='held'",(error.code,request,grant,reservation))
            raise

    def _check_grant(self,db,grant_id,reservation_id):
        stamp=self.leases._now()
        error=self.leases._grant_error(db,grant_id,reservation_id,stamp)
        if error in ('grant_expired','execution_not_authorized'):
            raise _Denied(error,grant_id,reservation_id,stamp)
        if error:raise DispatchError(error)

    def _row(self, db, request_id):
        row=db.execute('SELECT * FROM provider_dispatch WHERE request_id=?',(_id(request_id),)).fetchone()
        if not row or row['state'] not in _STATES:raise DispatchError('dispatch_not_found')
        return dict(row)

    def read(self, request_id):
        with self.store._connect() as db:
            row=self._row(db,request_id)
        return {key:value for key,value in row.items() if key!='response'}

    def _lease(self, db, row):
        account=db.execute('SELECT account_id FROM provider_reservations WHERE id=?',(row['reservation_id'],)).fetchone()
        if not account:raise DispatchError('stale_reservation')
        # Snapshot reconstruction stays in this transaction, never changes owner authority.
        owner=db.execute('''SELECT r.*,a.persistent_owner_id FROM provider_reservations r
            JOIN provider_accounts a ON a.account_id=r.account_id WHERE r.id=?''',(row['reservation_id'],)).fetchone()
        from .provider_leases import Reservation
        reservation=Reservation(owner['account_id'],owner['persistent_owner_id'],owner['id'],owner['epoch'],owner['controller_instance_id'])
        request=db.execute('SELECT * FROM provider_request_leases WHERE id=?',(row['request_id'],)).fetchone()
        return RequestLease(reservation,request['id'],request['grant_id'],request['purpose'],request['expires_at'])

    def _spec(self, db, row):
        return DispatchSpec(self._lease(db,row),row['attempt_id'],row['generation'],row['request_nonce'],row['payload_digest'],row['profile_digest'],row['launch_nonce'],row['provider_name'],row['gateway_name'],row['internal_network_name'],row['external_network_name'])

    def spec(self, request_id):
        with self._tx() as db:return self._spec(db,self._row(db,request_id))

    def _cas(self, db, row, state, **fields):
        if state not in _STATES:raise DispatchError('invalid_state')
        allowed={'provider_id','gateway_id','internal_network_id','external_network_id','operation','operation_receipt','failure_code','uncertain','cancel_requested','response','response_digest'}
        if set(fields)-allowed:raise DispatchError('invalid_transition_fields')
        changes={'state':state,'version':row['version']+1,'updated_at':self.leases._now(),**fields}
        count=db.execute('UPDATE provider_dispatch SET '+','.join(key+'=?' for key in changes)+' WHERE request_id=? AND version=? AND state=?',(*changes.values(),row['request_id'],row['version'],row['state'])).rowcount
        if count!=1:raise DispatchError('dispatch_cas_conflict')
        return {**row,**changes}

    def _authorize(self, db, row):
        spec=self._spec(db,row)
        self.leases._reservation(db,spec.lease.reservation,held=True)
        if row['cancel_requested']:raise DispatchError('dispatch_cancelled')
        self._check_grant(db,spec.lease.grant_id,row['reservation_id'])
        request=self.leases._request(db,spec.lease)
        if request['state']!='held':raise DispatchError('request_requires_reconciliation')
        if self.leases._now()>=spec.lease.expires_at:raise _Denied('request_expired',spec.lease.grant_id,row['reservation_id'],self.leases._now(),row['request_id'])
        return spec

    def _charge_budget(self,db,grant,nonce,payload_digest,profile_digest,*,replay):
        if self.budget is None:return None
        scope=self.budget_scope;root=scope.root
        if (scope.attempt_id,scope.generation)!=(grant['attempt_id'],grant['generation']):
            raise DispatchError('budget_grant_mismatch')
        root_current=db.execute('SELECT latest_generation FROM inference_budget_attempts WHERE attempt_id=?',(root.root_attempt_id,)).fetchone()
        if not root_current:raise DispatchError('budget_root_authorization_failed')
        for attempt_id,generation in ((scope.attempt_id,scope.generation),(root.root_attempt_id,root_current['latest_generation'])):
            row=db.execute('SELECT a.session_id,a.turn_id,s.owner_id,s.project_id FROM attempts a JOIN sessions s ON s.id=a.session_id WHERE a.id=?',(attempt_id,)).fetchone()
            if (not row or tuple(row)!=(root.session_id,root.turn_id,root.owner_id,root.project_id)
                    or not self.leases._attempt_valid(db,attempt_id,generation)):
                raise DispatchError('budget_root_authorization_failed')
        outcome=self.budget.admit(db,scope,nonce=nonce,payload_digest=payload_digest,profile_digest=profile_digest)
        if outcome.decision=='refused':return BudgetRefusal(outcome.reason,outcome.retry_after_seconds)
        if (outcome.decision=='replay')!=replay:raise DispatchError('budget_dispatch_inconsistent')
        return outcome

    def admit(self,reservation,grant_id,*,request_nonce,payload,profile_digest,remaining_seconds=160):
        result=self._admit(reservation,grant_id,request_nonce=request_nonce,payload=payload,
                           profile_digest=profile_digest,remaining_seconds=remaining_seconds)
        # Ordinary budget refusal has already committed its clock observation.
        if type(result) is BudgetRefusal:raise BudgetAdmissionRefused(result)
        return result

    def _admit(self,reservation,grant_id,*,request_nonce,payload,profile_digest,remaining_seconds=160):
        if not isinstance(request_nonce,str) or not _NONCE.fullmatch(request_nonce):raise DispatchError('invalid_request_nonce')
        if not isinstance(profile_digest,str) or not _HEX.fullmatch(profile_digest):raise DispatchError('invalid_profile_digest')
        if type(payload) is not bytes or len(payload)>256*1024:raise DispatchError('invalid_payload')
        if type(remaining_seconds) not in (int,float) or not 160<=remaining_seconds<=14400:raise DispatchError('cleanup_reserve_unavailable')
        payload_digest=hashlib.sha256(payload).hexdigest()
        with self.leases.account_lock(reservation.account_id):
            with self._tx() as db:
                self.leases._reservation(db,reservation,held=True)
                grant=db.execute('SELECT * FROM provider_execution_grants WHERE id=? AND reservation_id=?',(_id(grant_id),reservation.reservation_id)).fetchone()
                if not grant:raise DispatchError('grant_not_found')
                self._check_grant(db,grant_id,reservation.reservation_id)
                existing=db.execute('SELECT * FROM provider_dispatch WHERE attempt_id=? AND generation=? AND request_nonce=?',(grant['attempt_id'],grant['generation'],request_nonce)).fetchone()
                if existing:
                    if (existing['payload_digest']!=payload_digest or existing['profile_digest']!=profile_digest or existing['reservation_id']!=reservation.reservation_id):raise DispatchError('dispatch_idempotency_conflict')
                    outcome=self._charge_budget(db,grant,request_nonce,payload_digest,profile_digest,replay=True)
                    if type(outcome) is BudgetRefusal:return outcome
                    return self._spec(db,dict(existing))
                stamp=self.leases._now()
                if grant['expires_at']-stamp<160:raise DispatchError('cleanup_reserve_unavailable')
                if db.execute("SELECT 1 FROM provider_request_leases WHERE reservation_id=? AND state IN ('held','cleaning','quarantined')",(reservation.reservation_id,)).fetchone():raise DispatchError('request_lease_busy')
                frozen=db.execute('SELECT profile_digest FROM provider_dispatch WHERE attempt_id=? AND generation=? LIMIT 1',(grant['attempt_id'],grant['generation'])).fetchone()
                if frozen and frozen['profile_digest']!=profile_digest:raise DispatchError('attempt_profile_conflict')
                if db.execute('SELECT COUNT(*) FROM provider_dispatch WHERE attempt_id=?',(grant['attempt_id'],)).fetchone()[0]>=32:raise DispatchError('attempt_inference_cap')
                outcome=self._charge_budget(db,grant,request_nonce,payload_digest,profile_digest,replay=False)
                if type(outcome) is BudgetRefusal:return outcome
                if outcome is not None:remaining_seconds=min(remaining_seconds,outcome.remaining_seconds)
                identity=str(uuid.uuid4());launch=uuid.uuid4().hex;root_deadline=min(stamp+remaining_seconds,grant['expires_at']);expiry=min(stamp+120,root_deadline-40)
                db.execute("INSERT INTO provider_request_leases(id,reservation_id,grant_id,purpose,state,expires_at,created_at) VALUES(?,?,?,'inference','held',?,?)",(identity,reservation.reservation_id,grant_id,expiry,stamp))
                prefix='cwb2-e'+str(reservation.epoch)+'-'+launch
                db.execute('''INSERT INTO provider_dispatch(request_id,reservation_id,attempt_id,generation,request_nonce,payload_digest,profile_digest,launch_nonce,provider_name,gateway_name,internal_network_name,external_network_name,state,version,created_at,updated_at,deadline_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'admitted',1,?,?,?)''',(identity,reservation.reservation_id,grant['attempt_id'],grant['generation'],request_nonce,payload_digest,profile_digest,launch,prefix+'-provider',prefix+'-gateway',prefix+'-internal',prefix+'-external',stamp,stamp,root_deadline))
                return self._spec(db,self._row(db,identity))

    def _verify_resources(self,spec,resources,*,created,deadline,settled=False):
        if type(resources) is not Resources:raise DispatchError('invalid_runtime_identity')
        observations=self.runtime.inspect(spec,resources,deadline=deadline)
        if type(observations) is not tuple or len(observations)!=4:raise DispatchError('runtime_identity_unconfirmed')
        expected=(
            (resources.provider_id,spec.provider_name,spec.labels('provider'),('created',) if created else (('created','running','exited') if settled else ('running','exited'))),
            (resources.gateway_id,spec.gateway_name,spec.labels('gateway'),('created',) if created else (('created','running','exited') if settled else ('running','exited'))),
            (resources.internal_network_id,spec.internal_network_name,spec.network_labels('internal'),('created',)),
            (resources.external_network_id,spec.external_network_name,spec.network_labels('external'),('created',)))
        for (expected_id,name,labels,states),observation in zip(expected,observations):
            if (type(observation) is not ObservedResource or observation.runtime_id!=expected_id or observation.name!=name
                    or observation.labels!=labels or observation.state not in states):raise DispatchError('runtime_identity_unconfirmed')
        if time.monotonic()>deadline:raise DispatchError('runtime_operation_timeout')

    def _quarantine(self,request_id,code,*,uncertain):
        with self._tx() as db:
            row=self._row(db,request_id)
            self._cas(db,row,'quarantined',failure_code=code,uncertain=int(uncertain))
            db.execute("UPDATE provider_request_leases SET state='quarantined',reason=? WHERE id=? AND state!='released'",(code,request_id))

    def launch(self,request_id,payload):
        spec=self.spec(request_id)
        if type(payload) is not bytes or hashlib.sha256(payload).hexdigest()!=spec.payload_digest:raise DispatchError('payload_binding_mismatch')
        deadline=time.monotonic()+max(0,min(120,spec.lease.expires_at-self.leases._now()))
        with self.leases.account_lock(spec.lease.reservation.account_id):
            with self._tx() as db:
                row=self._row(db,request_id)
                if row['state']!='admitted':raise DispatchError('dispatch_already_launched')
                self._authorize(db,row)
                row=self._cas(db,row,'create_intent',operation='create')
            try:
                resources=self.runtime.create(spec,payload,deadline=deadline)
                self._verify_resources(spec,resources,created=True,deadline=deadline)
            except Exception:
                self._quarantine(request_id,'create_outcome_unknown',uncertain=True)
                raise DispatchError('create_outcome_unknown') from None
            with self._tx() as db:
                row=self._row(db,request_id)
                if row['state']!='create_intent':raise DispatchError('dispatch_cas_conflict')
                row=self._cas(db,row,'created',**asdict(resources),operation=None)
            try:
                with self._tx() as db:
                    row=self._row(db,request_id);self._authorize(db,row)
                    self._cas(db,row,'start_intent',operation='start')
            except LeaseError:
                self._quarantine(request_id,'dispatch_cancelled',uncertain=False)
                raise DispatchError('dispatch_cancelled') from None
            try:
                self.runtime.start(spec,resources,deadline=deadline)
                self._verify_resources(spec,resources,created=False,deadline=deadline)
            except Exception:
                self._quarantine(request_id,'start_outcome_unknown',uncertain=True)
                raise DispatchError('start_outcome_unknown') from None
            with self._tx() as db:
                row=self._row(db,request_id)
                if row['state']!='start_intent':raise DispatchError('dispatch_cas_conflict')
                self._cas(db,row,'running',operation=None)
            try:self.leases.authorize_request(spec.lease)
            except LeaseError:
                self._quarantine(request_id,'dispatch_cancelled',uncertain=False)
                raise DispatchError('dispatch_cancelled') from None
        return resources

    def collect(self,request_id):
        spec=self.spec(request_id)
        with self.leases.account_lock(spec.lease.reservation.account_id):
            with self._tx() as db:
                row=self._row(db,request_id);self._authorize(db,row)
                if row['state']!='running':raise DispatchError('invalid_collect_state')
                resources=Resources(*(row[key] for key in ('provider_id','gateway_id','internal_network_id','external_network_id')))
            deadline=time.monotonic()+5
            try:
                response=self.runtime.collect(spec,resources,limit=self.response_bytes,deadline=deadline)
                if type(response) is not bytes or len(response)>self.response_bytes or time.monotonic()>deadline:raise DispatchError('collection_failed')
            except Exception:
                self._quarantine(request_id,'collection_failed',uncertain=False)
                raise DispatchError('collection_failed') from None
            with self._tx() as db:
                row=self._row(db,request_id)
                try:self._authorize(db,row)
                except LeaseError:
                    self._cas(db,row,'quarantined',failure_code='dispatch_cancelled')
                    return False
                self._cas(db,row,'collected',response=response,response_digest=hashlib.sha256(response).hexdigest())
        return True

    def reconcile(self,request_id):
        spec=self.spec(request_id)
        with self.leases.account_lock(spec.lease.reservation.account_id):
            with self._tx() as db:
                row=self._row(db,request_id)
                if not row['uncertain'] and row['state'] not in ('create_intent','start_intent'):raise DispatchError('reconciliation_not_required')
                frozen=self._cas(db,row,'quarantined',uncertain=1)
            deadline=time.monotonic()+20
            try:resolution=self.runtime.resolve(spec,dict(frozen),deadline=deadline)
            except Exception:raise DispatchError('operation_still_unknown') from None
            if (time.monotonic()>deadline or type(resolution) is not OperationResolution or resolution.request_id!=request_id or resolution.launch_nonce!=spec.launch_nonce
                    or resolution.dispatch_version!=frozen['version'] or resolution.operation!=frozen['operation']
                    or resolution.outcome not in ('completed','definitive_no_effect','settled_cleaned')
                    or not isinstance(resolution.evidence_sha256,str) or not _HEX.fullmatch(resolution.evidence_sha256)):
                raise DispatchError('operation_still_unknown')
            resources=resolution.resources
            if frozen['operation'] in ('start','cleanup'):
                identities=tuple(frozen[key] for key in ('provider_id','gateway_id','internal_network_id','external_network_id'))
                if all(value is None for value in identities) and frozen['operation']=='cleanup':
                    if resources is not None:raise DispatchError('operation_still_unknown')
                else:
                    try:bound=Resources(*identities)
                    except DispatchError:raise DispatchError('operation_still_unknown') from None
                    if resources!=bound:raise DispatchError('operation_still_unknown')
            elif resolution.outcome=='completed' and type(resources) is not Resources:raise DispatchError('operation_still_unknown')
            elif resolution.outcome in ('definitive_no_effect','settled_cleaned') and resources is not None:raise DispatchError('operation_still_unknown')
            if resources is not None and not (resolution.outcome=='settled_cleaned' or (frozen['operation']=='cleanup' and resolution.outcome=='completed')):self._verify_resources(spec,resources,created=False,deadline=time.monotonic()+5,settled=True)
            with self._tx() as db:
                row=self._row(db,request_id)
                if row['version']!=frozen['version']:raise DispatchError('dispatch_cas_conflict')
                attempt=db.execute('SELECT session_id FROM attempts WHERE id=?',(row['attempt_id'],)).fetchone()
                if attempt is None:raise DispatchError('dispatch_not_found')
                self.store._event(db,attempt['session_id'],row['attempt_id'],
                                  'provider_operation_resolved',asdict(resolution))
                self._cas(db,row,'quarantined',uncertain=0,operation=None,provider_id=resources.provider_id if resources else None,gateway_id=resources.gateway_id if resources else None,internal_network_id=resources.internal_network_id if resources else None,external_network_id=resources.external_network_id if resources else None,failure_code='reconciled_requires_cleanup',operation_receipt=json.dumps(asdict(resolution),sort_keys=True,separators=(',',':')))
        # Resolution never restarts or retries a request. Cleanup proof remains mandatory.

    def cleanup(self,request_id):
        spec=self.spec(request_id)
        with self.leases.account_lock(spec.lease.reservation.account_id):
            with self._tx() as db:
                row=self._row(db,request_id)
                released=row['state'] in ('cleaned','delivered')
                if not released and (row['uncertain'] or row['state'] in ('create_intent','start_intent')):raise DispatchError('operation_still_unknown')
            if not released:
                try:self.leases.cleanup_request(spec.lease)
                except LeaseError:
                    row=self.read(request_id)
                    if row['uncertain'] and row['operation']=='cleanup':
                        raise DispatchError('cleanup_outcome_unknown') from None
                    raise
            after_cleanup=getattr(self.runtime,'after_request_cleanup',None)
            if after_cleanup is not None:
                try:after_cleanup(spec)
                except Exception:raise DispatchError('post_cleanup_material_pending') from None

    def deliver(self,request_id):
        with self._tx() as db:
            row=self._row(db,request_id)
            if row['state'] not in ('cleaned','delivered'):raise DispatchError('response_not_cleaned')
            # Owner may already be released; attempt/cancel authority remains authoritative.
            if row['cancel_requested'] or not self.leases._attempt_valid(db,row['attempt_id'],row['generation']):raise DispatchError('response_revoked')
            request=db.execute('SELECT grant_id FROM provider_request_leases WHERE id=?',(request_id,)).fetchone()
            self._check_grant(db,request['grant_id'],row['reservation_id'])
            if row['response'] is None or hashlib.sha256(row['response']).hexdigest()!=row['response_digest']:raise DispatchError('response_unavailable')
            if row['state']=='cleaned':self._cas(db,row,'delivered')
            return bytes(row['response'])
