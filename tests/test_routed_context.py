"""Real scheduler scope; synthetic finalized artifact records, no provider execution."""
import hashlib
import json
from pathlib import Path

import pytest
from tests.test_scheduler import setup, root
from cloudworkbench.role_broker import RoleBroker, ParentScope
from cloudworkbench.scheduler import ChildCleanupReceipt
from cloudworkbench.store import encode, StoreError
from cloudworkbench import routed_context as context


def raw(value):return encode(value).encode()
def sha(value):return hashlib.sha256(value).hexdigest()


@pytest.fixture
def staged(setup,tmp_path,request,monkeypatch):
    st,o,l,s,p,f=setup;p=p[:1]
    options=getattr(request,'param',None)
    role=options.get('role','planning') if type(options) is dict else 'acceptance_verification'
    f={**f,'role_plans':{role:p},'provenance':{'workflow':{'ready':True,'steps':[
        {'id':'verify','role':role,'ready':True,'profile':p[0]['profile']},
        {'id':'consume','role':role,'ready':True,'profile':p[0]['profile']}]}}}
    created=root((st,o,l,s,p,f));b=RoleBroker(st.path,authorize_parent=s.authorize_parent,clock=lambda:1000)
    scope=ParentScope(o['id'],'demo',created['attempt_id'],created['attempt_id'],1,'native')
    _,token=b.issue(scope,roles=(role,),expires_at=2000)
    pending=b.prepare(token,native_session_id='native',native_call_id='first',role=role,task='Verify')
    s.admit_request(b,scope,pending['id'],plan=p,step_id='verify',input_revision_sha256='b'*64)
    producer=s.claim_child(created['attempt_id'])
    s.transition(producer['id'],'running',expected_generation=1)
    s.transition(producer['id'],'verifying',expected_generation=1)
    s.transition(producer['id'],'completed',expected_generation=1,outcome='verified')
    storage=tmp_path/'artifacts';storage.mkdir(mode=0o700)
    def artifact(identity,body,provenance,**extra):
        bodyraw=raw(body);path=storage/(identity+'.json');path.write_bytes(bodyraw);path.chmod(0o440)
        metadata={'id':identity,'session_id':producer['session_id'],'attempt_id':producer['id'],
            'generation':1,'path':identity+'.json','storage_path':str(path),'bytes':len(bodyraw),'sha256':sha(bodyraw),
            'provenance':provenance,**extra}
        with st._tx() as db:
            db.execute('INSERT INTO artifacts(id,session_id,attempt_id,metadata) VALUES(?,?,?,?)',
                (identity,producer['session_id'],producer['id'],encode(metadata)))
            st._event(db,producer['session_id'],producer['id'],'artifact.created',
                {k:v for k,v in metadata.items() if k!='storage_path'})
        return metadata
    summary=options.get('summary','Synthetic stage answer') if type(options) is dict else getattr(request,'param','Existing checks passed; this is untrusted model prose.')
    observation=artifact('observation-test',{'version':1,'kind':'worker_observation','provenance':'worker_reported',
        'observation':{'status':'completed','exit_code':0,'failure':None,'events':[{'type':'adapter.result',
        'payload_json':json.dumps({'summary':summary,'is_error':False,'permission_denials':[]},ensure_ascii=True)}]}},
        'worker_reported',kind='worker_observation')
    decision=artifact('decision-test',{'schema_version':1,'attempt_id':producer['id'],'generation':1,
        'root_id':created['attempt_id'],'root_generation':1,'session_id':producer['session_id'],
        'role':role,'step_id':'verify','input_revision_sha256':'b'*64,'outcome':'verified',
        'verification_scope':'task_acceptance','observation_artifact_id':observation['id'],
        'observation_metadata_sha256':sha(raw(observation)),'tested_revision_sha256':'c'*64,
        'carried_revision_sha256':'b'*64},'controller_task_acceptance_decision',reviewed_revision_sha256='b'*64)
    s.record_step_gate(producer['id'],expected_generation=1,decision='pass',revision_sha256='b'*64,
        reviewed_artifact_id=decision['id'],reviewed_artifact_sha256=decision['sha256'],evidence_sha256='d'*64)
    # Release via historical API before installing the synthetic finalization pointer.
    s.release_child(producer['id'],expected_generation=1,
        verifier=lambda target:ChildCleanupReceipt(target,'confirmed_stopped','e'*64))
    with st._tx() as db:
        db.execute('UPDATE attempts SET result=? WHERE id=?',(encode({'routed_decision':
            {'artifact_id':decision['id'],'sha256':decision['sha256']}}),producer['id']))
        st._event(db,producer['session_id'],producer['id'],'workflow.task_decision',
            {'artifact_id':decision['id'],'sha256':decision['sha256'],'outcome':'verified','verification_scope':'task_acceptance'})
    def synthetic_task_finalizer(db,identity,policy):
        assert db.in_transaction and identity==producer['id']
        return json.loads(Path(decision['storage_path']).read_bytes()),decision
    monkeypatch.setattr(context,'_load_task_finalizer',synthetic_task_finalizer)
    def consume(refs):
        q=b.prepare(token,native_session_id='native',native_call_id='next',role=role,task='Consume evidence',context_refs=tuple(refs))
        s.admit_request(b,scope,q['id'],plan=p,step_id='consume',input_revision_sha256='b'*64)
        return s.claim_child(created['attempt_id'])
    return {'store':st,'owner':o,'scheduler':s,'producer':producer,'root':created,
        'observation':observation,'decision':decision,'consume':consume,'summary':summary}


