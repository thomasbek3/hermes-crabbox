#!/usr/bin/env python3
"""Dedicated cloud Grok OIDC maintenance; never invokes a model or a login UI.

Installed Grok 1.0.34 uses OIDC discovery, refresh_token, and auth.json.lock.
This host-only client preserves its saved record. It is not a generic credential
manager, and must not run against personal profiles or future routed-account
reservations without their separate refresh-ownership integration.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import argparse
import pwd
import grp
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

AUTH_DIR = Path('/var/lib/cloud-workbench/auth/grok/.grok')
ISSUER = 'https://auth.x.ai'
CLIENT_ID = 'b1a00492-073a-47ea-816f-4c329264a828'
NAMESPACE = ISSUER + '::' + CLIENT_ID
AUTH = 'auth.json'
LOCK = 'auth.json.lock'
INTENT = '.cloud-refresh-intent.json'
CANDIDATE = '.cloud-refresh-candidate.json'
LIMIT = 65536
REFRESH_BEFORE_SECONDS = 8400


class RefreshError(Exception):
    pass


def decode(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise RefreshError('auth_schema_unsupported')
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise RefreshError('auth_schema_unsupported') from None


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode() + b'\n'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def directory(auth_dir=AUTH_DIR):
    if not auth_dir.is_absolute() or '..' in auth_dir.parts:
        raise RefreshError('auth_directory_must_be_absolute')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in auth_dir.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                raise RefreshError('auth_directory_unsafe')
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RefreshError('auth_directory_unsafe')
        return fd
    except BaseException:
        os.close(fd)
        raise


def read(parent, name, *, optional=False):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        if optional:
            return None
        raise RefreshError('auth_missing') from None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_gid != os.getegid() or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1 or not 0 < before.st_size <= LIMIT):
            raise RefreshError('auth_file_unsafe')
        chunks = []
        remaining = LIMIT + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
        if (len(data) != before.st_size or identity(before) != identity(os.fstat(fd))
                or identity(before) != identity(os.stat(name, dir_fd=parent, follow_symlinks=False))):
            raise RefreshError('auth_file_changed')
        return data
    finally:
        os.close(fd)


def write(parent, name, data):
    if not 0 < len(data) <= LIMIT:
        raise RefreshError('auth_size_unsupported')
    temporary = '.cloud-refresh-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def token(value):
    return (type(value) is str and 16 <= len(value) <= 16384
            and all(33 <= ord(char) <= 126 for char in value))


def credential(data):
    value = decode(data)
    if type(value) is not dict or set(value) != {NAMESPACE}:
        raise RefreshError('auth_schema_unsupported')
    row = value[NAMESPACE]
    if (type(row) is not dict or row.get('auth_mode') != 'oidc'
            or row.get('oidc_issuer') != ISSUER or row.get('oidc_client_id') != CLIENT_ID
            or not token(row.get('key')) or not token(row.get('refresh_token'))):
        raise RefreshError('auth_schema_unsupported')
    try:
        expires = datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
        if expires.tzinfo is None:
            raise ValueError()
        deadline = expires.timestamp()
    except (KeyError, AttributeError, ValueError, OverflowError):
        raise RefreshError('auth_schema_unsupported') from None
    return value, row, deadline


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RefreshError('oauth_redirect_refused')


def request_json(url, fields=None):
    # No inherited proxies, redirects, response-body logging, or retry loop.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    body = None if fields is None else urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=body, headers={'Accept': 'application/json',
        **({'Content-Type': 'application/x-www-form-urlencoded'} if body is not None else {})})
    try:
        with opener.open(request, timeout=20) as response:
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise RefreshError('oauth_response_unsupported')
            return decode(data)
    except urllib.error.HTTPError as error:
        # Deliberately never read error bodies: they can echo bearer material.
        code = error.code
        error.close()
        raise RefreshError('reauthentication_required' if code in (400, 401, 403)
                           else 'oauth_request_failed') from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise RefreshError('oauth_request_failed') from None


def endpoint():
    value = request_json(ISSUER + '/.well-known/openid-configuration')
    if type(value) is not dict or value.get('issuer') != ISSUER:
        raise RefreshError('oauth_discovery_unsupported')
    target = value.get('token_endpoint')
    if type(target) is not str:
        raise RefreshError('oauth_discovery_unsupported')
    parsed = urllib.parse.urlsplit(target)
    if (parsed.scheme != 'https' or parsed.netloc not in ('auth.x.ai', 'accounts.x.ai')
            or not parsed.path.startswith('/') or parsed.query or parsed.fragment):
        raise RefreshError('oauth_discovery_unsupported')
    return target


def lock_unchanged(parent, fd):
    held = os.fstat(fd)
    current = os.stat(LOCK, dir_fd=parent, follow_symlinks=False)
    if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
        raise RefreshError('auth_lock_changed')


def clear_intent(parent):
    os.unlink(INTENT, dir_fd=parent)
    os.fsync(parent)


def recover(parent, lock_fd, current):
    saved = read(parent, INTENT, optional=True)
    if saved is None:
        if read(parent, CANDIDATE, optional=True) is not None:
            raise RefreshError('refresh_outcome_unknown')
        return False
    intent = decode(saved)
    if (type(intent) is not dict or set(intent) != {'version', 'before', 'after'}
            or type(intent['version']) is not int or intent['version'] != 1
            or type(intent['before']) is not str or len(intent['before']) != 64
            or intent['after'] is not None and (type(intent['after']) is not str or len(intent['after']) != 64)):
        raise RefreshError('refresh_outcome_unknown')
    lock_unchanged(parent, lock_fd)
    if intent['after'] is not None and digest(current) == intent['after']:
        credential(current)
        if read(parent, CANDIDATE, optional=True) is not None:
            raise RefreshError('refresh_outcome_unknown')
        clear_intent(parent)
        return True
    candidate = read(parent, CANDIDATE, optional=True)
    if (candidate is not None and intent['after'] is not None
            and digest(current) == intent['before'] and digest(candidate) == intent['after']):
        credential(candidate)
        os.replace(CANDIDATE, AUTH, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        clear_intent(parent)
        return True
    # A lost token response can have rotated the refresh token at the issuer.
    # Never automatically submit that same refresh token again.
    raise RefreshError('refresh_outcome_unknown')


def run(auth_dir=AUTH_DIR, *, worker_user='cloud-worker', worker_group='cloud-workbench'):
    try:
        worker_uid = pwd.getpwnam(worker_user).pw_uid
        worker_gid = grp.getgrnam(worker_group).gr_gid
    except KeyError:
        raise RefreshError('dedicated_worker_account_missing') from None
    if worker_uid == 0 or os.geteuid() != worker_uid or os.getegid() != worker_gid:
        raise RefreshError('dedicated_worker_required')
    os.umask(0o077)
    parent = directory(Path(auth_dir))
    lock_fd = None
    try:
        lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                          0o600, dir_fd=parent)
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise RefreshError('auth_lock_unsafe')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'refresh_locked'
        os.fchmod(lock_fd, 0o600)
        lock_unchanged(parent, lock_fd)
        original = read(parent, AUTH)
        if recover(parent, lock_fd, original):
            return 'refresh_recovered'
        value, row, expiry = credential(original)
        if expiry > time.time() + REFRESH_BEFORE_SECONDS:
            return 'auth_current'
        target = endpoint()
        if read(parent, AUTH) != original:
            raise RefreshError('auth_file_changed')
        lock_unchanged(parent, lock_fd)
        intent = {'version': 1, 'before': digest(original), 'after': None}
        write(parent, INTENT, encode(intent))
        # No scope is supplied: the saved record contains none. OAuth refresh
        # retains the original grant instead of guessing or expanding scopes.
        response = request_json(target, {'grant_type': 'refresh_token',
            'client_id': CLIENT_ID, 'refresh_token': row['refresh_token']})
        if (type(response) is not dict or not token(response.get('access_token'))
                or type(response.get('token_type')) is not str
                or response['token_type'].lower() != 'bearer'
                or type(response.get('expires_in')) is not int
                or not 600 <= response['expires_in'] <= 31 * 86400
                or 'refresh_token' in response and not token(response['refresh_token'])):
            raise RefreshError('oauth_response_unsupported')
        row['key'] = response['access_token']
        if 'refresh_token' in response:
            row['refresh_token'] = response['refresh_token']
        row['expires_at'] = datetime.fromtimestamp(time.time() + response['expires_in'], timezone.utc).isoformat()
        updated = encode(value)
        credential(updated)
        lock_unchanged(parent, lock_fd)
        if read(parent, AUTH) != original:
            raise RefreshError('auth_file_changed')
        intent['after'] = digest(updated)
        write(parent, INTENT, encode(intent))
        write(parent, CANDIDATE, updated)
        os.replace(CANDIDATE, AUTH, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        clear_intent(parent)
        return 'auth_refreshed'
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(parent)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--auth-dir', type=Path, default=AUTH_DIR)
    parser.add_argument('--worker-user', default='cloud-worker')
    parser.add_argument('--worker-group', default='cloud-workbench')
    args = parser.parse_args()
    try:
        print(json.dumps({'status': run(args.auth_dir, worker_user=args.worker_user, worker_group=args.worker_group)}))
    except RefreshError as error:
        print(json.dumps({'status': str(error)}))
        sys.exit(1)
    except Exception:
        print('{"status":"credential_maintenance_failed"}')
        sys.exit(1)
