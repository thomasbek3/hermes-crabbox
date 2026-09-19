"""One admission/response codec choice shared by dispatch and recovery."""
from dataclasses import dataclass, fields
import json

from .hermes_inference_protocol import (
    AdmittedRequest, EncodedResponse, FILE_TOOLS, ProtocolError,
    admit_request, encode_response,
)
from .inference_transport import InferenceResult, PinnedCLI
from .native_responses import NativeError, NativeProfile, ResponsesDecoder, validate_request
from .profile_binding import canonical_pinned_profile_digest


@dataclass(frozen=True)
class NativeAdmittedRequest:
    profile: NativeProfile
    request_json: bytes


def provider_profile_digest(profile):
    if type(profile) is PinnedCLI:
        return canonical_pinned_profile_digest(profile)
    if type(profile) is NativeProfile:
        return profile.digest
    raise ValueError('unsupported_provider_profile')


def admit_provider_request(payload, profile):
    if type(profile) is PinnedCLI:
        return admit_request(payload, profile)
    if type(profile) is not NativeProfile:
        raise ProtocolError('invalid_profile')
    try:
        body, schemas, _ = validate_request(profile, payload)
    except NativeError:
        raise ProtocolError('invalid_native_request') from None
    if set(schemas) - FILE_TOOLS:
        raise ProtocolError('unsupported_tool')
    return NativeAdmittedRequest(profile, body)


def encode_provider_result(admitted, value, *, completion_id, created):
    if type(admitted) is AdmittedRequest:
        if type(value) is not dict or set(value) != {field.name for field in fields(InferenceResult)}:
            raise ProtocolError('invalid_provider_result', 502)
        return encode_response(admitted, InferenceResult(**value),
                               completion_id=completion_id, created=created)
    if type(admitted) is not NativeAdmittedRequest:
        raise ProtocolError('invalid_admitted_request', 502)
    if (type(value) is not dict or set(value) != {'profile_digest', 'response', 'transport_stopped'}
            or value['profile_digest'] != admitted.profile.digest
            or value['transport_stopped'] is not True or type(value['response']) is not dict):
        raise ProtocolError('invalid_provider_result', 502)
    try:
        _, schemas, used = validate_request(admitted.profile, json.loads(admitted.request_json))
        decoder = ResponsesDecoder(admitted.profile, schemas, used)
        event = {'type': 'response.completed', 'response': value['response']}
        body = json.dumps(event, allow_nan=False, separators=(',', ':')).encode()
        decoder.feed(b'data: ' + body + b'\n\n')
        result = decoder.finish()
    except (NativeError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_native_response', 502) from None
    metadata = {'profile_digest': admitted.profile.digest,
                'usage_status': result.usage_status,
                'identity_trust': 'worker_reported', 'billing_verified': False}
    return EncodedResponse(200, 'text/event-stream', result.sse,
                           json.dumps(metadata, separators=(',', ':')).encode())