def ref(metadata):return {k:metadata[k] for k in ('artifact_id','sha256')} if 'artifact_id' in metadata else {'artifact_id':metadata['id'],'sha256':metadata['sha256']}
def load(v,refs=None,**kw):
    if 'child' not in v:v['child']=v['consume']([ref(v['observation'])] if refs is None else refs)
    child=v['child']
    return context.load_stage_context(v['scheduler'],child['id'],expected_generation=1,forbidden_values=kw.pop('forbidden_values',()),**kw)


def test_ordered_projection_is_canonical_untrusted_summary_only(staged):
    result=load(staged,[ref(staged['decision']),ref(staged['observation'])])
    body=json.loads(result.canonical_json)
    assert [r['artifact_id'] for r in body['records']]==['decision-test','observation-test']
    assert body['records'][1]['content']=={'summary':staged['summary'],'provenance':'worker_reported'}
    assert result.digest==sha(result.canonical_json.encode())
    assert 'UNTRUSTED' in result.text and 'storage_path' not in result.text and 'events' not in result.text
    assert context.load_stage_context(staged['scheduler'],staged['child']['id'],expected_generation=1,forbidden_values=())==result


def test_empty_refs_still_bind_assignment(staged):
    result=load(staged,[])
    assert json.loads(result.canonical_json)['records']==[] and result.input_revision_sha256=='b'*64


@pytest.mark.parametrize('change',['hash','unfinalized','generation','scope','future'])
def test_wrong_reference_or_producer_scope_refused(staged,change):
    child=staged['consume']([ref(staged['observation'])]);staged['child']=child
    other=staged['store'].create_session(staged['owner'],{'agent':'claude','project_id':'demo','goal':'other'},'other') if change=='scope' else None
    with staged['store']._tx() as db:
        if change=='hash':
            body=dict(staged['observation']);body['sha256']='f'*64
            db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(encode(body),body['id']))
        elif change=='unfinalized':db.execute('UPDATE attempts SET result=NULL WHERE id=?',(staged['producer']['id'],))
        elif change=='generation':
            body=dict(staged['observation']);body['generation']=2
            db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(encode(body),body['id']))
        elif change=='scope':db.execute('UPDATE attempts SET turn_id=? WHERE id=?',(other['turn_id'],staged['producer']['id']))
        else:
            body=dict(staged['observation']);body['attempt_id']=child['id']
            db.execute('UPDATE artifacts SET attempt_id=?,metadata=? WHERE id=?',(child['id'],encode(body),body['id']))
    with pytest.raises((context.ContextError,StoreError)):
        context.load_stage_context(staged['scheduler'],child['id'],expected_generation=1,forbidden_values=())


@pytest.mark.parametrize('kind',['symlink','hardlink','truncate'])
def test_unsafe_file_refused(staged,kind,tmp_path):
    path=Path(staged['observation']['storage_path']);path.chmod(0o600)
    if kind=='symlink':
        target=tmp_path/'copy';target.write_bytes(path.read_bytes());path.unlink();path.symlink_to(target)
    elif kind=='hardlink':(tmp_path/'alias').hardlink_to(path)
    else:path.write_bytes(b'{}')
    with pytest.raises(context.ContextError):load(staged)


