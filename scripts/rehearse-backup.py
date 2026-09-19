#!/usr/bin/env python3
"""Synthetic local-only CLI backup/restore rehearsal; never reads live service state."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from cloudworkbench.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--pytest-report', type=Path)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='cwb-p22-rehearsal-')).resolve()
    source = root / 'source'
    source.mkdir(mode=0o700)
    paths = {name: source / name for name in ('artifacts', 'inputs', 'workspaces', 'results', 'baselines')}
    for path in paths.values():
        path.mkdir()
    store = Store(source / 'state.sqlite')
    credential = secrets.token_urlsafe(32)
    owner = store.add_client('synthetic-rehearsal-owner', credential, ['observe', 'retrieve', 'submit', 'cancel'], ['rehearsal'])
    task = store.create_session(owner, {'project_id': 'rehearsal', 'goal': 'Synthetic offline backup rehearsal; no agent execution', 'agent': 'fixture', 'environment_version': 'synthetic'}, 'create')
    aid, sid = task['attempt_id'], task['session_id']
    content = bytes(range(256)) * 2048
    expected_hash = hashlib.sha256(content).hexdigest()
    artifact_path = paths['artifacts'] / 'binary.bin'
    artifact_path.write_bytes(content)
    store.register_artifact(aid, {'path': 'binary.bin', 'storage_path': str(artifact_path), 'sha256': expected_hash, 'bytes': len(content), 'mime': 'application/octet-stream'}, expected_generation=task['generation'])
    item = store.reserve_input(owner, {'name': 'input.txt', 'mime': 'text/plain'}, 'input')
    input_path = paths['inputs'] / 'input.txt'
    input_path.write_bytes(b'synthetic supplied input')
    store.finalize_input(owner, item['id'], {'storage_path': str(input_path), 'sha256': hashlib.sha256(input_path.read_bytes()).hexdigest(), 'bytes': input_path.stat().st_size}, key='finalize')
    store.cancel(owner, aid, 'cancel')
    store.archive(owner, sid, 'archive')
    workspace = paths['workspaces'] / sid
    (workspace / 'work').mkdir(parents=True)
    (workspace / 'work' / 'example.py').write_text('print("synthetic")\n')
    (workspace / 'native').mkdir()
    (workspace / 'native' / 'public.jsonl').write_text('{"type":"synthetic"}\n')
    (workspace / 'native' / '.credentials.json').write_text('REHEARSAL_ONLY_CREDENTIAL')
    baseline = json.dumps({'commit': 'synthetic', 'files': {'example.py': {'contents': 'cHJpbnQoMSk='}}}).encode()
    (paths['baselines'] / (sid + '.json')).write_bytes(baseline)
    (paths['baselines'] / (sid + '.ready.json')).write_text('{"ready":true}')
    config = root / 'worker.json'
    config.write_text(json.dumps({'database': str(store.path), 'state_root': str(source), 'artifact_root': str(paths['artifacts']), 'input_root': str(paths['inputs']), 'runtime': {'root': str(paths['workspaces'])}}))
    tool = Path(__file__).with_name('backup-state.py')
    backup = root / 'bundle'
    restored = root / 'isolated-restore'
    restored.mkdir(mode=0o700)
    common = ['--max-bytes', str(16 * 1024**2), '--reserve-bytes', '0']
    def invoke(command):
        result = subprocess.run([sys.executable, str(tool), *command, *common], check=True, capture_output=True, text=True, timeout=60)
        return json.loads(result.stdout)
    source_hash_before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    with sqlite3.connect(store.path) as snapshot:
        source_logical_before = hashlib.sha256('\n'.join(snapshot.iterdump()).encode()).hexdigest()
    created = invoke(['create', '--config', str(config), '--destination', str(backup), '--acknowledge-quiesced'])
    source_hash_after = hashlib.sha256(store.path.read_bytes()).hexdigest()
    with sqlite3.connect(store.path) as snapshot:
        source_logical_after = hashlib.sha256('\n'.join(snapshot.iterdump()).encode()).hexdigest()
    assert source_hash_before == source_hash_after and source_logical_before == source_logical_after
    receipt = invoke(['restore', '--backup', str(backup), '--target', str(restored)])
    restored_hash = hashlib.sha256((restored / 'components/artifacts/binary.bin').read_bytes()).hexdigest()
    assert restored_hash == expected_hash
    assert (restored / 'components/baselines' / (sid + '.json')).read_bytes() == baseline
    assert (restored / 'components/baselines' / (sid + '.ready.json')).is_file()
    assert store.authenticate(credential) == owner
    with sqlite3.connect(restored / 'database.sqlite') as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT state FROM attempts').fetchone()[0] == 'cancelled'
        assert db.execute('SELECT archived FROM sessions').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM clients WHERE revoked_at IS NULL').fetchone()[0] == 0
        assert str(restored) in json.loads(db.execute('SELECT metadata FROM artifacts').fetchone()[0])['storage_path']
    assert not (restored / 'components/workspaces' / sid / 'native/.credentials.json').exists()
    output = {'kind': 'synthetic-local-offline-restore-rehearsal', 'timestamp': datetime.now(timezone.utc).isoformat(),
              'host': platform.node(), 'platform': platform.platform(), 'python': platform.python_version(),
              'result': 'passed', 'source_database_sha256_before': source_hash_before, 'source_database_sha256_after': source_hash_after,
              'source_logical_sha256_before': source_logical_before, 'source_logical_sha256_after': source_logical_after, 'live_service_state_read': False, 'remote_mutations': False,
              'repository_baselines_restored': True, 'provider_executed': False, 'original_credential_still_valid': True,
              'restored_credentials_disabled': True, 'known_auth_file_omitted': True,
              'binary_sha256': restored_hash, 'binary_bytes': len(content),
              'isolated_root': str(root), 'create_result': created, 'restore_result': receipt,
              'limits': {'max_bytes': 16 * 1024**2, 'reserve_bytes': 0, 'scope': 'tiny synthetic local data only'},
              'remaining': ['real Omarchy off-host restore', 'encrypted backup destination/key', 'service restart/reboot qualification', 'retention policy and purge']}
    if args.pytest_report:
        document = ET.parse(args.pytest_report).getroot()
        cases = document.findall('.//testcase')
        failures, errors = document.findall('.//failure'), document.findall('.//error')
        assert cases and not failures and not errors
        skipped = len(document.findall('.//skipped'))
        output['pytest'] = {'summary': f'{len(cases)-skipped} passed, {skipped} skipped', 'cases': len(cases), 'failures': len(failures), 'errors': len(errors), 'junit_path': str(args.pytest_report), 'junit_sha256': hashlib.sha256(args.pytest_report.read_bytes()).hexdigest()}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(output, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'result': 'passed', 'receipt': str(args.receipt), 'binary_sha256': restored_hash, 'credentials_restored': False}))


if __name__ == '__main__':
    main()
