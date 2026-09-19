#!/usr/bin/env python3
"""Preview or classify a task; never starts workers or changes active policy."""
import argparse
import json
import os
from pathlib import Path
from cloudworkbench.jev_client import JevError
from cloudworkbench.workflow_routing import WORKFLOWS, WorkflowError, payload, select_workflow


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--summary-file',type=Path,required=True)
    p.add_argument('--workflow',choices=tuple(WORKFLOWS))
    p.add_argument('--allow',action='append',choices=tuple(WORKFLOWS))
    p.add_argument('--live',action='store_true',help='Send summary to TypeSafe using TYPESAFE_API_KEY')
    p.add_argument('--confidence-threshold',type=float,default=.8)
    args=p.parse_args()
    try:
        with args.summary_file.open('rb') as f: summary=f.read(12001).decode('utf-8')
        allowed=args.allow or list(WORKFLOWS)
        if not args.live and args.workflow is None:
            print(json.dumps({'status':'preview','payload':payload(summary,allowed),'dispatch_performed':False},indent=2)); return 0
        client=None
        if args.live and args.workflow is None:
            from cloudworkbench.jev_client import JevClient
            client=JevClient(os.environ.get('TYPESAFE_API_KEY',''))
        result=select_workflow(summary,allowed_workflows=allowed,client=client,explicit_workflow=args.workflow,
            external_allowed=args.live,confidence_threshold=args.confidence_threshold)
        print(json.dumps(result.receipt(),indent=2))
        return 0 if result.status=='selected' else 2
    except (WorkflowError, JevError) as error:
        print(json.dumps({'status':'invalid_or_unavailable','reason':str(error),'dispatch_performed':False})); return 2
    except (OSError, UnicodeError):
        print(json.dumps({'status':'invalid_or_unavailable','reason':'summary_unavailable','dispatch_performed':False})); return 2

if __name__=='__main__': raise SystemExit(main())
