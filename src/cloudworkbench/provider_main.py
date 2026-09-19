"""One disposable request; controller closes stdin after writing.

Transport defaults: execution120s, terminate1s, cleanup3s; controller must also
enforce its outer container deadline. No provider proxy is inferred from ambient env.
"""
import argparse
import json
import os
from pathlib import Path
import select
import stat
import sys
import tempfile
import time

from .adapters import _open_directory_nofollow
from .inference_transport import PinnedCLI, TransportError, _decode
from .provider_bootstrap import run_token_request, run_native_request
from .native_responses import NativeProfile, NativeError


def read_profile(path):
    parent = _open_directory_nofollow(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o022 or info.st_nlink != 1 or not 0 < info.st_size <= 8192):
            raise TransportError('invalid_profile_file')
        value = _decode(os.read(fd, 8193))
    finally:
        os.close(fd)
    if isinstance(value,dict) and value.get('transport')=='native-responses-v1':
        if set(value)!={'transport','provider','model','effort'}: raise TransportError('invalid_profile_file')
        try: return NativeProfile(value['provider'],value['model'],value['effort'])
        except NativeError: raise TransportError('invalid_profile_file') from None
    if (not isinstance(value, dict) or set(value) != {'path', 'sha256', 'version', 'native_model', 'effort'}
            or any(not isinstance(item, str) for item in value.values())):
        raise TransportError('invalid_profile_file')
    value['path'] = Path(value['path'])
    return PinnedCLI(**value)


def read_request(fd, *, max_bytes=256 * 1024, timeout=5):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TransportError('request_read_timeout')
        chunk = os.read(fd, min(32768, max_bytes + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise TransportError('request_too_large')
    value = _decode(data)
    if not isinstance(value, dict):
        raise TransportError('invalid_request')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=Path('/run/provider/profile.json'))
    parser.add_argument('--proxy', help='Controller-selected numeric HTTP egress proxy')
    args = parser.parse_args(argv)
    try:
        profile = read_profile(args.profile)
        request = read_request(sys.stdin.fileno())
        if type(profile) is NativeProfile:
            result = run_native_request(profile, request, proxy=args.proxy)
        else:
            # Controller mounts /tmp as private tmpfs. Outer destruction is mandatory.
            folder = Path(tempfile.mkdtemp(prefix='provider-', dir='/tmp'))
            home, scratch = folder / 'home', folder / 'scratch'
            home.mkdir(mode=0o700)
            scratch.mkdir(mode=0o700)
            result = run_token_request(profile, request, home=home, scratch=scratch, proxy=args.proxy)
    except Exception as exc:
        code = exc.code if isinstance(exc, TransportError) else 'provider_bootstrap_failed'
        result = {'version': 1, 'status': 'error', 'error': code, 'result': None,
                  'container_cleanup_required': True, 'credential_reuse_authorized': False}
    try:
        encoded = json.dumps(result, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError, RecursionError):
        result = {'version': 1, 'status': 'error', 'error': 'response_serialization_failed',
                  'result': None, 'container_cleanup_required': True, 'credential_reuse_authorized': False}
        encoded = json.dumps(result, separators=(',', ':'))
    sys.stdout.write(encoded + '\n')
    sys.stdout.flush()
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
