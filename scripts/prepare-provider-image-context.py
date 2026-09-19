#!/usr/bin/env python3
"""Prepare a fresh local provider build context; never build or activate an image."""
import argparse
import hashlib
import json
from pathlib import Path

MODULES = ('__init__.py', 'adapters.py', 'inference_transport.py',
           'provider_bootstrap.py', 'provider_main.py', 'egress.py', 'native_responses.py')


def prepare(destination: Path):
    root = Path(__file__).resolve().parents[1]
    sources = {name: (root / 'src/cloudworkbench' / name).read_bytes() for name in MODULES}
    dockerfile = (root / 'deploy/Dockerfile.provider').read_bytes()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    package = destination / 'cloudworkbench'
    package.mkdir()
    for name, data in sources.items():
        (package / name).write_bytes(data)
    (destination / 'Dockerfile').write_bytes(dockerfile)
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    (destination / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='New directory; existing contexts are refused')
    args = parser.parse_args()
    print(json.dumps(prepare(args.destination), indent=2))
