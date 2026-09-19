#!/usr/bin/env python3
"""Scoped live HTTPS proof; two disposable TEST clients, revoked in finally."""
from pathlib import Path
import datetime,hashlib,json,os,runpy,secrets,socket,sqlite3,tempfile
from cloudworkbench.store import Store,now
from cloudworkbench.cli import Client

os.umask(0o077)
assert socket.gethostname()=='omarchy' and os.geteuid()==0
stage=Path('/tmp/cwb-p15-api-20260917/snapshot')
deployment=json.loads((stage.parent/'p15-api-deployment-receipt.json').read_text());assert deployment['passed']
for name,digest in deployment['published_readback_sha256'].items():assert hashlib.sha256((Path('/opt/cloud-workbench')/name).read_bytes()).hexdigest()==digest
config=json.loads(Path('/etc/cloud-workbench/api.json').read_text());origin=config['dashboard_origin'];assert origin.startswith('https://')
store=Store(Path(config['database']), shared_group=True)
def clients():
 with store._connect() as db:return [dict(row) for row in db.execute('SELECT id,name,projects,scopes,revoked_at FROM clients ORDER BY id')]
before=clients();created=[];receipt={'host':socket.gethostname(),'started_at':now(),'passed':False,'provider_called':False,'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'deployment_receipt_sha256':hashlib.sha256((stage.parent/'p15-api-deployment-receipt.json').read_bytes()).hexdigest()}
output=Path('/var/lib/cloud-workbench/operator-qualifications/p15-api-20260917');output.mkdir(mode=0o700,parents=True,exist_ok=False)
try:
 with tempfile.TemporaryDirectory(prefix='credentials-',dir=output) as temporary:
  private=Path(temporary);owner_path=private/'owner.token';owner_path.write_bytes(Path('/var/lib/cloud-workbench/control/client.token').read_bytes());owner_path.chmod(0o600)
  owner=Client(origin,owner_path);principal=store.authenticate(owner.token);assert principal
  denied=[]
  for label,projects in [('other-owner',['sample-repo']),('excluded-project',['sample-web'])]:
   token=secrets.token_urlsafe(48);p=store.add_client('TEST P15 '+label+' 20260917',token,['observe','retrieve'],projects);created.append(p['id'])
   path=private/(label+'.token');path.write_text(token);path.chmod(0o600);denied.append(Client(origin,path))
   assert store.authenticate(token)==p and p['id']!=principal['id']
  qualifier=runpy.run_path(str(stage/'scripts/qualify-result-bundle.py'))
  receipt['zip']=qualifier['qualify'](owner,denied,'95eea001-1c8f-427b-97f2-061b95546818',output)
  receipt['denial_principals']=[{'id':x['id'],'name':x['name'],'projects':x['projects'],'scopes':x['scopes']} for x in clients() if x['id'] in created]
  receipt['passed']=True
except Exception as error:
 receipt['error_type']=type(error).__name__
finally:
 with store._tx() as db:
  for identity in created:
   row=db.execute('SELECT name FROM clients WHERE id=?',(identity,)).fetchone();assert row and row['name'].startswith('TEST P15 ')
   db.execute('UPDATE clients SET revoked_at=? WHERE id=?',(now(),identity))
 after=clients();receipt['test_principals_revoked']=all(x['revoked_at'] is not None for x in after if x['id'] in created)
 receipt['existing_principals_unchanged']=[x for x in after if x['id'] not in created]==before
 receipt['temporary_credentials_removed']=not list(output.glob('credentials-*'))
 receipt['finished_at']=now()
 if not all(receipt[k] for k in ('test_principals_revoked','existing_principals_unchanged','temporary_credentials_removed')):receipt['passed']=False
 (output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps(receipt,indent=2))
raise SystemExit(0 if receipt['passed'] else 1)
