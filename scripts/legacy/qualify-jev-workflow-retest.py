#!/usr/bin/env python3
"""Three additional synthetic calls explicitly authorized after the client fix."""

# Archived one-off operation; use the supported host installer instead.
if __name__ == '__main__':
    raise SystemExit('Archived operation is disabled. See docs/AGENT-SETUP.md for supported installation.')

import hashlib
import json
from pathlib import Path
import subprocess
import time
from cloudworkbench.jev_client import JevClient,JevError
from cloudworkbench.workflow_routing import WORKFLOWS,payload,parse_assessment

CASES=(
 ('feature','Build CSV export for a sample inventory application, including automated tests.'),
 ('plan_review','Only adversarially review this proposed plan: replace every database write at once, without a migration or rollback. Find gaps and risky assumptions. Do not implement it.'),
 ('code_review','Review this existing sample function for correctness and edge cases: def average(xs): return sum(xs) / len(xs). Do not change it.'),
)

def main():
 root=Path(__file__).resolve().parents[2]
 with (root/'evidence/jev-workflow-live-authorized-three.jsonl').open('x') as log:
  def record(row):
   import os
   log.write(json.dumps(row,allow_nan=False)+'\n');log.flush();os.fsync(log.fileno())
  record({'kind':'authorization','max_requests':3,'synthetic_only':True,'dispatch_performed':False,
          'source_hashes':{n:hashlib.sha256((root/'src/cloudworkbench'/n).read_bytes()).hexdigest() for n in ('jev_client.py','workflow_routing.py','pstack_routing.py')}})
  auth=subprocess.run(['op','item','get','musuvrnksfwqbd4uveurdxck4m','--fields','credential','--reveal'],capture_output=True,text=True,timeout=60)
  if auth.returncode: record({'kind':'blocked','reason':'credential_unavailable'});return 2
  client=JevClient(auth.stdout.strip());del auth
  for ordinal,(expected,summary) in enumerate(CASES,1):
   request=payload(summary,list(WORKFLOWS))
   record({'kind':'intent','ordinal':ordinal,'expected':expected,'summary_sha256':hashlib.sha256(summary.encode()).hexdigest()})
   start=time.monotonic()
   try:
    response=client.evaluate(request)
    result=parse_assessment(response,request,confidence_threshold=.8)
    safe={'model':result.reported_model,'answers':{'workflow':{
        'type':'choice','choice':response['answers']['workflow']['choice'],
        'confidence':result.confidence,'probabilities':dict(result.probabilities)}},
        'usage':{'input_tokens':result.input_tokens,'output_tokens':result.output_tokens}}
    record({'kind':'result','ordinal':ordinal,'expected':expected,'matches_expected':result.workflow==expected,
            'elapsed_seconds':round(time.monotonic()-start,4),'response':safe,'result':result.receipt()})
    print(json.dumps({'ordinal':ordinal,'expected':expected,'workflow':result.workflow,'confidence':result.confidence,
                      'reason':result.reason,'model':result.reported_model,'elapsed_seconds':round(time.monotonic()-start,4)}),flush=True)
   except JevError as e:
    record({'kind':'error','ordinal':ordinal,'reason':e.code});print(json.dumps({'ordinal':ordinal,'reason':e.code}),flush=True)
   except Exception:
    record({'kind':'error','ordinal':ordinal,'reason':'invalid_qualification_response'});print('Response failed validation.',flush=True);return 2
 return 0

if __name__=='__main__':raise SystemExit(main())
