#!/usr/bin/env python3
"""One real registered-repository task plus follow-up, SSE replay and patch proof."""
from datetime import datetime,timezone
import hashlib,json,os
from pathlib import Path
import runpy,secrets,socket,sqlite3,subprocess,sys,tempfile,time,urllib.error,urllib.request

def assert_continuation_binding(prior, current):
    """A reused execution must belong to the identical deployment boundary."""
    for field in ('host', 'uid', 'config_sha256', 'image_digest', 'registered_commit', 'source_sha256'):
        assert field in prior and field in current, 'Missing continuation binding: ' + field
        assert prior[field] == current[field], 'Changed continuation binding: ' + field
    assert set(current['source_sha256']) == {
        'runner.py', 'api.py', 'store.py', 'environments.py', 'repositories.py', 'artifacts.py'}
    assert all(isinstance(value, str) and len(value) == 64
               for value in current['source_sha256'].values())


helper=runpy.run_path(str(Path(__file__).with_name('http-smoke.py')))
call=helper['call'];BASE=helper['BASE'];TOKEN=helper['TOKEN']
from cloudworkbench.repositories import snapshot,initialize_workspace
from cloudworkbench.store import Store

now=lambda:datetime.now(timezone.utc).isoformat()
sha=lambda data:hashlib.sha256(data).hexdigest()
config_path=Path('/etc/cloud-workbench/worker.json');config=json.loads(config_path.read_text())
definition=config['projects']['sample-repo']['repository'];registered=config['repositories'][definition['repository_id']]
receipt={'schema_version':1,'started_at':now(),'host':socket.gethostname(),'uid':os.getuid(),
         'provider_called':True,'synthetic_adapter':False,'passed':False,'checks':{},
         'driver_path':str(Path(__file__).resolve()),'driver_sha256':sha(Path(__file__).read_bytes()),
         'config_sha256':sha(config_path.read_bytes()),'image_digest':config['runtime']['image'],
         'registered_commit':definition['commit'],
         'source_sha256':{name:sha((Path('/opt/cloud-workbench/src/cloudworkbench')/name).read_bytes()) for name in ['runner.py','api.py','store.py','environments.py','repositories.py','artifacts.py']}}


def event_history(sid):
    data,_=call('GET',f'/v1/sessions/{sid}/events?follow=false',raw=True)
    return [json.loads(line[5:]) for line in data.decode().splitlines() if line.startswith('data:')]


def sse(sid,cursor=0,stop_on_tool=False):
    headers={'Authorization':'Bearer '+TOKEN,'Accept':'text/event-stream'}
    if cursor:headers['Last-Event-ID']=str(cursor)
    request=urllib.request.Request(BASE+f'/v1/sessions/{sid}/events?follow=true',headers=headers)
    start=time.monotonic();events=[];block=[];was_active=False
    with urllib.request.urlopen(request,timeout=20) as stream:
        assert stream.headers.get_content_type()=='text/event-stream'
        while time.monotonic()-start<300:
            line=stream.readline(1024*1024)
            if not line:break
            assert len(line)<1024*1024
            line=line.decode().rstrip('\r\n')
            if line:
                if not line.startswith(':'):block.append(line)
                continue
            data=next((line[5:].strip() for line in block if line.startswith('data:')),None)
            eventid=next((line[3:].strip() for line in block if line.startswith('id:')),None)
            block=[]
            if data is None:continue
            event=json.loads(data)
            assert int(eventid)==event['sequence'] and event['sequence']>cursor
            cursor=event['sequence'];events.append(event)
            state=call('GET','/v1/sessions/'+sid)['state']
            was_active=was_active or state not in {'completed','failed','cancelled','interrupted'}
            if stop_on_tool and event['type']=='tool.started':
                assert state not in {'completed','failed','cancelled','interrupted'}
                return events,{'last_event_id':cursor,'disconnected_while_active':True,'event_count':len(events)}
    return events,{'last_event_id':cursor,'observed_active':was_active,'event_count':len(events)}


def wait(sid,turns):
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        result=call('GET','/v1/sessions/'+sid)
        if len(result.get('turns',[]))>=turns and result['state'] in {'completed','failed','cancelled','interrupted'}:return result
        time.sleep(.5)
    raise RuntimeError('bounded task wait expired')


