"""Loopback-only API process; it has no Docker group or provider credential access."""
import argparse
import json
import os
import math
from pathlib import Path
import time
import uvicorn
from .api import create_app
from .store import Store


def worker_readiness(config, *, now=None):
    try:
        with (Path(config['state_root']) / 'heartbeat.json').open('rb') as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError('Oversized heartbeat')
        data = json.loads(raw)
        stamp = data['timestamp']
        errors = data['errors']
        available = data['available_agents']
        blocked = data['blocked_agents']
        if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not math.isfinite(stamp)
                or not isinstance(errors, list) or not isinstance(available, list)
                or any(not isinstance(agent, str) for agent in available)
                or not isinstance(blocked, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in blocked.items())):
            raise ValueError('Invalid heartbeat')
        age = (time.time() if now is None else now) - stamp
        configured = config.get('agents', [])
        usable = sorted(set(available).intersection(configured).difference(blocked))
        if not config.get('test_mode', False):
            usable = [agent for agent in usable if agent != 'fixture']
        if not config.get('claude_enabled', False):
            usable = [agent for agent in usable if agent != 'claude']
        if config.get('hermes_enabled') is not True:
            usable = [agent for agent in usable if agent != 'hermes']
        ok = 0 <= age < 15 and not errors and bool(usable)
        return {'ready': ok, 'release': 'pre-release',
                'worker': {'timestamp': stamp, 'errors': errors,
                           'available_agents': usable, 'blocked_agents': blocked},
                'provider_enabled': bool({'claude', 'hermes'}.intersection(usable)),
                'reason': None if ok else 'Worker stale, unhealthy, or no adapter ready'}
    except (OSError, ValueError, KeyError, TypeError):
        return {'ready': False, 'reason': 'Worker heartbeat unavailable or invalid'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    os.umask(0o007)
    config = json.loads(Path(args.config).read_text())
    if config.get('bind', '127.0.0.1') != '127.0.0.1' or config.get('port', 7780) == 7777:
        raise ValueError('Initial API requires loopback and a separate port')
    def ready():
        return worker_readiness(config)
    settings = {**config, 'readiness':ready}
    uvicorn.run(create_app(Store(Path(config['database']), shared_group=True), settings), host='127.0.0.1', port=config.get('port',7780), access_log=False)


if __name__ == '__main__':
    main()
