#!/usr/bin/env python3
"""Offline v2 backup/restore command. No service control or credential restore."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys

from cloudworkbench.operations import OperationsError, backup_state, restore_state


def private_value(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            raise OperationsError('secret scan file must be a small regular file')
        value = os.read(fd, 16385).strip()
        if not value:
            raise OperationsError('secret scan file is empty')
        return value
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Build a private plaintext offline bundle; encrypt before off-host transfer')
    create.add_argument('--config', required=True, type=Path, help='v2 worker configuration, never a legacy v1 directory')
    create.add_argument('--destination', required=True, type=Path)
    create.add_argument('--acknowledge-quiesced', action='store_true', help='Operator confirms API, worker, and all workspace writers are stopped')
    create.add_argument('--scan-secret-file', action='append', default=[], type=Path, help='Read a credential only to reject accidental copies; never include it')
    restore = commands.add_parser('restore', help='Restore only into an existing empty isolated directory; never start services')
    restore.add_argument('--backup', required=True, type=Path)
    restore.add_argument('--target', required=True, type=Path)
    for command in (create, restore):
        command.add_argument('--max-bytes', type=int, default=4 * 1024**3)
        command.add_argument('--max-files', type=int, default=100000)
        command.add_argument('--max-seconds', type=float, default=120)
        command.add_argument('--reserve-bytes', type=int, default=1024**3)
    args = parser.parse_args(argv)
    try:
        limits = {name: getattr(args, name) for name in ('max_bytes', 'max_files', 'max_seconds', 'reserve_bytes')}
        if args.command == 'create':
            config = json.loads(args.config.read_text())
            state = Path(config['state_root'])
            components = {'artifacts': Path(config['artifact_root']), 'inputs': Path(config['input_root']),
                          'results': state / 'results', 'workspaces': Path(config['runtime']['root'])}
            if os.path.lexists(state / 'baselines'):
                components['baselines'] = state / 'baselines'
            manifest = backup_state(Path(config['database']), components, args.destination,
                                    quiesced=args.acknowledge_quiesced,
                                    forbidden_values=tuple(private_value(path) for path in args.scan_secret_file), **limits)
            print(json.dumps({'created': True, 'encrypted': False, 'file_count': manifest['file_count'],
                              'bytes': manifest['bytes'], 'clients_disabled': manifest['clients_disabled'],
                              'scope': manifest['scope'], 'services_changed': False}))
        else:
            receipt = restore_state(args.backup, args.target, **limits)
            print(json.dumps(receipt, sort_keys=True))
        return 0
    except (OperationsError, OSError, ValueError, KeyError, TypeError):
        # CLI errors deliberately avoid reflecting source/config/secret contents.
        print('Backup/restore refused or failed; verify offline state, supported configuration, paths, limits and manifest integrity.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
