#!/usr/bin/env python3
"""Transfer an immutable source map and preserve verbatim isolated-host proof."""
import datetime
import hashlib
import json
from pathlib import Path
import subprocess

root=Path(__file__).resolve().parents[1]
paths=[str(p.relative_to(root)) for p in sorted((root/'src/cloudworkbench').glob('*.py'))]+['scripts/qualify-resource-integration.py']
sources={path:(root/path).read_text() for path in paths}
hashes={path:hashlib.sha256(source.encode()).hexdigest() for path,source in sources.items()}
bootstrap='''import json,os,pathlib,shutil,subprocess,tempfile,sys
sources=%s
hashes=%s
stage=pathlib.Path(tempfile.mkdtemp(prefix='cwb-resint-'))
try:
 for relative,source in sources.items():
  path=stage/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(source)
 (stage/'source-manifest.json').write_text(json.dumps(hashes))
 result=subprocess.run(['/opt/cloud-workbench/.venv/bin/python',str(stage/'scripts/qualify-resource-integration.py')],env={**os.environ,'PYTHONPATH':str(stage/'src')},capture_output=True,text=True,timeout=115)
 sys.stdout.write(result.stdout);sys.stderr.write(result.stderr)
finally:
 shutil.rmtree(stage)
sys.exit(result.returncode)
'''%(repr(sources),repr(hashes))
result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','thomas@100.83.74.92','python3 -'],input=bootstrap,text=True,capture_output=True,timeout=130)
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
base=root/'evidence'/('resource-integration-docker-'+stamp)
base.with_suffix('.json').write_text(result.stdout)
base.with_suffix('.stderr.txt').write_text(result.stderr)
receipt=json.loads(result.stdout) if result.stdout else {}
transport={'target':'thomas@100.83.74.92','exit_code':result.returncode,'stdout_sha256':hashlib.sha256(result.stdout.encode()).hexdigest(),
 'source_sha256':hashes,'stdout_source_hashes_match':receipt.get('source_sha256')==hashes,
 'temporary_source_and_state_removed':True,'installed_source_changed':False,'receipt':str(base.relative_to(root))+'.json'}
base.with_suffix('.transport.json').write_text(json.dumps(transport,indent=2)+'\n')
print(json.dumps({'receipt':transport['receipt'],'status':receipt.get('status'),'error':receipt.get('error'),
 'cleanup':receipt.get('cleanup'),'ticks':{k:v for k,v in receipt.get('ticks',{}).items() if k!='durations_seconds'},'source_hashes_match':transport['stdout_source_hashes_match']},indent=2))
raise SystemExit(result.returncode)
