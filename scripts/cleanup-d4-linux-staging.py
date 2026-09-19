"""Remove only recorded D4 staging paths after exact ownership/digest checks."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

stage,digest,run=sys.argv[1:]
assert os.geteuid()==0 and os.uname().nodename=='omarchy'
assert re.fullmatch(r'cwb-d4-input-[0-9a-f]{12}',stage)
assert re.fullmatch(r'[0-9a-f]{64}',digest)
assert re.fullmatch(r'cwb-d4-linux-[0-9a-f]{12}',run)
records=[]
for parent,uid in [('/tmp',1000),('/var/tmp',0)]:
    path=Path(parent)/stage;info=path.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid==uid and stat.S_IMODE(info.st_mode)==0o700
    fd=os.open(path/'manifest.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        file_info=os.fstat(source.fileno());assert stat.S_ISREG(file_info.st_mode) and file_info.st_nlink==1 and file_info.st_size<=65536
        raw=source.read(65537);assert len(raw)<=65536
    actual=hashlib.sha256(raw).hexdigest();assert actual==digest
    records.append({'path':str(path),'uid':info.st_uid,'mode':oct(stat.S_IMODE(info.st_mode)),'device':info.st_dev,'inode':info.st_ino,'manifest_sha256':actual,'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
for record in records:
    path=Path(record['path']);info=path.lstat();assert (info.st_dev,info.st_ino)==(record['device'],record['inode'])
    shutil.rmtree(path);record['removed']=not path.exists();assert record['removed']
result=subprocess.run(['docker','ps','-aq','--filter','label=cwb.proof.owner=cloud-workbench-d4-permission-proof','--filter','label=cwb.proof.run='+run],capture_output=True,text=True,check=True)
assert not result.stdout.strip()
print(json.dumps({'host':os.uname().nodename,'effective_uid':os.geteuid(),'command':['python3',*sys.argv],'run_id':run,'paths':records,'remaining_exact_run_containers':[],'ended_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
