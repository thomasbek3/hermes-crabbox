"""Bounded workspace-byte transport; no runtime, cleanup or outcome authority."""
from dataclasses import asdict, dataclass
import base64
import hashlib
import inspect
import json
import math
import re
import time


class ExportError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExportLimits:
    max_bytes: int = 32 * 1024**2
    max_entries: int = 2000
    max_depth: int = 32
    max_seconds: float = 30

    def __post_init__(self):
        if (type(self.max_bytes) is not int or not 0 <= self.max_bytes <= 128 * 1024**2
                or type(self.max_entries) is not int or not 1 <= self.max_entries <= 10000
                or type(self.max_depth) is not int or not 1 <= self.max_depth <= 64
                or type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds)
                or not 0 < self.max_seconds <= 120):
            raise ExportError('invalid_export_limits')


@dataclass(frozen=True)
class ExportFile:
    path: str
    data: bytes
    executable: bool


@dataclass(frozen=True)
class ExportTree:
    files: tuple[ExportFile, ...]
    directories: tuple[str, ...]
    sha256: str


_DENIED = frozenset({'.git', '.hermes', '.codex', '.claude', '.ssh', '.aws', '.azure',
    '.config', '.local', '.cache', '.credentials', 'credential_state', '.npmrc', '.pypirc',
    '.netrc', '.docker', '.kube', '.gnupg', '.envrc', 'credentials', 'auth', 'native',
    'node_modules', '.venv', '__pycache__'})


