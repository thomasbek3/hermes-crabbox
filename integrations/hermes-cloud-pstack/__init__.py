"""Hermes tools for the already-running cloud task agent."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat


WORKSPACE = Path('/workspace')
MAX_READ_BYTES = 12000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SUMMARY_BYTES = 16000
MAX_INSTRUCTION_BYTES = 65536


def _result(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _error(code):
    return _result({'ok': False, 'error': code})


def _text(value, maximum):
    return (type(value) is str and value.strip() and '\x00' not in value
            and len(value.encode('utf-8')) <= maximum)


def cloud_route_task(args, **kwargs):
    try:
        if type(args) is not dict or set(args) != {'summary'} or not _text(args['summary'], MAX_SUMMARY_BYTES):
            return _error('invalid_arguments')
        from cloudworkbench.agent_pstack import route_task
        return _result(route_task(args['summary']))
    except Exception:
        return _error('cloud_backend_unavailable')


def cloud_run_pstack_stage(args, **kwargs):
    try:
        if (type(args) is not dict or set(args) != {'stage_id', 'instruction'}
                or type(args['stage_id']) is not str
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', args['stage_id'])
                or not _text(args['instruction'], MAX_INSTRUCTION_BYTES)):
            return _error('invalid_arguments')
        from cloudworkbench.agent_pstack import run_stage
        return _result(run_stage(args['stage_id'], args['instruction']))
    except Exception:
        return _error('cloud_backend_unavailable')


def _open_file(parts):
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', directory_flags)
    try:
        for part in (*WORKSPACE.parts[1:], *parts[:-1]):
            child = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    finally:
        os.close(fd)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def cloud_read_workspace(args, **kwargs):
    """Return at most 12000 bytes from a regular UTF-8 workspace file."""
    try:
        if type(args) is not dict or not {'path'} <= set(args) <= {'path', 'offset', 'limit'}:
            return _error('invalid_arguments')
        path = args['path']
        offset, limit = args.get('offset', 0), args.get('limit', MAX_READ_BYTES)
        if (type(path) is not str or not 0 < len(path.encode('utf-8')) <= 1024
                or any(ord(char) < 32 or char == '\\' for char in path)
                or type(offset) is not int or not 0 <= offset <= MAX_FILE_BYTES
                or type(limit) is not int or not 1 <= limit <= MAX_READ_BYTES):
            return _error('invalid_arguments')
        parts = path.split('/')
        if any(part in ('', '.', '..') for part in parts):
            return _error('invalid_arguments')
        fd = _open_file(parts)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return _error('workspace_file_unavailable')
            if before.st_size > MAX_FILE_BYTES:
                return _error('workspace_file_too_large')
            stream.seek(offset)
            data = stream.read(limit)
            after = os.fstat(stream.fileno())
            if _identity(before) != _identity(after):
                return _error('workspace_file_changed')
        if b'\x00' in data:
            return _error('workspace_file_not_text')
        return _result({'ok': True, 'path': path, 'offset': offset,
                        'next_offset': offset + len(data),
                        'eof': offset + len(data) >= after.st_size,
                        'content': data.decode('utf-8', errors='replace')})
    except (OSError, ValueError, TypeError, UnicodeError):
        return _error('workspace_file_unavailable')


def _schema(name, description, properties, required):
    return {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': required, 'additionalProperties': False}}


def register(ctx):
    ctx.register_tool(
        name='cloud_route_task', toolset='cloud_pstack', handler=cloud_route_task,
        schema=_schema('cloud_route_task',
            'Select the allowed pstack workflow for this already-running cloud task. '
            'Send a short task summary without credentials or private raw files.',
            {'summary': {'type': 'string', 'maxLength': MAX_SUMMARY_BYTES}}, ['summary']))
    ctx.register_tool(
        name='cloud_run_pstack_stage', toolset='cloud_pstack', handler=cloud_run_pstack_stage,
        schema=_schema('cloud_run_pstack_stage',
            'Execute a stage returned by cloud_route_task using its fixed model and effort. '
            'Include relevant previous findings and workspace file paths in the instruction.',
            {'stage_id': {'type': 'string', 'maxLength': 128},
             'instruction': {'type': 'string', 'maxLength': MAX_INSTRUCTION_BYTES}},
            ['stage_id', 'instruction']))
    ctx.register_tool(
        name='cloud_read_workspace', toolset='cloud_workspace_read', handler=cloud_read_workspace,
        schema=_schema('cloud_read_workspace',
            'Read a regular text file relative to /workspace without writes. '
            'Absolute paths, symlinks, traversal and hard-linked files are refused. '
            'Offset and limit count bytes; use next_offset to continue. '
            'UTF-8 characters split by a byte boundary are replaced.',
            {'path': {'type': 'string', 'maxLength': 1024},
             'offset': {'type': 'integer', 'minimum': 0, 'maximum': MAX_FILE_BYTES, 'default': 0},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': MAX_READ_BYTES,
                       'default': MAX_READ_BYTES}}, ['path']))
    ctx.register_skill('orchestrate', Path(__file__).parent / 'skills' / 'orchestrate' / 'SKILL.md')
