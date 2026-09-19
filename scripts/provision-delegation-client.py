#!/usr/bin/env python3
"""Run with Omarchy operator privileges. Write one private service credential; never print it."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
from cloudworkbench.store import Store


def main():
    p=argparse.ArgumentParser();p.add_argument('name');args=p.parse_args()
    if os.geteuid()!=0 or socket.gethostname()!='omarchy':
        raise SystemExit('Run as the operator on Omarchy.')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', args.name):
        raise SystemExit('Name must use lowercase letters, numbers and hyphens, max40 characters.')
    os.umask(0o077)
    root=Path('/var/lib/cloud-workbench/delegation-clients');root.mkdir(mode=0o700,exist_ok=True)
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o077:
        raise SystemExit('Unsafe credential directory.')
    path=root/(args.name+'.token')
    cfg=json.loads(Path('/etc/cloud-workbench/api.json').read_text())
    store=Store(Path(cfg['database']),shared_group=True)
    if path.exists() or path.is_symlink():
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o077 or info.st_nlink!=1 or info.st_size>1024:
                raise SystemExit('Unsafe existing credential file.')
            token=os.read(fd,1024).decode().strip()
        finally:os.close(fd)
        principal=store.authenticate(token)
        if not principal or principal['name']!='delegation-'+args.name:
            raise SystemExit('Existing credential is revoked/unregistered or belongs to another client; no replacement was created.')
    else:
        token=secrets.token_urlsafe(32)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:f.write(token+'\n')
        principal=store.add_client('delegation-'+args.name,token,['submit','observe','retrieve','cancel'],['hermes-tasks'])
    print(json.dumps({'client_id':principal['id'],'name':principal['name'],'credential_file':str(path),'scopes':principal['scopes'],'projects':principal['projects'],'secret_printed':False}))

if __name__=='__main__':main()
