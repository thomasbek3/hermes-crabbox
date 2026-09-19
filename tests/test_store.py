from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import pytest
from cloudworkbench.store import Store, StoreError

@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'state.db')

@pytest.fixture
def owner(store):
    return store.add_client('owner', 'a' * 40, ['submit','observe','retrieve','cancel'], ['project'])

def task(agent='claude', **extra):
    return {'project_id':'project','goal':'literal $(touch forbidden)','agent':agent, **extra}

def test_hashed_auth_and_restart_durability(store, owner):
    created = store.create_session(owner, task(), 'create')
    store.claim_next()
    reopened = Store(store.path)
    assert reopened.get_attempt(created['attempt_id'])['request']['goal'] == task()['goal']
    assert reopened.active_attempts()[0]['state'] == 'preparing'
    assert reopened.authenticate('a' * 40)['id'] == owner['id']
    assert reopened.authenticate('bad') is None
    assert b'a' * 40 not in store.path.read_bytes()
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT token_hash FROM clients').fetchone()[0] == hashlib.sha256(('a'*40).encode()).hexdigest()

def test_concurrent_idempotency_single_creation(store, owner):
    with ThreadPoolExecutor(max_workers=12) as pool:
        values = list(pool.map(lambda _:store.create_session(owner, task(), 'same'), range(24)))
    assert len({v['session_id'] for v in values}) == 1
    assert len(store.list_sessions(owner)) == 1
    with pytest.raises(StoreError) as err:
        store.create_session(owner, task(goal='different'), 'same')
    assert err.value.status_code == 409

def test_atomic_capacity_across_store_connections(store, owner):
    for n in range(12):
        store.create_session(owner, task(agent=f'agent{n}'), f'create{n}')
    with ThreadPoolExecutor(max_workers=12) as pool:
        claims = list(pool.map(lambda _:Store(store.path).claim_next(capacity=2), range(24)))
    assert sum(c is not None for c in claims) == 2
    assert len(store.active_attempts()) == 2

def test_cancel_holds_lease_until_runtime_stopped(store, owner):
    first = store.create_session(owner, task(), 'first')
    second = store.create_session(owner, task(), 'second')
    claim = store.claim_next()
    assert claim['id'] == first['attempt_id']
    store.transition(claim['id'], 'running', runtime_id='runtime', expected_generation=store.get_attempt(claim['id'])['generation'])
    cancelled = store.cancel(owner, claim['id'], 'cancel')
    assert cancelled['state'] == 'running' and cancelled['cancel_requested']
    assert store.claim_next() is None
    store.transition(claim['id'], 'cancelled', expected_generation=store.get_attempt(claim['id'])['generation'])
    assert store.claim_next()['id'] == second['attempt_id']

def test_external_capacity_disabled_adapters_and_events(store, owner):
    created = store.create_session(owner, task(), 'create')
    assert store.claim_next(capacity=2, external_running=2) is None
    assert store.claim_next(blocked_agents={'claude':'auth_unavailable'}) is None
    assert store.get_attempt(created['attempt_id'])['reason'] == 'auth_unavailable'
    assert store.events(owner, created['session_id'])[-1]['type'] == 'attempt.blocked'
    store.claim_next(blocked_agents={'claude':'auth_unavailable'})
    assert len(store.events(owner, created['session_id'])) == 2

def test_followups_single_writer_and_resume_new_attempt(store, owner):
    created = store.create_session(owner, task(), 'create')
    store.claim_next()
    follow = store.add_message(owner, created['session_id'], 'next thing', 'follow')
    assert follow['delivery'] == 'queued'
    assert store.claim_next(capacity=10) is None
    with pytest.raises(StoreError):
        store.resume(owner, created['session_id'], 'early-resume')
    store.transition(created['attempt_id'], 'failed', reason='setup', expected_generation=store.get_attempt(created['attempt_id'])['generation'])
    next_attempt = store.claim_next()
    assert next_attempt['turn_id'] == follow['turn_id']
    assert next_attempt['request']['goal'] == 'next thing'
    store.transition(next_attempt['id'], 'interrupted', reason='missing_runtime', expected_generation=store.get_attempt(next_attempt['id'])['generation'])
    resumed = store.resume(owner, created['session_id'], 'resume')
    assert resumed['attempt_id'] != next_attempt['id']
    assert resumed['resume_mode'] == 'reconstructed'
    assert store.get_attempt(resumed['attempt_id'])['parent_attempt_id'] == next_attempt['id']

