#!/usr/bin/env python3
"""Read-only v1 metadata inventory. No tokens, handoff, logs, prompts or file bodies."""
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

BASE = Path('/home/thomas/cloud')


def run(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30).stdout


def tree_stats(path):
    total = files = symlinks = special = hardlinks = dirs = 0
    if not path.is_dir() or path.is_symlink():
        return {'present': False}
    for root, children, leaves in os.walk(path, followlinks=False):
        for name in children + leaves:
            s = (Path(root) / name).lstat()
            if stat.S_ISLNK(s.st_mode): symlinks += 1
            elif stat.S_ISREG(s.st_mode):
                files += 1; total += s.st_size; hardlinks += s.st_nlink > 1
            elif stat.S_ISDIR(s.st_mode): dirs += 1
            else: special += 1
    return {'present': True, 'regular_file_bytes': total, 'regular_files': files, 'directories': dirs, 'symlinks': symlinks, 'multiply_linked_files': hardlinks, 'special_files': special}


def main():
    assert os.uname().nodename == 'omarchy'
    source = BASE / 'server' / 'cloudd.py'
    raw = source.read_bytes()
    tree = ast.parse(raw)
    methods = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    # Record route fragments only from handler comparisons; never general constants.
    routes = {}
    for method in ('do_GET', 'do_POST', 'do_DELETE'):
        routes[method] = sorted({n.value for c in ast.walk(methods[method]) if isinstance(c, ast.Compare) for n in ast.walk(c) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in ('jobs', 'log', 'file', '/jobs', 'skill.tar')})
    containers = []
    for cid in run(['docker', 'ps', '--all', '--filter', 'label=cloudd=1', '--quiet', '--no-trunc']).split():
        obj = json.loads(run(['docker', 'inspect', cid]))[0]
        hc = obj['HostConfig']
        containers.append({'id': obj['Id'], 'name': obj['Name'].lstrip('/'), 'state': obj['State']['Status'], 'memory_bytes': hc.get('Memory'), 'nano_cpus': hc.get('NanoCpus'), 'pids_limit': hc.get('PidsLimit'), 'storage_options': hc.get('StorageOpt')})
    jobs = []
    for p in sorted((BASE / 'jobs').iterdir()):
        if not re.fullmatch(r'[0-9]{4}-[0-9]{6}-[a-f0-9]{4}', p.name) or p.is_symlink() or not p.is_dir(): continue
        container = next((c for c in containers if c['name'] == 'cloud-' + p.name), None)
        jobs.append({'id': p.name, 'state': container['state'] if container else 'unknown_no_container', 'state_basis': 'docker_inspect' if container else 'No container; source has memory-only registry; historical exit status not inferred from disk.', 'workspace': str(p / 'work'), 'workspace_stats': tree_stats(p / 'work'), 'log_present_unread': (p / 'log').is_file(), 'git_directory_present': (p / 'work' / 'repo' / '.git').is_dir()})
    # Source-derived launch settings only. Do not read process environment/config.
    limits = {'memory': '4g', 'cpus': '2', 'max_jobs_default': 6, 'pids_limit_explicit': False, 'workspace_quota_explicit': False}
    statements = [ast.unparse(n) for n in tree.body if isinstance(n, ast.Assign)]
    assert "MEM, CPUS = ('4g', '2')" in statements
    launch = ast.unparse(methods['start'])
    assert "'--memory', MEM, '--cpus', CPUS" in launch
    assert '--pids-limit' not in launch and '--storage-opt' not in launch
    report = {'host': os.uname().nodename, 'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'source_sha256': hashlib.sha256(raw).hexdigest(), 'source_lines': len(raw.splitlines()), 'routes_fragments': routes, 'source_launch_defaults': limits, 'limits_scope': 'Source defaults only; no process environment or credential-bearing config read. Container-specific limits listed when containers exist.', 'containers': containers, 'jobs': jobs, 'mutations': False, 'secrets_read': False, 'job_contents_read': False, 'registry_persistence': 'Memory-only dictionary; source startup removes cloudd=1 containers. No historical statuses reconstructed.'}
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
