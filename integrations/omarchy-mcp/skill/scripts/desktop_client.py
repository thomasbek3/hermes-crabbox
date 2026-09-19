"""Open a task desktop through private SSH pipes and a local Crabbox viewer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import re
import shutil
import signal
import socket
import stat
import subprocess
import time
import uuid

SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ControlMaster=no',
       '-o', 'ControlPath=none', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
       '-o', 'ServerAliveCountMax=2', '-o', 'ExitOnForwardFailure=yes']


def _stop(child):
    if child is None:
        return
    if child.stdin is not None and not child.stdin.closed:
        try:
            child.stdin.close()
        except BrokenPipeError:
            pass
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def _handoff(child):
    packet = bytearray()
    deadline = time.monotonic() + 70
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline and child.poll() is None:
            if not selector.select(timeout=1):
                continue
            chunk = os.read(child.stdout.fileno(), 8193 - len(packet))
            if not chunk:
                break
            packet.extend(chunk)
            if len(packet) > 8192:
                break
            if b'\n' in packet:
                data = json.loads(packet)
                if (data.get('schema') == 'crabbox/vnc-handoff/v1'
                        and data.get('host') == '127.0.0.1'
                        and type(data.get('port')) is int and 1 <= data['port'] <= 65535
                        and isinstance(data.get('password'), str)
                        and 1 <= len(data['password']) <= 4096
                        and not any(c in data['password'] for c in '\r\n\x00')):
                    return data
                break
    raise ValueError('Desktop handoff unavailable; task must be running with desktop support')


def open_desktop(session_id):
    """Caller must authorize session access through the task API first."""
    remote = forward = viewer = None
    previous = {}
    def interrupt(*_):
        raise KeyboardInterrupt
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError('Invalid session ID')
        host = os.environ.get('HERMES_CRABBOX_SSH_HOST', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:-]{0,252}', host):
            raise ValueError('Set HERMES_CRABBOX_SSH_HOST to the authorized SSH host or user@host')
        if os.name != 'posix':
            raise ValueError('Desktop viewer helper requires macOS/Linux; use WSL on Windows')
        executable = os.environ.get('HERMES_CRABBOX_VIEWER') or shutil.which('crabbox')
        lsof = shutil.which('lsof')
        if not executable or not lsof or not shutil.which('ssh'):
            raise ValueError('Install Crabbox, OpenSSH and lsof for desktop viewing')
        binary = Path(executable).expanduser().resolve()
        info = binary.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022 or not os.access(binary, os.X_OK)):
            raise ValueError('Trusted local Crabbox installation required')
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, interrupt)
        command = ('sudo -n -u cloud-worker env PYTHONPATH=/opt/cloud-workbench/src '
                   '/opt/cloud-workbench/.venv/bin/python -m cloudworkbench.desktop_bridge '
                   + session_id)
        remote = subprocess.Popen([*SSH, host, command], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        data = _handoff(remote)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        forward = subprocess.Popen([*SSH, '-N', '-L',
            f'127.0.0.1:{port}:127.0.0.1:{data["port"]}', host],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if remote.poll() is not None or forward.poll() is not None:
                raise ValueError('Desktop SSH connection failed')
            owned = subprocess.run([lsof, '-nP', '-a', '-iTCP@127.0.0.1:' + str(port),
                '-sTCP:LISTEN', '-t'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
            if owned.returncode == 0 and owned.stdout.split() == [str(forward.pid).encode()]:
                break
            time.sleep(0.2)
        else:
            raise ValueError('Desktop SSH tunnel did not become ready')
        viewer = subprocess.Popen([str(binary), 'webvnc', 'local', '--vnc-host', '127.0.0.1',
            '--vnc-port', str(port), '--username', 'crabbox', '--password-stdin',
            '--security-type', 'vnc', '--redact-credentials=true', '--open'],
            stdin=subprocess.PIPE, start_new_session=True)
        viewer.stdin.write(data['password'].encode() + b'\n')
        viewer.stdin.close()
        data.clear()
        print('Desktop viewer starting. Keep this command running; Ctrl+C closes desktop access.')
        while remote.poll() is None and forward.poll() is None and viewer.poll() is None:
            time.sleep(0.5)
        if viewer.poll() == 0:
            return 0
        raise ValueError('Desktop disconnected; the task may have ended')
    except KeyboardInterrupt:
        return 0
    except Exception:
        raise ValueError('Desktop unavailable or disconnected; check task desktop support and SSH access') from None
    finally:
        for child in (viewer, forward, remote):
            _stop(child)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