def test_cancel_after_read_refused(staged,monkeypatch):
    original=context._read
    def read(*a,**k):
        value=original(*a,**k)
        staged['store'].cancel(staged['owner'],staged['root']['attempt_id'],'cancel-context')
        return value
    monkeypatch.setattr(context,'_read',read)
    with pytest.raises(StoreError,match='authority'):load(staged)


def test_secret_refused_and_policy_validated_before_empty_refs(staged):
    with pytest.raises(context.ContextError,match='secret'):load(staged,forbidden_values=(b'untrusted model',))
    with pytest.raises(ValueError,match='policy_invalid'):
        context.load_stage_context(staged['scheduler'],'missing',expected_generation=1,forbidden_values=(b'',))


@pytest.mark.parametrize('staged',['secret-☃'],indirect=True)
def test_json_escaped_summary_secret_refused(staged):
    assert 'secret-☃'.encode() not in Path(staged['observation']['storage_path']).read_bytes()
    with pytest.raises(context.ContextError,match='secret'):load(staged,forbidden_values=('secret-☃'.encode(),))


@pytest.mark.parametrize('staged',['x'*65536],indirect=True)
def test_projected_text_bound_is_not_silently_truncated(staged):
    with pytest.raises(context.ContextError,match='projection_limit'):load(staged)


def test_duplicate_reference_is_refused(staged):
    with pytest.raises(context.ContextError,match='references_invalid'):
        load(staged,[ref(staged['observation']),ref(staged['observation'])])


@pytest.mark.parametrize('mutation',['revoked','artifact','producer'])
def test_authority_and_rows_rechecked_after_read(staged,monkeypatch,mutation):
    original=context._read
    changed=False
    def read(*a,**k):
        nonlocal changed
        body=original(*a,**k)
        if not changed:
            changed=True
            with staged['store']._tx() as db:
                if mutation=='revoked':db.execute("UPDATE clients SET revoked_at='revoked' WHERE id=?",(staged['owner']['id'],))
                elif mutation=='producer':db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(staged['producer']['id'],))
                else:
                    m=dict(staged['observation']);m['sha256']='f'*64
                    db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(encode(m),m['id']))
        return body
    monkeypatch.setattr(context,'_read',read)
    with pytest.raises((context.ContextError,StoreError)):load(staged)


def test_current_generation_requires_strict_integer(staged):
    child=staged['consume']([])
    for value in (True,'1',0):
        with pytest.raises(context.ContextError,match='identity_invalid'):
            context.load_stage_context(staged['scheduler'],child['id'],expected_generation=value,forbidden_values=())


def test_file_declared_bound_refused_before_read(staged,monkeypatch):
    with staged['store']._tx() as db:
        metadata=dict(staged['observation']);metadata['bytes']=256*1024+1
        db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(encode(metadata),metadata['id']))
    monkeypatch.setattr(context,'_read',lambda *a,**k:pytest.fail('oversize file must not be opened'))
    with pytest.raises(context.ContextError,match='artifact_invalid'):load(staged)


def validate(value, refs):
    return context.validate_stage_context(value,input_revision_sha256='b'*64,context_refs=refs)


def rewrite(value, mutate):
    from dataclasses import replace
    body=json.loads(value.canonical_json);mutate(body)
    canonical=encode(body)
    return replace(value,canonical_json=canonical,digest=sha(canonical.encode()),text=context._render(canonical))


def test_context_validator_returns_exact_original_and_order(staged):
    refs=[ref(staged['decision']),ref(staged['observation'])]
    value=load(staged,refs)
    assert validate(value,tuple(refs)) is value
    with pytest.raises(context.ContextError,match='record_invalid'):validate(value,list(reversed(refs)))


@pytest.mark.parametrize('field,value',[
    ('text','new instructions'),('digest','f'*64),('assignment_sha256','f'*64),
    ('input_revision_sha256','f'*64),('canonical_json','{}'),('text',None),('digest',False),
])
def test_context_object_tampering_refused(staged,field,value):
    from dataclasses import replace
    original=load(staged)
    with pytest.raises(context.ContextError):validate(replace(original,**{field:value}),[ref(staged['observation'])])