def test_outcomes_terminal_immutability_and_transactional_events(store, owner):
    created = store.create_session(owner, task(), 'create')
    aid, sid = created['attempt_id'], created['session_id']
    store.claim_next()
    before = store.events(owner, sid)
    with pytest.raises(StoreError):
        store.transition(aid, 'completed', outcome='verified', expected_generation=store.get_attempt(aid)['generation'])
    assert store.events(owner, sid) == before
    store.transition(aid, 'running', expected_generation=store.get_attempt(aid)['generation'])
    store.transition(aid, 'verifying', expected_generation=store.get_attempt(aid)['generation'])
    with pytest.raises(StoreError):
        store.transition(aid, 'completed', expected_generation=store.get_attempt(aid)['generation'])
    store.transition(aid, 'completed', outcome='rejected', exit_code=0, expected_generation=store.get_attempt(aid)['generation'])
    with pytest.raises(StoreError):
        store.transition(aid, 'running', expected_generation=store.get_attempt(aid)['generation'])
    events = store.events(owner, sid)
    assert [e['sequence'] for e in events] == list(range(1,len(events)+1))
    assert store.events(owner,sid, after=events[-2]['sequence']) == events[-1:]

def test_principal_isolation_and_input_readiness(store, owner):
    stranger = store.add_client('stranger', 'b'*40, ['submit','observe','retrieve','cancel'], ['project'])
    created = store.create_session(owner, task(), 'create')
    for op in [lambda:store.get_session(stranger,created['session_id']), lambda:store.events(stranger,created['session_id']), lambda:store.cancel(stranger,created['attempt_id'],'cancel'), lambda:store.add_message(stranger,created['session_id'],'hi','message')]:
        with pytest.raises(StoreError) as err:
            op()
        assert err.value.status_code == 404
    item = store.reserve_input(owner, {'name':'a.txt','mime':'text/plain'}, 'input')
    with pytest.raises(StoreError):
        store.create_session(owner, task(input_ids=[item['id']]), 'pending')
    store.finalize_input(owner,item['id'], {'storage_path':'/trusted/a','sha256':'a'*64,'bytes':3})
    store.create_session(owner, task(input_ids=[item['id']]), 'ready')
    with pytest.raises(StoreError):
        store.create_session(stranger,task(input_ids=[item['id']]),'foreign-input')

def test_observer_cannot_mutate(store):
    observer=store.add_client('observer','a'*40,['observe'],['project'])
    with pytest.raises(StoreError) as err:
        store.create_session(observer,task(),'key')
    assert err.value.status_code==403

def test_fencing_rejects_stale_generation_without_event(store, owner):
    created=store.create_session(owner,task(),'create')
    attempt=store.claim_next()
    fenced=store.fence_attempt(attempt['id'],attempt['generation'])
    before=store.events(owner,created['session_id'])
    with pytest.raises(StoreError) as err:
        store.transition(attempt['id'],'running',expected_generation=attempt['generation'])
    assert err.value.status_code==409
    assert store.events(owner,created['session_id'])==before
    assert store.transition(attempt['id'],'running',expected_generation=fenced['generation'])['state']=='running'

def test_fenced_artifact_and_event_writes_rejected(store, owner):
    created=store.create_session(owner,task(),'create')
    attempt=store.claim_next()
    store.fence_attempt(attempt['id'],attempt['generation'])
    for operation in [lambda:store.append_event(attempt['id'],'assistant.message',{'text':'old'},expected_generation=attempt['generation']),lambda:store.register_artifact(attempt['id'],{'path':'old'},expected_generation=attempt['generation'])]:
        with pytest.raises(StoreError) as err:
            operation()
        assert err.value.status_code==409
    assert store.list_artifacts(owner,created['session_id'])==[]

