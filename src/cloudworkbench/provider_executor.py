"""Worker-only composition of relay identity, durable dispatch and provider codec.

The controller must separately supervise cancellation while Docker calls run and
atomically charge root policy during admission. This module grants neither duty.
"""
import hashlib
import json
import time

from .hermes_inference_protocol import ProtocolError
from .inference_relay import AttemptBinding, DispatchContext, DispatchResult, _json
from .inference_service import ServiceResponse
from .inference_transport import TransportError, _decode
from .provider_leases import LeaseError
from .provider_dispatch import BudgetAdmissionRefused
from .profile_binding import canonical_pinned_profile_digest
from .provider_protocol import admit_provider_request, encode_provider_result, provider_profile_digest


def _failure(code, status=503, *, cleaned=False):
    return DispatchResult(ServiceResponse(status, 'application/json',
        json.dumps({'error': {'code': code, 'message': code, 'type': 'provider_execution_error'}}).encode()), cleaned)


class ProviderExecutor:
    def __init__(self, dispatch, *, reservation, grant_id, binding, profile, remaining_seconds, clock=time.time):
        if type(binding) is not AttemptBinding or not callable(remaining_seconds) or not callable(clock):
            raise ValueError('invalid_executor_config')
        if binding.profile_digest!=provider_profile_digest(profile):
            raise ValueError('profile_digest_mismatch')
        self.dispatch, self.reservation, self.grant_id = dispatch, reservation, grant_id
        self.binding, self.profile = binding, profile
        self.remaining_seconds, self.clock = remaining_seconds, clock

    def authorize(self, binding):
        if binding != self.binding:
            return False
        try:
            with self.dispatch._tx() as db:
                self.dispatch._check_grant(db, self.grant_id, self.reservation.reservation_id)
                grant = db.execute('SELECT attempt_id,generation FROM provider_execution_grants WHERE id=?', (self.grant_id,)).fetchone()
                return grant is not None and grant['attempt_id'] == binding.attempt_id and grant['generation'] == binding.generation
        except Exception:
            return False

    def __call__(self, context, payload, cancel):
        state={'request_id':None}
        result=self._execute(context,payload,cancel,state)
        if not result.outer_cleanup_confirmed and state['request_id'] is not None:
            try:
                if self.dispatch.read(state['request_id'])['state'] in ('cleaned','delivered'):
                    # Retry post-release material cleanup; the state alone proves only physical cleanup.
                    self.dispatch.cleanup(state['request_id'])
                    return DispatchResult(result.response,True)
            except Exception:
                pass
        return result

    def _execute(self, context, payload, cancel, state):
        request_id = None
        cleaned = False
        cleanup_allowed = False
        try:
            if (type(context) is not DispatchContext or context.binding != self.binding
                    or hashlib.sha256(_json(payload)).hexdigest() != context.payload_digest
                    or not self.authorize(context.binding)):
                return _failure('execution_grant_unavailable',403,cleaned=True)
            if cancel.is_set():
                self.dispatch.leases.revoke_grant(self.grant_id)
                return _failure('request_cancelled',409,cleaned=True)
            admitted = admit_provider_request(payload, self.profile)
            spec = self.dispatch.admit(self.reservation, self.grant_id,
                request_nonce=context.request_nonce, payload=admitted.request_json,
                profile_digest=context.binding.profile_digest, remaining_seconds=self.remaining_seconds())
            request_id = spec.lease.request_id
            state['request_id']=request_id
            row = self.dispatch.read(request_id)
            if row['state'] == 'admitted':
                self.dispatch.launch(request_id, admitted.request_json)
                cleanup_allowed = True
                if cancel.is_set():
                    self.dispatch.leases.revoke_grant(self.grant_id)
                    raise LeaseError('request_cancelled')
                self.dispatch.collect(request_id)
            elif row['state'] not in ('cleaned','delivered'):
                raise LeaseError('dispatch_requires_reconciliation')
            self.dispatch.cleanup(request_id)
            cleaned = True
            if cancel.is_set():
                self.dispatch.leases.revoke_grant(self.grant_id)
                return _failure('request_cancelled',409,cleaned=True)
            raw = self.dispatch.deliver(request_id)
            envelope = _decode(raw)
            expected = {'version','status','error','result','container_cleanup_required','credential_reuse_authorized'}
            if (not isinstance(envelope,dict) or set(envelope)!=expected or type(envelope['version']) is not int
                    or envelope['version']!=1 or envelope['container_cleanup_required'] is not True
                    or envelope['credential_reuse_authorized'] is not False):
                return _failure('invalid_provider_envelope',502,cleaned=True)
            if envelope['status'] != 'ok' or envelope['error'] is not None:
                codes={'auth_missing':401,'auth_invalid':401,'auth_schema_unsupported':401,'auth_expired':401,
                       'provider_auth_rejected':401,'rate_limited':429,
                       'cancelled':409,'wall_timeout':504,'process_group_cleanup_unconfirmed':503,
                       'transport_cleanup_unconfirmed':503,'transport_error':502,
                       'native_provider_failed':502,'invalid_native_response':502,
                       'credential_exposure_detected':502}
                if envelope['error'] in ('auth_missing','auth_invalid','auth_schema_unsupported','auth_expired',
                                         'provider_auth_rejected','credential_exposure_detected'):
                    self.dispatch.leases.quarantine(self.reservation)
                code=envelope['error'] if isinstance(envelope['error'],str) and envelope['error'] in codes else 'provider_failed'
                return _failure(code,codes.get(code,502),cleaned=True)
            value=envelope['result']
            encoded=encode_provider_result(admitted,value,
                completion_id='chatcmpl-'+context.request_nonce,created=int(row['created_at']))
            return DispatchResult(ServiceResponse(encoded.status_code,encoded.content_type,encoded.body),True)
        except ProtocolError as exc:
            return _failure(exc.code,exc.status_code,cleaned=cleaned or request_id is None)
        except TransportError:
            return _failure('invalid_provider_json',502,cleaned=cleaned or request_id is None)
        except BudgetAdmissionRefused as exc:
            codes={'root_request_limit':429,'attempt_request_limit':429,'attempt_rate_limit':429,
                   'root_budget_exceeded':409,'attempt_budget_exceeded':409,
                   'insufficient_cleanup_reserve':503,'budget_clock_rollback':503}
            return _failure(exc.code if exc.code in codes else 'budget_refused',codes.get(exc.code,503),cleaned=True)
        except LeaseError as exc:
            codes={'request_cancelled':409,'request_lease_busy':409,'grant_revoked':403,
                   'cleanup_reserve_unavailable':503,'dispatch_idempotency_conflict':409,
                   'dispatch_requires_reconciliation':503,'dispatch_already_launched':409}
            code=exc.code if exc.code in codes else 'provider_dispatch_failed'
            return _failure(code,codes.get(code,503),cleaned=cleaned or request_id is None)
        except Exception:
            return _failure('provider_dispatch_failed',cleaned=cleaned or request_id is None)
        finally:
            if request_id is not None and not cleaned and cleanup_allowed:
                try:
                    self.dispatch.cleanup(request_id)
                except Exception:
                    # Unknown runtime effects remain quarantined; the supervisor reconciles.
                    pass