@pytest.mark.parametrize('mutation',[
    lambda b:b.update(schema_version=True),
    lambda b:b.update(generation=True),
    lambda b:b.update(root_generation=0),
    lambda b:b.update(consumer_id='../escape'),
    lambda b:b.update(unknown='field'),
    lambda b:b['records'][0].update(unknown='field'),
    lambda b:b['records'][0].update(producer_generation=True),
    lambda b:b['records'][0].update(input_revision_sha256='invalid'),
    lambda b:b['records'][0].update(producer_attempt_id=b['consumer_id']),
    lambda b:b['records'][0]['content'].update(provenance='controller'),
    lambda b:b['records'][0]['content'].update(summary={'instructions':'execute'}),
])
def test_rehashed_invalid_inner_schema_refused(staged,mutation):
    value=load(staged)
    with pytest.raises(context.ContextError):validate(rewrite(value,mutation),[ref(staged['observation'])])


def test_reformatted_and_duplicate_key_json_not_canonical(staged):
    from dataclasses import replace
    value=load(staged)
    for canonical in (json.dumps(json.loads(value.canonical_json),indent=2),value.canonical_json[:-1]+',"schema_version":1}'):
        changed=replace(value,canonical_json=canonical,digest=sha(canonical.encode()),text=context._render(canonical))
        with pytest.raises(context.ContextError):validate(changed,[ref(staged['observation'])])


@pytest.mark.parametrize('refs',[None,{},[{'artifact_id':'ok','sha256':'a'*64,'path':'bad'}],
    [{'artifact_id':'ok','sha256':'a'*64}]*2])
def test_validator_rejects_invalid_expected_refs(staged,refs):
    with pytest.raises(context.ContextError):validate(load(staged,[]),refs)


def test_rehashed_decision_scope_and_carried_revision_cannot_change(staged):
    value=load(staged,[ref(staged['decision'])])
    for key,val in [('verification_scope','stage_contract'),('carried_revision_sha256','f'*64),('outcome',True)]:
        changed=rewrite(value,lambda b:b['records'][0]['content'].update({key:val}))
        with pytest.raises(context.ContextError):validate(changed,[ref(staged['decision'])])


def test_invalid_unicode_in_rehashed_inner_text_has_fixed_error(staged):
    from dataclasses import replace
    value=load(staged)
    body=json.loads(value.canonical_json);body['records'][0]['content']['summary']='\ud800'
    canonical=json.dumps(body,ensure_ascii=True)
    changed=replace(value,canonical_json=canonical,digest=sha(canonical.encode()),text=context._render(canonical))
    with pytest.raises(context.ContextError,match='binding_invalid'):validate(changed,[ref(staged['observation'])])