def verify_delivery(sid,attempt_id):
    result=call('GET',f'/v1/sessions/{sid}/diff')
    assert result['attempt_id']==attempt_id and result['base_commit']==definition['commit']
    assert result['complete_text_patch'] and result['excluded_from_text_patch']==[]
    artifacts=call('GET',f'/v1/sessions/{sid}/artifacts')['artifacts']
    bodies={};metadata=[]
    for item in artifacts:
        if item['attempt_id']!=attempt_id:continue
        content,headers=call('GET',f'/v1/artifacts/{item["id"]}/content',raw=True)
        assert sha(content)==item['sha256'] and len(content)==item['bytes']
        assert headers.get('content-disposition','').startswith('attachment')
        bodies[item['path']]=content;metadata.append({key:item[key] for key in ('path','sha256','bytes','id')})
    patch=bodies['@delivery/changes.patch'];assert sha(patch)==result['patch_sha256']
    manifest=json.loads(bodies['@delivery/manifest.json']);assert manifest['changed_files']==result['changed_files']
    baseline=snapshot(Path(registered['path']),definition['commit'])
    env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0'}
    with tempfile.TemporaryDirectory(prefix='cwb-patch-proof-') as temporary:
        root=Path(temporary);work=root/'work';work.mkdir();initialize_workspace(work,baseline)
        patch_path=root/'changes.patch';patch_path.write_bytes(patch)
        # No agent Git configuration or hook can enter this independently constructed tree.
        command=['git','-c','core.hooksPath=/dev/null','-C',str(work),'apply']
        subprocess.run(command+['--check',str(patch_path)],env=env,check=True,capture_output=True,timeout=30)
        subprocess.run(command+[str(patch_path)],env=env,check=True,capture_output=True,timeout=30)
        for changed in result['changed_files']:
            path=work/changed['path']
            if changed['status']=='deleted':assert not path.exists()
            else:assert sha(path.read_bytes())==changed['after_sha256']==sha(bodies[changed['path']])
    return {'diff':result,'artifacts':metadata,'patch_apply_check':True,'applied_hashes_match':True,'verification_source':'protected worker runtime; patch bytes matched exported artifact hashes'}


def denied(method,path,token):
    request=urllib.request.Request(BASE+path,headers={'Authorization':'Bearer '+token},method=method)
    try:
        with urllib.request.urlopen(request,timeout=10):return False
    except urllib.error.HTTPError as error:return error.code in (401,403,404)

