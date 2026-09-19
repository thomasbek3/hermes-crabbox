#!/usr/bin/env python3
"""Publish exactly five P15 host files and restart only the idle v2 API."""

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import sqlite3
import subprocess
from datetime import datetime, timezone

PUBLISH = frozenset('src/cloudworkbench/'+name for name in
                    ('api.py','cli.py','result_bundle.py','dashboard.py','static/dashboard.html'))
TARGET = Path('/opt/cloud-workbench')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
def validate(source, base):
    manifest=json.loads((source/'manifest.json').read_text())
    if set(manifest['p15_api_overlay']['publish_files'])!=PUBLISH:raise ValueError('Unexpected API publication scope')
    for name,digest in manifest['files'].items():
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts or relative.as_posix()!=name:raise ValueError('Invalid source path')
        path=source/name
        if path.is_symlink() or sha(path)!=digest:raise ValueError('Source snapshot changed')
    if not base['passed'] or base['host']!='archived-worker.invalid':raise ValueError('No successful base deployment')
    for name,digest in base['deployed_source_sha256'].items():
        if name not in PUBLISH and manifest['files'].get(name)!=digest:raise ValueError('Unrelated source drift')
    return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--base-receipt',type=Path,required=True)
    parser.add_argument('--base-receipt-sha',required=True)
    parser.add_argument('--checkpoint',default='p15-api-20260917')
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname()!='archived-worker.invalid':raise ValueError('Root on Omarchy required')
    if not args.checkpoint.replace('-','').isalnum() or len(args.checkpoint)>60:raise ValueError('Invalid checkpoint')
    if sha(args.base_receipt)!=args.base_receipt_sha:raise ValueError('Base receipt changed')
    base=json.loads(args.base_receipt.read_text());source=args.snapshot.resolve();manifest=validate(source,base)
    helpers=runpy.run_path(str(source/'scripts/deploy-auth-checkpoint.py'));run=helpers['run'];atomic=helpers['atomic']
    for name,digest in base['deployed_source_sha256'].items():
        if sha(TARGET/name)!=digest:raise ValueError('Live source drift')
    for name,digest in base['config_sha256_after'].items():
        if sha(Path('/etc/cloud-workbench')/(name+'.json'))!=digest:raise ValueError('Live configuration drift')
    worker=json.loads(Path('/etc/cloud-workbench/worker.json').read_text());database=Path(worker['database'])
    def idle():
        with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as db:
            pending=db.execute("SELECT COUNT(*) FROM attempts WHERE state NOT IN ('completed','failed','cancelled','interrupted','paused')").fetchone()[0]
        return pending==0 and not helpers['owned_live_containers'](worker['runtime'].get('owner','primary'))
    if not idle():raise ValueError('Deployment requires idle database/runtime')
    backup=Path('/var/lib/cloud-workbench/operator-backups')/args.checkpoint
    if backup.exists():raise ValueError('Backup already exists')
    before={service:run(['systemctl','show',service,'--property=MainPID','--value']) for service in ('cloudd','cloud-workbench-worker','cloud-workbench-api')}
    receipt={'host':socket.gethostname(),'started_at':now(),'passed':False,'execute':args.execute,'stage':'validated','driver_sha256':sha(__file__),
             'snapshot_manifest_sha256':sha(source/'manifest.json'),'base_receipt_sha256':sha(args.base_receipt),
             'publish_sha256':{n:manifest['files'][n] for n in sorted(PUBLISH)},'pids_before':before,'backup':str(backup),'services_started':[],'stopped_services':[]}
    if not args.execute:print(json.dumps(receipt,indent=2));return 0
    for path in (source,*source.rglob('*')):
        if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode & 0o022:raise ValueError('Immutable root-owned stage required')
    try:
        backup.mkdir(mode=0o700)
        absent=[]
        for name in sorted(PUBLISH):
            path=TARGET/name
            if path.exists():atomic(backup/name,path.read_bytes(),0o600)
            else:absent.append(name)
        atomic(backup/'previously-absent.json',json.dumps(absent).encode(),0o600)
        receipt['stage']='quiesce'
        run(['systemctl','stop','cloud-workbench-api']);receipt['stopped_services']=['cloud-workbench-api']
        if not idle():raise ValueError('Work appeared before API admission stopped')
        receipt['stage']='publish'
        for name in sorted(PUBLISH):atomic(TARGET/name,(source/name).read_bytes())
        receipt['stage']='restart'
        run(['systemctl','start','cloud-workbench-api']);receipt['services_started']=['cloud-workbench-api'];receipt['stopped_services']=[]
        token=Path('/var/lib/cloud-workbench/control/client.token').read_text().strip()
        ready,error=helpers['wait_readiness'](token);receipt.update(readiness=ready,readiness_error=error)
        if error:raise ValueError('API readiness failed')
        receipt['pids_after']={s:run(['systemctl','show',s,'--property=MainPID','--value']) for s in before}
        if any(receipt['pids_after'][s]!=before[s] for s in ('cloudd','cloud-workbench-worker')):raise ValueError('Unrelated service PID changed')
        receipt['published_readback_sha256']={n:sha(TARGET/n) for n in sorted(PUBLISH)}
        if receipt['published_readback_sha256']!=receipt['publish_sha256']:raise ValueError('Published source mismatch')
        for name,digest in base['config_sha256_after'].items():
            if sha(Path('/etc/cloud-workbench')/(name+'.json'))!=digest:raise ValueError('Configuration changed')
        for name,digest in base['deployed_source_sha256'].items():
            if name not in PUBLISH and sha(TARGET/name)!=digest:raise ValueError('Unrelated live source changed')
        receipt.update(passed=True,stage='complete',configs_unchanged=True,unrelated_source_unchanged=True)
    except Exception as error:
        receipt['error_type']=type(error).__name__;receipt['recovery']='Inspect live state and exact backup; no automatic rollback'
    finally:
        receipt['service_states']=helpers['service_states']();receipt['finished_at']=now()
        atomic(source.parent/'p15-api-deployment-receipt.json',(json.dumps(receipt,indent=2)+'\n').encode(),0o600)
        print(json.dumps(receipt,indent=2))
    return 0 if receipt['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
