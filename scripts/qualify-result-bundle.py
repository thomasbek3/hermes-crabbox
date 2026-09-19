#!/usr/bin/env python3
"""Read-only HTTPS bundle qualifier; private local files only, no remote mutation."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import zipfile

from cloudworkbench.cli import Client, ClientError, segment
from cloudworkbench.models import TERMINAL
from cloudworkbench.result_bundle import MAX_BYTES, MAX_FILES


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def inspect_archive(blob, session_id, attempt_id):
    require(len(blob) <= MAX_BYTES, 'bundle size limit')
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        require(len(names) == len(set(names)) and len(names) <= MAX_FILES, 'bundle entry limit or duplicate')
        require(all(entry.compress_type == zipfile.ZIP_STORED and entry.file_size <= MAX_BYTES and
                    stat.S_IMODE(entry.external_attr >> 16) == 0o600 for entry in entries), 'bundle entry encoding or permissions')
        require(sum(entry.file_size for entry in entries) <= MAX_BYTES, 'bundle expanded size')
        result = json.loads(archive.read('result.json'))
        manifest_bytes = archive.read('artifact-manifest.json')
        manifest = json.loads(manifest_bytes)
        receipt = json.loads(archive.read('verification.json'))
        require(result['session_id'] == session_id and result['attempt_id'] == attempt_id and result['state'] in TERMINAL,
                'bundle snapshot identity')
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        require(result['artifact_manifest_sha256'] == receipt['artifact_manifest_sha256'] == digest, 'manifest binding')
        expected = {'result.json', 'summary.md', 'artifact-manifest.json', 'verification.json'}
        for item in manifest:
            name = 'files/' + item['path']
            data = archive.read(name)
            require(len(data) == item['bytes'] and hashlib.sha256(data).hexdigest() == item['sha256'], 'artifact digest')
            expected.add(name)
        require(set(names) == expected and len(manifest) == result['artifact_count'], 'bundle inventory')
        return {'session_id': session_id, 'attempt_id': attempt_id, 'state': result['state'],
                'outcome': result['outcome'], 'artifact_count': len(manifest), 'entries': len(names),
                'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest(), 'artifact_manifest_sha256': digest}


def qualify(client, denied_clients, session_id, destination):
    route = '/sessions/' + segment(session_id) + '/bundle'
    session = client.json('GET', '/sessions/' + segment(session_id))
    latest = max(session['attempts'], key=lambda value: value['generation'])
    require(latest['state'] in TERMINAL, 'selected session is not terminal')
    for denied in denied_clients:
        try:
            with denied.request('GET', route):
                raise ValueError('denial credential unexpectedly retrieved bundle')
        except ClientError as exc:
            require(str(exc).startswith('HTTP 404:'), 'expected owner/project denial was not 404')
    with client.request('GET', route) as response:
        blob = response.read(MAX_BYTES + 1)
        require(response.headers.get('Content-Type') == 'application/zip', 'bundle media type')
        require(response.headers.get('Content-Disposition', '').startswith('attachment;'), 'bundle attachment')
        require(response.headers.get('Content-Length') == str(len(blob)), 'bundle response length')
        require(response.headers.get('ETag') == '"' + hashlib.sha256(blob).hexdigest() + '"', 'bundle response digest')
        require(response.headers.get('X-Cloud-Attempt-Id') == latest['id'] and
                response.headers.get('X-Cloud-Session-Id') == session_id, 'bundle response identity')
    receipt = inspect_archive(blob, session_id, latest['id'])
    output = destination / 'result.zip'
    client.bundle(session_id, output)
    require(output.read_bytes() == blob, 'snapshot changed between GET and CLI; choose a quiescent session')
    try:
        client.bundle(session_id, output)
        raise ValueError('CLI overwrote existing file')
    except FileExistsError:
        pass
    require(output.read_bytes() == blob and not list(destination.glob('.cloud2-download-*')), 'CLI no-clobber cleanup')
    receipt.update(owner_project_denials='passed', cli_atomic_no_clobber='passed', deterministic_retrieval='passed',
                   remote_mutations=False, concurrent_followup='local regression only; not performed live')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--other-owner-token-file', required=True, type=Path)
    parser.add_argument('--excluded-project-token-file', required=True, type=Path)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        require(args.server.startswith('https://'), 'HTTPS is required')
        client = Client(args.server, args.token_file)
        denied = [Client(args.server, path) for path in (args.other_owner_token_file, args.excluded_project_token_file)]
        require(len({client.token, *(item.token for item in denied)}) == 3, 'three distinct credentials required')
        args.output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        receipt = qualify(client, denied, args.session_id, args.output_dir)
        (args.output_dir/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
        print('Bundle qualification passed; private receipt written.')
        return 0
    except (ClientError, ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile):
        # Remote diagnostics and content may contain sensitive values.
        print('Bundle qualification failed; no success receipt produced.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
