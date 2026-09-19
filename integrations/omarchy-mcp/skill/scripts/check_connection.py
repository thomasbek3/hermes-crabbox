#!/usr/bin/env python3
"""Read-only HTTP caller check. JSON output never includes task data or credentials."""
import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError

from omarchy_cloud import Client, ClientError, connection_defaults


def check(server, token_file, timeout=10):
    try:
        client = Client(server, token_file, timeout)
    except (OSError, ValueError, ClientError):
        return {'status': 'blocked', 'code': 'configuration',
                'next': 'Check the HTTPS origin and service credential. Use OMARCHY_CLOUD_TOKEN or an owner-only regular token file (mode 600).'}
    try:
        # Use the client's no-proxy, no-redirect transport, but never echo error bodies.
        from urllib.request import Request
        request = Request(client.server + '/v1/sessions?limit=1&offset=0',
                          headers={'Authorization': 'Bearer ' + client.token, 'Accept': 'application/json'})
        with client.opener.open(request, timeout=timeout) as response:
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError()
        data = json.loads(payload)
        if not isinstance(data, dict) or not isinstance(data.get('sessions'), list):
            raise ValueError()
    except HTTPError as error:
        code = error.code
        error.close()
        if code in (401, 403):
            return {'status': 'blocked', 'code': 'access', 'http_status': code,
                    'next': 'Ask the host operator to check this caller credential and scopes; provider and Tailscale keys do not authenticate the task API.'}
        return {'status': 'blocked', 'code': 'http', 'http_status': code,
                'next': 'Check the selected API origin and task service with the operator.'}
    except (URLError, TimeoutError, OSError):
        return {'status': 'blocked', 'code': 'network',
                'next': 'Check Tailscale connectivity on the machine actually executing these tools, DNS, and the host service.'}
    except ClientError:
        return {'status': 'blocked', 'code': 'redirect',
                'next': 'Redirect refused. Ask the operator for the direct HTTPS API origin.'}
    except (ValueError, UnicodeError):
        return {'status': 'blocked', 'code': 'response',
                'next': 'The endpoint did not return the task API sessions schema; check the API origin.'}
    return {'status': 'ready', 'code': 'http_read_access',
            'verified': ['HTTP reachability', 'credential accepted for task listing'],
            'not_verified': ['MCP protocol', 'task submission', 'worker execution', 'model access', 'desktop viewing'],
            'next': 'For MCP, register the server and read get_delegation_guide. Run a small worker task only with authorization to use model quota.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', help='Explicit HTTP API origin; omit to use installed settings')
    parser.add_argument('--token-file', type=Path, default=Path(os.environ.get(
        'OMARCHY_CLOUD_TOKEN_FILE', '~/.config/omarchy-cloud/token')).expanduser())
    args = parser.parse_args(argv)
    try:
        server = args.server or connection_defaults()['server']
        result = check(server, args.token_file)
    except (OSError, ValueError, ClientError):
        result = {'status': 'blocked', 'code': 'configuration',
                  'next': 'Repair installed connection.json or supply an explicit --server.'}
    print(json.dumps(result))
    return 0 if result['status'] == 'ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
