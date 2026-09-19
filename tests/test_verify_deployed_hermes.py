import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('verify_deployed_hermes',ROOT/'scripts/legacy/verify-deployed-hermes.py')
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
SID='11111111-1111-1111-1111-111111111111';AID='22222222-2222-2222-2222-222222222222'
NATIVE='20260918_151018_79c714'


def test_submission_intent_is_durable_before_post_and_identity_immediately_after(tmp_path):
    receipt={'turns':[]};path=tmp_path/'receipt.json'
    def submit(key):
        saved=json.loads(path.read_text());assert saved['turns'][0]['submission']=='intent'
        assert saved['turns'][0]['idempotency_key']==key
        return {'session_id':SID,'attempt_id':AID,'generation':1}
    turn=v.persist_submission(receipt,path,1,{'goal':'fix'},submit)
    assert turn['submission']=='acknowledged'
    assert json.loads(path.read_text())['turns'][0]['attempt_id']==AID


def test_lost_ack_preserves_unknown_intent_and_never_retries(tmp_path):
    receipt={'turns':[]};path=tmp_path/'receipt.json';called=[]
    def lost(key):called.append(key);raise TimeoutError()
    with pytest.raises(TimeoutError):v.persist_submission(receipt,path,1,{'goal':'fix'},lost)
    saved=json.loads(path.read_text());assert saved['turns'][0]['submission']=='intent'
    with pytest.raises(v.ProofError,match='submission_limit_or_replay'):
        v.persist_submission(saved,path,1,{'goal':'fix'},lost)
    assert len(called)==1


def test_maximum_two_submissions(tmp_path):
    receipt={'turns':[]};path=tmp_path/'receipt.json';calls=[]
    def submit(key):
        calls.append(key);return {'session_id':SID,'attempt_id':AID,'generation':len(calls)}
    for n in (1,2):v.persist_submission(receipt,path,n,{'message':'fix'},submit)
    with pytest.raises(v.ProofError):v.persist_submission(receipt,path,3,{},submit)
    assert len(calls)==2 and len(set(calls))==2


def completed():
    return {'id':AID,'generation':1,'agent':'hermes','state':'completed','outcome':'verified',
        'result':{'provider_result':{'is_error':False,'native_session_id':NATIVE},'image_digest':v.IMAGE,
        'environment':{'manifest_sha256':v.ENVIRONMENT_SHA,'manifest':{'version':'hermes-grok-v1'}}}}


def test_complete_requires_native_identity_and_protected_environment():
    assert v.validate_completed(completed(),{'attempt_id':AID,'generation':1},NATIVE)==NATIVE


@pytest.mark.parametrize('bad',['native','image','manifest','outcome','error','generation','agent'])
def test_false_success_and_wrong_followup_refused(bad):
    item=completed()
    if bad=='native':item['result']['provider_result']['native_session_id']='20260918_151018_000000'
    if bad=='image':item['result']['image_digest']='sha256:'+'0'*64
    if bad=='manifest':item['result']['environment']['manifest_sha256']='0'*64
    if bad=='outcome':item['outcome']='unverified'
    if bad=='error':item['result']['provider_result']['is_error']=0
    if bad=='generation':item['generation']=2
    if bad=='agent':item['agent']='claude'
    with pytest.raises(v.ProofError):v.validate_completed(item,{'attempt_id':AID,'generation':1},NATIVE)


@pytest.mark.parametrize('valid',[True,False])
def test_independent_cases_execute_and_reject_original_bug(tmp_path,valid):
    source=('import datetime,re\ndef valid_date(v):\n'
      '    if not isinstance(v,str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",v): return False\n'
      '    try: datetime.date.fromisoformat(v); return True\n'
      '    except ValueError: return False\n') if valid else 'def valid_date(v): return True\n'
    (tmp_path/'booking.py').write_text(source)
    check=v.CHECK.replace("'/workspace'",repr(str(tmp_path)))
    result=subprocess.run([sys.executable,'-I','-B','-c',check,json.dumps(v.CASES)],capture_output=True,timeout=20)
    if valid:assert json.loads(result.stdout)=={'accepted':True,'cases':27}
    else:assert result.returncode!=0


@pytest.mark.parametrize('mismatch',[False,True])
def test_download_hash_and_attempt_scoping(tmp_path,monkeypatch,mismatch):
    names=['booking.py','test_booking.py','report.md'];raw=b'bounded content'
    rows=[{'id':name,'attempt_id':AID,'path':name,'bytes':len(raw),'sha256':v.sha(raw)} for name in names]
    rows.append({**rows[0],'attempt_id':'foreign','sha256':'0'*64})
    if mismatch:rows[0]['sha256']='0'*64
    def request(token,method,path,**kwargs):return raw if kwargs.get('binary') else {'artifacts':rows}
    monkeypatch.setattr(v,'request',request)
    if mismatch:
        with pytest.raises(v.ProofError,match='download_hash_mismatch'):v.download('private',SID,AID,tmp_path,False)
        assert list(tmp_path.iterdir())==[]
    else:assert set(v.download('private',SID,AID,tmp_path,False))==set(names)


def test_post_run_source_hash_detects_drift(tmp_path):
    (tmp_path/'runner.py').write_bytes(b'original')
    expected={'runner.py':v.sha(b'original')}
    assert v.sources_match(expected,tmp_path) is True
    (tmp_path/'runner.py').write_bytes(b'changed')
    assert v.sources_match(expected,tmp_path) is False
