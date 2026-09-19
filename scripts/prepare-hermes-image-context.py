#!/usr/bin/env python3
"""Prepare pinned tracked source only; never reads personal Hermes state."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

HERMES_COMMIT='3b0e392e5a6922034feccac5771041ac78467757'
PSTACK_COMMIT='204e77a7a011c4613dc9c4913a481d77cc0ebe54'

def tracked_archive(source,commit):
    def git(*args):
        return subprocess.check_output(['git','-C',str(source),*args],timeout=120)
    if git('rev-parse','HEAD').decode().strip()!=commit:
        raise ValueError('Source revision differs from reviewed pin')
    if git('status','--porcelain','--untracked-files=no').strip():
        raise ValueError('Tracked source is modified')
    data=git('archive','--format=tar',commit)
    if len(data)>256*1024*1024:
        raise ValueError('Source archive exceeds build-context budget')
    files={}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for item in archive:
            name=Path(item.name)
            if name.is_absolute() or '..' in name.parts or item.issym() or item.islnk():
                raise ValueError('Unsafe tracked archive entry')
            if item.isdir():continue
            if not item.isfile() or item.size>32*1024*1024:
                raise ValueError('Unsupported tracked archive entry')
            if name.name in {'.env','auth.json','.credentials.json','config.yaml'}:
                raise ValueError('Credential/config filename in source archive')
            files[item.name]=(archive.extractfile(item).read(),item.mode & 0o111 != 0)
    return files

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes',type=Path,required=True)
    parser.add_argument('--pstack',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    sources={'hermes':tracked_archive(args.hermes,HERMES_COMMIT),'pstack':tracked_archive(args.pstack,PSTACK_COMMIT)}
    args.destination.mkdir(mode=0o700)
    hashes={}
    for kind,files in sources.items():
        for name,(data,executable) in files.items():
            path=args.destination/kind/name
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as output:output.write(data)
            path.chmod(0o755 if executable else 0o644)
            hashes[kind+'/'+name]=hashlib.sha256(data).hexdigest()
    receipt={'hermes_commit':HERMES_COMMIT,'pstack_commit':PSTACK_COMMIT,'scope':'git tracked archives only; no installed auth/config/runtime state','files_sha256':hashes,'total_bytes':sum(len(data) for f in sources.values() for data,_ in f.values())}
    (args.destination/'source-manifest.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
    print(json.dumps({'files':len(hashes),'total_bytes':receipt['total_bytes'],'manifest_sha256':hashlib.sha256((args.destination/'source-manifest.json').read_bytes()).hexdigest()}))

if __name__=='__main__':main()
