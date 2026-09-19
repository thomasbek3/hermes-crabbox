import hashlib,json,os
from pathlib import Path
import socket,sqlite3,subprocess
from datetime import datetime,timezone
import cloudworkbench.runner as runner
import cloudworkbench.api as api
from cloudworkbench.environments import EnvironmentRegistry

cpath=Path('/etc/cloud-workbench/worker.json');apath=Path('/etc/cloud-workbench/api.json')
c=json.loads(cpath.read_text());hashfile=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
root=Path('/opt/cloud-workbench');names=('api','runner','store','environments','repositories','artifacts')
with sqlite3.connect(c['database']) as db:
    pending=db.execute("SELECT id,state FROM attempts WHERE state NOT IN ('completed','failed','cancelled','interrupted','paused')").fetchall()
    temp_clients=db.execute("SELECT name,revoked_at IS NOT NULL FROM clients WHERE name LIKE 'Repository qualification observer %'").fetchall()
registry=EnvironmentRegistry(c['environment_registry'],read_only=True)
records={p:registry.resolve(p,v) for p,v in [('sample-web','demo-v1'),('sample-document','document-v1'),('sample-repo','repo-v1')]}
receipt={'timestamp':datetime.now(timezone.utc).isoformat(),'host':socket.gethostname(),'uid':os.getuid(),
    'image_digest':c['runtime']['image'],'module_paths':{'runner':runner.__file__,'api':api.__file__},
    'source_sha256':{f'src/cloudworkbench/{name}.py':hashfile(root/'src/cloudworkbench'/f'{name}.py') for name in names},
    'config_sha256':{'worker':hashfile(cpath),'api':hashfile(apath)},
    'registry_permissions':oct(Path(c['environment_registry']).stat().st_mode&0o777),
    'registry_uid':Path(c['environment_registry']).stat().st_uid,
    'environment_sha256':{key:value['manifest_sha256'] for key,value in records.items()},
    'pending_attempts':pending,'temporary_observer_revocations':temp_clients,
    'services':{name:subprocess.check_output(['systemctl','is-active',name],text=True).strip() for name in ['cloud-workbench-api','cloud-workbench-worker','cloudd']},
    'legacy_pid':subprocess.check_output(['systemctl','show','cloudd','--property=MainPID','--value'],text=True).strip(),
    'provider_image_host_workspace_exists':Path('/workspace').exists()}
print(json.dumps(receipt,indent=2))
