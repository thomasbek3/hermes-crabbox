"""Private SSH-pipe handoff for a running task's Crabbox desktop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

from cloudworkbench.crabbox_runtime import CrabboxRuntime


def stop(child):
    if child is None:
        return
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


def current_attempt(db, session_id):
    row = db.execute(
        'SELECT a.* FROM attempts a JOIN sessions s ON s.id=a.session_id '
        'WHERE s.id=? AND s.archived=0 ORDER BY a.generation DESC LIMIT 1',
        (session_id,),
    ).fetchone()
    if (row is None or row['agent'] != 'hermes' or row['state'] != 'running'
            or row['cancel_requested']):
        raise ValueError('Desktop requires a running Hermes task')
    return row


def main():
    child = None
    try:
        if len(sys.argv) != 2 or str(uuid.UUID(sys.argv[1])) != sys.argv[1]:
            raise ValueError('Invalid session')
        if not all(not stream.isatty() and (stat.S_ISFIFO(os.fstat(stream.fileno()).st_mode)
                   or stat.S_ISSOCK(os.fstat(stream.fileno()).st_mode))
                   for stream in (sys.stdout, sys.stdin)):
            raise ValueError('Private pipe required')
        session_id = sys.argv[1]
        config = json.loads(Path('/etc/cloud-workbench/worker.json').read_text())
        runtime = CrabboxRuntime({**config['runtime'], **config['hermes_runtime'],
            'hermes_journal_root': str(Path(config['state_root']) / 'hermes-runtime')})
        with sqlite3.connect('file:/var/lib/cloud-workbench/control/state.db?mode=ro',
                             uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            attempt = current_attempt(db, session_id)
            rid = attempt['runtime_id']
            record = runtime.cb_record(rid, attempt['generation'])
            if (not record or record['session'] != session_id
                    or record['attempt'] != attempt['id'] or record.get('desktop') is not True
                    or record.get('phase') != 'executing' or not runtime.cb_alive(record)):
                raise ValueError('Desktop unavailable for this attempt')
            env = runtime.cb_env(rid)
            child = subprocess.Popen(
                [runtime.crabbox, 'vnc', '--provider', 'local-container', '--id', rid,
                 '--native-handoff'], env=env, cwd=env['HOME'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ, 'handoff')
                selector.register(sys.stdin.buffer, selectors.EVENT_READ, 'parent')
                deadline = time.monotonic() + 60
                packet = bytearray()
                sent = False
                while child.poll() is None:
                    if not sent and time.monotonic() >= deadline:
                        raise ValueError('Desktop handoff timed out')
                    latest = current_attempt(db, session_id)
                    if (latest['id'], latest['generation'], latest['runtime_id']) != (
                            attempt['id'], attempt['generation'], rid):
                        raise ValueError('Desktop task changed')
                    for key, _ in selector.select(timeout=1):
                        if key.data == 'parent':
                            if not os.read(sys.stdin.fileno(), 1):
                                return 0
                            raise ValueError('Unexpected parent input')
                        chunk = os.read(child.stdout.fileno(), 8193 - len(packet))
                        if not chunk:
                            raise ValueError('Desktop handoff ended')
                        packet.extend(chunk)
                        if len(packet) > 8192:
                            raise ValueError('Invalid desktop handoff')
                        if b'\n' not in packet:
                            continue
                        data = json.loads(packet)
                        if (data.get('schema') != 'crabbox/vnc-handoff/v1'
                                or data.get('host') != '127.0.0.1'
                                or type(data.get('port')) is not int or not 1 <= data['port'] <= 65535
                                or not isinstance(data.get('username'), str) or len(data['username']) > 256
                                or not isinstance(data.get('password'), str)
                                or not 1 <= len(data['password']) <= 4096
                                or any(c in data['password'] for c in '\r\n\x00')):
                            raise ValueError('Invalid desktop handoff')
                        sys.stdout.write(json.dumps(data, separators=(',', ':')) + '\n')
                        sys.stdout.flush()
                        packet.clear()
                        data.clear()
                        selector.unregister(child.stdout)
                        sent = True
                raise ValueError('Desktop tunnel ended')
    except (Exception, KeyboardInterrupt):
        print('Desktop unavailable or disconnected; task must be running with desktop support.',
              file=sys.stderr)
        return 1
    finally:
        stop(child)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
    raise SystemExit(main())
