"""Capture a foreground Crabbox job into its private, redacted event spool."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import uuid

from .adapters import EventSpoolWriter, _open_directory_nofollow
from .hermes_job_entrypoint import SESSION_PATTERN, capture


class CaptureError(ValueError):
    pass


class CaptureSignal(BaseException):
    def __init__(self, signum):
        self.exit_code = 128 + signum


def require(value):
    if not value:
        raise CaptureError()


def absolute_path(value):
    require(type(value) is str and value.startswith('/') and '\x00' not in value and
            all(part not in ('', '.', '..') for part in value.split('/')[1:]))
    return Path(value)


def read_private(path, maximum):
    parent = _open_directory_nofollow(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                before.st_uid == os.geteuid() and stat.S_IMODE(before.st_mode) == 0o600 and
                0 < before.st_size <= maximum)
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                   value.st_mtime_ns, value.st_ctime_ns)
        require(signature(before) == signature(after) and len(data) == before.st_size)
        return data


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def invalid_constant(_value):
    raise CaptureError()


def read_record(path):
    record = json.loads(read_private(path, 1024**2), object_pairs_hook=pairs,
                        parse_constant=invalid_constant)
    fields = {'command', 'env', 'event_spool', 'secret_file', 'resume_session_id', 'receipt'}
    require(type(record) is dict and fields <= set(record) <= fields | {'additional_secret_file'})
    command = record['command']
    require(type(command) is list and 0 < len(command) <= 1024 and
            all(type(value) is str and '\x00' not in value for value in command) and
            bool(command[0]))
    env = record['env']
    require(type(env) is dict and all(type(key) is str and key and '=' not in key and
            '\x00' not in key and type(value) is str and '\x00' not in value
            for key, value in env.items()))
    home = absolute_path(env.get('HOME'))
    descriptor = _open_directory_nofollow(home)
    try:
        info = os.fstat(descriptor)
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077)
    finally:
        os.close(descriptor)
    paths = [absolute_path(record[field]) for field in ('event_spool', 'secret_file', 'receipt')]
    if 'additional_secret_file' in record:
        paths.append(absolute_path(record['additional_secret_file']))
    require(len(set([path, *paths])) == len(paths) + 1)
    resume = record['resume_session_id']
    require(resume is None or type(resume) is str and re.fullmatch(SESSION_PATTERN, resume))
    return record


def read_additional_secrets(path):
    values = json.loads(read_private(path, 262144), object_pairs_hook=pairs,
                        parse_constant=invalid_constant)
    require(type(values) is list and 1 <= len(values) <= 16)
    require(all(type(value) is str and 16 <= len(value.encode()) <= 16384
                and all(33 <= ord(char) <= 126 for char in value) for value in values))
    return tuple(value.encode() for value in values)


def write_receipt(path, exit_code):
    parent = _open_directory_nofollow(path.parent)
    temporary = '.capture-receipt-' + uuid.uuid4().hex
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(json.dumps({'exit_code': exit_code}, separators=(',', ':')).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def main(argv=None):
    os.umask(0o077)
    arguments = sys.argv[1:] if argv is None else argv
    record, writer, code = None, None, 1
    watched_signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

    def stopped(signum, _frame):
        raise CaptureSignal(signum)

    previous = {signum: signal.signal(signum, stopped) for signum in watched_signals}
    try:
        require(len(arguments) == 1)
        record = read_record(absolute_path(arguments[0]))
        key = read_private(Path(record['secret_file']), 16384)
        require(len(key) >= 16 and not any(char in key for char in (b'\x00', b'\r', b'\n')))
        forbidden = (key,)
        if 'additional_secret_file' in record:
            forbidden += read_additional_secrets(Path(record['additional_secret_file']))
        require(not any(secret in value.encode() for secret in forbidden for value in
                        [*record['command'], *record['env'].keys(), *record['env'].values()]))
        writer = EventSpoolWriter(Path(record['event_spool']), forbidden=forbidden)
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink):
            code = capture(record['command'], record['env'], writer,
                           expected_session_id=record['resume_session_id'])
        require(type(code) is int)
    except CaptureSignal as exc:
        code = exc.exit_code
    except BaseException:
        code = 1
        sys.stderr.write('crabbox_capture_failed\n')
    finally:
        for signum in watched_signals:
            signal.signal(signum, signal.SIG_IGN)
        if writer is not None:
            try:
                writer.close()
            except BaseException:
                code = 1
        if record is not None:
            try:
                write_receipt(Path(record['receipt']), code)
            except BaseException:
                code = 1
                sys.stderr.write('crabbox_capture_receipt_failed\n')
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return code


if __name__ == '__main__':
    sys.exit(main())