other_id=None
try:
    # Readiness must be true, not merely a running systemd process.
    assert call('GET','/v1/ready')['ready'] is True
    baseline=snapshot(Path(registered['path']),definition['commit'])
    assert baseline['files']['booking.py']['content']==b'def valid_date(value):\n    return True\n'
    request={'project_id':'sample-repo','agent':'claude','environment_version':'repo-v1',
        'goal':'Inspect the supplied booking.py repository files. Fix valid_date(value) to accept only real calendar dates formatted exactly YYYY-MM-DD, rejecting malformed strings, impossible dates and nonstrings. Add unit tests and run them. Write report.md explaining the fix and test results. Use only these local files; no dependencies or network fetching are needed.',
        'acceptance':[{'id':'booking-validity','description':'Valid dates pass; malformed and nonexistent dates fail.','mandatory':True}]}
    if len(sys.argv)==3 and sys.argv[1]=='--continue-from':
        prior_bytes=Path(sys.argv[2]).read_bytes();prior=json.loads(prior_bytes)
        assert_continuation_binding(prior,receipt)
        receipt['continuation_binding_checked_before_reuse']=True
        assert prior['first_state']['state']=='completed' and prior['first_state']['outcome']=='verified'
        assert prior['sse']['no_missing_or_duplicate_events']
        created={'session_id':prior['session_id'],'attempt_id':prior['first_state']['attempt_id']}
        sid=receipt['session_id']=created['session_id']
        receipt['continued_receipt_sha256']=sha(prior_bytes)
        receipt['initial_driver_sha256']=prior['driver_sha256']
        receipt['first_execution_reused']=True
        receipt['sse']=prior['sse']
        result=wait(sid,1);history=event_history(sid)
        assert history[-1]['sequence']==receipt['sse']['resume']['last_event_id']
    else:
        created=call('POST','/v1/sessions',request);sid=receipt['session_id']=created['session_id']
        first_events,disconnect=sse(sid,stop_on_tool=True)
        resumed_events,reconnect=sse(sid,cursor=disconnect['last_event_id'])
        receipt['sse']={'first':disconnect,'resume':reconnect,'resumed_with_last_event_id':True}
        result=wait(sid,1);history=event_history(sid)
        observed=first_events+resumed_events
        assert [event['sequence'] for event in observed]==[event['sequence'] for event in history]
        assert len({event['sequence'] for event in observed})==len(observed)
        receipt['sse']['no_missing_or_duplicate_events']=True
    receipt['first_state']={'attempt_id':created['attempt_id'],'state':result['state'],'outcome':result.get('outcome')}
    assert result['state']=='completed' and result.get('outcome')=='verified'
    receipt['checks']['live_sse_disconnect_and_replay']=True
    receipt['first_delivery']=verify_delivery(sid,created['attempt_id'])
    receipt['checks']['real_repository_patch_and_verifier']=True
    first_attempt=next(a for a in result['attempts'] if a['id']==created['attempt_id'])
    pin=first_attempt['result']['environment']
    assert pin['manifest']['repositories'][0]==definition and pin['manifest']['secret_refs']==['claude-subscription']
    receipt['environment_manifest_sha256']=pin['manifest_sha256']
    store=Store(Path(config['database']),shared_group=True)
    other_token=secrets.token_urlsafe(48)
    other=store.add_client('Repository qualification observer '+sid,other_token,['observe','retrieve'],['sample-repo']);other_id=other['id']
    patch_id=receipt['first_delivery']['diff']['patch_artifact_id']
    assert denied('GET',f'/v1/sessions/{sid}/diff',other_token)
    assert denied('GET',f'/v1/artifacts/{patch_id}/content',other_token)
    assert denied('GET',f'/v1/sessions/{sid}/diff','invalid-token')
    receipt['checks']['cross_principal_diff_and_patch_denied']=True
    follow=call('POST',f'/v1/sessions/{sid}/messages',{'message':'Update README.md with exact accepted and rejected valid_date examples and the command to run the unit tests. Preserve the working implementation and report.md; rerun tests.'})
    try:call('GET',f'/v1/sessions/{sid}/diff')
    except urllib.error.HTTPError as error:assert error.code==409
    else:raise AssertionError('New unfinished turn exposed old patch')
    follow_events,follow_sse=sse(sid,cursor=history[-1]['sequence'])
    followed=wait(sid,2)
    receipt['followup_state']={'attempt_id':follow['attempt_id'],'state':followed['state'],'outcome':followed.get('outcome')}
    assert followed['state']=='completed' and followed.get('outcome')=='verified'
    receipt['followup_delivery']=verify_delivery(sid,follow['attempt_id'])
    follow_attempt=next(a for a in followed['attempts'] if a['id']==follow['attempt_id'])
    assert follow_attempt['result']['environment']==pin
    assert follow_attempt['result']['repository']==first_attempt['result']['repository']
    assert follow_attempt['id']!=first_attempt['id']
    receipt['checks']['same_baseline_pinned_followup_and_delivery']=True
    receipt['attempts']=[{key:a.get(key) for key in ('id','state','outcome','created_at','started_at','updated_at','generation')} for a in [first_attempt,follow_attempt]]
    receipt['provider_provenance']=[e['payload'] for e in event_history(sid) if e['type']=='adapter.provenance']
    receipt['passed']=True
except Exception as exc:
    receipt['error_type']=type(exc).__name__
finally:
    if other_id:
        with sqlite3.connect(config['database']) as db:db.execute('UPDATE clients SET revoked_at=? WHERE id=?',(now(),other_id))
        receipt['temporary_observer_revoked']=True
    receipt['finished_at']=now()
    print(json.dumps(receipt,indent=2),flush=True)
raise SystemExit(0 if receipt['passed'] else 1)
