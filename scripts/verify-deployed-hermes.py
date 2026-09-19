#!/usr/bin/env python3
"""Two real Hermes turns through the already activated production API; never retries submission.

Use a fresh private proof-root. A saved submitted/unknown intent requires explicit
operator recovery using its idempotency key/session IDs; rerunning this program
against an existing proof-root is refused. No service/config/client-grant changes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

IMAGE='sha256:5a03c9d2d1fc20683029c47683a3b0470ad2d7ce79eeb43ced1bdfba10770693'
ENVIRONMENT_SHA='552804d2c3873487b5cd7dd399f833a59f5fcd2f9c672876f63f2b5c6ba546bf'
TERMINAL={'completed','failed','cancelled','interrupted','paused'}
PROMPTS=(
 'Use pstack:tdd. Fix supplied booking.py valid_date(value): accept only real calendar dates formatted exactly YYYY-MM-DD; '
 'reject malformed/impossible dates and nonstrings, including bool/None. Keep a boolean return value. '
 'Write and run stdlib tests in test_booking.py and write report.md explaining the fix and test results. '
 'Use only /workspace, no dependencies or network. Do not change protected checks.',
 'Continue this exact native session. Preserve the correct booking.valid_date behavior. Use pstack:tdd to extend '
 'test_booking.py with leap-year/century, formatting and nonstring regression coverage, run all tests again, '
 'and add README.md documenting valid_date and the exact unittest command. Update report.md with the results. '
 'Use only /workspace, no dependencies/network or protected-check changes.')
CASES=[('2026-09-17',True),('2024-02-29',True),('2000-02-29',True),('1900-02-29',False),
 ('2026-02-29',False),('2026-02-30',False),('2026-04-31',False),('2026-12-31',True),
 ('0001-01-01',True),('9999-12-31',True),('0000-01-01',False),('2026-13-01',False),
 ('2026-00-01',False),('2026-01-00',False),('2026-9-17',False),('2026-09-7',False),
 (' 2026-09-17',False),('2026-09-17 ',False),('2026-09-17\n',False),('garbage',False),
 ('20260917',False),('',False),(None,False),(True,False),(20260917,False),([],False),({},False)]
CHECK='''import json,subprocess,sys,tempfile
cases=json.loads(sys.argv[1])
code="import json,sys;sys.path.insert(0,'/workspace');from booking import valid_date;print(json.dumps(valid_date(json.loads(sys.argv[1]))))"
for value,expected in cases:
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p=subprocess.run([sys.executable,'-I','-B','-c',code,json.dumps(value)],stdout=out,stderr=err,timeout=3)
        out.seek(0);raw=out.read(4097)
    assert p.returncode==0 and len(raw)<=4096
    assert json.loads(raw) is expected
print(json.dumps({'accepted':True,'cases':len(cases)}))
'''

class ProofError(Exception):pass

def require(value,code):
    if not value:raise ProofError(code)

def sha(raw):return hashlib.sha256(raw).hexdigest()
def encode(v):return (json.dumps(v,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()

def write(path,value):
    fd=os.open(str(path)+'.new',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(encode(value));out.flush();os.fsync(out.fileno())
    os.replace(str(path)+'.new',path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def local_read(path,limit=2*1024**2):
    require(path.is_absolute() and path.resolve()==path,'noncanonical_private_path')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd);require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=limit,'invalid_private_file')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(limit+1)
        require(len(raw)<=limit,'local_file_limit');return raw
    finally:os.close(fd)

def command(argv,timeout=10):
    p=subprocess.run(argv,capture_output=True,timeout=timeout)
    require(p.returncode==0,'operator_command_failed');return p.stdout

def services():
    return {name:command(['systemctl','show',name,'--property=ActiveState,MainPID']).decode().splitlines()
        for name in ('cloud-workbench-api','cloud-workbench-worker','cloudd')}

def grants():
    with sqlite3.connect('file:/var/lib/cloud-workbench/control/state.db?mode=ro',uri=True) as db:
        return sha(encode(db.execute('SELECT * FROM clients ORDER BY id').fetchall()))

def request(token,method,path,body=None,key=None,binary=False):
    headers={'Authorization':'Bearer '+token}
    if body is not None:headers['Content-Type']='application/json'
    if key is not None:headers['Idempotency-Key']=key
    req=urllib.request.Request('http://127.0.0.1:7780'+path,data=encode(body) if body is not None else None,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=10) as response:raw=response.read(2*1024**2+1)
    except urllib.error.HTTPError as exc:raise ProofError('api_http_'+str(exc.code)) from None
    require(len(raw)<=2*1024**2,'api_response_limit')
    return raw if binary else json.loads(raw)

def persist_submission(receipt,path,number,body,submit):
    require(number in (1,2) and len(receipt['turns'])==number-1,'submission_limit_or_replay')
    turn={'number':number,'idempotency_key':'hermes-deployed-'+secrets.token_hex(16),
        'request_sha256':sha(encode(body)),'submission':'intent','submitted_at':time.time()}
    receipt['turns'].append(turn);write(path,receipt)
    # The intent stays unknown on transport/ack failure. Never retry this POST.
    response=submit(turn['idempotency_key'])
    require(isinstance(response,dict) and all(isinstance(response.get(k),str) and re.fullmatch(r'[a-f0-9-]{36}',response[k])
        for k in ('session_id','attempt_id')) and type(response.get('generation')) is int,'submission_response_invalid')
    turn.update({k:response[k] for k in ('session_id','attempt_id','generation')});turn['submission']='acknowledged'
    write(path,receipt);return turn

def validate_completed(attempt,turn,previous_native=None):
    require(attempt.get('id')==turn['attempt_id'] and attempt.get('generation')==turn['generation']
        and attempt.get('agent')=='hermes' and attempt.get('state')=='completed' and attempt.get('outcome')=='verified',
        'attempt_not_verified')
    result=attempt.get('result') or {};p=result.get('provider_result') or {};env=result.get('environment') or {}
    native=p.get('native_session_id')
    require(p.get('is_error') is False and isinstance(native,str) and re.fullmatch(r'[0-9]{8}_[0-9]{6}_[0-9a-f]{6}',native),
        'native_session_missing')
    require(previous_native is None or native==previous_native,'native_followup_changed')
    require(result.get('image_digest')==IMAGE and env.get('manifest_sha256')==ENVIRONMENT_SHA
        and env.get('manifest',{}).get('version')=='hermes-grok-v1','environment_or_image_changed')
    return native

def download(token,sid,aid,directory,followup):
    expected={'booking.py','test_booking.py','report.md'}|({'README.md'} if followup else set())
    rows=request(token,'GET',f'/v1/sessions/{sid}/artifacts')['artifacts'];selected={}
    for item in rows:
        if item.get('attempt_id')!=aid or item.get('path') not in expected:continue
        name=item['path'];require(name not in selected,'duplicate_artifact_path')
        raw=request(token,'GET',f"/v1/artifacts/{item['id']}/content",binary=True)
        require(type(item.get('bytes')) is int and len(raw)==item['bytes'] and sha(raw)==item['sha256'],'download_hash_mismatch')
        fd=os.open(directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o444)
        with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
        selected[name]={k:item[k] for k in ('id','path','bytes','sha256')}
    require(set(selected)==expected,'required_artifacts_missing')
    if followup:
        text=(directory/'README.md').read_text();require('valid_date' in text and 'unittest' in text,'followup_documentation_missing')
    return selected

def independent(directory,proof,turn):
    name='hermes-deployed-check-'+secrets.token_hex(8);owner=proof.name
    argv=['/usr/bin/docker','run','--rm','--pull=never','--name',name,'--label','io.cloudworkbench.acceptance='+owner,
        '--network=none','--read-only','--user','65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges:true',
        '--cpus=1','--memory=256m','--memory-swap=256m','--pids-limit=64','--log-driver=none',
        '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777','--workdir','/workspace',
        '--mount',f'type=bind,src={directory},dst=/workspace,readonly',
        '--mount',f'type=bind,src={proof}/independent-check.py,dst=/check.py,readonly',
        '--entrypoint','python3',IMAGE,'-I','-B','/check.py',json.dumps(CASES)]
    try:
        value=json.loads(command(argv,timeout=45));require(value=={'accepted':True,'cases':len(CASES)},'independent_check_failed')
        return value
    finally:
        p=subprocess.run(['/usr/bin/docker','inspect',name],capture_output=True,timeout=10)
        if p.returncode==0:
            data=json.loads(p.stdout)[0];require(data['Config']['Labels'].get('io.cloudworkbench.acceptance')==owner,'check_cleanup_scope_changed')
            command(['/usr/bin/docker','rm','-f',data['Id']])

def remaining(turns):
    ids=[]
    for turn in turns:
        if 'attempt_id' in turn:
            ids+=command(['/usr/bin/docker','ps','-aq','--no-trunc','--filter','label=io.cloudworkbench.owner=primary',
                '--filter','label=io.cloudworkbench.attempt='+turn['attempt_id']]).decode().splitlines()
    return sorted(set(ids))

def sources_match(expected, root=Path('/opt/cloud-workbench/src/cloudworkbench')):
    return all(re.fullmatch(r'[a-z_]+\.py',name) and sha(local_read(root/name))==digest
        for name,digest in expected.items())

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof-root',type=Path,required=True);parser.add_argument('--activation-receipt',type=Path,required=True)
    args=parser.parse_args(argv);root=args.proof_root
    require(os.geteuid()==0 and socket.gethostname()=='omarchy','root_omarchy_required')
    require(root.is_absolute() and root.resolve()==root and root.parent==Path('/var/lib/cloud-workbench/qualifications')
        and re.fullmatch(r'hermes-deployed-proof-[a-f0-9]{12}',root.name),'invalid_proof_root')
    require(not root.exists(),'existing_proof_requires_explicit_recovery')
    activation_raw=local_read(args.activation_receipt);activation=json.loads(activation_raw)
    require(activation.get('passed') is True and activation.get('stage')=='complete' and activation.get('execute') is True
        and activation.get('host')=='omarchy' and activation.get('environment_sha256')==ENVIRONMENT_SHA,'completed_activation_required')
    require(sources_match(activation['source_sha256']),'deployed_source_changed')
    cfg={k:local_read(Path('/etc/cloud-workbench')/(k+'.json')) for k in ('api','worker')}
    require(all(sha(raw)==activation['config_sha256_after'][k] for k,raw in cfg.items()),'deployed_config_changed')
    before=services();before_grants=grants()
    token=local_read(Path('/var/lib/cloud-workbench/control/client.token'),16384).decode().strip()
    ready=request(token,'GET','/v1/ready');caps=request(token,'GET','/v1/capabilities')
    require(ready.get('ready') is True and {'hermes','claude'}<=set(ready.get('worker',{}).get('available_agents',[])), 'deployed_worker_not_ready')
    require(caps.get('capabilities',{}).get('hermes',{}).get('enabled') is True,'hermes_not_advertised')
    require(json.loads(command(['/usr/bin/docker','image','inspect',IMAGE]))[0]['Id']==IMAGE,'image_unavailable')
    root.mkdir(mode=0o700);check=root/'independent-check.py';check.write_text(CHECK);check.chmod(0o444)
    receipt={'schema_version':1,'host':'omarchy','script_sha256':sha(Path(__file__).read_bytes()),
        'activation_sha256':sha(activation_raw),'source_sha256':activation['source_sha256'],'config_sha256':{k:sha(v) for k,v in cfg.items()},
        'services_before':before,'grants_sha256_before':before_grants,'turns':[],'started_at':time.time(),'passed':False}
    path=root/'receipt.json';write(path,receipt);native=None;sid=None
    try:
        for number,prompt in enumerate(PROMPTS,1):
            body={'project_id':'sample-web','agent':'hermes','model':'grok-4.6','environment_version':'hermes-grok-v1',
                'goal':prompt,'acceptance':[{'id':'booking-validity','description':'Valid dates pass; malformed and nonexistent dates fail.','mandatory':True}]} if number==1 else {'message':prompt}
            endpoint='/v1/sessions' if number==1 else f'/v1/sessions/{sid}/messages'
            turn=persist_submission(receipt,path,number,body,lambda key:request(token,'POST',endpoint,body,key=key))
            require(sid is None or sid==turn['session_id'],'followup_api_session_changed');sid=turn['session_id']
            deadline=time.monotonic()+600
            while True:
                require(time.monotonic()<deadline,'turn_deadline_exceeded')
                detail=request(token,'GET',f'/v1/sessions/{sid}')
                attempt=next(a for a in detail['attempts'] if a['id']==turn['attempt_id'])
                if attempt['state'] in TERMINAL:break
                time.sleep(.75)
            native=validate_completed(attempt,turn,native)
            turn.update(state=attempt['state'],outcome=attempt['outcome'],native_session_id=native)
            write(path,receipt)
            directory=root/('turn-'+str(number));directory.mkdir(mode=0o555)
            turn['artifacts']=download(token,sid,turn['attempt_id'],directory,number==2)
            turn['independent']=independent(directory,root,number)
            write(root/('turn-'+str(number)+'.json'),detail)
            events=request(token,'GET',f'/v1/sessions/{sid}/events?follow=false',binary=True)
            events_path=root/('turn-'+str(number)+'.sse');events_path.write_bytes(events);events_path.chmod(0o600)
            turn['events']={'sha256':sha(events),'bytes':len(events)};turn['finished_at']=time.time();write(path,receipt)
        receipt['passed']=True
    except Exception as exc:
        receipt['failure']=str(exc) if type(exc) is ProofError else type(exc).__name__
    finally:
        try:
            if not receipt['passed']:
                for turn in receipt['turns']:
                    if 'attempt_id' in turn:request(token,'POST',f"/v1/attempts/{turn['attempt_id']}/cancel",key='cancel-'+turn['idempotency_key'])
            end=time.monotonic()+45
            while remaining(receipt['turns']) and time.monotonic()<end:time.sleep(.5)
            receipt['remaining_container_ids']=remaining(receipt['turns'])
            receipt['unknown_submission']=any(t['submission']!='acknowledged' for t in receipt['turns'])
            receipt['cleanup_confirmed']=not receipt['remaining_container_ids'] and not receipt['unknown_submission']
            for turn in receipt['turns']:
                if 'attempt_id' in turn:
                    detail=request(token,'GET',f"/v1/sessions/{turn['session_id']}")
                    state=next(a['state'] for a in detail['attempts'] if a['id']==turn['attempt_id'])
                    receipt['cleanup_confirmed'] &= state in TERMINAL
            receipt['services_after']=services();receipt['services_unchanged']=receipt['services_after']==before
            receipt['grants_unchanged']=grants()==before_grants
            receipt['configs_unchanged']=all(local_read(Path('/etc/cloud-workbench')/(k+'.json'))==raw for k,raw in cfg.items())
            receipt['sources_unchanged']=sources_match(activation['source_sha256'])
            if not all(receipt[k] for k in ('cleanup_confirmed','services_unchanged','grants_unchanged','configs_unchanged','sources_unchanged')):receipt['passed']=False
        except Exception as exc:
            receipt['passed']=False;receipt['cleanup_error']=str(exc) if type(exc) is ProofError else type(exc).__name__
        receipt['finished_at']=time.time();write(path,receipt)
    print(json.dumps({'passed':receipt['passed'],'receipt':str(path),'session_id':sid}));return 0 if receipt['passed'] else 1

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print(json.dumps({'preflight_failure':str(exc) if type(exc) is ProofError else type(exc).__name__}));sys.exit(1)
