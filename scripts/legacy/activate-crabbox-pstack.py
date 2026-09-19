#!/usr/bin/env python3
"""Prepare or explicitly activate the opt-in Crabbox pstack environment on Omarchy."""
from __future__ import annotations

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')


import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import grp
import re
import socket
import sqlite3
import stat
import subprocess
import time
import uuid


ROOT = Path('/var/lib/cloud-workbench')
PACKAGE = Path('/opt/cloud-workbench/src/cloudworkbench')
VERSION = 'hermes-tasks-pstack-v1'
PREVIOUS = 'hermes-tasks-desktop-v1'
MODULES = ('crabbox_runtime.py', 'crabbox_capture.py', 'runner.py')
SERVICES = ('cloud-workbench-api', 'cloud-workbench-worker')
SECRET_REFS = ['grok-dedicated-oauth', 'claude-dedicated-subscription',
               'codex-dedicated-oauth', 'typesafe-jev']
PATHS = {'crabbox_pstack_claude_token': str(ROOT/'auth/claude-token'),
         'crabbox_pstack_codex_auth': str(ROOT/'auth/example-native-login/codex/.codex/auth.json'),
         'crabbox_pstack_jev_key': str(ROOT/'auth/typesafe-key')}


def require(value, code):
    if not value:
        raise RuntimeError(code)


def run(*args):
    result = subprocess.run(args, capture_output=True, timeout=45)
    require(result.returncode == 0, 'operation_failed')
    return result.stdout.decode().strip()


def protected(path, *, directory=False):
    info = path.lstat()
    require(path.resolve() == path and info.st_uid == 0 and not info.st_mode & 0o022
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1),
            'protected_path_required')


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, raw, mode=0o600, gid=0):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, gid)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        temp.unlink(missing_ok=True)


def sqlite_copy(source, destination):
    with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as original, sqlite3.connect(destination) as saved:
        original.backup(saved)
    destination.chmod(0o600)


def idle():
    database = ROOT/'control/state.db'
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
        require(not db.execute("SELECT 1 FROM attempts WHERE state NOT IN ('completed','failed','cancelled','interrupted','paused') LIMIT 1").fetchone(), 'tasks_not_idle')
    require(not run('/usr/bin/docker', 'ps', '-aq'), 'containers_present')


