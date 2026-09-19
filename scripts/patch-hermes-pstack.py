#!/usr/bin/env python3
"""Expose actual role iterations in the pinned Hermes JSONL completion event."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

ORIGINAL_SHA256 = 'e9229445739e6755ee1f088b0f6eb000a9761b47a8a56e932a9922e778c5d31a'
OLD = b'                   "duration_ms": int((time.time() - self._start) * 1000)}'
NEW = (b'                   "api_calls": data.get("api_calls") if type(data.get("api_calls")) is int and data["api_calls"] >= 0 else None,\n'
       + OLD)


def patched_content(data):
    digest = hashlib.sha256(data).hexdigest()
    if digest == ORIGINAL_SHA256 and data.count(OLD) == 1:
        return data.replace(OLD, NEW), True
    if (data.count(NEW) == 1
            and hashlib.sha256(data.replace(NEW, OLD)).hexdigest() == ORIGINAL_SHA256):
        return data, False
    raise ValueError('pinned_hermes_stream_source_mismatch')


def patch_file(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
            raise ValueError('invalid_hermes_stream_source')
        original = stream.read(262145)
    updated, changed = patched_content(original)
    if changed:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.pstack-patch-', delete=False) as out:
                temporary = Path(out.name)
                out.write(updated)
                out.flush()
                os.fsync(out.fileno())
            temporary.chmod(stat.S_IMODE(info.st_mode))
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return {'patch': 'pstack-stream-api-calls-v1', 'changed': changed,
            'source_sha256': ORIGINAL_SHA256, 'patched_sha256': hashlib.sha256(updated).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('/opt/hermes/source'))
    args = parser.parse_args()
    print(json.dumps(patch_file(args.source / 'hermes_cli' / 'stream_json.py'), sort_keys=True))


if __name__ == '__main__':
    main()