@pytest.fixture
def stage_finalized(staged,monkeypatch):
    """Unit seam: controller finalizer authentication is stubbed, scope/files remain real."""
    from cloudworkbench.stage_contracts import stage_contract
    st=staged['store'];producer=staged['producer']
    with st._connect() as db:
        step=db.execute('SELECT role FROM workflow_steps WHERE step_id=?',('verify',)).fetchone()
    role=step['role'];contract=stage_contract(role,'b'*64,())
    output={'schema_version':1,'contract_sha256':contract.digest,'role':role,
        'input_revision_sha256':'b'*64,'context_refs':[],'summary':'Complete structured stage evidence',
        'evidence':[{'location':'code.py:1','reason':'Exact prior-stage evidence'}]}
    if role in ('planning','plan_revision'):
        output.update(status='complete',plan=[{'step':1,'action':'Implement guarded change',
            'validation':'Run the exact regression suite'}])
    elif role in ('plan_review','code_review'):
        output.update(recommendation='approve',findings=[{'severity':'low','location':'code.py:1',
            'reason':'Document intent','resolved':False}])
    else:output['status']='complete'
    def write(metadata,body):
        path=Path(metadata['storage_path']);path.chmod(0o600);bodyraw=raw(body);path.write_bytes(bodyraw);path.chmod(0o440)
        updated={**metadata,'sha256':sha(bodyraw),'bytes':len(bodyraw)}
        with st._tx() as db:
            db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(encode(updated),updated['id']))
            db.execute("UPDATE events SET payload=? WHERE type='artifact.created' AND json_extract(payload,'$.id')=?",
                (encode({k:v for k,v in updated.items() if k!='storage_path'}),updated['id']))
        return updated
    doc=json.loads(Path(staged['observation']['storage_path']).read_bytes())
    doc['observation']['events'][0]['payload_json']=encode({'summary':encode(output),'is_error':False,'permission_denials':[]})
    staged['observation']=write(staged['observation'],doc)
    final=json.loads(Path(staged['decision']['storage_path']).read_bytes())
    final.update(verification_scope='stage_contract',stage_output=output,outcome='unverified',
        observation_metadata_sha256=sha(raw(staged['observation'])))
    staged['decision']=write({**staged['decision'],'provenance':'controller_stage_contract'},final)
    with st._tx() as db:
        db.execute('UPDATE attempts SET outcome=?,result=? WHERE id=?',('unverified',encode({'routed_stage_decision':
            {'artifact_id':staged['decision']['id'],'sha256':staged['decision']['sha256']}}),producer['id']))
        db.execute("UPDATE events SET type='workflow.stage_decision',payload=? WHERE type='workflow.task_decision'",
            (encode({'artifact_id':staged['decision']['id'],'sha256':staged['decision']['sha256'],
                'outcome':'unverified','gate':'pass','verification_scope':'stage_contract'}),))
    calls=[]
    def authenticated(db,identity,policy):
        assert db.in_transaction and identity==producer['id']
        calls.append(identity)
        return final,staged['decision']
    monkeypatch.setattr(context,'_load_stage_finalizer',authenticated)
    return staged,output,calls


@pytest.mark.parametrize('staged',[{'role':'planning'},{'role':'code_review'}],indirect=True)
def test_finalized_stage_context_preserves_full_plan_review_and_evidence(stage_finalized):
    value,output,calls=stage_finalized
    result=load(value,[ref(value['decision']),ref(value['observation'])])
    records=json.loads(result.canonical_json)['records']
    assert len(calls)==2  # One producer authentication in each authority snapshot.
    for record in records:
        assert record['content']=={'provenance':'worker_reported','verification_scope':'stage_contract','stage_output':output}
    assert records[0]['content']['stage_output']['evidence']
    assert context.validate_stage_context(result,input_revision_sha256='b'*64,
        context_refs=[ref(value['decision']),ref(value['observation'])]) is result


@pytest.mark.parametrize('staged',[{'role':'planning'}],indirect=True)
def test_failed_finalizer_authentication_never_projects_stage_data(stage_finalized,monkeypatch):
    value,_,_=stage_finalized
    def refused(*a,**k):raise context.ContextError('context_stage_finalization_invalid')
    monkeypatch.setattr(context,'_load_stage_finalizer',refused)
    with pytest.raises(context.ContextError,match='stage_finalization'):load(value,[ref(value['decision'])])


@pytest.mark.parametrize('staged',[{'role':'planning'}],indirect=True)
def test_stage_finalizer_link_drift_between_reads_refused(stage_finalized,monkeypatch):
    value,_,_=stage_finalized;original=context._load_stage_finalizer;calls=0
    def changed(*a):
        nonlocal calls
        calls+=1;body,metadata=original(*a)
        return ({**body,'outcome':'needs_review'} if calls==2 else body),metadata
    monkeypatch.setattr(context,'_load_stage_finalizer',changed)
    with pytest.raises(context.ContextError,match='authority_changed'):load(value,[ref(value['decision'])])


@pytest.mark.parametrize('staged',[{'role':'planning'}],indirect=True)
def test_structured_stage_secret_in_evidence_is_rejected(stage_finalized):
    value,_,_=stage_finalized
    with pytest.raises(context.ContextError,match='secret_refused'):
        load(value,[ref(value['decision'])],forbidden_values=(b'Exact prior-stage evidence',))


@pytest.mark.parametrize('staged',[{'role':'planning'}],indirect=True)
def test_context_validator_rejects_rehashed_invalid_plan_and_authority_upgrade(stage_finalized):
    value,_,_=stage_finalized;result=load(value,[ref(value['decision'])]);refs=[ref(value['decision'])]
    for mutation in (
        lambda b:b['records'][0]['content']['stage_output']['plan'][0].update(step=7),
        lambda b:b['records'][0]['content']['stage_output'].update(role='code_review'),
        lambda b:b['records'][0]['content']['stage_output'].update(verification_pass=True),
        lambda b:b['records'][0]['content'].update(provenance='controller_verified'),
    ):
        with pytest.raises(context.ContextError):validate(rewrite(result,mutation),refs)


