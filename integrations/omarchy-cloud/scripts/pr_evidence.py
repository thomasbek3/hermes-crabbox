"""Prepare task media locally; publish one PR evidence comment only with --publish."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

from omarchy_cloud import Client, ClientError, read_file, segment

MAX_ASSETS = 12
MAX_ASSET_BYTES = 50 * 1024**2
MAX_TOTAL_BYTES = 200 * 1024**2
MAX_MANIFEST_BYTES = 65536
SUFFIXES = {'.png', '.jpg', '.jpeg', '.mp4', '.webm'}
MANIFEST = 'pr-evidence/manifest.json'


class EvidenceError(ValueError):
    pass


def require(value, code):
    if not value:
        raise EvidenceError(code)


def identifier(value):
    require(type(value) is str and str(uuid.UUID(value)) == value, 'invalid_task_identity')
    return value


def pr_url(value):
    require(type(value) is str and re.fullmatch(
        r'https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,100}/pull/[1-9][0-9]*', value),
        'explicit_github_pull_request_url_required')
    return value


def plain(value, maximum):
    require(type(value) is str and len(value.encode()) <= maximum
            and all(ord(char) >= 32 or char in '\n\t' for char in value), 'invalid_manifest_text')
    text = html.escape(value, quote=True).replace('@', '&#64;')
    return re.sub(r'([\\`*_{}\[\]()#+.!|>])', r'\\\1', text)


def unique_pairs(items):
    value = {}
    for key, item in items:
        require(key not in value, 'duplicate_json_key')
        value[key] = item
    return value


def manifest_value(data):
    value = json.loads(data, object_pairs_hook=unique_pairs)
    require(type(value) is dict and set(value) == {'schema_version','assets','summary'}
            and type(value['schema_version']) is int and value['schema_version'] == 1,
            'unsupported_manifest')
    plain(value['summary'], 12000)
    assets = value['assets']
    require(type(assets) is list and 1 <= len(assets) <= MAX_ASSETS, 'invalid_asset_count')
    seen = set()
    for asset in assets:
        require(type(asset) is dict and set(asset) == {'path','label'}, 'invalid_asset')
        path = asset['path']
        require(type(path) is str and len(path) <= 240 and path.startswith('pr-evidence/')
                and re.fullmatch(r'[A-Za-z0-9_./-]+', path)
                and all(part not in ('','.','..') for part in path.split('/'))
                and Path(path).suffix.lower() in SUFFIXES and path not in seen, 'invalid_asset_path')
        require(type(asset['label']) is str and asset['label'].strip(), 'invalid_asset_label')
        plain(asset['label'], 500)
        seen.add(path)
    return value


def private_directory(path):
    absolute = Path(os.path.abspath(path))
    require(absolute.resolve() == absolute, 'symlink_output_path_refused')
    absolute.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = absolute.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022,
            'unsafe_output_directory')
    return absolute


def write_new(path, content, *, exclusive=False):
    if not exclusive and os.path.lexists(path):
        require(read_file(path, max(len(content), 1)) == content, 'existing_output_differs')
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def artifact_metadata(item, session, attempt, maximum):
    require(type(item) is dict and item.get('session_id') == session and item.get('attempt_id') == attempt,
            'artifact_task_mismatch')
    identifier(item.get('id'))
    require(type(item.get('bytes')) is int and 0 < item['bytes'] <= maximum
            and type(item.get('sha256')) is str and re.fullmatch(r'[0-9a-f]{64}', item['sha256']),
            'invalid_artifact_integrity_metadata')


def download(client, item, destination, maximum):
    if os.path.lexists(destination):
        data = read_file(destination, maximum)
        require(len(data) == item['bytes'] and hashlib.sha256(data).hexdigest() == item['sha256'],
                'existing_artifact_digest_mismatch')
        return
    fd, temporary = tempfile.mkstemp(prefix='.evidence-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as stream, client.request('GET', '/artifacts/'+segment(item['id'])+'/content') as response:
            require(response.headers.get('Content-Encoding','identity') == 'identity', 'encoded_artifact_refused')
            length = response.headers.get('Content-Length')
            require(length is not None and str(item['bytes']) == length, 'artifact_length_header_mismatch')
            etag = response.headers.get('ETag','').strip('"')
            require(etag == item['sha256'], 'artifact_digest_header_mismatch')
            digest, total = hashlib.sha256(), 0
            while True:
                chunk = response.read(min(128*1024, maximum+1-total))
                if not chunk: break
                total += len(chunk)
                require(total <= maximum and total <= item['bytes'], 'artifact_download_too_large')
                digest.update(chunk)
                stream.write(chunk)
            require(total == item['bytes'] and digest.hexdigest() == item['sha256'], 'artifact_digest_mismatch')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)


def media_signature(path, suffix):
    with path.open('rb') as stream: head = stream.read(32)
    valid = {'.png':head.startswith(b'\x89PNG\r\n\x1a\n'),
             '.jpg':head.startswith(b'\xff\xd8\xff'), '.jpeg':head.startswith(b'\xff\xd8\xff'),
             '.mp4':len(head) >= 12 and head[4:8] == b'ftyp',
             '.webm':head.startswith(b'\x1a\x45\xdf\xa3')}
    require(valid[suffix], 'media_signature_mismatch')


def prepare(client, session, pr, output_dir):
    identifier(session)
    if pr is not None:
        pr_url(pr)
    snapshot = client.json('GET', '/sessions/'+segment(session))
    attempts = snapshot.get('attempts', [])
    require(type(attempts) is list and attempts, 'task_has_no_attempt')
    attempt = attempts[-1]
    attempt_id = identifier(attempt.get('id'))
    require(attempt.get('state') in ('completed','failed','cancelled','interrupted'), 'task_not_terminal')
    listing = client.json('GET', '/sessions/'+segment(session)+'/artifacts').get('artifacts')
    require(type(listing) is list and len(listing) <= 10000, 'invalid_artifact_listing')
    by_path = {}
    for item in listing:
        require(type(item) is dict, 'invalid_artifact_listing')
        if item.get('attempt_id') != attempt_id: continue
        path = item.get('path')
        require(type(path) is str and path not in by_path, 'ambiguous_artifact_path')
        by_path[path] = item
    require(MANIFEST in by_path, 'evidence_manifest_missing_from_latest_attempt')
    artifact_metadata(by_path[MANIFEST], session, attempt_id, MAX_MANIFEST_BYTES)
    root = private_directory(private_directory(output_dir)/session/attempt_id)
    local_manifest = root/'manifest.json'
    download(client, by_path[MANIFEST], local_manifest, MAX_MANIFEST_BYTES)
    manifest = manifest_value(read_file(local_manifest, MAX_MANIFEST_BYTES))
    total = 0
    for asset in manifest['assets']:
        require(asset['path'] in by_path, 'manifest_asset_not_exported')
        metadata = by_path[asset['path']]
        artifact_metadata(metadata, session, attempt_id, MAX_ASSET_BYTES)
        total += metadata['bytes']
    require(total <= MAX_TOTAL_BYTES, 'total_media_limit_exceeded')
    asset_dir = private_directory(root/'assets')
    local_assets = []
    body = [f'<!-- omarchy-evidence:{session}:{attempt_id} -->', '## Task evidence',
            f'Task `{session}` · attempt `{attempt_id}` · state **{attempt["state"]}**.',
            'Worker-provided evidence; this comment does not certify that checks passed.',
            plain(manifest['summary'],12000)]
    for index, asset in enumerate(manifest['assets'],1):
        name = f'{index:02d}-'+Path(asset['path']).name
        destination = asset_dir/name
        download(client, by_path[asset['path']], destination, MAX_ASSET_BYTES)
        media_signature(destination, destination.suffix.lower())
        label = plain(asset['label'],500).replace('\n',' ').replace('\t',' ')
        relative = './assets/'+name
        body.append(f'### {label}')
        if destination.suffix.lower() in ('.png','.jpg','.jpeg'):
            body.append(f'![{label}]({relative})')
        else:
            body.append(f'![]({relative})')
        local_assets.append({'path':relative,'sha256':by_path[asset['path']]['sha256'],
                             'bytes':by_path[asset['path']]['bytes']})
    content = ('\n\n'.join(body)+'\n').encode()
    require(len(content) <= 65536, 'escaped_comment_too_large')
    write_new(root/'comment.md', content)
    receipt = {'session_id':session,'attempt_id':attempt_id,'pr':pr,'state':'prepared',
               'directory':str(root),'body_file':'comment.md','body_sha256':hashlib.sha256(content).hexdigest(),
               'assets':local_assets}
    write_new(root/'prepared.json', (json.dumps({**receipt,'pr':None},indent=2)+'\n').encode())
    if pr is not None:
        target_file = 'prepared-'+hashlib.sha256(pr.encode()).hexdigest()[:16]+'.json'
        write_new(root/target_file, (json.dumps(receipt,indent=2)+'\n').encode())
    return receipt


def gh_call(arguments, cwd):
    try:
        result = subprocess.run(['gh', *arguments], cwd=cwd, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=180,
            env={**os.environ,'GH_PROMPT_DISABLED':'1','GH_PAGER':'cat'})
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError('github_cli_unavailable_or_timed_out') from None
    require(result.returncode == 0, 'github_cli_failed_inspect_before_retry')
    require(len(result.stdout.encode()) <= 1024*1024, 'github_response_too_large')
    return result.stdout


def publish(prepared, call=gh_call):
    root = private_directory(Path(prepared['directory']))
    url = pr_url(prepared['pr'])
    require(not os.path.lexists(root/'publication.json'), 'publication_already_attempted_inspect_receipt_and_pr')
    version = call(['--version'], root)
    match = re.search(r'gh version (\d+)\.(\d+)\.(\d+)', version)
    require(match and tuple(map(int,match.groups())) >= (2,99,0), 'github_cli_2_99_or_newer_required')
    require('--attach' in call(['pr','comment','--help'],root), 'github_attachment_support_required')
    observed = json.loads(call(['pr','view',url,'--json','url'],root))
    require(type(observed) is dict and observed.get('url','').lower() == url.lower(), 'github_pr_identity_mismatch')
    body = read_file(root/'comment.md',65536)
    require(hashlib.sha256(body).hexdigest() == prepared['body_sha256'], 'prepared_body_changed')
    command = ['pr','comment',url,'--body-file','comment.md']
    for asset in prepared['assets']:
        path = root/asset['path']
        require(path.resolve().parent == (root/'assets').resolve() and not (root/'assets').is_symlink(), 'unsafe_prepared_asset')
        data = read_file(path,MAX_ASSET_BYTES)
        require(len(data) == asset['bytes'] and hashlib.sha256(data).hexdigest() == asset['sha256'], 'prepared_asset_changed')
        command += ['--attach',asset['path']]
    intent = {'state':'publication_started','pr':url,'session_id':prepared['session_id'],
              'attempt_id':prepared['attempt_id'],'note':'Do not retry blindly after an uncertain failure.'}
    write_new(root/'publication.json',(json.dumps(intent,indent=2)+'\n').encode(),exclusive=True)
    response = call(command,root).strip()
    require(re.fullmatch(re.escape(url)+r'#issuecomment-[0-9]+',response), 'publication_result_uncertain_inspect_pr')
    result = {**intent,'state':'published','comment_url':response}
    write_new(root/'published.json',(json.dumps(result,indent=2)+'\n').encode())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session_id')
    parser.add_argument('--pr',help='Explicit GitHub PR URL; required only with --publish')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--publish',action='store_true')
    args = parser.parse_args(argv)
    try:
        identifier(args.session_id)
        if args.pr is not None:
            pr_url(args.pr)
        require(not args.publish or args.pr is not None, 'publish_requires_explicit_pr_url')
        client = Client(os.environ.get('OMARCHY_CLOUD_SERVER','https://omarchy.tail0d5eb6.ts.net'),
            Path(os.environ.get('OMARCHY_CLOUD_TOKEN_FILE','~/.config/omarchy-cloud/token')).expanduser())
        result = prepare(client,args.session_id,args.pr,args.output_dir)
        if args.publish: result = publish(result)
        print(json.dumps(result,indent=2))
        return 0
    except EvidenceError as exc:
        print('pr-evidence: '+str(exc),file=sys.stderr)
        return 1
    except (ClientError,OSError,ValueError,KeyError,TypeError):
        print('pr-evidence: preparation/publication failed; inspect private output and PR before retrying.',file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
