#!/usr/bin/env python3
"""Run on the worker computer as root; write one private caller credential, never print it."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
from cloudworkbench.store import Store


SCOPES = ['submit', 'observe', 'retrieve', 'cancel']


def provision_client(name, *, config, credential_dir, project):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', name):
        raise ValueError('Name must use lowercase letters, numbers and hyphens, max 40 characters.')
    config, root = Path(config), Path(credential_dir)
    if not config.is_absolute() or not root.is_absolute():
        raise ValueError('Configuration and credential directory paths must be absolute.')
    cfg = json.loads(config.read_text())
    if project not in cfg.get('projects', {}):
        raise ValueError('Project is not configured in the host API; pass --project with a configured project ID.')
    database = Path(cfg['database'])
    if not database.is_absolute() or not database.is_file():
        raise ValueError('Configured host database does not exist; initialize the host before provisioning callers.')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Unsafe credential directory: requires an operator-owned private directory (0700).')
    path = root / (name + '.token')
    store = Store(database, shared_group=True)
    if path.exists() or path.is_symlink():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 1024):
                raise ValueError('Unsafe existing credential file.')
            token = os.read(fd, 1024).decode().strip()
        finally:
            os.close(fd)
        principal = store.authenticate(token)
        if (not principal or principal['name'] != 'delegation-' + name
                or set(principal['projects']) != {project} or set(principal['scopes']) != set(SCOPES)):
            raise ValueError('Existing credential is revoked, unregistered, or has a different client/project/scope; no replacement was created.')
    else:
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(token + '\n')
        try:
            principal = store.add_client('delegation-' + name, token, SCOPES, [project])
        except Exception:
            path.unlink()
            raise
    return {'client_id': principal['id'], 'name': principal['name'],
            'credential_file': str(path), 'scopes': principal['scopes'],
            'projects': principal['projects'], 'secret_printed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='Unique caller name, for example laptop-agent or cloud-muse')
    parser.add_argument('--config', type=Path, default=Path('/etc/cloud-workbench/api.json'))
    parser.add_argument('--credential-dir', type=Path, default=Path('/var/lib/cloud-workbench/delegation-clients'))
    parser.add_argument('--project', default='hermes-tasks')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo on the worker computer, using its configured service Python environment.')
    os.umask(0o077)
    try:
        result = provision_client(args.name, config=args.config, credential_dir=args.credential_dir, project=args.project)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