@pytest.mark.parametrize('staged',[{'role':'planning'}],indirect=True)
def test_real_finalizer_rejects_synthetic_incomplete_controller_envelope(stage_finalized,monkeypatch):
    value,_,_=stage_finalized
    from cloudworkbench.routed_stage_decision import load_finalized_stage
    def real(db,producer,policy):
        try:return load_finalized_stage(db,producer,forbidden_values=policy)
        except ValueError:raise context.ContextError('context_stage_finalization_invalid') from None
    monkeypatch.setattr(context,'_load_stage_finalizer',real)
    with pytest.raises(context.ContextError,match='stage_finalization_invalid'):
        load(value,[ref(value['decision'])])


def test_real_committed_planning_artifact_and_observation_feed_next_stage(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from tests import test_routed_stage_decision as stage_test
    from tests.test_routed_runtime import setup as runtime_setup
    from tests.test_routed_results import result_phase
    prepared_stage=stage_test.make_stage(tmp_path,role='planning')
    runtime=runtime_setup.__wrapped__(tmp_path,monkeypatch)
    driven=stage_test.driven.__wrapped__(prepared_stage,runtime,monkeypatch,SimpleNamespace(param='complete'))
    cleanup=stage_test.cleanup.__wrapped__(driven,tmp_path)
    data=next(cleanup)
    try:
        phase=result_phase.__wrapped__(data,tmp_path,monkeypatch)
        value=stage_test.completed_input.__wrapped__(phase,prepared_stage,tmp_path)
        committed=stage_test.complete(value)
        body,metadata=stage_test.load(value)
        stage_test.release(value,committed)
        scheduler=data['scheduler'];store=scheduler.store
        root_id=body['root_id']
        with store._connect() as db:
            next_step=dict(db.execute('SELECT * FROM workflow_steps WHERE root_id=? AND ordinal=1',(root_id,)).fetchone())
            root=dict(db.execute('SELECT * FROM workflow_roots WHERE root_id=?',(root_id,)).fetchone())
        broker=RoleBroker(store.path,authorize_parent=scheduler.authorize_parent,clock=lambda:1000)
        scope=ParentScope(root['owner_id'],root['project_id'],root_id,root_id,1,'native')
        _,token=broker.issue(scope,roles=(next_step['role'],),expires_at=2000)
        refs=[{'artifact_id':metadata['id'],'sha256':metadata['sha256']},
            {'artifact_id':value.results.observation.artifact_id,'sha256':value.results.observation.sha256}]
        pending=broker.prepare(token,native_session_id='native',native_call_id='next',
            role=next_step['role'],task='Review the exact committed plan',context_refs=tuple(refs))
        plan=json.loads(root['frozen'])['role_plans'][next_step['role']]
        scheduler.admit_request(broker,scope,pending['id'],plan=plan,step_id=next_step['step_id'],
            input_revision_sha256=body['carried_revision_sha256'])
        child=scheduler.claim_child(root_id)
        result=context.load_stage_context(scheduler,child['id'],expected_generation=1,forbidden_values=())
        projected=json.loads(result.canonical_json)['records']
        assert [v['artifact_id'] for v in projected]==[v['artifact_id'] for v in refs]
        assert all(v['content']['stage_output']==body['stage_output'] for v in projected)
        assert projected[0]['content']['stage_output']['plan'][0]['validation']=='Run the protected criterion.'
        assert all(v['content']['provenance']=='worker_reported' for v in projected)
        assert context.validate_stage_context(result,input_revision_sha256=body['carried_revision_sha256'],
            context_refs=refs) is result
    finally:
        with pytest.raises(StopIteration):next(cleanup)


@pytest.mark.parametrize('staged',[{'role':'planning'}],indirect=True)
@pytest.mark.parametrize('summary',['漢'*2700,'😀'*2000],ids=['cjk','emoji'])
def test_valid_utf8_stage_output_survives_ascii_context_serialization(stage_finalized,summary):
    value,output,_=stage_finalized
    original=load(value,[ref(value['decision'])])
    output={**output,'summary':summary}
    from cloudworkbench.stage_contracts import MAX_OUTPUT_BYTES
    utf8=json.dumps(output,ensure_ascii=False,separators=(',',':')).encode()
    assert len(utf8)<=MAX_OUTPUT_BYTES<len(json.dumps(output,ensure_ascii=True).encode())
    changed=rewrite(original,lambda body:body['records'][0]['content'].update(stage_output=output))
    assert context.validate_stage_context(changed,input_revision_sha256='b'*64,
        context_refs=[ref(value['decision'])]) is changed
    assert context._structured_output(output,'planning','b'*64)==output


def test_actual_task_reader_rejects_synthetic_incomplete_finalization(staged,monkeypatch):
    from cloudworkbench.routed_decision import load_finalized_task
    def actual(db,producer,policy):
        try:return load_finalized_task(db,producer,forbidden_values=policy)
        except ValueError:raise context.ContextError('context_task_finalization_invalid') from None
    monkeypatch.setattr(context,'_load_task_finalizer',actual)
    with pytest.raises(context.ContextError,match='task_finalization_invalid'):
        load(staged,[ref(staged['decision'])])


def test_real_finalized_task_and_observation_context(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from tests import test_routed_decision as task
    from tests.test_routed_results import result_phase
    from tests.test_routed_verification import composed,prepare,execute
    from cloudworkbench.scheduler import RoleScheduler
    from cloudworkbench.routed_decision import load_finalized_task
    original=RoleScheduler.enqueue_root
    def enqueue(scheduler,*args,frozen,**kw):
        frozen=json.loads(json.dumps(frozen))
        step=dict(frozen['provenance']['workflow']['steps'][0]);step['id']='consume'
        frozen['provenance']['workflow']['steps'].append(step)
        return original(scheduler,*args,frozen=frozen,**kw)
    monkeypatch.setattr(RoleScheduler,'enqueue_root',enqueue)
    stage=task.stage.__wrapped__(tmp_path,monkeypatch)
    runtime=task.setup.__wrapped__(tmp_path,monkeypatch)
    driven=task.driven.__wrapped__(stage,runtime,monkeypatch,SimpleNamespace(param=None))
    cleanup=task.cleanup.__wrapped__(driven,tmp_path);data=next(cleanup)
    try:
        phase=result_phase.__wrapped__(data,tmp_path,monkeypatch)
        value=composed.__wrapped__(phase,tmp_path,monkeypatch)
        prepared=prepare(value);execute(value,prepared);committed=task.complete(value,prepared)
        with data['scheduler'].store._tx() as db:body,metadata=load_finalized_task(db,committed.attempt_id)
        task.release(value,prepared,committed)
        scheduler=data['scheduler'];store=scheduler.store
        with store._connect() as db:root=dict(db.execute('SELECT * FROM workflow_roots').fetchone())
        broker=RoleBroker(store.path,authorize_parent=scheduler.authorize_parent,clock=lambda:1000)
        scope=ParentScope(root['owner_id'],root['project_id'],root['root_id'],root['root_id'],1,'native')
        _,token=broker.issue(scope,roles=('acceptance_verification',),expires_at=2000)
        refs=[ref(metadata),{'artifact_id':value.results.observation.artifact_id,'sha256':value.results.observation.sha256}]
        pending=broker.prepare(token,native_session_id='native',native_call_id='context-consume',role='acceptance_verification',task='Read historical evidence',context_refs=tuple(refs))
        scheduler.admit_request(broker,scope,pending['id'],plan=json.loads(root['frozen'])['role_plans']['acceptance_verification'],
            step_id='consume',input_revision_sha256=body['carried_revision_sha256'])
        child=scheduler.claim_child(root['root_id'])
        result=context.load_stage_context(scheduler,child['id'],expected_generation=1,forbidden_values=())
        records=json.loads(result.canonical_json)['records']
        assert records[0]['content']['verification_scope']=='task_acceptance'
        assert records[0]['content']['tested_revision_sha256']==body['tested_revision_sha256']
        assert records[1]['content']['provenance']=='worker_reported'
        assert body['input_binding']['attempt_id']==root['root_id']
    finally:cleanup.close()
