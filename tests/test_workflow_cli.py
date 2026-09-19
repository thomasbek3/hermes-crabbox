import json
import os
from pathlib import Path
import subprocess
import sys

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/route-workflow.py'

def run(tmp_path,*args):
    p=tmp_path/'summary.txt';p.write_text('Build synthetic export')
    env=dict(os.environ);env.pop('TYPESAFE_API_KEY',None)
    result=subprocess.run([sys.executable,str(SCRIPT),'--summary-file',str(p),*args],env=env,capture_output=True,text=True)
    assert not result.stderr
    return result.returncode,json.loads(result.stdout)

def test_missing_key_returns_safe_json(tmp_path):
    code,data=run(tmp_path,'--live')
    assert code==2 and data['reason']=='invalid_api_key' and data['dispatch_performed'] is False

def test_preview_and_explicit_are_offline(tmp_path):
    code,data=run(tmp_path)
    assert code==0 and data['status']=='preview'
    code,data=run(tmp_path,'--workflow','feature')
    assert code==0 and data['steps'][1]['profile']=='astra-high'

def test_invalid_threshold_returns_reason(tmp_path):
    code,data=run(tmp_path,'--workflow','feature','--confidence-threshold','0')
    assert code==2 and data['reason']=='invalid_confidence_threshold'
