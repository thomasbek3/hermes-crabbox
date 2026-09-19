"""Pure bounded codec for pinned Hermes chat_completions, not a listener or tool runner.

Only file-tool inference is qualified here. The caller owns endpoint authorization,
profile selection, a fresh one-use provider transport, cancellation, auth leases and
outer container cleanup. No response authorizes credential reuse. SSE is buffered
AFTER the CLI result is completely validated, not an incremental inference stream.
"""
from dataclasses import dataclass
import json
import re

from .inference_transport import (
    PinnedCLI, InferenceResult, TransportError, validate_transcript, validate_decision,
)

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
FILE_TOOLS = frozenset({'read_file', 'write_file', 'patch', 'search_files'})
_FIELDS = frozenset({'model', 'messages', 'tools', 'stream', 'stream_options',
                     'reasoning_effort', 'reasoning', 'n', 'tool_choice'})
_USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


class ProtocolError(ValueError):
    def __init__(self, code, status_code=400):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError('duplicate_json_key')
        value[key] = item
    return value


def _json(value, maximum):
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_json') from None
    if len(data) > maximum:
        raise ProtocolError('body_limit', 413)
    return data


def _decode(raw):
    if isinstance(raw, dict):
        raw = _json(raw, MAX_REQUEST_BYTES)
    if not isinstance(raw, bytes):
        raise ProtocolError('invalid_json')
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError('body_limit', 413)
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ProtocolError('invalid_json')))
    except ProtocolError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_json') from None


@dataclass(frozen=True)
class AdmittedRequest:
    """Canonical immutable bytes; transport_request returns a fresh object each time."""
    profile: PinnedCLI
    request_json: bytes
    stream: bool
    include_usage: bool
    allowed_tool_names: frozenset[str] = FILE_TOOLS

    @property
    def transport_request(self):
        return json.loads(self.request_json)


@dataclass(frozen=True)
class EncodedResponse:
    status_code: int
    content_type: str
    body: bytes
    metadata_json: bytes

    @property
    def metadata(self):
        return json.loads(self.metadata_json)


def admit_request(raw, profile: PinnedCLI, *, allowed_tool_names=FILE_TOOLS) -> AdmittedRequest:
    if type(profile) is not PinnedCLI:
        raise ProtocolError('invalid_profile')
    if (type(allowed_tool_names) is not frozenset or len(allowed_tool_names) > 32
            or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', name)
                   for name in allowed_tool_names)):
        raise ProtocolError('invalid_tool_capability')
    value = _decode(raw)
    if not isinstance(value, dict) or not {'model', 'messages'} <= set(value):
        raise ProtocolError('invalid_request')
    if set(value) - _FIELDS:
        raise ProtocolError('unsupported_parameter')
    if value['model'] != profile.native_model:
        raise ProtocolError('model_mismatch')
    for key in ('stream',):
        if key in value and type(value[key]) is not bool:
            raise ProtocolError('invalid_stream')
    stream = value.get('stream', False)
    include_usage = False
    if 'stream_options' in value:
        options = value['stream_options']
        if (not stream or not isinstance(options, dict) or set(options) != {'include_usage'}
                or type(options['include_usage']) is not bool):
            raise ProtocolError('unsupported_stream_options')
        include_usage = options['include_usage']
    if 'n' in value and (type(value['n']) is not int or value['n'] != 1):
        raise ProtocolError('unsupported_choice_count')
    if 'tool_choice' in value and value['tool_choice'] != 'auto':
        raise ProtocolError('unsupported_tool_choice')
    if 'reasoning_effort' in value and value['reasoning_effort'] != profile.effort:
        raise ProtocolError('effort_mismatch')
    if 'reasoning' in value:
        reasoning = value['reasoning']
        if (not isinstance(reasoning, dict) or set(reasoning) != {'enabled', 'effort'}
                or reasoning['enabled'] is not True or reasoning['effort'] != profile.effort):
            raise ProtocolError('effort_mismatch')
    request = {'messages': value['messages'], 'tools': value.get('tools', [])}
    try:
        data, tools, _ = validate_transcript(request, MAX_REQUEST_BYTES)
    except TransportError as exc:
        raise ProtocolError(exc.code) from None
    if set(tools) - allowed_tool_names:
        raise ProtocolError('unsupported_tool')
    return AdmittedRequest(profile, data, stream, include_usage, allowed_tool_names)


