#!/usr/bin/env python3
"""Transfer exact source to a unique temporary directory; preserve verbatim proof."""
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
paths = ['src/cloudworkbench/runtime.py', 'src/cloudworkbench/resource_samples.py', 'scripts/qualify-resource-samples.py']
sources = {path: (root/path).read_text() for path in paths}
bootstrap = '''import json,os,pathlib,shutil,subprocess,tempfile,sys
sources = %s
stage = pathlib.Path(tempfile.mkdtemp(prefix='cwb-resprobe-'))
try:
 for relative,source in sources.items():
  path=stage/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(source)
 (stage/'src/cloudworkbench/__init__.py').write_text('')
 result=subprocess.run([sys.executable,str(stage/'scripts/qualify-resource-samples.py')],env={**os.environ,'PYTHONPATH':str(stage/'src')},capture_output=True,text=True,timeout=45)
 sys.stdout.write(result.stdout);sys.stderr.write(result.stderr)
finally:
 shutil.rmtree(stage)
sys.exit(result.returncode)
''' % repr(sources)
result = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','thomas@100.83.74.92','python3 -'],
                        input=bootstrap, text=True, capture_output=True, timeout=60)
(root/'evidence/resource-samples-docker-proof.stderr.txt').write_text(result.stderr)
if result.returncode:
    raise SystemExit('Disposable proof failed; inspect private stderr receipt.')
receipt = json.loads(result.stdout)
assert receipt['status'] == 'passed' and receipt['cleanup_verified'] is True
assert receipt['source_sha256'] == {path: hashlib.sha256(source.encode()).hexdigest() for path,source in sources.items()}
(root/'evidence/resource-samples-docker-proof.json').write_text(result.stdout)
transport = {'target':'thomas@100.83.74.92','stdout_sha256':hashlib.sha256(result.stdout.encode()).hexdigest(),
             'source_sha256':receipt['source_sha256'],'unique_temporary_source_removed':True,'installed_source_changed':False}
(root/'evidence/resource-samples-proof-transport.json').write_text(json.dumps(transport,indent=2)+'\n')
print(json.dumps({'status':receipt['status'],'cleanup_verified':receipt['cleanup_verified'],
                  'observations':receipt['sample_window']['observed_samples'],'load_probe':receipt['load_probe']}))
