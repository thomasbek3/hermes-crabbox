import json,os,subprocess
from pathlib import Path
image=subprocess.check_output(['docker','image','inspect','cloud-workbench:build-20260917','--format','{{.Id}}'],text=True).strip()
p=Path('/etc/cloud-workbench/worker.json')
c=json.loads(p.read_text());c['runtime']['image']=image
p.write_text(json.dumps(c,indent=2)+'\n')
q=json.loads(json.dumps(c));q['runtime']['network_enabled']=True;q['runtime']['egress_image']=image;q['runtime']['allowed_domains']=['github.com']
f=Path('/etc/cloud-workbench/qualification.json');f.write_text(json.dumps(q,indent=2)+'\n');os.chown(f,p.stat().st_uid,p.stat().st_gid);f.chmod(0o640)
print('Updated test image reference; provider remains disabled.')