def test_newer_schema_fail_closed_without_mutation(store):
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE schema_version SET version=999')
    with pytest.raises(StoreError) as err:
        Store(store.path)
    assert err.value.status_code==503
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT version FROM schema_version').fetchall()==[(999,)]

def test_attempt_inputs_and_context_are_session_scoped(store, owner):
    item=store.reserve_input(owner,{'name':'source.txt','mime':'text/plain'},'input')
    store.finalize_input(owner,item['id'],{'storage_path':'/inputs/protected','sha256':'a'*64,'bytes':3})
    first=store.create_session(owner,task(input_ids=[item['id']]),'first')
    unrelated=store.create_session(owner,task(goal='private different session'),'other')
    attempt=store.claim_next()
    inputs=store.get_attempt_inputs(attempt['id'],attempt['generation'])
    assert inputs[0]['storage_path']=='/inputs/protected'
    with pytest.raises(StoreError):
        store.get_attempt_inputs(attempt['id'],attempt['generation']+1)
    store.transition(attempt['id'],'running',expected_generation=attempt['generation'])
    store.transition(attempt['id'],'verifying',expected_generation=attempt['generation'])
    store.transition(attempt['id'],'completed',expected_generation=attempt['generation'],outcome='unverified',result={'summary':'delivered first','private_native_state':'must not appear'})
    follow=store.add_message(owner,first['session_id'],'do next','next')
    context=store.get_context(follow['attempt_id'])
    assert context==[{'turn_id':first['turn_id'],'goal':task()['goal'],'outcome':'unverified','summary':'delivered first'}]

def test_claim_records_execution_start_excluding_queue_time(store, owner):
    created=store.create_session(owner,task(),'create')
    assert store.get_attempt(created['attempt_id'])['started_at'] is None
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE attempts SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?",(created['attempt_id'],))
    claimed=store.claim_next()
    assert claimed['started_at'] > '2020'
    assert claimed['created_at'].startswith('2000')

def test_acceptance_canonicalization_and_unique_ids(store, owner):
    from pydantic import ValidationError
    created=store.create_session(owner,task(acceptance=['Human visual check',{'id':'smoke','description':'Protected test','mandatory':True}]),'create')
    criteria=store.get_attempt(created['attempt_id'])['request']['acceptance']
    assert criteria==[{'id':'manual-1','description':'Human visual check','mandatory':True},{'id':'smoke','description':'Protected test','mandatory':True}]
    with pytest.raises(ValidationError):
        store.create_session(owner,task(acceptance=['Human',{'id':'manual-1','description':'Collision'}]),'duplicate')

def test_pending_limit_concurrent_admission_has_no_partial_sessions(tmp_path):
    policy={'max_pending_attempts':3}
    store=Store(tmp_path/'state.db',policy=policy)
    owner=store.add_client('owner','a'*40,['submit','observe','cancel'],['project'])
    def create(index):
        try:
            return Store(store.path,policy=policy).create_session(owner,task(),f'create-{index}')
        except StoreError as exc:
            assert exc.status_code==503
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(create,range(24)))
    admitted=[value for value in results if value]
    assert len(admitted)==3
    with sqlite3.connect(store.path) as db:
        assert [db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('sessions','turns','attempts','events','idempotency')]==[3,3,3,3,3]
    index=next(index for index,value in enumerate(results) if value)
    assert store.create_session(owner,task(),f'create-{index}')==results[index]
    store.cancel(owner,admitted[0]['attempt_id'],'cancel')
    assert store.create_session(owner,task(),'after-cancel')['state']=='queued'