def updated_configs(originals, candidate, registry_path, image):
    configs = copy.deepcopy(originals)
    manifest = candidate['manifest']
    for config in configs.values():
        project = config['projects']['hermes-tasks']
        require(VERSION not in project['environment_versions'], 'environment_already_configured')
        project['environment_versions'].append(VERSION)
        project.setdefault('operator_approved_environments', {})[VERSION] = candidate['manifest_sha256']
        project.setdefault('environment_resources', {})[VERSION] = copy.deepcopy(manifest['resources'])
        config['environment_registry'] = str(registry_path)
    worker = configs['worker']
    worker['operator_approved_images'] = list(dict.fromkeys(worker.get('operator_approved_images', [])+[image]))
    policy = worker['hermes_runtime']
    for key in ('crabbox_images', 'crabbox_desktop_images', 'crabbox_pstack_images', 'tool_image_allowlist'):
        policy[key] = list(dict.fromkeys(policy.get(key, [])+[image]))
    profile = manifest['network_profile']
    profiles = policy.get('environment_network_profiles', [policy['environment_network_profile']])
    policy['environment_network_profiles'] = list(dict.fromkeys(profiles+[profile]))
    policy.setdefault('tool_network_by_profile', {})[profile] = 'bridge'
    policy.setdefault('environment_secret_refs_by_image', {})[image] = list(SECRET_REFS)
    policy.update(PATHS)
    return configs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0 and socket.gethostname() == 'archived-worker.invalid', 'root_omarchy_required')
    require(pwd.getpwnam('cloud-worker').pw_uid == 959 and grp.getgrnam('cloud-workbench').gr_gid == 960,
            'worker_identity_changed')
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', args.image), 'immutable_image_required')
    require(args.source_dir.is_relative_to(ROOT/'qualifications'), 'source_stage_location_invalid')
    protected(args.source_dir, directory=True)
    require({item.name for item in args.source_dir.iterdir()} == set(MODULES), 'stage_requires_exact_three_modules')
    source, before = {}, {}
    for name in MODULES:
        protected(args.source_dir/name)
        protected(PACKAGE/name)
        source[name] = (args.source_dir/name).read_bytes()
        compile(source[name], name, 'exec')
        before[name] = (PACKAGE/name).read_bytes()
    require(b'environment_secret_refs_by_image' in source['runner.py'], 'runner_image_secret_policy_required')
    image = json.loads(run('/usr/bin/docker', 'image', 'inspect', args.image))[0]
    require(image['Id'] == args.image and image['Os'] == 'linux' and image['Architecture'] == 'amd64',
            'image_identity_mismatch')
    paths = {name: Path('/etc/cloud-workbench')/(name+'.json') for name in ('api','worker')}
    for path in paths.values(): protected(path)
    raw = {name: path.read_bytes() for name,path in paths.items()}
    configs = {name: json.loads(value) for name,value in raw.items()}
    previous_registry = Path(configs['worker']['environment_registry'])
    require(configs['api']['environment_registry'] == str(previous_registry), 'registry_config_mismatch')
    protected(previous_registry)
    approval = configs['worker']['projects']['hermes-tasks']['operator_approved_environments'][PREVIOUS]
    require(configs['api']['projects']['hermes-tasks']['operator_approved_environments'][PREVIOUS] == approval,
            'previous_approval_mismatch')
    with sqlite3.connect(previous_registry.as_uri()+'?mode=ro', uri=True) as db:
        row = db.execute('SELECT digest,manifest FROM environments WHERE project=? AND version=?',
                         ('hermes-tasks',PREVIOUS)).fetchone()
    require(row and row[0] == approval, 'previous_manifest_missing')
    from cloudworkbench.environments import validate_manifest
    previous, digest = validate_manifest(json.loads(row[1]))
    require(digest == approval, 'previous_manifest_drift')
    manifest = copy.deepcopy(previous)
    manifest.update(version=VERSION, image_digest=args.image, base_image_digest=previous['image_digest'],
                    secret_refs=list(SECRET_REFS))
    manifest, digest = validate_manifest(manifest)
    candidate = {'manifest':manifest, 'manifest_sha256':digest, 'qualified':False, 'qualification':None}
    registry_path = ROOT/'environments'/(VERSION+'.db')
    require(not os.path.lexists(registry_path), 'activation_registry_already_exists')
    updated = updated_configs(configs, candidate, registry_path, args.image)
    for source_path in PATHS.values():
        path = Path(source_path)
        info = path.lstat()
        require(path.resolve() == path and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid == 959 and not info.st_mode & 0o027 and info.st_size > 0,
                'dedicated_credential_metadata_invalid')
    idle()
    require(all(run('systemctl', 'is-active', service) == 'active' for service in SERVICES), 'service_not_active')
    require(run('systemctl', 'show', 'cloud-workbench-worker', '--property=User', '--value') == 'cloud-worker',
            'worker_service_identity_changed')
    legacy = run('systemctl', 'show', 'cloudd', '--property=MainPID', '--value')
    receipt = {'environment':VERSION, 'image':args.image, 'manifest_sha256':digest,
               'source_sha256':{name:sha(value) for name,value in source.items()},
               'previous_source_sha256':{name:sha(value) for name,value in before.items()},
               'default_changed':False, 'qualified':False, 'model_calls':0, 'stage':'prepared'}
    if not args.execute:
        print(json.dumps(receipt, indent=2))
        return
    backup = ROOT/'operator-backups'/('crabbox-pstack-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    receipt['backup'] = str(backup)
    for name,value in raw.items(): atomic(backup/(name+'.json'),value)
    for name,value in before.items(): atomic(backup/name,value)
    sqlite_copy(previous_registry, backup/'environments.db')
    sqlite_copy(ROOT/'control/state.db', backup/'state.db')
    atomic(backup/'deployment.json',encoded(receipt))
    published = False
    try:
        for service in SERVICES: run('systemctl', 'stop', service)
        idle()
        require(all(path.read_bytes() == raw[name] for name,path in paths.items()), 'config_drift')
        require(all((PACKAGE/name).read_bytes() == value for name,value in before.items()), 'live_source_drift')
        require(all((args.source_dir/name).read_bytes() == value for name,value in source.items()), 'staged_source_drift')
        sqlite_copy(previous_registry, registry_path)
        from cloudworkbench.environments import EnvironmentRegistry
        registered = EnvironmentRegistry(registry_path).register(manifest)
        require(registered['manifest_sha256'] == digest and registered['qualified'] is False, 'candidate_mismatch')
        os.chown(registry_path,0,960)
        registry_path.chmod(0o640)
        published = True
        for name,value in source.items(): atomic(PACKAGE/name,value,0o644)
        for name,path in paths.items(): atomic(path,encoded(updated[name]),0o640,960)
        atomic(registry_path.with_suffix('.approval.json'),encoded({'manifest_sha256':digest,
            'operator_approved':True,'qualified':False,'image':args.image,'model_calls':0}),0o640,960)
        for service in reversed(SERVICES): run('systemctl','start',service)
        require(all(run('systemctl','is-active',service) == 'active' for service in SERVICES),'service_start_failed')
        require(run('systemctl','show','cloudd','--property=MainPID','--value') == legacy,'legacy_service_changed')
        require(all(path.read_bytes() == encoded(updated[name]) for name,path in paths.items()), 'published_config_drift')
        receipt['stage'] = 'activated_opt_in'
    except BaseException:
        receipt['stage'] = 'activation_failed'
        try:
            for service in SERVICES: run('systemctl','stop',service)
            if published:
                for name,value in before.items(): atomic(PACKAGE/name,value,0o644)
                for name,path in paths.items(): atomic(path,raw[name],0o640,960)
            for service in reversed(SERVICES): run('systemctl','start',service)
            receipt['rollback'] = 'previous_sources_and_configs_restored_no_task_database_restore'
        except Exception:
            receipt['rollback'] = 'incomplete_inspect_private_backup'
        raise
    finally:
        atomic(backup/'deployment.json',encoded(receipt))
    print(json.dumps(receipt,indent=2))


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'activated':False,'error':str(exc) if type(exc) is RuntimeError else type(exc).__name__}))
        raise SystemExit(1)
