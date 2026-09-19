#!/usr/bin/env python3
"""Install the portable caller skill offline without replacing existing edits."""
import argparse
from pathlib import Path
import shutil
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1] / 'integrations/omarchy-mcp/skill'
NAME = 'omarchy-cloud-delegate'


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


def install(source, skills_dir):
    source, skills_dir = Path(source), Path(skills_dir).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ValueError('The source skill directory is missing or is a symbolic link')
    files = contents(source)
    required = {'SKILL.md', 'LICENSE', 'scripts/omarchy_cloud.py', 'scripts/pr_evidence.py'}
    if not required.issubset({str(path) for path in files}):
        raise ValueError('The source skill is incomplete')
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
    choice.add_argument('--agent', choices=['hermes', 'codex'])
    choice.add_argument('--skills-dir', type=Path, help='Parent skills directory for another agent or profile')
    args = parser.parse_args()
    directory = args.skills_dir or Path.home() / ('.hermes/skills' if args.agent == 'hermes' else '.agents/skills')
    try:
        destination, changed = install(SOURCE, directory)
    except (OSError, ValueError) as error:
        print(f'Installation stopped: {error}', file=sys.stderr)
        return 1
    print(('Installed: ' if changed else 'Already up to date: ') + str(destination))
    print('No credentials or MCP settings changed. See docs/QUICKSTART.md to connect.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
