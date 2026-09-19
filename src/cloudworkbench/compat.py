"""Opt-in legacy verbs over v2; never replaces the running v1 endpoint."""
import argparse
import json
import hashlib
import stat
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

from .cli import Client, ClientError, segment
from .models import TERMINAL, LIVE


def latest(snapshot):
    attempts = snapshot.get('attempts')
    if not isinstance(attempts,list) or not attempts:
        raise ClientError('session has no valid attempt')
    attempt = max(attempts,key=lambda a:a['generation'])
    if attempt.get('state') not in TERMINAL | LIVE | {'queued'}:
        raise ClientError('unknown attempt state')
    return attempt


def logs(client, sid):
    cursor, total = 0, 0
    for _ in range(100):
        with client.request('GET',f'/sessions/{segment(sid)}/events?follow=false&after={cursor}') as response:
            if not response.headers.get('Content-Type','').startswith('text/event-stream'):
                raise ClientError('expected event stream')
            body=response.read(8*1024**2+1)
        total += len(body)
        if len(body)>8*1024**2 or total>32*1024**2:
            raise ClientError('log exceeds compatibility client limit')
        previous=cursor
        for line in body.decode('utf8').splitlines():
            if line.startswith('data: '):
                event=json.loads(line[6:]); sequence=event['sequence']
                if not isinstance(sequence,int) or sequence<=cursor:
                    raise ClientError('invalid event sequence')
                cursor=sequence
                print(client.clean(json.dumps(event,ensure_ascii=False)))
        if cursor==previous:
            return
    raise ClientError('log exceeds event page limit')


def parser():
    p=argparse.ArgumentParser(prog='cloud-compat')
    p.add_argument('--config',type=Path,default=Path(os.environ.get('CLOUD_COMPAT_CONFIG','~/.config/cloud2/compat.json')).expanduser())
    p.add_argument('--timeout',type=float,default=30)
    sub=p.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('agent');run.add_argument('task')
    for name in ('repo','branch','name','model','provider','idempotency-key'):
        run.add_argument('--'+name)
    sub.add_parser('ls')
    for command in ('status','wait','log','file','rm'):
        cmd=sub.add_parser(command);cmd.add_argument('id')
        if command=='file': cmd.add_argument('path')
        if command=='wait': cmd.add_argument('--max-seconds',type=float,default=3600)
        if command=='rm': cmd.add_argument('--confirm',action='store_true')
    return p


def main(argv=None):
    args=parser().parse_args(argv);client=None
    try:
        if args.command=='rm' and not args.confirm:
            raise ClientError('rm requires --confirm for purge; it never means cancel or archive')
        if args.command=='wait' and not 0<args.max_seconds<=86400:
            raise ClientError('wait limit must be between0 and86400 seconds')
        fd=os.open(args.config,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as source:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o022 or info.st_size>65536:
                raise ClientError('configuration must be owner-owned, regular and not writable by others')
            config=json.loads(source.read(65537))
        client=Client(config['server'],Path(config['token_file']).expanduser(),args.timeout)
        command=args.command
        if command=='run':
            if args.provider or args.name:
                raise ClientError('--provider and --name are unsupported; no silent substitution')
            route=config['default']
            if args.repo:
                route=config.get('repositories',{}).get(args.repo,{}).get(args.branch or '')
                if not route:
                    raise ClientError('repository/ref is not registered; no ad-hoc clone')
            elif args.branch:
                raise ClientError('--branch requires a registered --repo')
            payload={'project_id':route['project_id'],'environment_version':route['environment_version'],
                     'agent':args.agent,'goal':args.task}
            if args.model: payload['model']=args.model
            result=client.json('POST','/sessions',payload,args.idempotency_key or str(uuid.uuid4()))
            sid=result.get('id') or result.get('session_id')
            if not isinstance(sid,str) or not sid: raise ClientError('missing session identifier')
            print(client.clean(sid));return 0
        if command=='ls':
            offset=0
            for _ in range(100):
                rows=client.json('GET',f'/sessions?limit=100&offset={offset}')['sessions']
                for row in rows: print(client.clean(json.dumps(row,ensure_ascii=False)))
                if len(rows)<100: return 0
                offset+=len(rows)
            raise ClientError('listing limit exceeded; use cloud2 pagination')
        base=f'/sessions/{segment(args.id)}'
        if command=='rm':
            client.json('DELETE',base,{},str(uuid.uuid4()));return 0
        if command=='log': logs(client,args.id);return 0
        if command in ('status','wait'):
            deadline=time.monotonic()+(args.max_seconds if command=='wait' else 0)
            while True:
                attempt=latest(client.json('GET',base))
                if command=='status' or attempt['state'] in TERMINAL:
                    print(client.clean(json.dumps(attempt,ensure_ascii=False)))
                    if command=='wait': logs(client,args.id)
                    return int(attempt['state'] in {'failed','cancelled','interrupted','paused'} or attempt.get('outcome')=='rejected')
                remaining=deadline-time.monotonic()
                if remaining<=0: raise ClientError('wait deadline reached; remote task remains unchanged')
                time.sleep(min(2,remaining))
        if command=='file':
            attempt=latest(client.json('GET',base))
            rows=client.json('GET',base+'/artifacts')['artifacts']
            matches=[a for a in rows if a['attempt_id']==attempt['id'] and a['path']==args.path]
            if len(matches)!=1: raise ClientError('path is not a unique exported artifact of latest attempt')
            with tempfile.TemporaryDirectory(prefix='cloud-compat-') as directory:
                path=Path(directory)/'artifact';client.download(matches[0]['id'],path)
                with path.open('rb') as source:
                    digest=hashlib.file_digest(source,'sha256').hexdigest()
                if digest!=matches[0]['sha256']:
                    raise ClientError('artifact does not match listing digest')
                with path.open('rb') as source:
                    while chunk:=source.read(65536): sys.stdout.buffer.write(chunk)
            return 0
    except (ClientError,OSError,ValueError,KeyError,TypeError) as exc:
        message=str(exc) if isinstance(exc,ClientError) else type(exc).__name__
        print('cloud-compat: '+(client.clean(message) if client else message),file=sys.stderr);return 1
    except KeyboardInterrupt:
        print('cloud-compat: disconnected; remote work continues',file=sys.stderr);return 130


if __name__=='__main__': sys.exit(main())