def _safe_path(value):
    if (type(value) is not str or not value or len(value.encode('utf-8', errors='surrogatepass')) > 1024
            or '\\' in value or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ExportError('unsafe_export_path')
    if any(p.lower() in _DENIED or p.lower() == '.env' or p.lower().startswith('.env.')
           or p.lower() in {'auth.json', 'credentials.json', 'token.json', 'tokens.json'} for p in value.split('/')):
        raise ExportError('private_export_path')
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def normalize_selectors(selected_paths):
    if type(selected_paths) is not tuple or not selected_paths or len(selected_paths) > 10000:
        raise ExportError('invalid_export_selectors')
    selected = tuple(sorted(_safe_path(path) for path in selected_paths))
    if len(_canonical(list(selected))) > 32768:
        raise ExportError('export_selector_limit')
    seen = set()
    for path in selected:
        parts = path.split('/')
        if path in seen or any('/'.join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ExportError('overlapping_export_selectors')
        seen.add(path)
    return selected


def _limits(limits):
    if type(limits) is not ExportLimits:
        raise ExportError('invalid_export_limits')
    return limits


def max_stream_bytes(limits=ExportLimits()):
    limits = _limits(limits)
    # Paths are <=1024 UTF-8 bytes (<=6144 canonical escaped bytes). Per-file
    # base64 rounding costs <=4 bytes beyond the aggregate; metadata fits 8192.
    return 4 * ((limits.max_bytes + 2) // 3) + limits.max_entries * 8192 + 65536


_COLLECTOR_PREFIX = '''import os,sys,stat,time,json,base64,hashlib
class ExportError(ValueError):pass
'''
_COLLECTOR_BODY = r'''
limits=config['limits'];selected=config['selected'];workspace=config['workspace']
deadline=time.monotonic()+limits['max_seconds']
files={};directories=set();consumed=0;count=0

def check():
 if time.monotonic()>=deadline:raise ExportError('time_limit')

def signature(value):
 return (value.st_dev,value.st_ino,value.st_mode,value.st_size,value.st_mtime_ns,value.st_ctime_ns,value.st_nlink)

def directory(path):
 global count
 if path not in directories:
  count+=1
  if count>limits['max_entries']:raise ExportError('entry_limit')
  directories.add(path)

def validate_path(path):
 _safe_path(path)
 if len(path.split('/'))>limits['max_depth']:raise ExportError('depth_limit')

def visit(parent,name,path,remaining=None):
 global count,consumed
 check();validate_path(path)
 before=os.stat(name,dir_fd=parent,follow_symlinks=False)
 is_dir=stat.S_ISDIR(before.st_mode)
 if not is_dir and (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1):raise ExportError('unsafe_entry')
 if remaining and not is_dir:raise ExportError('non_directory_ancestor')
 flags=os.O_RDONLY|os.O_NOFOLLOW|(os.O_DIRECTORY if is_dir else os.O_NONBLOCK)
 fd=os.open(name,flags,dir_fd=parent)
 try:
  opened=os.fstat(fd)
  if signature(before)!=signature(opened):raise ExportError('source_changed')
  if is_dir:
   directory(path)
   if remaining:
    visit(fd,remaining[0],path+'/'+remaining[0],remaining[1:])
   else:
    names=[]
    with os.scandir(fd) as entries:
     for child in entries:
      check();names.append(child.name)
      if len(names)>limits['max_entries']-count:raise ExportError('entry_limit')
    for child in sorted(names):visit(fd,child,path+'/'+child)
  else:
   count+=1
   if count>limits['max_entries']:raise ExportError('entry_limit')
   if opened.st_size>limits['max_bytes']-consumed:raise ExportError('byte_limit')
   content=bytearray()
   while True:
    check();chunk=os.read(fd,min(65536,limits['max_bytes']-consumed+1))
    if not chunk:break
    consumed+=len(chunk)
    if consumed>limits['max_bytes']:raise ExportError('byte_limit')
    content.extend(chunk)
   if len(content)!=opened.st_size:raise ExportError('source_changed')
   files[path]=(bytes(content),bool(opened.st_mode&0o111))
  if signature(opened)!=signature(os.fstat(fd)) or signature(before)!=signature(os.stat(name,dir_fd=parent,follow_symlinks=False)):
   raise ExportError('source_changed')
 finally:os.close(fd)

def run():
 check()
 fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
 ancestors=[]
 try:
  for component in workspace.split('/')[1:]:
   before=os.stat(component,dir_fd=fd,follow_symlinks=False)
   child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
   if signature(before)!=signature(os.fstat(child)):os.close(child);raise ExportError('source_changed')
   ancestors.append((fd,component,before));fd=child
  initial=os.fstat(fd)
  for path in selected:
   parts=path.split('/');visit(fd,parts[0],parts[0],parts[1:])
  if signature(initial)!=signature(os.fstat(fd)):raise ExportError('source_changed')
  current=fd
  for parent,name,before in reversed(ancestors):
   opened=os.fstat(current);named=os.stat(name,dir_fd=parent,follow_symlinks=False)
   if (before.st_dev,before.st_ino)!=(opened.st_dev,opened.st_ino) or (before.st_dev,before.st_ino)!=(named.st_dev,named.st_ino):
    raise ExportError('source_changed')
   current=parent
 finally:
  os.close(fd)
  for parent,_,_ in ancestors:os.close(parent)
 digest=hashlib.sha256();written=0
 def emit(value,terminal=False):
  nonlocal written
  check()
  raw=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')+b'\n'
  written+=len(raw)
  if written>config['max_stream']:raise ExportError('stream_limit')
  if not terminal:digest.update(raw)
  sys.stdout.buffer.write(raw)
 emit({'type':'header','version':1,'selected_paths':selected})
 for path in sorted(directories):emit({'type':'directory','path':path})
 for path,(data,executable) in sorted(files.items()):
  emit({'type':'file','path':path,'size':len(data),'data_b64':base64.b64encode(data).decode('ascii'),
        'sha256':hashlib.sha256(data).hexdigest(),'executable':executable})
 emit({'type':'terminal','entries':count,'bytes':consumed,'sha256':digest.hexdigest()},terminal=True)
 sys.stdout.buffer.flush()
try:run()
except BaseException:
 sys.stderr.write('workspace_export_failed\n')
 sys.exit(2)
'''


def collector_program(selected_paths, limits=ExportLimits(), *, workspace='/workspace'):
    selected = normalize_selectors(selected_paths); limits = _limits(limits)
    if len(selected) > limits.max_entries or any(len(path.split('/')) > limits.max_depth for path in selected):
        raise ExportError('export_selection_limit')
    # This hook is only for local fixtures. Runtime callers must retain /workspace.
    if (type(workspace) is not str or len(workspace.encode('utf-8',errors='surrogatepass')) > 4096
            or not workspace.startswith('/') or workspace == '/'
            or any(p in ('', '.', '..') for p in workspace.split('/')[1:])
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in workspace)):
        raise ExportError('invalid_export_workspace')
    config = {'selected':list(selected), 'limits':asdict(limits), 'workspace':workspace,
              'max_stream':max_stream_bytes(limits)}
    return (_COLLECTOR_PREFIX + '_DENIED=frozenset(' + repr(sorted(_DENIED)) + ')\n'
            + 'config=json.loads(' + repr(_canonical(config).decode('ascii')) + ')\n'
            + inspect.getsource(_safe_path) + '\n' + _COLLECTOR_BODY)


def _parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict or _canonical(value) != raw:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ExportError('invalid_export_json') from None


def decode_export(data, *, selected_paths, limits=ExportLimits(), forbidden_values=()):
    limits = _limits(limits); selected = normalize_selectors(selected_paths)
    if len(selected) > limits.max_entries or any(len(path.split('/')) > limits.max_depth for path in selected):
        raise ExportError('export_selection_limit')
    if (type(forbidden_values) is not tuple or len(forbidden_values) > 128
            or any(type(value) is not bytes or not value or len(value) > 65536 for value in forbidden_values)):
        raise ExportError('invalid_export_secret_scan')
    if (type(data) is not bytes or len(data) > max_stream_bytes(limits) or not data.endswith(b'\n')):
        raise ExportError('invalid_export_stream')
    deadline = time.monotonic() + limits.max_seconds
    expected_header = {'type':'header','version':1,'selected_paths':list(selected)}
    entries = 0; consumed = 0; files = []; directories = []; directory_set = set(); seen = set(); terminal = False
    digest = hashlib.sha256(); offset = 0; last_dir = last_file = None; in_files = False
    while offset < len(data):
        if time.monotonic() >= deadline: raise ExportError('export_decode_deadline')
        end = data.find(b'\n', offset)
        raw = data[offset:end]
        value = _parse(raw)
        if terminal: raise ExportError('export_trailing_data')
        if offset == 0:
            if value != expected_header or type(value.get('version')) is not int:
                raise ExportError('export_selection_mismatch')
            digest.update(data[offset:end+1]);offset=end+1;continue
        kind = value.get('type')
        if kind == 'terminal':
            if (set(value) != {'type','entries','bytes','sha256'} or type(value['entries']) is not int
                    or type(value['bytes']) is not int or value['entries'] != entries or value['bytes'] != consumed
                    or value['sha256'] != digest.hexdigest()):
                raise ExportError('invalid_export_terminal')
            terminal = True;offset=end+1;continue
        if kind not in ('directory','file'):
            raise ExportError('invalid_export_record')
        path = _safe_path(value.get('path'))
        if len(path.split('/')) > limits.max_depth or path in seen:
            raise ExportError('invalid_export_tree')
        if any(secret in path.encode('utf-8') for secret in forbidden_values):
            raise ExportError('export_secret_refused')
        if not any(path == p or path.startswith(p+'/') or (kind == 'directory' and p.startswith(path+'/')) for p in selected):
            raise ExportError('export_outside_selection')
        parent = path.rpartition('/')[0]
        if parent and parent not in directory_set:
            raise ExportError('export_missing_parent')
        entries += 1
        if entries > limits.max_entries: raise ExportError('export_entry_limit')
        seen.add(path)
        if kind == 'directory':
            if set(value) != {'type','path'} or in_files or (last_dir is not None and path <= last_dir):
                raise ExportError('invalid_export_order')
            last_dir = path;directories.append(path);directory_set.add(path)
        else:
            if (set(value) != {'type','path','size','data_b64','sha256','executable'}
                    or type(value['size']) is not int or not 0 <= value['size'] <= limits.max_bytes-consumed
                    or type(value['executable']) is not bool or type(value['data_b64']) is not str
                    or len(value['data_b64']) != 4*((value['size']+2)//3)
                    or type(value['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',value['sha256'])
                    or (last_file is not None and path <= last_file)):
                raise ExportError('invalid_export_file')
            try:content = base64.b64decode(value['data_b64'],validate=True)
            except (ValueError,UnicodeError):raise ExportError('invalid_export_base64') from None
            if (len(content) != value['size'] or base64.b64encode(content).decode('ascii') != value['data_b64']
                    or hashlib.sha256(content).hexdigest() != value['sha256']):
                raise ExportError('export_content_mismatch')
            if any(secret in content for secret in forbidden_values):
                raise ExportError('export_secret_refused')
            consumed += len(content);last_file = path;in_files = True
            files.append(ExportFile(path,content,value['executable']))
        digest.update(data[offset:end+1]);offset=end+1
    if not terminal or not set(selected) <= seen:
        raise ExportError('incomplete_export')
    return ExportTree(tuple(files),tuple(directories),digest.hexdigest())
