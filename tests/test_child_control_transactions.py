import sqlite3
import time
import pytest
from test_child_launch import child_launch, setup, bind, params
from cloudworkbench import scheduler as module
from cloudworkbench.store import StoreError


@pytest.mark.parametrize('method',['bind','begin_child_start','check_child_start_authority','confirm_child_started','fence_child_execution'])
def test_real_writer_contention_is_fail_fast_without_global_timeout_change(child_launch,method):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    holder=st._connect()
    try:
        assert holder.execute('PRAGMA busy_timeout').fetchone()[0]==30000
        holder.execute('BEGIN IMMEDIATE')
        started=time.monotonic()
        with pytest.raises(StoreError,match='database unavailable'):
            bind(child_launch) if method=='bind' else getattr(s,method)(c['id'],**params(bound))
        assert time.monotonic()-started<.5
        assert s.read_child_launch(c['id'],expected_generation=1)==bound
    finally:holder.rollback();holder.close()
    assert s.read_child_launch(c['id'],expected_generation=1)==bound
    assert s.begin_child_start(c['id'],**params(bound)).may_start


def test_long_sqlite_vm_is_interrupted_and_transaction_rolls_back(child_launch):
    st,o,l,s,r,c,args=child_launch;before=st.get_attempt(c['id'])
    started=time.monotonic()
    with pytest.raises(StoreError,match='database unavailable'):
        with module._child_control_tx(st) as db:
            db.execute("UPDATE attempts SET reason='synthetic' WHERE id=?",(c['id'],))
            db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n').fetchone()
    assert time.monotonic()-started<.5
    assert st.get_attempt(c['id'])==before


def test_expired_before_commit_rolls_back_and_never_returns_permission(child_launch,monkeypatch):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    original=st._event
    def delayed(*a,**kw):
        value=original(*a,**kw);time.sleep(.07);return value
    with monkeypatch.context() as patch:
        patch.setattr(st,'_event',delayed)
        with pytest.raises(StoreError,match='database deadline'):s.begin_child_start(c['id'],**params(bound))
    assert s.read_child_launch(c['id'],expected_generation=1).state=='bound'
    assert s.begin_child_start(c['id'],**params(bound)).may_start


@pytest.mark.parametrize('failure',['ack_lost','ack_late'])
def test_committed_intent_with_uncertain_ack_cannot_regrant_launch(child_launch,monkeypatch,failure):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    class Connection(sqlite3.Connection):
        def commit(self):
            super().commit()
            if failure=='ack_lost':raise sqlite3.OperationalError('synthetic acknowledgement failure')
            time.sleep(.07)
    def connect(path):
        db=sqlite3.connect(path,timeout=0,isolation_level=None,factory=Connection);db.row_factory=sqlite3.Row;return db
    with monkeypatch.context() as patch:
        patch.setattr(module,'_control_connect',connect)
        with pytest.raises(StoreError,match='commit outcome uncertain'):s.begin_child_start(c['id'],**params(bound))
    assert s.read_child_launch(c['id'],expected_generation=1).state=='start_intent'
    assert not s.begin_child_start(c['id'],**params(bound)).may_start


def test_precommit_failure_is_uncertain_to_caller_but_rollback_preserves_bound(child_launch,monkeypatch):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    class Connection(sqlite3.Connection):
        def commit(self):raise sqlite3.OperationalError('synthetic precommit failure')
    def connect(path):
        db=sqlite3.connect(path,timeout=0,isolation_level=None,factory=Connection);db.row_factory=sqlite3.Row;return db
    with monkeypatch.context() as patch:
        patch.setattr(module,'_control_connect',connect)
        with pytest.raises(StoreError,match='commit outcome uncertain'):s.begin_child_start(c['id'],**params(bound))
    assert s.read_child_launch(c['id'],expected_generation=1).state=='bound'


