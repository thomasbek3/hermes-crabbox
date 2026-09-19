#!/usr/bin/env python3
"""Build an unactivated provider candidate from an explicit fresh source context."""

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

import argparse
import datetime
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
MODULES = {'__init__.py', 'adapters.py', 'inference_transport.py',
           'provider_bootstrap.py', 'provider_main.py', 'egress.py', 'native_responses.py'}
BASE = 'sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
BASE_TAG = 'cwb-hermes-candidate:0.21.3-hermesbuild-4e555a944e'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'operator@worker.example.invalid']


def context_archive(stage):
    if stage.is_symlink() or any(p.is_symlink() for p in stage.rglob('*')):
        raise ValueError('Context must not contain symlinks')
    manifest = json.loads((stage / 'source-manifest.json').read_text())
    if not isinstance(manifest, dict) or set(manifest) != MODULES:
        raise ValueError('Unexpected provider module manifest')
    expected = {'Dockerfile', 'source-manifest.json'} | {'cloudworkbench/' + n for n in manifest}
    if {str(p.relative_to(stage)) for p in stage.rglob('*') if p.is_file()} != expected:
        raise ValueError('Unexpected provider context files')
    payloads = {}
    for name, digest in manifest.items():
        data = (stage / 'cloudworkbench' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest or data != (ROOT / 'src/cloudworkbench' / name).read_bytes():
            raise ValueError('Provider context source drift: ' + name)
        payloads['cloudworkbench/' + name] = data
    dockerfile = (stage / 'Dockerfile').read_bytes()
    if dockerfile != (ROOT / 'deploy/Dockerfile.provider').read_bytes():
        raise ValueError('Provider Dockerfile drift')
    payloads['Dockerfile'] = dockerfile
    payloads['source-manifest.json'] = (json.dumps(manifest, sort_keys=True) + '\n').encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz', format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(payloads.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return manifest, buf.getvalue()


def build(stage, receipt_path, log_path):
    manifest, archive = context_archive(stage)
    paths = [receipt_path.resolve(), log_path.resolve()]
    if paths[0] == paths[1] or any(p.is_relative_to(stage.resolve()) for p in paths):
        raise ValueError('Output paths must be distinct and outside the source context')
    if any(p.exists() or p.is_symlink() for p in (receipt_path, log_path)):
        raise FileExistsError('Receipt and build log must both be new files')
    binding = {'context': str(stage.resolve()), 'sources': manifest, 'host': 'archived-worker.invalid',
               'context_archive_sha256': hashlib.sha256(archive).hexdigest(),
               'tag': None, 'remote_build_outcome': 'not_started'}
    # Exclusive creation precedes SSH; a failed build retains an explicit failure receipt.
    with receipt_path.open('x') as receipt_file:
        try:
            with log_path.open('xb') as log_file:
                def output(argv):
                    return subprocess.check_output(SSH + [shlex.join(argv)], text=True, timeout=30).strip()

                if output(['hostname']) != 'archived-worker.invalid':
                    raise ValueError('Unexpected remote host')
                for image in (BASE, BASE_TAG):
                    if output(['docker', 'image', 'inspect', image, '--format', '{{.Id}}']) != BASE:
                        raise ValueError('Pinned provider base image mismatch')
                tag = 'cwb-provider-candidate:' + uuid.uuid4().hex[:12]
                binding.update(tag=tag, remote_build_outcome='unknown')
                receipt_file.write(json.dumps({'status': 'building', **binding, 'activated': False}) + '\n')
                receipt_file.flush()
                argv = ['docker', 'build', '--network', 'none', '--build-arg', 'BASE_IMAGE=' + BASE_TAG, '-t', tag, '-']
                try:
                    result = subprocess.run(SSH + [shlex.join(argv)], input=archive, capture_output=True, timeout=120)
                except subprocess.TimeoutExpired as exc:
                    log_file.write((exc.stdout or b'') + (exc.stderr or b''))
                    raise
                log_file.write(result.stdout + result.stderr)
                log_file.flush()
                if result.returncode != 0:
                    raise RuntimeError('Provider image build failed; see build log')
                binding['remote_build_outcome'] = 'built_pending_inspection'
                meta = json.loads(output(['docker', 'image', 'inspect', tag]))[0]
                base_meta = json.loads(output(['docker', 'image', 'inspect', BASE]))[0]
                if meta['RootFS']['Layers'][:len(base_meta['RootFS']['Layers'])] != base_meta['RootFS']['Layers']:
                    raise ValueError('Provider candidate base layer mismatch')
                receipt = {
                    'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'host': 'archived-worker.invalid', 'context': str(stage.resolve()),
                    'context_archive_sha256': hashlib.sha256(archive).hexdigest(),
                    'base': BASE, 'tag': tag, 'image_id': meta['Id'],
                    'base_layers': base_meta['RootFS']['Layers'], 'candidate_layers': meta['RootFS']['Layers'],
                    'base_layer_prefix_matches': True, 'entrypoint': meta['Config']['Entrypoint'],
                    'user': meta['Config']['User'], 'sources': manifest, 'build_exit': result.returncode,
                    'activated': False, 'real_credentials': False, 'provider_calls': False,
                    'review': 'Current source review and runtime qualification required; no review approval implied',
                    'runtime_uid_gid': '958:959',
                }
                receipt_file.seek(0)
                receipt_file.truncate()
                receipt_file.write(json.dumps(receipt, indent=2) + '\n')
                return receipt
        except Exception as exc:
            receipt_file.seek(0)
            receipt_file.truncate()
            receipt_file.write(json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
                                           'activated': False, **binding}) + '\n')
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True, type=Path)
    parser.add_argument('--receipt', required=True, type=Path, help='New receipt file; existing files refused')
    parser.add_argument('--build-log', required=True, type=Path, help='New log file; existing files refused')
    args = parser.parse_args()
    print(json.dumps(build(args.context, args.receipt, args.build_log), indent=2))
