#!/usr/bin/env python3
"""Install the portable caller skill offline without replacing existing edits."""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import urlencode

SOURCE = Path(__file__).resolve().parents[1] / 'integrations/omarchy-mcp/skill'
NAME = 'omarchy-cloud-delegate'
AGENT_DIRS = {'hermes': '.hermes/skills', 'codex': '.agents/skills',
              'claude': '.claude/skills', 'cursor': '.cursor/skills'}

spec = importlib.util.spec_from_file_location('portable_caller', SOURCE / 'scripts/omarchy_cloud.py')
caller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caller)


def contents(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if '__pycache__' in path.parts or path.name == '.DS_Store':
            continue
        if path.is_symlink():
            raise ValueError('Skill directories must not contain symbolic links')
        if path.is_file():
            result[path.relative_to(root)] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError('Skill directories must contain only regular files')
    return result


def install(source, skills_dir, connection=None):
    source, skills_dir = Path(source), Path(skills_dir).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ValueError('The source skill directory is missing or is a symbolic link')
    files = contents(source)
    required = {'SKILL.md', 'LICENSE', 'scripts/omarchy_cloud.py', 'scripts/pr_evidence.py'}
    if not required.issubset({path.as_posix() for path in files}):
        raise ValueError('The source skill is incomplete')
    if connection is not None:
        connection = caller.validate_connection(connection)
        mcp_config = {'url': connection['server'] + '/mcp',
                      'headers': {'Authorization': 'Bearer ${env:OMARCHY_CLOUD_TOKEN}'}}
        cursor_link = 'https://cursor.com/link/mcp/install?' + urlencode({
            'name': 'hermes-crabbox', 'config': base64.b64encode(json.dumps(mcp_config).encode()).decode()})
        files[Path('scripts/connection.json')] = (json.dumps(connection, indent=2) + '\n').encode()
        files[Path('cursor.mcp.json')] = (json.dumps({'mcpServers': {'hermes-crabbox': mcp_config}}, indent=2) + '\n').encode()
        files[Path('CONNECTION.md')] = (
            '# This installation\n\n'
            'Use these operator-selected settings for this worker host.\n'
            'The bundled HTTP and evidence clients load scripts/connection.json automatically.\n'
            'Explicit OMARCHY_CLOUD_* environment variables override those defaults.\n\n'
            f'- HTTP origin: `{connection["server"]}`\n'
            f'- MCP endpoint: `{connection["server"]}/mcp`\n'
            f'- Project: `{connection["project"]}`\n'
            f'- Environment version: `{connection["environment"]}`\n\n'
            f'[Add this host to Cursor]({cursor_link})\n\n'
            'That link configures MCP; it does not create a credential or network access.\n'
            'Credentials are separate: OMARCHY_CLOUD_TOKEN or an owner-only token file.\n'
            'Run `python3 scripts/check_connection.py` here for a read-only check.\n'
        ).encode()
    destination = skills_dir / NAME
    if destination.is_symlink():
        raise ValueError('The destination is a symbolic link; no files changed')
    if destination.exists():
        if destination.is_dir() and contents(destination) == files:
            return destination, False
        raise ValueError('An existing skill differs; back it up or choose --skills-dir. No files changed')
    skills_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.omarchy-skill-', dir=skills_dir) as staging:
        staged = Path(staging) / NAME
        staged.mkdir()
        for relative, data in files.items():
            path = staged / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # copytree refuses an existing destination, including a concurrently created one.
        shutil.copytree(staged, destination)
    return destination, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--agent', choices=sorted(AGENT_DIRS))
    choice.add_argument('--skills-dir', type=Path, help='Parent skills directory for another agent or profile')
    parser.add_argument('--server', help='Operator-provided HTTPS origin; no /mcp suffix or credentials')
    parser.add_argument('--project', default='hermes-tasks')
    parser.add_argument('--environment-version', default='hermes-tasks-desktop-soul-v1')
    parser.add_argument('--json', action='store_true', help='Machine-readable installation receipt')
    args = parser.parse_args()
    directory = args.skills_dir or Path.home() / AGENT_DIRS[args.agent]
    try:
        connection = None if args.server is None else {
            'server': args.server, 'project': args.project, 'environment': args.environment_version}
        if connection is None and (args.project != 'hermes-tasks' or args.environment_version != 'hermes-tasks-desktop-soul-v1'):
            raise ValueError('Custom project/environment requires --server')
        destination, changed = install(SOURCE, directory, connection)
    except (OSError, ValueError, caller.ClientError) as error:
        # Do not echo arbitrary filesystem errors or user-supplied credential values.
        detail = str(error) if isinstance(error, (ValueError, caller.ClientError)) else 'Cannot access installation files'
        if args.json:
            print(json.dumps({'status': 'blocked', 'detail': detail}))
        else:
            print(f'Installation stopped: {detail}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({'status': 'installed' if changed else 'unchanged', 'directory': str(destination),
                          'connection_configured': connection is not None,
                          'next': 'Supply the service credential, run scripts/check_connection.py, then reload agent skills.'}))
        return 0
    print(('Installed: ' if changed else 'Already up to date: ') + str(destination))
    print('No credentials or MCP settings changed. See docs/QUICKSTART.md to connect.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
