"""Immutable operator-managed environments and bounded candidate qualification."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
from typing import Annotated, Callable, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$')]
Digest = Annotated[str, Field(pattern=r'^sha256:[0-9a-f]{64}$')]
Sha256 = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Argv = Annotated[list[Annotated[str, Field(min_length=1, max_length=8192)]], Field(min_length=1, max_length=128)]


QUALIFICATION_ERRORS = frozenset({
    'environment_error', 'qualification_failed', 'qualification_receipt_invalid',
    'qualification_identity_mismatch', 'readiness_failed', 'cli_version_mismatch',
    'docker_unavailable', 'image_inspection_failed', 'image_platform_mismatch',
    'probe_timeout', 'probe_cleanup_failed',
})


class EnvironmentError(ValueError):
    def __init__(self, message, *, code='environment_error'):
        super().__init__(message)
        self.code = code if code in QUALIFICATION_ERRORS else 'qualification_failed'


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)


class Command(Strict):
    id: Identifier
    argv: Argv
    timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator('argv')
    @classmethod
    def safe_argv(cls, value):
        if any('\0' in word for word in value) or value[0].startswith('-'):
            raise ValueError('Invalid argv')
        return value


class Check(Command):
    description: str = Field(min_length=1, max_length=8192)
    # Identifies an operator-owned protected script, never a user-supplied host path.
    script_id: Identifier | None = None
    script_sha256: Sha256 | None = None
    script_name: Identifier | None = None

    @model_validator(mode='after')
    def bound_script(self):
        if bool(self.script_id) != bool(self.script_sha256):
            raise ValueError('Protected scripts require both ID and digest')
        if self.script_name and not self.script_id:
            raise ValueError('Script filename requires a protected script reference')
        return self


def relative_path(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '..' in p.parts or '\\' in value or '\0' in value or str(p) != value:
        raise ValueError('Expected canonical relative path')
    return value


class Repository(Strict):
    repository_id: Identifier
    commit: str = Field(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
    destination: str = Field(min_length=1, max_length=256)

    _path = field_validator('destination')(relative_path)


class Resources(Strict):
    cpus: int = Field(default=2, ge=1, le=6)
    memory_mib: int = Field(default=4096, ge=128, le=16384)
    pids: int = Field(default=512, ge=16, le=1024)
    workspace_mib: int = Field(default=20480, ge=64, le=102400)


class Manifest(Strict):
    schema_version: Literal[1] = 1
    project_id: Identifier
    version: Identifier
    os: Literal['linux'] = 'linux'
    architecture: Literal['amd64', 'arm64']
    base_image_digest: Digest
    image_digest: Digest
    package_lockfiles: dict[str, Sha256] = Field(default_factory=dict, max_length=64)
    cli_versions: dict[Identifier, Annotated[str, Field(min_length=1, max_length=128)]] = Field(min_length=1, max_length=32)
    repositories: list[Repository] = Field(default_factory=list, max_length=16)
    install_commands: list[Command] = Field(default_factory=list, max_length=32)
    startup_commands: list[Command] = Field(default_factory=list, max_length=16)
    readiness_probes: list[Command] = Field(min_length=1, max_length=16)
    secret_refs: list[Identifier] = Field(default_factory=list, max_length=16)
    network_profile: Identifier = 'none'
    skills: list[Identifier] = Field(default_factory=list, max_length=0)
    mcp: list[Identifier] = Field(default_factory=list, max_length=0)
    checks: list[Check] = Field(default_factory=list, max_length=100)
    resources: Resources = Field(default_factory=Resources)
    expected_deliverables: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(default_factory=list, max_length=100)
    # Explicit migration marker; these manifests do not claim a registered Git checkout.
    legacy_template_id: Identifier | None = None

    @model_validator(mode='after')
    def validate_collections(self):
        for path in self.package_lockfiles:
            relative_path(path)
        for path in self.expected_deliverables:
            relative_path(path)
        for commands in (self.install_commands,self.startup_commands,self.readiness_probes,self.checks):
            if len({command.id for command in commands}) != len(commands):
                raise ValueError('Command IDs must be unique within a stage')
        repos = self.repositories
        if len({repo.repository_id for repo in repos}) != len(repos):
            raise ValueError('Duplicate repository')
        paths = [PurePosixPath(repo.destination) for repo in repos]
        if any(a == b or a in b.parents or b in a.parents for i,a in enumerate(paths) for b in paths[i+1:]):
            raise ValueError('Repository destinations overlap')
        if self.legacy_template_id and repos:
            raise ValueError('Legacy template cannot claim repository checkout')
        return self


class ProbeReceipt(Strict):
    id: Identifier
    exit_code: int = Field(ge=0, le=255)


class QualificationReceipt(Strict):
    manifest_sha256: Sha256
    image_digest: Digest
    os: Literal['linux']
    architecture: Literal['amd64', 'arm64']
    probes: list[ProbeReceipt] = Field(min_length=1, max_length=16)
    source: Literal['docker_image_readiness', 'trusted_builder']
    cli_versions: dict[Identifier, str]


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def validate_manifest(value):
    manifest = Manifest.model_validate(value)
    encoded = canonical(manifest.model_dump()).encode()
    if len(encoded) > 256 * 1024:
        raise EnvironmentError('Manifest exceeds size limit')
    return manifest.model_dump(), hashlib.sha256(encoded).hexdigest()


class EnvironmentRegistry:
    def __init__(self, path: str | Path, *, read_only=False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise EnvironmentError('Environment registry is unavailable')
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS environments(
                    project TEXT NOT NULL,version TEXT NOT NULL,digest TEXT NOT NULL,
                    manifest TEXT NOT NULL,created_at TEXT NOT NULL,
                    qualified_receipt TEXT,PRIMARY KEY(project,version));
                CREATE TABLE IF NOT EXISTS qualification_attempts(
                    id TEXT PRIMARY KEY,project TEXT NOT NULL,version TEXT NOT NULL,
                    started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,
                    receipt TEXT,error_code TEXT);
                CREATE TABLE IF NOT EXISTS active_environments(
                    project TEXT PRIMARY KEY,version TEXT NOT NULL,digest TEXT NOT NULL,
                    activated_at TEXT NOT NULL);
            ''')
        os.chmod(self.path,0o640)

    @contextmanager
    def db(self):
        target = self.path.resolve().as_uri()+'?mode=ro' if self.read_only else self.path
        db = sqlite3.connect(target,uri=self.read_only,timeout=10,isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise EnvironmentError('Environment registry is read-only')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def register(self, value: dict):
        manifest,digest = validate_manifest(value)
        project,version = manifest['project_id'],manifest['version']
        with self.transaction() as db:
            old = db.execute('SELECT digest FROM environments WHERE project=? AND version=?',(project,version)).fetchone()
            if old and old['digest'] != digest:
                raise EnvironmentError('Environment version is immutable; register a new version')
            db.execute('INSERT OR IGNORE INTO environments(project,version,digest,manifest,created_at) VALUES(?,?,?,?,?)',
                       (project,version,digest,canonical(manifest),timestamp()))
        return self.get(project,version)

    def get(self, project, version, *, include_diagnostic=False):
        with self.db() as db:
            row = db.execute('SELECT * FROM environments WHERE project=? AND version=?',(project,version)).fetchone()
            latest = db.execute('SELECT id,started_at,finished_at,status,error_code FROM qualification_attempts WHERE project=? AND version=? ORDER BY rowid DESC LIMIT 1',
                                (project,version)).fetchone() if include_diagnostic else None
        if not row:
            raise EnvironmentError('Unknown environment version')
        manifest,digest = validate_manifest(json.loads(row['manifest']))
        if digest != row['digest']:
            raise EnvironmentError('Environment manifest integrity failed')
        result = {'manifest':manifest,'manifest_sha256':digest,'qualified':bool(row['qualified_receipt']),
                  'qualification':json.loads(row['qualified_receipt']) if row['qualified_receipt'] else None}
        if include_diagnostic:
            result['latest_qualification'] = dict(latest) if latest else None
        return result

    def resolve(self, project, version, *, operator_approved_sha256=None):
        record = self.get(project,version)
        if not record['qualified']:
            if operator_approved_sha256 != record['manifest_sha256']:
                raise EnvironmentError('Environment is not qualified or explicitly operator approved')
            record['operator_approved'] = True
        return record

    def qualify(self, project, version, builder: Callable[[dict],dict]):
        record = self.get(project,version)
        attempt = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute('INSERT INTO qualification_attempts(id,project,version,started_at,status) VALUES(?,?,?,?,?)',
                       (attempt,project,version,timestamp(),'running'))
        try:
            # Builder receives a detached copy, so it cannot mutate the registry snapshot.
            value = builder(json.loads(canonical(record)))
            receipt = QualificationReceipt.model_validate(value).model_dump()
            manifest = record['manifest']
            if any(receipt[key] != expected for key,expected in (
                ('manifest_sha256',record['manifest_sha256']),('image_digest',manifest['image_digest']),
                ('os',manifest['os']),('architecture',manifest['architecture']),('cli_versions',manifest['cli_versions']))):
                raise EnvironmentError('Qualification identity mismatch', code='qualification_identity_mismatch')
            expected = sorted(p['id'] for p in manifest['readiness_probes'])
            if sorted(p['id'] for p in receipt['probes']) != expected or any(p['exit_code'] for p in receipt['probes']):
                raise EnvironmentError('Readiness probes did not all pass', code='readiness_failed')
        except Exception as exc:
            code = (exc.code if isinstance(exc, EnvironmentError) else
                    'qualification_receipt_invalid' if isinstance(exc, ValidationError) else 'qualification_failed')
            with self.transaction() as db:
                db.execute('UPDATE qualification_attempts SET status=?,finished_at=?,error_code=? WHERE id=?',
                           ('failed',timestamp(),code,attempt))
            raise EnvironmentError('Candidate qualification failed; active environment unchanged ('+code+')', code=code) from None
        qualified = {**receipt,'qualification_id':attempt,'qualified_at':timestamp()}
        with self.transaction() as db:
            db.execute('UPDATE qualification_attempts SET status=?,finished_at=?,receipt=? WHERE id=?',
                       ('passed',qualified['qualified_at'],canonical(receipt),attempt))
            # First passing receipt remains stable even if qualified again.
            db.execute('UPDATE environments SET qualified_receipt=COALESCE(qualified_receipt,?) WHERE project=? AND version=?',
                       (canonical(qualified),project,version))
        return self.resolve(project,version)

    def active(self, project):
        with self.db() as db:
            row = db.execute('SELECT version FROM active_environments WHERE project=?',(project,)).fetchone()
        return self.resolve(project,row['version']) if row else None

    def activate(self, project, version, *, expected_active: str | None):
        candidate = self.resolve(project,version)
        with self.transaction() as db:
            row = db.execute('SELECT version FROM active_environments WHERE project=?',(project,)).fetchone()
            if (row['version'] if row else None) != expected_active:
                raise EnvironmentError('Active version changed; inspect before activating')
            db.execute('INSERT INTO active_environments VALUES(?,?,?,?) ON CONFLICT(project) DO UPDATE SET version=excluded.version,digest=excluded.digest,activated_at=excluded.activated_at',
                       (project,version,candidate['manifest_sha256'],timestamp()))
        return candidate


def docker_qualifier(record: dict, *, docker='docker', uid=65534):
    """Probe a prebuilt immutable image; never mount host files or inject credentials."""
    manifest = record['manifest']
    digest = manifest['image_digest']
    try:
        inspected = subprocess.run([docker,'image','inspect',digest,'--format','{{.Id}} {{.Os}} {{.Architecture}}'],
                                   capture_output=True,text=True,timeout=30,check=True).stdout.strip().split()
    except subprocess.TimeoutExpired:
        raise EnvironmentError('Image inspection timed out', code='probe_timeout') from None
    except subprocess.CalledProcessError:
        raise EnvironmentError('Image inspection failed', code='image_inspection_failed') from None
    except OSError:
        raise EnvironmentError('Docker executable unavailable', code='docker_unavailable') from None
    if inspected != [digest,manifest['os'],manifest['architecture']]:
        raise EnvironmentError('Image identity/platform mismatch', code='image_platform_mismatch')
    resources = manifest['resources']
    receipts = []
    # Keep provider --version output inside the bounded container, never host logs.
    version_check = (
        'import re,subprocess,sys; '
        'p=subprocess.Popen([sys.argv[1],"--version"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT); '
        'data=p.stdout.read(65537); '
        'p.kill() if len(data)>65536 else None; '
        'code=p.wait(); '
        'sys.exit(0 if code==0 and len(data)<=65536 and '
        're.search(r"(?<![A-Za-z0-9_.-])"+re.escape(sys.argv[2])+r"(?![A-Za-z0-9_.-])",'
        'data.decode("utf-8",errors="replace")) else 1)'
    )
    version_probes = [{'id':'cli-'+name,'argv':['python3','-c',version_check,name,version],
                       'timeout_seconds':30,'internal':True}
                      for name,version in manifest['cli_versions'].items()]
    for probe in version_probes + manifest['readiness_probes']:
        name = 'cwb-env-probe-'+uuid.uuid4().hex
        argv = [docker,'run','--name',name,'--label','io.cloudworkbench.environment-probe=true',
                '--pull','never','--network','none','--read-only','--no-healthcheck','--user',str(uid),
                '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit',str(resources['pids']),
                '--memory',str(resources['memory_mib'])+'m','--memory-swap',str(resources['memory_mib'])+'m',
                '--cpus',str(resources['cpus']),'--log-driver','none','--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=32m',
                '--entrypoint',probe['argv'][0],digest,*probe['argv'][1:]]
        try:
            result = subprocess.run(argv,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                    timeout=probe['timeout_seconds'],check=False)
            if probe.get('internal'):
                if result.returncode:
                    raise EnvironmentError('Pinned CLI version probe failed', code='cli_version_mismatch')
            else:
                receipts.append({'id':probe['id'],'exit_code':result.returncode})
        except subprocess.TimeoutExpired:
            raise EnvironmentError('Candidate probe timed out', code='probe_timeout') from None
        except OSError:
            raise EnvironmentError('Docker executable unavailable', code='docker_unavailable') from None
        finally:
            # A lost run response or timeout is still followed by explicit removal.
            try:
                cleanup = subprocess.run([docker,'rm','--force','--volumes',name],stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,timeout=20,check=False)
            except (subprocess.TimeoutExpired,OSError):
                raise EnvironmentError('Probe cleanup uncertain; inspect its labeled runtime', code='probe_cleanup_failed') from None
            if cleanup.returncode:
                raise EnvironmentError('Probe cleanup uncertain; inspect its labeled runtime', code='probe_cleanup_failed')
    return {'manifest_sha256':record['manifest_sha256'],'image_digest':digest,'os':manifest['os'],
            'architecture':manifest['architecture'],'probes':receipts,'source':'docker_image_readiness',
            'cli_versions':manifest['cli_versions']}


def legacy_manifest(project_id: str, version: str, project: dict, runtime: dict,
                    *, architecture: str, cli_versions: dict, readiness_probes: list):
    """Explicitly import trusted demo config without copying credential/host paths."""
    if version not in project.get('environment_versions', []):
        raise EnvironmentError('Version is not configured')
    checks = []
    for check in project.get('checks', []):
        value = {key:check[key] for key in ('id','description','argv')}
        value['timeout_seconds'] = check.get('timeout',30)
        if check.get('script_source'):
            source = Path(check['script_source'])
            if source.is_symlink() or not source.is_file():
                raise EnvironmentError('Protected check must be an operator-owned regular file')
            with source.open('rb') as stream:
                content = stream.read(1024*1024+1)
            if len(content)>1024*1024:
                raise EnvironmentError('Protected script exceeds limit')
            value.update(script_id=check['id'],script_sha256=hashlib.sha256(content).hexdigest(),
                         script_name=check.get('script_name',source.name))
        checks.append(value)
    value = {'project_id':project_id,'version':version,'architecture':architecture,
             'base_image_digest':runtime['image'],'image_digest':runtime['image'],
             'cli_versions':cli_versions,'readiness_probes':readiness_probes,'checks':checks,
             'resources':{key:runtime[key] for key in ('cpus','memory_mib','pids','workspace_mib') if key in runtime},
             'network_profile':'legacy-configured' if runtime.get('network_enabled') else 'none',
             'legacy_template_id':project_id if project.get('template') else None}
    return validate_manifest(value)[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description='Operator-only immutable environment registry')
    parser.add_argument('--registry',type=Path,required=True)
    commands = parser.add_subparsers(dest='command',required=True)
    register = commands.add_parser('register'); register.add_argument('manifest',type=Path)
    for name in ('show','qualify','activate'):
        child = commands.add_parser(name);child.add_argument('project');child.add_argument('version')
        if name == 'activate':
            child.add_argument('--expected-active',required=True,help='Current version, or NONE for first activation')
    active = commands.add_parser('active');active.add_argument('project')
    args = parser.parse_args(argv)
    try:
        registry = EnvironmentRegistry(args.registry)
        if args.command=='register':
            with args.manifest.open('rb') as source:
                payload=source.read(256*1024+1)
            if len(payload)>256*1024:
                raise EnvironmentError('Manifest exceeds size limit')
            result=registry.register(json.loads(payload))
        elif args.command=='qualify':
            result=registry.qualify(args.project,args.version,docker_qualifier)
        elif args.command=='activate':
            result=registry.activate(args.project,args.version,expected_active=None if args.expected_active=='NONE' else args.expected_active)
        elif args.command=='active':
            result=registry.active(args.project)
        else:
            result=registry.get(args.project,args.version,include_diagnostic=True)
        print(json.dumps(result,indent=2))
        return 0
    except Exception as exc:
        # Operator manifest contents and Docker stderr may contain secrets; never echo them.
        code = exc.code if isinstance(exc,EnvironmentError) else 'environment_error'
        print('Environment operation failed ('+code+'); inspect show for qualification status.',file=__import__('sys').stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