def test_pending_limit_counts_live_and_rolls_back_followup_resume(tmp_path):
    store=Store(tmp_path/'state.db',policy={'max_pending_attempts':1})
    owner=store.add_client('owner','a'*40,['submit','observe','cancel'],['project'])
    first=store.create_session(owner,task(),'first')
    running=store.claim_next()
    with pytest.raises(StoreError) as error:
        store.add_message(owner,first['session_id'],'followup','follow')
    assert error.value.status_code==503
    assert len(store.get_session(owner,first['session_id'])['turns'])==1
    store.transition(running['id'],'failed',reason='setup',expected_generation=running['generation'])
    second=store.create_session(owner,task(),'second')
    with pytest.raises(StoreError) as error:
        store.resume(owner,first['session_id'],'resume')
    assert error.value.status_code==503
    assert len(store.get_session(owner,first['session_id'])['attempts'])==1
    store.cancel(owner,second['attempt_id'],'cancel')
    resumed=store.resume(owner,first['session_id'],'resume')
    assert store.resume(owner,first['session_id'],'resume')==resumed
    assert store.claim_next()['id']==resumed['attempt_id']


def test_owner_input_reservation_concurrency_and_idempotent_retry(tmp_path):
    policy={'input_max_bytes':10,'input_owner_bytes':30,'input_global_bytes':100}
    store=Store(tmp_path/'state.db',policy=policy)
    owner=store.add_client('owner','a'*40,['submit'],['project'])
    request={'name':'input','mime':'text/plain'}
    def reserve(index):
        try:
            return Store(store.path,policy=policy).reserve_input(owner,request,f'reserve-{index}')
        except StoreError as exc:
            assert exc.status_code==429
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(reserve,range(24)))
    assert sum(value is not None for value in results)==3
    index=next(index for index,value in enumerate(results) if value)
    assert store.reserve_input(owner,request,f'reserve-{index}')==results[index]
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM inputs').fetchone()[0]==3
        assert db.execute('SELECT COUNT(*) FROM idempotency').fetchone()[0]==3


def test_global_input_reservations_across_principals(tmp_path):
    policy={'input_max_bytes':10,'input_owner_bytes':100,'input_global_bytes':40}
    store=Store(tmp_path/'state.db',policy=policy)
    owners=[store.add_client(f'owner{i}',str(i)*40,['submit'],['project']) for i in range(4)]
    def reserve(index):
        try:
            return Store(store.path,policy=policy).reserve_input(owners[index%4],{'name':'input','mime':'text/plain'},f'input-{index}')
        except StoreError as exc:
            assert exc.status_code==503
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(reserve,range(24)))
    assert sum(value is not None for value in results)==4
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM inputs').fetchone()[0]==4


def test_finalized_inputs_charge_actual_bytes_and_enforce_file_max(tmp_path):
    store=Store(tmp_path/'state.db',policy={'input_max_bytes':10,'input_owner_bytes':20,'input_global_bytes':100})
    owner=store.add_client('owner','a'*40,['submit'],['project'])
    request={'name':'input','mime':'text/plain'}
    first=store.reserve_input(owner,request,'first')
    store.reserve_input(owner,request,'second')
    for size,status in [(11,413),(-1,422),(True,422)]:
        with pytest.raises(StoreError) as err:
            store.finalize_input(owner,first['id'],{'bytes':size,'sha256':'a'*64,'storage_path':'/input'},key='put')
        assert err.value.status_code==status
    assert store.get_input(owner,first['id'])['state']=='pending'
    result=store.finalize_input(owner,first['id'],{'bytes':0,'sha256':'a'*64,'storage_path':'/input'},key='put')
    assert result['state']=='ready'
    assert store.finalize_input(owner,first['id'],{'bytes':0,'sha256':'a'*64,'storage_path':'/retry'},key='put')==result
    third=store.reserve_input(owner,request,'third')
    assert third['state']=='pending'
    with pytest.raises(StoreError) as err:
        store.reserve_input(owner,request,'fourth')
    assert err.value.status_code==429


def test_default_limits_and_invalid_policy(tmp_path):
    store=Store(tmp_path/'default.db')
    assert store.policy=={'max_pending_attempts':100,'input_max_bytes':100*1024**2,'input_owner_bytes':1024**3,'input_global_bytes':4*1024**3}
    for policy in [{'unknown':1},{'max_pending_attempts':0},{'input_max_bytes':True},{'input_global_bytes':-1}]:
        with pytest.raises(ValueError):
            Store(tmp_path/'invalid.db',policy=policy)

