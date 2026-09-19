"""Replay the three authorized synthetic replies; this never calls the API."""
import importlib.util
import json
from pathlib import Path
from cloudworkbench.workflow_routing import WORKFLOWS,payload,parse_assessment

ROOT=Path(__file__).resolve().parents[1]

def test_captured_jev_replies_match_contract_and_requested_workflows():
    spec=importlib.util.spec_from_file_location('jev_qualification',ROOT/'scripts/qualify-jev-workflow-retest.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    rows=[json.loads(x) for x in (ROOT/'evidence/jev-workflow-live-authorized-three.jsonl').read_text().splitlines()]
    results=[r for r in rows if r['kind']=='result']
    assert len([r for r in rows if r['kind']=='intent'])==len(results)==3
    for row,(expected,summary) in zip(results,module.CASES,strict=True):
        selection=parse_assessment(row['response'],payload(summary,list(WORKFLOWS)),confidence_threshold=.8)
        assert selection.workflow==expected==row['expected']
        assert selection.reported_model=='jev-1.13.0'
        assert selection.receipt()==row['result']
        assert selection.input_tokens>0 and selection.output_tokens>0
        assert not row['result']['dispatch_performed']