def _usage(result):
    raw = result.usage
    known = {}
    if isinstance(raw, dict) and result.usage_status.get('state') == 'reported':
        known = {key: value for key, value in raw.items() if key in _USAGE_KEYS
                 and type(value) is int and 0 <= value <= 10**12}
    if set(known) != set(_USAGE_KEYS):
        return None, {'state': 'partial' if known else 'unknown',
                      'reason': 'incomplete_token_counts' if known else (result.usage_status.get('reason')
                          if result.usage_status.get('reason') in ('missing_result', 'provider_did_not_report')
                          else 'provider_did_not_report'),
                      'reported_counts': known}
    # Claude/Anthropic input excludes both cache categories. Never invent missing cache zeros.
    prompt = known['input_tokens'] + known['cache_creation_input_tokens'] + known['cache_read_input_tokens']
    usage = {'prompt_tokens': prompt, 'completion_tokens': known['output_tokens'],
             'total_tokens': prompt + known['output_tokens'],
             'prompt_tokens_details': {'cached_tokens': known['cache_read_input_tokens']}}
    return usage, {'state': 'reported', 'reason': None, 'reported_counts': known,
                   'basis': 'worker_reported_input_plus_cache_categories_not_verified_billing'}


def _metadata(request, result):
    requested = {'model': request.profile.native_model, 'effort': request.profile.effort,
                 'cli_version': request.profile.version, 'binary_sha256': request.profile.sha256,
                 'trust': 'controller_pinned'}
    if result.requested_identity != requested:
        raise ProtocolError('result_identity_mismatch', 502)
    reported = result.reported_identity
    if (not isinstance(reported, dict) or reported.get('model') not in (None, request.profile.native_model)
            or reported.get('effort') not in (None, request.profile.effort)
            or reported.get('status') not in ('unknown', 'reported')
            or (reported.get('status') == 'reported') != (reported.get('model') is not None)):
        raise ProtocolError('result_identity_mismatch', 502)
    _, usage = _usage(result)
    return {'schema_version': 1, 'model_identity': {'requested': requested,
            'reported': {key: reported.get(key) for key in ('model', 'effort', 'status')}, 'verified': False},
            'usage_status': usage, 'provenance': 'worker_reported', 'billing_verified': False,
            'streaming': 'buffered_after_validated_cli_result',
            'cleanup': {'scope': 'process_group',
                        'process_group_stopped': result.cleanup.get('process_group_stopped') if isinstance(result.cleanup, dict) and type(result.cleanup.get('process_group_stopped')) is bool else None,
                        'container_cleanup_required': True,
                        'credential_reuse_authorized': False}}


def _error_response(code, status, metadata):
    body = _json({'error': {'message': code, 'type': 'inference_transport_error', 'code': code},
                  'x_cloudworkbench': metadata}, MAX_RESPONSE_BYTES)
    return EncodedResponse(status, 'application/json', body, _json(metadata, 8192))