def test_source_event_dedupe_replays_atomically_and_rejects_mismatch(store,owner):
    created=store.create_session(owner,task(),'create')
    payload={'text':'hello','source':'provider'}
    sequence=store.append_event(created['attempt_id'],'assistant.message',payload,expected_generation=created['generation'],dedupe_key='record-1')
    assert store.append_event(created['attempt_id'],'assistant.message',dict(reversed(list(payload.items()))),expected_generation=created['generation'],dedupe_key='record-1')==sequence
    assert '_source_event' not in payload
    with pytest.raises(StoreError) as err:
        store.append_event(created['attempt_id'],'assistant.message',{'text':'changed'},expected_generation=created['generation'],dedupe_key='record-1')
    assert err.value.status_code==409
    events=store.events(owner,created['session_id'])
    assert len(events)==2 and events[-1]['payload']['_source_event']=='record-1'
    other_type=store.append_event(created['attempt_id'],'tool.started',payload,expected_generation=created['generation'],dedupe_key='record-1')
    assert other_type==sequence+1
    first=store.append_event(created['attempt_id'],'assistant.message',payload,expected_generation=created['generation'])
    second=store.append_event(created['attempt_id'],'assistant.message',payload,expected_generation=created['generation'])
    assert second==first+1


def test_concurrent_source_event_dedupe_and_key_validation(store,owner):
    created=store.create_session(owner,task(),'create')
    def append(_):
        return Store(store.path).append_event(created['attempt_id'],'assistant.message',{'text':'one record'},expected_generation=created['generation'],dedupe_key='source1')
    with ThreadPoolExecutor(max_workers=12) as pool:
        sequences=list(pool.map(append,range(24)))
    assert set(sequences)=={2}
    assert len(store.events(owner,created['session_id']))==2
    for key in ['', 'x'*129, 123]:
        with pytest.raises(StoreError) as err:
            store.append_event(created['attempt_id'],'assistant.message',{},expected_generation=created['generation'],dedupe_key=key)
        assert err.value.status_code==422


def test_environment_snapshot_is_prior_generation_and_session_scoped(store,owner):
    first=store.create_session(owner,task(),'first')
    active=store.claim_next()
    snapshot={'manifest':{'version':'pinned'},'manifest_sha256':'a'*64}
    store.transition(active['id'],'preparing',expected_generation=active['generation'],result={'environment':snapshot})
    assert store.get_environment_snapshot(first['session_id'],active['generation']) is None
    assert store.get_environment_snapshot(first['session_id'],active['generation']+1)==snapshot
    other=store.create_session(owner,task(),'other')
    assert store.get_environment_snapshot(other['session_id'],active['generation']+1) is None
    snapshot['manifest']['version']='caller mutation'
    assert store.get_environment_snapshot(first['session_id'],active['generation']+1)['manifest']['version']=='pinned'
    with pytest.raises(ValueError):
        store.get_environment_snapshot(first['session_id'],True)


def test_project_authorization_filters_before_session_pagination(tmp_path):
    store = Store(tmp_path / 'pagination.sqlite')
    owner = store.add_client('pagination', 'p' * 40, ['submit', 'observe'], ['allowed', 'denied'])
    sessions = []
    for index, project in enumerate(('allowed', 'denied', 'allowed', 'denied')):
        task = store.create_session(owner, {'project_id': project, 'goal': 'pagination fixture', 'agent': 'fixture', 'environment_version': 'test'}, str(index))
        sessions.append(task['session_id'])
        with store._tx() as db:
            db.execute('UPDATE sessions SET created_at=? WHERE id=?', (f'2026-01-0{index+1}T00:00:00+00:00', task['session_id']))
    restricted = {**owner, 'projects': ['allowed']}
    assert [s['id'] for s in store.list_sessions(restricted, limit=1, offset=0)] == [sessions[2]]
    assert [s['id'] for s in store.list_sessions(restricted, limit=1, offset=1)] == [sessions[0]]
    assert store.list_sessions(restricted, limit=1, offset=2) == []
    assert store.list_sessions({**owner, 'projects': []}) == []
