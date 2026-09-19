"""Bounded native Responses inference; Hermes alone executes function tools.

Credentials are injected by an isolated provider entrypoint, never discovered.
Success is buffered: the controller must prove outer cleanup before delivery.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from .inference_transport import _schema, validate_decision

_ROUTES = {
    ('openai-codex', 'gpt-6-astra', 'high'): 'https://chatgpt.com/backend-api/codex/responses',
    ('openai-codex', 'gpt-5.6-sol', 'max'): 'https://chatgpt.com/backend-api/codex/responses',
    ('xai-oauth', 'grok-4.6', 'xhigh'): 'https://api.x.ai/v1/responses',
}
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_-]{0,127}\Z')
_ID = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')


class NativeError(ValueError):
    """Only fixed safe codes cross this boundary; never provider error text."""


@dataclass(frozen=True)
class NativeProfile:
    provider: str
    model: str
    effort: str

    def __post_init__(self):
        if not all(isinstance(v, str) for v in (self.provider, self.model, self.effort)) or (self.provider, self.model, self.effort) not in _ROUTES:
            raise NativeError('unsupported_profile')

    @property
    def endpoint(self):
        return _ROUTES[(self.provider, self.model, self.effort)]

    @property
    def digest(self):
        return hashlib.sha256(_encode({'transport': 'native-responses-v1', 'provider': self.provider,
            'model': self.model, 'effort': self.effort, 'endpoint': self.endpoint})).hexdigest()


@dataclass(frozen=True)
class NativeResult:
    status: str
    error: str | None = None
    response: dict | None = field(default=None, repr=False)
    sse: bytes = field(default=b'', repr=False)
    usage_status: str = 'unknown'
    usage_reason: str = 'missing_result'
    identity_trust: str = 'worker_reported'
    transport_stopped: bool = True
    container_cleanup_required: bool = True
    credential_reuse_authorized: bool = False


def _encode(value):
    try: return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError): raise NativeError('invalid_json') from None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result: raise NativeError('duplicate_json_key')
        result[key] = value
    return result


def _decode(raw):
    try: return json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(NativeError('invalid_json')))
    except (ValueError, UnicodeError, RecursionError): raise NativeError('invalid_json') from None


def _identifier(value):
    return isinstance(value, str) and _ID.fullmatch(value)


def validate_request(profile, request, limit=262144):
    body = _encode(request)
    if len(body) > limit: raise NativeError('request_limit')
    value = _decode(body)
    allowed = {'model','instructions','input','tools','reasoning','store','stream','include','tool_choice','parallel_tool_calls','max_output_tokens','prompt_cache_key'}
    if not isinstance(value, dict) or set(value) - allowed: raise NativeError('unsupported_request')
    reasoning = value.get('reasoning')
    if (value.get('model') != profile.model or not isinstance(reasoning, dict)
            or set(reasoning) - {'effort','summary'} or reasoning.get('effort') != profile.effort
            or ('summary' in reasoning and reasoning['summary'] != 'auto')):
        raise NativeError('profile_mismatch')
    if 'prompt_cache_key' in value and (not isinstance(value['prompt_cache_key'], str)
            or not re.fullmatch(r'pck_[0-9a-f]{24}', value['prompt_cache_key'])):
        raise NativeError('invalid_prompt_cache_key')
    if value.get('store') is not False or value.get('stream') is not True:
        raise NativeError('native_stream_required')
    if not isinstance(value.get('instructions', ''), str): raise NativeError('invalid_instructions')
    if value.get('include', []) != [] and value['include'] != ['reasoning.encrypted_content']:
        raise NativeError('unsupported_include')
    if value.get('tool_choice', 'auto') not in ('auto', 'none', 'required'): raise NativeError('unsupported_tool_choice')
    if 'parallel_tool_calls' in value and type(value['parallel_tool_calls']) is not bool: raise NativeError('invalid_parallel_tools')
    if 'max_output_tokens' in value and (type(value['max_output_tokens']) is not int or not 1 <= value['max_output_tokens'] <= 32768):
        raise NativeError('invalid_output_limit')
    tools = value.get('tools', [])
    if not isinstance(tools, list) or len(tools) > 64: raise NativeError('invalid_tools')
    schemas = {}
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool)-{'type','name','description','parameters','strict'}
                or tool.get('type') != 'function' or not isinstance(tool.get('name'), str)
                or not _NAME.fullmatch(tool['name']) or tool['name'] in schemas
                or not isinstance(tool.get('description',''),str)
                or type(tool.get('strict',False)) is not bool): raise NativeError('invalid_tools')
        try: _schema(tool.get('parameters'))
        except (ValueError, TypeError): raise NativeError('unsupported_tool_schema') from None
        schemas[tool['name']] = tool['parameters']
    items = value.get('input')
    if not isinstance(items,list) or not 1 <= len(items) <= 256: raise NativeError('invalid_input')
    calls = {}; answered = set()
    for item in items:
        if not isinstance(item,dict): raise NativeError('invalid_input')
        kind = item.get('type', 'message')
        if kind == 'function_call':
            _check_call(item, schemas)
            if item['call_id'] in calls: raise NativeError('duplicate_call_id')
            calls[item['call_id']] = item
        elif kind == 'function_call_output':
            if (set(item)-{'type','id','call_id','output','status'} or not _identifier(item.get('call_id')) or item.get('call_id') not in calls
                    or item['call_id'] in answered or not isinstance(item.get('output'),str)):
                raise NativeError('uncorrelated_tool_output')
            answered.add(item['call_id'])
        elif kind == 'reasoning': _check_reasoning(item)
        elif kind == 'message': _check_message(item, incoming=True)
        else: raise NativeError('unsupported_input_item')
    if set(calls) != answered: raise NativeError('missing_tool_output')
    return body, schemas, set(calls)


def _check_call(item, schemas):
    if (set(item)-{'type','id','call_id','name','arguments','status'} or not _identifier(item.get('call_id'))
            or not isinstance(item.get('name'),str) or item.get('name') not in schemas or not isinstance(item.get('arguments'),str)):
        raise NativeError('invalid_function_call')
    args = _decode(item['arguments'])
    try:
        validate_decision({'kind':'tool_calls','text':None,'tool_calls':[{'id':item['call_id'],'name':item['name'],'arguments':args}]}, schemas, set())
    except (ValueError, TypeError): raise NativeError('invalid_function_arguments') from None


def _check_reasoning(item):
    if set(item)-{'type','id','summary','encrypted_content','status'}: raise NativeError('unsupported_reasoning')
    if 'encrypted_content' in item and not isinstance(item['encrypted_content'],str): raise NativeError('invalid_reasoning')
    summary = item.get('summary',[])
    if not isinstance(summary,list) or any(not isinstance(p,dict) or set(p)!={'type','text'} or p['type']!='summary_text' or not isinstance(p['text'],str) for p in summary):
        raise NativeError('invalid_reasoning')


def _check_message(item, incoming=False):
    if set(item)-{'type','id','role','content','status','phase'} or item.get('role') not in (('user','assistant','system','developer') if incoming else ('assistant',)):
        raise NativeError('invalid_message')
    content = item.get('content')
    if incoming and isinstance(content,str): return
    if not isinstance(content,list): raise NativeError('invalid_message')
    for part in content:
        if not isinstance(part,dict): raise NativeError('invalid_content')
        if part.get('type') in ('input_text','output_text'):
            if set(part)-{'type','text','annotations','logprobs'} or not isinstance(part.get('text'),str): raise NativeError('invalid_content')
        elif item.get('role')=='assistant' and part.get('type')=='refusal':
            if set(part)-{'type','refusal'} or not isinstance(part.get('refusal'),str): raise NativeError('invalid_content')
        else: raise NativeError('unsupported_content')


class ResponsesDecoder:
    """Bounded SSE assembly; completed output may be absent if item.done exists."""
    def __init__(self, profile, schemas, used, *, limit=2097152):
        self.profile=profile;self.schemas=schemas;self.used=used;self.limit=limit
        self.total=0;self.buffer=b'';self.items={};self.terminal=None;self.frames=0

    def feed(self, chunk):
        if not isinstance(chunk,bytes): raise NativeError('invalid_stream')
        self.total+=len(chunk)
        if self.total>self.limit: raise NativeError('response_limit')
        self.buffer+=chunk
        while b'\n\n' in self.buffer or b'\r\n\r\n' in self.buffer:
            match=re.search(b'\r?\n\r?\n',self.buffer)
            if match is None: break
            frame=self.buffer[:match.start()];self.buffer=self.buffer[match.end():]
            data=b'\n'.join(line[5:].lstrip(b' ') for line in frame.splitlines() if line.startswith(b'data:'))
            if not data or data==b'[DONE]': continue
            self.frames+=1
            if self.frames>8192: raise NativeError('frame_limit')
            event=_decode(data)
            if not isinstance(event,dict) or not isinstance(event.get('type'),str): raise NativeError('invalid_event')
            kind=event['type']
            if self.terminal is not None: raise NativeError('event_after_terminal')
            if kind in ('error','response.failed','response.incomplete'):
                nested=event.get('response')
                if nested is not None and not isinstance(nested,dict): raise NativeError('invalid_error_event')
                error=event.get('error') or (nested or {}).get('error') or {}
                code=event.get('code') or (error.get('code') if isinstance(error,dict) else None)
                raise NativeError('provider_auth_rejected' if code in ('invalid_api_key','authentication_error') else 'rate_limited' if code in ('rate_limit_exceeded','rate_limit_error') else 'provider_error')
            if kind=='response.output_item.done':
                index=event.get('output_index');item=event.get('item')
                if type(index) is not int or not 0<=index<128 or index in self.items or not isinstance(item,dict): raise NativeError('invalid_output_item')
                self.items[index]=item
            elif kind=='response.completed':
                self.terminal=event.get('response')
                if not isinstance(self.terminal,dict): raise NativeError('invalid_terminal')
                # A complete terminal event ends the response; later keepalives are irrelevant.
                self.buffer=b''
                break
            elif not kind.startswith('response.'): raise NativeError('unsupported_event')

    def finish(self):
        if self.buffer.strip(): raise NativeError('incomplete_frame')
        value=self.terminal
        if value is None: raise NativeError('missing_terminal')
        if value.get('status')!='completed' or not _identifier(value.get('id')): raise NativeError('invalid_terminal')
        if value.get('model')!=self.profile.model: raise NativeError('reported_model_mismatch')
        output=[self.items[i] for i in sorted(self.items)] if self.items else value.get('output')
        if not isinstance(output,list) or not 1<=len(output)<=128: raise NativeError('missing_output')
        if self.items and sorted(self.items)!=list(range(len(self.items))): raise NativeError('output_index_gap')
        if self.items and isinstance(value.get('output'),list) and value['output'] and value['output']!=output: raise NativeError('output_disagreement')
        ids=set(self.used); item_ids=set()
        for item in output:
            if not isinstance(item,dict): raise NativeError('invalid_output')
            if 'id' in item:
                if not _identifier(item['id']) or item['id'] in item_ids: raise NativeError('duplicate_item_id')
                item_ids.add(item['id'])
            kind=item.get('type')
            if kind=='function_call':
                _check_call(item,self.schemas)
                if item['call_id'] in ids: raise NativeError('duplicate_call_id')
                ids.add(item['call_id'])
            elif kind=='message': _check_message(item)
            elif kind=='reasoning': _check_reasoning(item)
            else: raise NativeError('unsupported_output')
        if not any(x['type'] in ('message','function_call') for x in output): raise NativeError('missing_decision')
        usage=value.get('usage');known=isinstance(usage,dict) and all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens'))
        response={'id':value['id'],'object':'response','status':'completed','model':value['model'],'output':output,'usage':usage if known else None}
        events=[{'type':'response.output_item.done','output_index':i,'item':item} for i,item in enumerate(output)]
        events.append({'type':'response.completed','response':response})
        sse=b''.join(b'event: '+e['type'].encode()+b'\ndata: '+_encode(e)+b'\n\n' for e in events)
        if len(sse)>self.limit*2: raise NativeError('delivery_limit')
        return NativeResult('ok',response=response,sse=sse,usage_status='known' if known else 'unknown',usage_reason='reported' if known else 'provider_did_not_report')


class NativeResponses:
    def __init__(self, profile: NativeProfile, *, timeout=120, proxy=None, connection_factory=None):
        if not isinstance(profile,NativeProfile): raise NativeError('unsupported_profile')
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0<timeout<=120: raise NativeError('invalid_timeout')
        self.profile=profile;self.timeout=timeout;self.proxy=None
        if proxy is not None:
            try:
                parsed=urlsplit(proxy);address=ipaddress.ip_address(parsed.hostname)
                allowed=any(address in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
                if parsed.scheme!='http' or not allowed or not parsed.port or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment: raise ValueError()
                self.proxy=(str(address),parsed.port)
            except (TypeError,ValueError): raise NativeError('invalid_proxy') from None
        self.factory=connection_factory or http.client.HTTPSConnection

    def execute(self, request, *, credential, account_id=None, cancel=None):
        body,schemas,used=validate_request(self.profile,request)
        if not isinstance(credential,str) or not 1<=len(credential)<=16384 or any(ord(c)<33 or ord(c)>126 for c in credential): raise NativeError('invalid_credential')
        if account_id is not None and (self.profile.provider!='openai-codex' or not isinstance(account_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',account_id)): raise NativeError('invalid_account_id')
        cancel=cancel or threading.Event()
        if cancel.is_set(): return NativeResult('error','cancelled')
        parsed=urlsplit(self.profile.endpoint)
        conn=self.factory(*(self.proxy or (parsed.hostname,443)),timeout=self.timeout)
        if self.proxy: conn.set_tunnel(parsed.hostname,443)
        headers={'Authorization':'Bearer '+credential,'Content-Type':'application/json','Accept':'text/event-stream','Accept-Encoding':'identity','User-Agent':'HermesAgent/0.21.3'}
        if self.profile.provider=='openai-codex':
            headers['originator']='hermes-agent'
            if account_id: headers['ChatGPT-Account-ID']=account_id
        result=[];done=threading.Event();abort=threading.Event();sockets=[];deadline=time.monotonic()+self.timeout
        def perform():
            response=None
            try:
                conn.connect()
                if conn.sock is not None: sockets.append(conn.sock)
                if cancel.is_set() or abort.is_set(): raise NativeError('cancelled')
                if time.monotonic()>=deadline: raise NativeError('wall_timeout')
                conn.request('POST',parsed.path,body=body,headers=headers)
                response=conn.getresponse()
                if response.status!=200:
                    raise NativeError('provider_auth_rejected' if response.status in (401,403) else 'rate_limited' if response.status==429 else 'redirect_refused' if 300<=response.status<400 else 'provider_http_error')
                if response.getheader('Content-Type','').split(';')[0].strip().lower()!='text/event-stream': raise NativeError('unexpected_content_type')
                if response.getheader('Content-Encoding','identity').lower()!='identity': raise NativeError('compressed_response_refused')
                decoder=ResponsesDecoder(self.profile,schemas,used)
                while True:
                    if cancel.is_set(): raise NativeError('cancelled')
                    if time.monotonic()>=deadline: raise NativeError('wall_timeout')
                    chunk=response.read1(4096)
                    if not chunk: break
                    decoder.feed(chunk)
                    if decoder.terminal is not None: break
                result.append(decoder.finish())
            except NativeError as error: result.append(NativeResult('error',str(error)))
            except Exception: result.append(NativeResult('error','transport_error'))
            finally:
                try:
                    if response is not None: response.close()
                except Exception: pass
                try: conn.close()
                except Exception: pass
                done.set()
        thread=threading.Thread(target=perform,daemon=True,name='native-provider-request');thread.start()
        code=None
        while not done.wait(.02):
            if cancel.is_set(): code='cancelled';break
            if time.monotonic()>=deadline: code='wall_timeout';break
        if code:
            abort.set()
            for sock in [*sockets,conn.sock]:
                try:
                    if sock is not None: sock.shutdown(socket.SHUT_RDWR)
                except (OSError,AttributeError): pass
            try: conn.close()
            except Exception: pass
            thread.join(.2)
            return NativeResult('error',code,transport_stopped=not thread.is_alive())
        thread.join(.2)
        return result[0] if result else NativeResult('error','transport_error',transport_stopped=not thread.is_alive())
