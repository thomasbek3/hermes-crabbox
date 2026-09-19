"""Root-only exact-attempt provisioning; no listener, sudo wrapper or live install.

The root process supplies policy and authorization callbacks. Requests cannot choose
paths, Unix identities, bootstrap code, or helper commands. Capabilities are local
caller/relay tokens, never provider credentials, and are omitted from return values.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from .hermes_adapter import RoutedLaunchPlan

SOURCE_FILES = frozenset('__init__.py routed_caller.py hermes_adapter.py pstack_routing.py inference_relay.py inference_service.py adapters.py'.split())
WORKER_UID, WORKER_GID, TOOL_GID, RELAY_GID = 959, 960, 1000, 1001
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')


class BootstrapError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class BootstrapRequest:
    attempt_id: str
    session_id: str
    generation: int
    profile_digest: str
    plan: RoutedLaunchPlan = field(repr=False)

    def __post_init__(self):
        if (any(type(v) is not str or not _ID.fullmatch(v) for v in (self.attempt_id, self.session_id))
                or type(self.generation) is not int or not 1 <= self.generation <= 2**31-1
                or type(self.profile_digest) is not str or not _HEX.fullmatch(self.profile_digest)
                or type(self.plan) is not RoutedLaunchPlan):
            raise BootstrapError('invalid_bootstrap_request')
        try:
            receipt = json.loads(self.plan.receipt_json)
            env = dict(self.plan.environment)
            config = json.loads(self.plan.config_json)
            if (self.plan.request_path not in ('/v1/responses', '/v1/chat/completions')
                    or self.plan.capability_environment != 'CWB_INFERENCE_CAPABILITY'
                    or self.plan.argv[:2] != ('/opt/hermes/venv/bin/hermes', 'chat')
                    or len(env) != len(self.plan.environment)
                    or receipt['profile_digest'] != self.profile_digest
                    or receipt['config_sha256'] != _sha(self.plan.config_json.encode())
                    or receipt['prompt_sha256'] != _sha(self.plan.prompt.encode())
                    or receipt['argv_sha256'] != _sha(_json(self.plan.argv))
                    or receipt['environment_sha256'] != _sha(_json(env))
                    or receipt['request_path'] != self.plan.request_path
                    or type(self.plan.workspace_readonly) is not bool
                    or receipt['workspace_readonly'] != self.plan.workspace_readonly
                    or config['providers']['cwb-inference']['base_url'] != 'http://127.0.0.1:9876/v1'):
                raise ValueError
            for body, cap in ((self.plan.prompt.encode(),131072),(self.plan.config_json.encode(),65536),(_json(asdict(self.plan)),262144)):
                if not 0 < len(body) <= cap:
                    raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError, AttributeError, RecursionError):
            raise BootstrapError('bootstrap_plan_mismatch') from None

    @property
    def digest(self):
        return _sha(_json(asdict(self)))

    @property
    def directory_name(self):
        return _sha(self.attempt_id.encode())[:24] + '.' + str(self.generation)


@dataclass(frozen=True)
class BootstrapMaterial:
    attempt_id: str
    generation: int
    binding_digest: str
    root: Path
    task_dir: Path
    worker_socket_dir: Path
    materialization_parent: Path
    http_capability_sha256: str
    worker_capability_sha256: str
    receipt_sha256: str


def _identity(info):
    return [info.st_dev, info.st_ino]


def _checked_read(path, cap, *, uid=0):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_mode & 0o022
                or before.st_nlink != 1 or not 0 < before.st_size <= cap):
            raise BootstrapError('untrusted_bootstrap_file')
        body = bytearray()
        while len(body) <= cap:
            part = os.read(fd, min(65536, cap + 1 - len(body)))
            if not part: break
            body.extend(part)
        after = os.fstat(fd)
        current = path.lstat()
        if (len(body) > cap or len(body) != before.st_size
                or (_identity(before), before.st_size, before.st_mtime_ns) != (_identity(after), after.st_size, after.st_mtime_ns)
                or _identity(current) != _identity(before)):
            raise BootstrapError('bootstrap_file_changed')
        return bytes(body)
    finally:
        os.close(fd)


class RoutedBootstrap:
    def __init__(self, *, destination_root, source_root, source_hashes, authorize, authorize_discard):
        if os.geteuid() != 0:
            raise BootstrapError('root_helper_required')
        if not callable(authorize) or not callable(authorize_discard):
            raise BootstrapError('trusted_authorizers_required')
        if type(source_hashes) is not dict or set(source_hashes) != SOURCE_FILES or any(type(h) is not str or not _HEX.fullmatch(h) for h in source_hashes.values()):
            raise BootstrapError('pinned_source_manifest_required')
        self.root = self._directory(destination_root)
        self.source = self._directory(source_root)
        if self.root == self.source or self.root in self.source.parents or self.source in self.root.parents:
            raise BootstrapError('bootstrap_roots_overlap')
        info = self.root.lstat()
        if info.st_gid != WORKER_GID or stat.S_IMODE(info.st_mode) & 0o777 != 0o750:
            raise BootstrapError('private_controller_root_required')
        if len(os.fsencode(self.root / ('f'*24+'.2147483647') / 'worker/socket')) > 103:
            raise BootstrapError('bootstrap_socket_path_too_long')
        self.root_identity = _identity(info)
        self.hashes = dict(source_hashes)
        self.authorize, self.authorize_discard = authorize, authorize_discard

    @staticmethod
    def _directory(value):
        path = Path(value)
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise BootstrapError('unsafe_bootstrap_directory')
        for item in (path, *path.parents):
            info = item.lstat()
            sticky = item != path and info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 and not sticky:
                raise BootstrapError('unsafe_bootstrap_directory')
        return path

    def _check_root(self):
        self._directory(self.root)
        info = self.root.lstat()
        if (_identity(info) != self.root_identity or info.st_gid != WORKER_GID
                or stat.S_IMODE(info.st_mode) & 0o777 != 0o750):
            raise BootstrapError('bootstrap_root_changed')

    def _sources(self):
        self._directory(self.source)
        result = {}
        for name in sorted(SOURCE_FILES):
            body = _checked_read(self.source / name, 1024 * 1024)
            if _sha(body) != self.hashes[name]:
                raise BootstrapError('bootstrap_source_changed')
            result[name] = body
        return result

    @staticmethod
    def _undo(created):
        for path, identity, directory in reversed(created):
            info = path.lstat()
            if _identity(info) != identity or stat.S_ISDIR(info.st_mode) != directory or path.is_symlink():
                raise BootstrapError('bootstrap_rollback_identity_changed')
            if directory: path.rmdir()
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1: path.unlink()
            else: raise BootstrapError('bootstrap_rollback_identity_changed')

    def provision(self, request):
        if os.geteuid() != 0 or type(request) is not BootstrapRequest:
            raise BootstrapError('root_request_required')
        if self.authorize(request) is not True:
            raise BootstrapError('bootstrap_authority_unavailable')
        self._check_root()
        sources = self._sources()
        destination = self.root / request.directory_name
        if os.path.lexists(destination):
            raise BootstrapError('bootstrap_attempt_exists')
        temporary = self.root / ('.pending-' + secrets.token_hex(16))
        created = []; published = False; material = None
        def directory(path, uid, gid, mode):
            path.mkdir(mode=0o700)
            created.append((path, _identity(path.lstat()), True))
            os.chown(path, uid, gid, follow_symlinks=False); path.chmod(mode)
        def file(path, body, uid, gid, mode):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created.append((path, _identity(os.fstat(fd)), False))
            try:
                with os.fdopen(fd, 'wb', closefd=False) as stream:
                    stream.write(body); stream.flush(); os.fsync(fd)
                os.fchown(fd, uid, gid); os.fchmod(fd, mode)
            finally: os.close(fd)
        try:
            directory(temporary, 0, WORKER_GID, 0o700)
            directory(temporary/'task', WORKER_UID, TOOL_GID, 0o751)
            directory(temporary/'task/source', WORKER_UID, TOOL_GID, 0o555)
            directory(temporary/'task/source/cloudworkbench', WORKER_UID, TOOL_GID, 0o555)
            directory(temporary/'worker', WORKER_UID, RELAY_GID, 0o2750)
            directory(temporary/'stages', WORKER_UID, TOOL_GID, 0o2750)
            for name, body in sources.items():
                file(temporary/'task/source/cloudworkbench'/name, body, WORKER_UID, TOOL_GID, 0o444)
            http_cap = secrets.token_hex(32); worker_cap = secrets.token_hex(32)
            if http_cap == worker_cap: raise BootstrapError('capability_collision')
            http_hash, worker_hash = _sha(http_cap.encode()), _sha(worker_cap.encode())
            for name, body in (('prompt.txt',request.plan.prompt.encode()),('config.yaml',request.plan.config_json.encode()),
                               ('launch.json',_json(asdict(request.plan))),('http-capability',http_cap.encode())):
                file(temporary/'task'/name, body, WORKER_UID, TOOL_GID, 0o440)
            binding = {'attempt_id':request.attempt_id,'generation':request.generation,'profile_digest':request.profile_digest}
            client = {'binding':binding,'worker_capability':worker_cap,'http_capability':http_cap,
                      'request_path':request.plan.request_path,'port':9876}
            file(temporary/'worker/client.json',_json(client),WORKER_UID,RELAY_GID,0o440)
            entries = {}
            for path, identity, is_dir in created[1:]:
                info = path.lstat()
                entry = {'identity':identity,'directory':is_dir,'uid':info.st_uid,'gid':info.st_gid,'mode':stat.S_IMODE(info.st_mode)}
                if not is_dir: entry['sha256'] = _sha(path.read_bytes())
                entries[path.relative_to(temporary).as_posix()] = entry
            receipt = {'version':1,'attempt_id':request.attempt_id,'session_id':request.session_id,'generation':request.generation,
                'binding_digest':request.digest,'root_identity':_identity(temporary.lstat()),'source_hashes':self.hashes,
                'http_capability_sha256':http_hash,'worker_capability_sha256':worker_hash,'entries':entries}
            receipt_bytes = _json(receipt)
            file(temporary/'receipt.json',receipt_bytes,0,WORKER_GID,0o440)
            material = BootstrapMaterial(request.attempt_id,request.generation,request.digest,destination,
                destination/'task',destination/'worker',destination/'stages',http_hash,worker_hash,_sha(receipt_bytes))
            if self.authorize(request) is not True: raise BootstrapError('bootstrap_authority_unavailable')
            if os.path.lexists(destination): raise BootstrapError('bootstrap_attempt_exists')
            # Private parent + exclusive attempt claim below prevent replacing another root invocation.
            claim = self.root / (request.directory_name + '.claim')
            claim_fd = os.open(claim,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            try:
                os.write(claim_fd,request.digest.encode());os.fsync(claim_fd)
                if os.path.lexists(destination): raise BootstrapError('bootstrap_attempt_exists')
                os.rename(temporary,destination);published=True
                destination.chmod(0o750)
                root_fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(root_fd)
                finally:os.close(root_fd)
            finally:
                os.close(claim_fd)
                # Claims are permanent identity fences, including interrupted publication.
            return material
        except OSError:
            error = BootstrapError('bootstrap_publication_retained' if published else 'bootstrap_filesystem_failure')
            if published:
                error.material = material
            raise error from None
        finally:
            if not published and created: self._undo(created)

    def discard_unlaunched(self, material):
        if os.geteuid() != 0 or type(material) is not BootstrapMaterial:
            raise BootstrapError('root_material_required')
        self._check_root()
        if material.root != self.root / (_sha(material.attempt_id.encode())[:24]+'.'+str(material.generation)):
            raise BootstrapError('bootstrap_material_mismatch')
        if self.authorize_discard(material) is not True:
            raise BootstrapError('unlaunched_authority_required')
        try:
            self._discard_unlaunched(material)
        except OSError:
            raise BootstrapError('bootstrap_discard_retained') from None

    def _discard_unlaunched(self, material):
        body = _checked_read(material.root/'receipt.json',1024*1024)
        if _sha(body) != material.receipt_sha256: raise BootstrapError('bootstrap_receipt_changed')
        value = json.loads(body)
        if (value['binding_digest'] != material.binding_digest or value['attempt_id'] != material.attempt_id
                or value['generation'] != material.generation or value['root_identity'] != _identity(material.root.lstat())):
            raise BootstrapError('bootstrap_material_mismatch')
        self._inventory(material.root, value)
        created = [(material.root,value['root_identity'],True)]
        for relative, expected in value['entries'].items():
            path=material.root/relative;info=path.lstat()
            if (path.is_symlink() or _identity(info)!=expected['identity'] or info.st_uid!=expected['uid']
                    or info.st_gid!=expected['gid'] or stat.S_IMODE(info.st_mode)!=expected['mode']
                    or stat.S_ISDIR(info.st_mode)!=expected['directory']):
                raise BootstrapError('bootstrap_material_changed')
            if not expected['directory'] and _sha(_checked_read(path,1024*1024,uid=WORKER_UID))!=expected['sha256']:
                raise BootstrapError('bootstrap_material_changed')
            created.append((path,expected['identity'],expected['directory']))
        created.sort(key=lambda item:len(item[0].parts))
        receipt_path = material.root/'receipt.json'
        receipt_identity = _identity(receipt_path.lstat())
        if self.authorize_discard(material) is not True:
            raise BootstrapError('unlaunched_authority_required')
        self._undo(created[1:])
        self._undo([(receipt_path,receipt_identity,False)])
        try:
            self._undo(created[:1])
        except OSError:
            # Root is not worker-writable. Restore recovery evidence only into the
            # exact retained root and never overwrite a replacement receipt.
            if _identity(material.root.lstat()) == value['root_identity']:
                fd = os.open(receipt_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o440)
                try:
                    with os.fdopen(fd,'wb',closefd=False) as stream:
                        stream.write(body);stream.flush();os.fsync(fd)
                    os.fchown(fd,0,WORKER_GID);os.fchmod(fd,0o440)
                finally:os.close(fd)
            raise

    @staticmethod
    def _inventory(root, receipt):
        entries = receipt['entries']
        if type(entries) is not dict or len(entries) > 64:
            raise BootstrapError('bootstrap_inventory_bound')
        directories = {'':receipt['root_identity']}
        for relative, expected in entries.items():
            if (type(relative) is not str or len(relative) > 256 or relative.startswith('/')
                    or any(p in ('','.','..') for p in relative.split('/')) or relative.count('/') > 4):
                raise BootstrapError('bootstrap_inventory_bound')
            if expected['directory']:directories[relative] = expected['identity']
        names = set(entries) | {'receipt.json'}
        for relative, identity in directories.items():
            path = root/relative
            fd = os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                if _identity(os.fstat(fd)) != identity:
                    raise BootstrapError('bootstrap_material_changed')
                expected = {name.rsplit('/',1)[-1] for name in names
                    if name.rpartition('/')[0] == relative}
                actual = set()
                with os.scandir(fd) as iterator:
                    for item in iterator:
                        if item.name not in expected or len(actual) >= len(expected):
                            raise BootstrapError('bootstrap_material_in_use_or_changed')
                        actual.add(item.name)
                if actual != expected:
                    raise BootstrapError('bootstrap_discard_incomplete_retained')
            finally:os.close(fd)