def test_uncertain_fence_commit_keeps_fence_and_occupancy(child_launch,monkeypatch):
    st,o,l,s,r,c,args=child_launch;bound=bind(child_launch)
    class Connection(sqlite3.Connection):
        def commit(self):super().commit();raise sqlite3.OperationalError('ack lost')
    def connect(path):
        db=sqlite3.connect(path,timeout=0,isolation_level=None,factory=Connection);db.row_factory=sqlite3.Row;return db
    with monkeypatch.context() as patch:
        patch.setattr(module,'_control_connect',connect)
        with pytest.raises(StoreError,match='commit outcome uncertain'):s.fence_child_execution(c['id'],**params(bound))
    assert s.read_child_launch(c['id'],expected_generation=1).state=='fenced'
    with st._connect() as db:assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0]==c['id']
    assert not st.get_attempt(c['id'])['cancel_requested']


@pytest.mark.parametrize('method', ['begin_child_start', 'check_child_start_authority', 'fence_child_execution'])
def test_brief_real_writer_lock_is_acquired_within_control_budget(child_launch, monkeypatch, method):
    import threading
    st, _, _, scheduler, _, child, _ = child_launch
    bound = bind(child_launch)
    if method == 'check_child_start_authority':
        scheduler.begin_child_start(child['id'], **params(bound))
    held, attempted, released = threading.Event(), threading.Event(), threading.Event()
    failures = []
    def writer():
        try:
            with st._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                held.set()
                assert attempted.wait(2)
                time.sleep(.005)
                db.commit()
                released.set()
        except BaseException as exc:
            failures.append(exc)
    thread = threading.Thread(target=writer)
    thread.start()
    assert held.wait(2)
    calls = []
    class Connection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'BEGIN IMMEDIATE':
                calls.append(sql)
                attempted.set()
            return super().execute(sql, *args, **kwargs)
    def connect(path):
        db=sqlite3.connect(path,timeout=0,isolation_level=None,factory=Connection)
        db.row_factory=sqlite3.Row
        return db
    started = time.monotonic()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, '_control_connect', connect)
            result = getattr(scheduler, method)(child['id'], **params(bound))
        assert released.is_set() and time.monotonic()-started < .5
        assert len(calls) >= 2
        if method == 'begin_child_start':
            assert result.may_start
            assert not scheduler.begin_child_start(child['id'], **params(bound)).may_start
        elif method == 'fence_child_execution':
            assert scheduler.read_child_launch(child['id'], expected_generation=1).state == 'fenced'
    finally:
        attempted.set()
        thread.join(2)
    assert not thread.is_alive() and not failures


def test_busy_inside_yielded_body_is_not_replayed(child_launch, monkeypatch):
    st = child_launch[0]
    connections = []
    original = module._control_connect
    def connect(path):
        db = original(path)
        connections.append(db)
        return db
    monkeypatch.setattr(module, '_control_connect', connect)
    bodies = []
    with pytest.raises(StoreError, match='database unavailable'):
        with module._child_control_tx(st) as db:
            bodies.append(True)
            db.execute("UPDATE attempts SET reason='must-rollback' WHERE id=?", (child_launch[5]['id'],))
            error = sqlite3.OperationalError('synthetic busy after body')
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
    assert len(connections) == len(bodies) == 1
    assert st.get_attempt(child_launch[5]['id'])['reason'] is None


def test_acquisition_consumes_original_deadline_without_yielding_after_expiry(child_launch, monkeypatch):
    st = child_launch[0]
    ticks = [0.0]
    attempts = []
    class Connection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'BEGIN IMMEDIATE':
                attempts.append(sql)
                ticks[0] += .03
                if len(attempts) == 1:
                    error = sqlite3.OperationalError('synthetic begin busy')
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
            return super().execute(sql, *args, **kwargs)
    def connect(path):
        db=sqlite3.connect(path,timeout=0,isolation_level=None,factory=Connection)
        db.row_factory=sqlite3.Row
        return db
    monkeypatch.setattr(module, '_control_connect', connect)
    monkeypatch.setattr(module.time, 'monotonic', lambda: ticks[0])
    monkeypatch.setattr(module.time, 'sleep', lambda delay: ticks.__setitem__(0,ticks[0]+delay))
    with pytest.raises(StoreError, match='database deadline'):
        with module._child_control_tx(st):
            pytest.fail('expired acquisition granted a transaction body')
    assert len(attempts) == 2 and ticks[0] < .062
