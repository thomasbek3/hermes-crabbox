"""Single-request CLI inference primitive for a trusted provider container.

No credential loading, provider calls on import, tool execution, endpoint, or leases.
Process-group cleanup is not container/cgroup cleanup or permission to reuse auth.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit

_HEX = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_-]{0,79}\Z')
_CALL_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_EFFORTS = frozenset({'low','medium','high','xhigh','max'})
_SCHEMA_KEYS = frozenset({'type','description','properties','required','additionalProperties','items',
                          'default','anyOf','enum','minLength','maxLength','minimum','maximum','minItems','maxItems'})
_SYSTEM = ('You are the inference-only decision component of Hermes. Interpret the ordered JSON '
           'transcript and supplied tool definitions in stdin. Hermes owns and executes all tools. '
           'Return only the requested decision schema: final text or tool calls. Preserve tool '
           'result IDs and request only declared tools. Never claim you executed a tool yourself. '
           'Limit the full decision to 64 KiB UTF-8 and final text to 32 KiB UTF-8.')
_DECISION_SCHEMA = {
    'type':'object','additionalProperties':False,'required':['kind','text','tool_calls'],
    'properties':{'kind':{'enum':['final','tool_calls']},'text':{'type':['string','null'],'maxLength':32768},
        'tool_calls':{'type':'array','maxItems':8,'items':{'type':'object','additionalProperties':False,
            'required':['id','name','arguments'],'properties':{'id':{'type':'string'},
                'name':{'type':'string'},'arguments':{'type':'object'}}}}}}


class TransportError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _encode(value, maximum):
    try:
        data=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
    except (TypeError,ValueError,RecursionError,UnicodeError):
        raise TransportError('invalid_json') from None
    if len(data)>maximum:
        raise TransportError('input_limit')
    return data


def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise TransportError('duplicate_json_key')
        result[key]=value
    return result


def _decode(data):
    try:
        return json.loads(data,object_pairs_hook=_object,
            parse_constant=lambda _: (_ for _ in ()).throw(TransportError('invalid_json')))
    except TransportError:
        raise
    except (ValueError,UnicodeError,RecursionError):
        raise TransportError('invalid_json') from None


@dataclass(frozen=True)
class PinnedCLI:
    path: Path
    sha256: str
    version: str
    native_model: str
    effort: str

    def __post_init__(self):
        if not isinstance(self.path,Path) or not self.path.is_absolute():
            raise TransportError('invalid_binary_path')
        if not isinstance(self.sha256,str) or not _HEX.fullmatch(self.sha256):
            raise TransportError('invalid_binary_digest')
        if not isinstance(self.version,str) or not re.fullmatch(r'\d+\.\d+\.\d+',self.version):
            raise TransportError('invalid_cli_version')
        if not isinstance(self.native_model,str) or not re.fullmatch(r'claude-[a-z0-9][a-z0-9._-]{1,127}',self.native_model):
            raise TransportError('invalid_native_model')
        if not isinstance(self.effort,str) or self.effort not in _EFFORTS:
            raise TransportError('unsupported_effort')


@dataclass(frozen=True)
class TransportLimits:
    input_bytes: int = 256 * 1024
    stdout_bytes: int = 2 * 1024 * 1024
    stderr_bytes: int = 64 * 1024
    wall_seconds: float = 120
    terminate_seconds: float = 1
    cleanup_seconds: float = 3

    def __post_init__(self):
        for name in ('input_bytes','stdout_bytes','stderr_bytes'):
            if type(getattr(self,name)) is not int or not 1<=getattr(self,name)<=8*1024*1024:
                raise TransportError('invalid_limit')
        for name in ('wall_seconds','terminate_seconds','cleanup_seconds'):
            value=getattr(self,name)
            if type(value) not in (int,float) or not 0<value<=600 or not math.isfinite(value):
                raise TransportError('invalid_limit')


@dataclass(frozen=True)
class InferenceResult:
    status: str
    decision: dict | None
    error_code: str | None
    requested_identity: dict
    reported_identity: dict
    usage: dict | None
    usage_status: dict
    reported_cost_usd: float | None
    cost_basis: str
    provenance: str
    cleanup: dict


def _finite(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _schema(schema, depth=0):
    if depth>8 or not isinstance(schema,dict) or set(schema)-_SCHEMA_KEYS:
        raise TransportError('unsupported_tool_schema')
    if 'anyOf' in schema:
        branches=schema['anyOf']
        if set(schema)-{'anyOf','description','default'} or not isinstance(branches,list) or not 1<=len(branches)<=4:
            raise TransportError('unsupported_tool_schema')
        if 'description' in schema and (not isinstance(schema['description'],str) or len(schema['description'])>8192):
            raise TransportError('unsupported_tool_schema')
        for branch in branches:_schema(branch,depth+1)
        if 'default' in schema:
            try:_arguments(schema['default'],schema,depth)
            except TransportError:raise TransportError('unsupported_tool_schema') from None
        return
    kind=schema.get('type')
    if kind not in ('object','array','string','integer','number','boolean','null'):
        raise TransportError('unsupported_tool_schema')
    applicable={'object':{'properties','required','additionalProperties'},'array':{'items','minItems','maxItems'},
                'string':{'minLength','maxLength'},'integer':{'minimum','maximum'},'number':{'minimum','maximum'},
                'boolean':set(),'null':set()}[kind]
    if set(schema)-({'type','description','default','enum'}|applicable):
        raise TransportError('unsupported_tool_schema')
    if 'description' in schema and (not isinstance(schema['description'],str) or len(schema['description'])>8192):
        raise TransportError('unsupported_tool_schema')
    if 'enum' in schema and (not isinstance(schema['enum'],list) or not 1<=len(schema['enum'])<=64):
        raise TransportError('unsupported_tool_schema')
    if kind=='object':
        props=schema.get('properties',{})
        required=schema.get('required',[])
        if (not isinstance(props,dict) or len(props)>64 or not isinstance(required,list)
                or any(not isinstance(k,str) or len(k)>100 for k in props)
                or any(not isinstance(k,str) or k not in props for k in required)
                or len(required)!=len(set(required)) or schema.get('additionalProperties',False) is not False):
            raise TransportError('unsupported_tool_schema')
        for value in props.values():
            _schema(value,depth+1)
    elif kind=='array':
        _schema(schema.get('items'),depth+1)
    for key in ('minLength','maxLength','minItems','maxItems'):
        if key in schema and (type(schema[key]) is not int or not 0<=schema[key]<=65536):
            raise TransportError('unsupported_tool_schema')
    for low,high in (('minLength','maxLength'),('minItems','maxItems'),('minimum','maximum')):
        if low in schema and high in schema:
            if not _finite(schema[low]) or not _finite(schema[high]) or schema[low]>schema[high]:
                raise TransportError('unsupported_tool_schema')
    for key in ('minimum','maximum'):
        if key in schema and (not _finite(schema[key])):
            raise TransportError('unsupported_tool_schema')

    if 'enum' in schema:
        for item in schema['enum']:
            try:_arguments(item,{k:v for k,v in schema.items() if k not in ('enum','default')},depth)
            except TransportError:raise TransportError('unsupported_tool_schema') from None
    if 'default' in schema:
        try:_arguments(schema['default'],schema,depth)
        except TransportError:raise TransportError('unsupported_tool_schema') from None


def _arguments(value,schema,depth=0):
    if depth>8:
        raise TransportError('invalid_tool_arguments')
    if 'anyOf' in schema:
        for branch in schema['anyOf']:
            try:_arguments(value,branch,depth+1);return
            except TransportError:pass
        raise TransportError('invalid_tool_arguments')
    kind=schema['type']
    valid={'object':type(value) is dict,'array':type(value) is list,'string':type(value) is str,
           'integer':type(value) is int,'number':type(value) in (int,float),
           'boolean':type(value) is bool,'null':value is None}[kind]
    if not valid or ('enum' in schema and not any(type(value) is type(v) and value==v for v in schema['enum'])):
        raise TransportError('invalid_tool_arguments')
    if kind=='object':
        props=schema.get('properties',{})
        if set(value)-set(props) or set(schema.get('required',[]))-set(value):
            raise TransportError('invalid_tool_arguments')
        for key,item in value.items():
            _arguments(item,props[key],depth+1)
    elif kind=='array':
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',1024):
            raise TransportError('invalid_tool_arguments')
        for item in value:
            _arguments(item,schema['items'],depth+1)
    elif kind=='string':
        if not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',32768):
            raise TransportError('invalid_tool_arguments')
    elif kind in ('number','integer'):
        if not _finite(value) or value<schema.get('minimum',-math.inf) or value>schema.get('maximum',math.inf):
            raise TransportError('invalid_tool_arguments')


def _tools(raw):
    if not isinstance(raw,list) or len(raw)>32:
        raise TransportError('invalid_tools')
    result={}
    for tool in raw:
        if not isinstance(tool,dict) or set(tool)!={'type','function'} or tool['type']!='function':
            raise TransportError('invalid_tools')
        function=tool['function']
        if not isinstance(function,dict) or set(function)-{'name','description','parameters'} or not {'name','parameters'}<=set(function):
            raise TransportError('invalid_tools')
        name=function['name']
        if not isinstance(name,str) or not _NAME.fullmatch(name) or name in result:
            raise TransportError('invalid_tools')
        if 'description' in function and (not isinstance(function['description'],str) or len(function['description'])>8192):
            raise TransportError('invalid_tools')
        _schema(function['parameters'])
        if function['parameters'].get('type')!='object':
            raise TransportError('unsupported_tool_schema')
        result[name]=function['parameters']
    return result


def validate_transcript(request, limit):
    data=_encode(request,limit)
    request=_decode(data)
    if not isinstance(request,dict) or set(request)!={'messages','tools'}:
        raise TransportError('invalid_request')
    tools=_tools(request['tools']); messages=request['messages']
    if not isinstance(messages,list) or not 1<=len(messages)<=256:
        raise TransportError('invalid_transcript')
    used=set(); pending=set(); started=False
    for message in messages:
        if not isinstance(message,dict):
            raise TransportError('invalid_transcript')
        role=message.get('role'); content=message.get('content')
        if role not in ('system','user','assistant','tool') or (content is not None and not isinstance(content,str)):
            raise TransportError('invalid_transcript')
        if role=='system' and started:
            raise TransportError('invalid_transcript')
        if role!='system':started=True
        if pending and role!='tool':
            raise TransportError('missing_tool_result')
        if role=='tool':
            if set(message)!={'role','content','tool_call_id'} or not isinstance(message['tool_call_id'],str) or message['tool_call_id'] not in pending or not isinstance(content,str):
                raise TransportError('invalid_tool_result')
            pending.remove(message['tool_call_id'])
        elif role=='assistant' and 'tool_calls' in message:
            if set(message)!={'role','content','tool_calls'} or not isinstance(message['tool_calls'],list) or not 1<=len(message['tool_calls'])<=8:
                raise TransportError('invalid_tool_calls')
            for call in message['tool_calls']:
                if not isinstance(call,dict) or set(call)!={'id','type','function'} or call['type']!='function':
                    raise TransportError('invalid_tool_calls')
                identity=call['id']; function=call['function']
                if not isinstance(identity,str) or not _CALL_ID.fullmatch(identity) or identity in used:
                    raise TransportError('invalid_tool_calls')
                if not isinstance(function,dict) or set(function)!={'name','arguments'} or not isinstance(function['name'],str) or function['name'] not in tools or not isinstance(function['arguments'],str):
                    raise TransportError('invalid_tool_calls')
                _arguments(_decode(function['arguments']),tools[function['name']])
                used.add(identity);pending.add(identity)
        elif set(message)!={'role','content'} or not isinstance(content,str):
            raise TransportError('invalid_transcript')
    if pending:
        raise TransportError('missing_tool_result')
    return data,tools,used


def validate_decision(value,tools,used):
    try:_encode(value,65536)
    except TransportError as exc:
        raise TransportError('decision_limit' if exc.code=='input_limit' else exc.code) from None
    if not isinstance(value,dict) or set(value)!={'kind','text','tool_calls'}:
        raise TransportError('invalid_decision')
    if value['kind']=='final':
        if not isinstance(value['text'],str) or len(value['text'].encode())>32768 or value['tool_calls']!=[]:
            raise TransportError('invalid_decision')
    elif value['kind']=='tool_calls':
        calls=value['tool_calls']
        if value['text'] is not None or not isinstance(calls,list) or not 1<=len(calls)<=8:
            raise TransportError('invalid_decision')
        seen=set(used)
        for call in calls:
            if not isinstance(call,dict) or set(call)!={'id','name','arguments'}:
                raise TransportError('invalid_decision')
            identity=call['id']; name=call['name']
            if not isinstance(identity,str) or not _CALL_ID.fullmatch(identity) or identity in seen or not isinstance(name,str) or name not in tools:
                raise TransportError('invalid_decision')
            _arguments(call['arguments'],tools[name]);seen.add(identity)
    else:
        raise TransportError('invalid_decision')
    return value


def _private_dir(path):
    if not isinstance(path,Path) or not path.is_absolute() or path!=path.resolve():
        raise TransportError('unsafe_private_directory')
    try: info=path.stat()
    except OSError:raise TransportError('unsafe_private_directory') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o777!=0o700:
        raise TransportError('unsafe_private_directory')


def child_environment(home,scratch,proxy=None):
    _private_dir(home);_private_dir(scratch)
    env={'HOME':str(home),'CLAUDE_CONFIG_DIR':str(home/'.claude'),'TMPDIR':str(scratch),
         'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8','TERM':'dumb','NO_COLOR':'1'}
    if proxy is not None:
        if not isinstance(proxy,str):raise TransportError('invalid_proxy')
        try:
            p=urlsplit(proxy); address=ipaddress.ip_address(p.hostname)
            if p.scheme!='http' or p.username or p.password or p.path or p.query or p.fragment or not p.port or not any(address in net for net in (ipaddress.ip_network('10.0.0.0/8'),ipaddress.ip_network('172.16.0.0/12'),ipaddress.ip_network('192.168.0.0/16'),ipaddress.ip_network('fc00::/7'))):
                raise ValueError
        except (ValueError,TypeError):raise TransportError('invalid_proxy') from None
        env.update({'HTTPS_PROXY':proxy,'HTTP_PROXY':proxy,'NO_PROXY':''})
    return env


def command(profile):
    return [str(profile.path),'--print','--input-format','text','--output-format','json',
        '--safe-mode','--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
        '--tools','','--disallowedTools','*','--no-session-persistence','--no-chrome',
        '--permission-prompts','none','--model',profile.native_model,'--effort',profile.effort,
        '--max-turns','1','--system-prompt',_SYSTEM,'--json-schema',json.dumps(_DECISION_SCHEMA,separators=(',',':'))]


def _verify_binary(profile):
    try:
        fd=os.open(profile.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not info.st_mode&0o111 or info.st_mode&0o022 or not 0<info.st_size<=512*1024*1024:
                raise TransportError('unsafe_binary')
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=profile.sha256:
            raise TransportError('binary_digest_mismatch')
    except OSError:raise TransportError('binary_unavailable') from None


def _group_exists(pid):
    try:os.killpg(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True


def _stop_group(proc,limits):
    def send(sig):
        try:os.killpg(proc.pid,sig)
        except (ProcessLookupError,PermissionError):pass
    if _group_exists(proc.pid):send(signal.SIGTERM)
    end=time.monotonic()+limits.terminate_seconds
    while time.monotonic()<end and _group_exists(proc.pid):
        proc.poll();time.sleep(.01)
    if _group_exists(proc.pid):send(signal.SIGKILL)
    end=time.monotonic()+limits.cleanup_seconds
    while time.monotonic()<end and _group_exists(proc.pid):
        proc.poll();time.sleep(.01)
    proc.poll()
    return {'process_group_stopped':not _group_exists(proc.pid),'scope':'process_group',
            'container_cleanup_required':True,'credential_reuse_authorized':False}


class InferenceTransport:
    """One request per instance. Caller owns immutable profile, container and auth leases."""
    def __init__(self,profile: PinnedCLI,*,home: Path,scratch: Path,proxy=None,limits=None):
        if type(profile) is not PinnedCLI:
            raise TransportError('invalid_profile')
        self.profile,self.home,self.scratch=profile,home,scratch
        self.env=child_environment(home,scratch,proxy)
        if limits is not None and type(limits) is not TransportLimits:
            raise TransportError('invalid_limit')
        self.limits=limits or TransportLimits()
        self._lock=threading.Lock();self._used=False

    def run(self,request,*,cancel: threading.Event | None = None):
        if not self._lock.acquire(False):raise TransportError('transport_busy')
        try:
            if self._used:raise TransportError('outer_cleanup_required')
            data,tools,used=validate_transcript(request,self.limits.input_bytes)
            _verify_binary(self.profile)
            if cancel is not None and cancel.is_set():raise TransportError('cancelled_before_start')
            self._used=True
            return self._run(data,tools,used,cancel)
        finally:self._lock.release()

    def _run(self,data,tools,used,cancel):
        try:
            proc=subprocess.Popen(command(self.profile),cwd=self.scratch,env=self.env,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                start_new_session=True,close_fds=True)
        except OSError:raise TransportError('cli_start_failed') from None
        selector=None;output=bytearray();stderr_size=0;offset=0;error=None
        deadline=time.monotonic()+self.limits.wall_seconds
        try:
            selector=selectors.DefaultSelector()
            for stream,kind in ((proc.stdin,'in'),(proc.stdout,'out'),(proc.stderr,'err')):
                os.set_blocking(stream.fileno(),False)
                selector.register(stream,selectors.EVENT_WRITE if kind=='in' else selectors.EVENT_READ,kind)
            while selector.get_map():
                if cancel is not None and cancel.is_set():error='cancelled';break
                if time.monotonic()>=deadline:error='wall_timeout';break
                for key,_ in selector.select(min(.05,max(0,deadline-time.monotonic()))):
                    stream=key.fileobj
                    if key.data=='in':
                        try:offset+=os.write(stream.fileno(),data[offset:offset+32768])
                        except BrokenPipeError:offset=len(data)
                        if offset==len(data):selector.unregister(stream);stream.close()
                    else:
                        chunk=os.read(stream.fileno(),32768)
                        if not chunk:selector.unregister(stream);stream.close();continue
                        if key.data=='out':
                            if len(output)+len(chunk)>self.limits.stdout_bytes:error='stdout_limit';break
                            output.extend(chunk)
                        else:
                            stderr_size+=len(chunk)
                            if stderr_size>self.limits.stderr_bytes:error='stderr_limit';break
                if error:break
            while error is None and proc.poll() is None:
                if cancel is not None and cancel.is_set():error='cancelled';break
                if time.monotonic()>=deadline:error='wall_timeout';break
                try:proc.wait(timeout=min(.05,max(.001,deadline-time.monotonic())))
                except subprocess.TimeoutExpired:pass
        except OSError:
            error='cli_io_error'
        finally:
            if selector is not None:selector.close()
            for stream in (proc.stdin,proc.stdout,proc.stderr):
                if not stream.closed:stream.close()
            cleanup=_stop_group(proc,self.limits)
        if not cleanup['process_group_stopped']:error='process_group_cleanup_unconfirmed'
        requested={'model':self.profile.native_model,'effort':self.profile.effort,
                   'cli_version':self.profile.version,'binary_sha256':self.profile.sha256,'trust':'controller_pinned'}
        reported={'model':None,'effort':None,'status':'unknown','reason':'provider_did_not_report'}
        usage=None;cost=None;decision=None;usage_reason='missing_result'
        if error is None:
            try:
                result=_decode(output)
                if not isinstance(result,dict) or result.get('type')!='result' or type(result.get('is_error')) is not bool:
                    raise TransportError('invalid_cli_result')
                if result['is_error']:
                    status=result.get('api_error_status')
                    error={401:'provider_auth_rejected',429:'rate_limited'}.get(status) if type(status) is int else None
                    error=error or 'provider_failed'
                elif proc.returncode!=0:raise TransportError('cli_exit_error')
                elif result.get('subtype')!='success':raise TransportError('invalid_cli_result')
                else:
                    decision=validate_decision(result.get('structured_output'),tools,used)
                if result.get('permission_denials'):
                    raise TransportError('unexpected_cli_tool_activity')
                usage_reason='provider_did_not_report'
                raw_usage=result.get('usage')
                if isinstance(raw_usage,dict) and raw_usage:
                    accepted={k:v for k,v in raw_usage.items() if k in ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens') and type(v) is int and 0<=v<=10**12}
                    usage=accepted or None
                raw_cost=result.get('total_cost_usd')
                if type(raw_cost) in (int,float) and _finite(raw_cost) and 0<=raw_cost<=10**9:cost=raw_cost
                models=result.get('modelUsage')
                model=result.get('model')
                if isinstance(models,dict) and models:
                    if set(models)!={self.profile.native_model}:raise TransportError('reported_model_mismatch')
                    model=self.profile.native_model
                if model is not None:
                    if model!=self.profile.native_model:raise TransportError('reported_model_mismatch')
                    reported.update({'model':model,'status':'reported','reason':None})
                effort=result.get('effort')
                if effort is not None:
                    if effort!=self.profile.effort:raise TransportError('reported_effort_mismatch')
                    reported['effort']=effort
            except TransportError as exc:
                if error not in ('provider_auth_rejected','rate_limited'):error=exc.code
                decision=None
        if error:decision=None
        return InferenceResult('cancelled' if error=='cancelled' else ('error' if error else 'ok'),decision,error,
            requested,reported,usage,{'state':'reported','reason':None} if usage else {'state':'unknown','reason':usage_reason},
            cost,'api_equivalent_estimate' if cost is not None else 'unknown','worker_reported',cleanup)