def _encode_response(request: AdmittedRequest, result: InferenceResult, *, completion_id: str, created: int) -> EncodedResponse:
    if type(request) is not AdmittedRequest or type(result) is not InferenceResult:
        raise ProtocolError('invalid_result', 502)
    if (type(request.profile) is not PinnedCLI or type(request.stream) is not bool
            or type(request.include_usage) is not bool or type(request.allowed_tool_names) is not frozenset
            or not isinstance(request.request_json, bytes)):
        raise ProtocolError('invalid_admitted_request', 502)
    if result.error_code is not None and not isinstance(result.error_code, str):
        raise ProtocolError('invalid_result_error', 502)
    if (not isinstance(completion_id, str) or not re.fullmatch(r'chatcmpl-[A-Za-z0-9_-]{1,80}', completion_id)
            or type(created) is not int or not 0 <= created <= 2**53-1):
        raise ProtocolError('invalid_completion_identity', 502)
    if type(result.usage_status) is not dict:
        raise ProtocolError('invalid_usage_status', 502)
    metadata = _metadata(request, result)
    cleanup = result.cleanup
    if (not isinstance(cleanup, dict) or cleanup.get('scope') != 'process_group'
            or cleanup.get('process_group_stopped') is not True
            or cleanup.get('container_cleanup_required') is not True
            or cleanup.get('credential_reuse_authorized') is not False):
        return _error_response('outer_cleanup_required', 503, metadata)
    if result.status != 'ok' or result.error_code is not None:
        codes = {'provider_auth_rejected': (401, 'provider_auth_rejected'), 'rate_limited': (429, 'rate_limited'),
                 'cancelled': (409, 'request_cancelled'), 'wall_timeout': (504, 'inference_timeout'),
                 'process_group_cleanup_unconfirmed': (503, 'outer_cleanup_required')}
        status, code = codes.get(result.error_code, (502, 'inference_failed'))
        return _error_response(code, status, metadata)
    try:
        _, tools, used = validate_transcript(request.transport_request, MAX_REQUEST_BYTES)
        if set(tools) - request.allowed_tool_names:
            raise ProtocolError('unsupported_tool')
        decision = validate_decision(result.decision, tools, used)
    except TransportError as exc:
        raise ProtocolError(exc.code, 502) from None
    tool_calls = [{'id': call['id'], 'type': 'function', 'function': {'name': call['name'],
                   'arguments': _json(call['arguments'], 65536).decode()}} for call in decision['tool_calls']]
    finish = 'tool_calls' if tool_calls else 'stop'
    message = {'role': 'assistant', 'content': decision['text']}
    if tool_calls:
        message['tool_calls'] = tool_calls
    usage, _ = _usage(result)
    common = {'id': completion_id, 'created': created, 'model': request.profile.native_model}
    if not request.stream:
        value = {**common, 'object': 'chat.completion', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                 'x_cloudworkbench': metadata}
        if usage is not None:
            value['usage'] = usage
        return EncodedResponse(200, 'application/json', _json(value, MAX_RESPONSE_BYTES), _json(metadata, 8192))
    chunks = []
    def chunk(delta, finish_reason=None):
        chunks.append({**common, 'object': 'chat.completion.chunk', 'choices': [
            {'index': 0, 'delta': delta, 'finish_reason': finish_reason}]})
    chunk({'role': 'assistant'})
    chunks[0]['x_cloudworkbench'] = metadata
    if tool_calls:
        for index, call in enumerate(tool_calls):
            chunk({'tool_calls': [{'index': index, **call}]})
    elif decision['text']:
        chunk({'content': decision['text']})
    chunk({}, finish)
    if request.include_usage and usage is not None:
        chunks.append({**common, 'object': 'chat.completion.chunk', 'choices': [], 'usage': usage,
                       'x_cloudworkbench': {'usage_status': metadata['usage_status']}})
    body = b''.join(b'data: ' + _json(value, MAX_RESPONSE_BYTES) + b'\n\n' for value in chunks) + b'data: [DONE]\n\n'
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProtocolError('body_limit', 413)
    return EncodedResponse(200, 'text/event-stream', body, _json(metadata, 8192))


def encode_response(request: AdmittedRequest, result: InferenceResult, *, completion_id: str, created: int) -> EncodedResponse:
    """All malformed encoder inputs are upstream failures, never client mistakes."""
    try:
        return _encode_response(request, result, completion_id=completion_id, created=created)
    except ProtocolError as exc:
        raise ProtocolError(exc.code, 502) from None
