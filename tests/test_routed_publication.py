from dataclasses import replace
import hashlib
import json
import os
import sqlite3

import pytest

from tests.test_routed_cleanup import cleanup, driven, stage, setup, ready, request
from cloudworkbench import routed_publication as module
from cloudworkbench.routed_publication import publish_observation, authorize_result, result_authority, PublicationError
from cloudworkbench.scheduler import _child_control_tx
from cloudworkbench.store import StoreError
from cloudworkbench.routed_driver import StageDriveError


@pytest.fixture
def publication(cleanup, tmp_path):
    request(cleanup)
    collector = ready(cleanup)
    evidence = collector.collect()
    root = tmp_path / 'published'
    root.mkdir(mode=0o700)
    args = dict(prepared=cleanup['driven'][3], spec=cleanup['spec'], quiesced=collector.quiesced,
        execution_profile=cleanup['wrapped'].executor.profile, collector=collector, cleanup=evidence,
        publication_root=root)
    return cleanup, args


def publish(value, **changes):
    data, args = value
    return publish_observation(data['scheduler'], **{**args, **changes})


def counts(value):
    with value[0]['scheduler'].store._connect() as db:
        return tuple(db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                     for name in ('artifacts','workflow_step_gates'))


def test_atomic_worker_observation_publication_exact_replay_and_no_promotion(publication):
    value, args = publication
    scheduler = value['scheduler']
    before = scheduler.store.get_attempt(value['spec'].attempt_id)
    first = publish(publication)
    assert first == publish(publication)
    assert counts(publication) == (1, 0)
    assert scheduler.store.get_attempt(value['spec'].attempt_id) == before
    with scheduler.store._connect() as db:
        metadata = json.loads(db.execute('SELECT metadata FROM artifacts').fetchone()[0])
        events = db.execute("SELECT * FROM events WHERE type='workflow.observation_published'").fetchall()
        assert len(events) == 1 and events[0]['sequence'] == first.event_sequence
        assert db.execute('SELECT child_attempt_id FROM workflow_roots').fetchone()[0] == before['id']
    raw = open(metadata['storage_path'], 'rb').read()
    assert hashlib.sha256(raw).hexdigest() == first.sha256
    doc = json.loads(raw)
    assert doc['provenance'] == 'worker_reported' and doc['verification_pass'] is False
    assert doc['gate_authorized'] is False and doc['revision_promotion_authorized'] is False
    assert doc['observation']['status'] == 'completed'
    assert scheduler.leases.current('account')['reservation'] == value['reservation']
    assert args['quiesced'].observation.result_json


@pytest.mark.parametrize('change', ['cancel_child','cancel_root','revoke_client','root_generation','foreign_owner','deadline'])
def test_publication_refuses_changed_authority_and_assignment(publication, change):
    value, args = publication
    scheduler = value['scheduler']; child = args['spec'].attempt_id
    with scheduler.store._tx() as db:
        root = db.execute('SELECT root_id FROM workflow_roots').fetchone()[0]
        if change == 'cancel_child': db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (child,))
        elif change == 'cancel_root': db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?', (root,))
        elif change == 'revoke_client': db.execute("UPDATE clients SET revoked_at='revoked'")
        elif change == 'root_generation': db.execute('UPDATE attempts SET generation=generation+1 WHERE id=?', (root,))
        elif change == 'foreign_owner': db.execute("UPDATE provider_reservations SET controller_instance_id='foreign'")
        elif change == 'deadline': db.execute('UPDATE workflow_roots SET deadline_at=999')
    with pytest.raises((PublicationError, StoreError)):
        publish(publication)
    assert counts(publication) == (0, 0)
    assert not list(args['publication_root'].iterdir())


def test_readonly_authority_works_in_query_only_transaction(publication):
    value, args = publication
    scheduler = value['scheduler']
    bound = authorize_result(scheduler, args['spec'], args['quiesced'])
    with _child_control_tx(scheduler.store, write=False) as db:
        db.execute('PRAGMA query_only=ON')
        result = result_authority(scheduler, db, args['spec'], args['quiesced'])
        assert result['binding'] == bound
        assert result['assignment']['input_revision_sha256'] == bound.input_revision_sha256


@pytest.mark.parametrize('field,value', [('verification_pass',True),('provenance','controller_verified')])
def test_worker_controller_claim_is_rejected(publication, field, value):
    obs = publication[1]['quiesced'].observation
    raw = json.loads(obs.result_json); raw[field] = value
    with pytest.raises(ValueError):
        publish(publication, result_bytes=json.dumps(raw).encode())
    assert counts(publication) == (0, 0)


def test_raw_and_parsed_observation_must_match(publication):
    obs = publication[1]['quiesced'].observation
    with pytest.raises(ValueError):
        publish(publication, events_bytes=obs.events_jsonl.encode().replace(b'done',b'changed'))
    assert counts(publication) == (0, 0)


def test_known_secret_in_worker_output_is_not_published(publication):
    with pytest.raises(PublicationError, match='secret_rejected'):
        publish(publication, forbidden_values=(b'done',))
    assert counts(publication) == (0, 0)


def test_staged_file_does_not_publish_after_cancel_race(publication, monkeypatch):
    value,args = publication; original = module._stage
    def race(root, raw):
        path = original(root,raw)
        with value['scheduler'].store._tx() as db:
            db.execute('UPDATE attempts SET cancel_requested=1 WHERE id=?',(args['spec'].attempt_id,))
        return path
    monkeypatch.setattr(module,'_stage',race)
    with pytest.raises(PublicationError): publish(publication)
    assert counts(publication) == (0, 0)
    assert len(list(args['publication_root'].glob('worker-observation-*.json'))) == 1


def test_event_failure_rolls_back_artifact_and_exact_retry_succeeds(publication, monkeypatch):
    scheduler = publication[0]['scheduler']; original = scheduler.store._event
    def fail(db, session, attempt, kind, payload):
        if kind == 'workflow.observation_published': raise RuntimeError('synthetic failed event')
        return original(db,session,attempt,kind,payload)
    monkeypatch.setattr(scheduler.store,'_event',fail)
    with pytest.raises(RuntimeError, match='synthetic failed event'): publish(publication)
    assert counts(publication) == (0, 0)
    monkeypatch.setattr(scheduler.store,'_event',original)
    assert publish(publication) == publish(publication)
    assert counts(publication) == (1, 0)


def test_cleanup_file_tamper_or_symlink_refuses(publication):
    path = publication[1]['cleanup'].evidence_path
    path.write_bytes(b'{}')
    with pytest.raises(ValueError): publish(publication)
    assert counts(publication) == (0, 0)


def test_publication_directory_symlink_refuses(publication, tmp_path):
    target = tmp_path/'link'; target.symlink_to(publication[1]['publication_root'])
    with pytest.raises(OSError): publish(publication, publication_root=target)
    assert counts(publication) == (0, 0)


def test_existing_file_drift_never_replayed(publication):
    publish(publication)
    path = next(publication[1]['publication_root'].glob('worker-observation-*.json'))
    path.chmod(0o600); path.write_bytes(b'{}')
    with pytest.raises(PublicationError): publish(publication)
    assert counts(publication) == (1, 0)


@pytest.mark.parametrize('field', ['profile','revision'])
def test_substituted_prepared_profile_or_revision_refused(publication, field):
    prepared = publication[1]['prepared']
    if field == 'profile': prepared = replace(prepared, profile_digest='f'*64)
    else: prepared = replace(prepared, materialization=replace(prepared.materialization, revision_sha256='f'*64))
    with pytest.raises(StageDriveError): publish(publication, prepared=prepared)
    assert counts(publication) == (0, 0)


def test_foreign_prepared_consumer_scope_refused(publication):
    prepared = publication[1]['prepared']
    prepared = replace(prepared, materialization=replace(prepared.materialization,
        consumer=replace(prepared.materialization.consumer, owner_id='foreign')))
    with pytest.raises(PublicationError, match='prepared_scope_changed'):
        publish(publication, prepared=prepared)
    assert counts(publication) == (0, 0)


def test_current_cleanup_membership_changes_after_recollect_are_refused(publication, monkeypatch):
    value,args = publication; original = module._stage
    def race(root, raw):
        path = original(root,raw)
        with value['scheduler'].store._tx() as db:
            db.execute("UPDATE provider_request_leases SET cleanup_receipt='{}'")
        return path
    monkeypatch.setattr(module,'_stage',race)
    with pytest.raises(PublicationError, match='cleanup_changed'): publish(publication)
    assert counts(publication) == (0, 0)


def test_postcommit_uncertainty_replays_exact_existing_artifact(publication, monkeypatch):
    from contextlib import contextmanager
    original=module._child_control_tx; raised=[]
    @contextmanager
    def uncertain(store, *, write=True):
        with original(store, write=write) as db: yield db
        if write and not raised:
            raised.append(True)
            raise StoreError(503, 'Child control commit outcome uncertain')
    monkeypatch.setattr(module,'_child_control_tx',uncertain)
    with pytest.raises(StoreError, match='uncertain'): publish(publication)
    assert counts(publication) == (1, 0)
    assert publish(publication) == publish(publication)
    assert counts(publication) == (1, 0)


def test_concurrent_exact_publication_never_duplicates_db_records(publication, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    original=module._stage; barrier=threading.Barrier(2)
    preflight=threading.Lock(); local=threading.local(); reached=[False,False]
    def stage(root, raw):
        # Serialize only preflight/cleanup. Both DB transactions have ended
        # before the barrier, so a legitimate early busy refusal cannot strand
        # the peer. Filesystem publication and final DB commit still race.
        reached[local.index]=True
        local.held=False;preflight.release()
        barrier.wait(2)
        return original(root,raw)
    monkeypatch.setattr(module,'_stage',stage)
    def race(index):
        preflight.acquire();local.held=True;local.index=index
        try:return publish(publication)
        finally:
            if local.held:preflight.release()
    results=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(race,index) for index in range(2)]
        for future in futures:
            try:results.append(future.result(timeout=5))
            except StoreError as exc:
                assert reached==[True,True] and exc.status_code==503
    assert reached==[True,True]
    monkeypatch.setattr(module,'_stage',original)
    final=publish(publication)
    assert all(result == final for result in results)
    assert counts(publication) == (1, 0)


def test_revoked_client_cannot_replay_previously_published_observation(publication):
    publish(publication)
    with publication[0]['scheduler'].store._tx() as db:
        db.execute("UPDATE clients SET revoked_at='revoked'")
    with pytest.raises(PublicationError): publish(publication)
    assert counts(publication) == (1, 0)


def test_hardlinked_staged_artifact_refuses_replay(publication, tmp_path):
    publish(publication)
    path = next(publication[1]['publication_root'].glob('worker-observation-*.json'))
    os.link(path, tmp_path/'second-link')
    with pytest.raises(PublicationError, match='file_untrusted'): publish(publication)
    assert counts(publication) == (1, 0)


def test_foreign_transaction_cannot_supply_result_authority(publication):
    value, args = publication
    db = sqlite3.connect(':memory:')
    try:
        db.execute('BEGIN')
        with pytest.raises(PublicationError, match='transaction_required'):
            result_authority(value['scheduler'], db, args['spec'], args['quiesced'])
    finally:
        db.close()


def test_decoded_secret_refused_by_publication(publication):
    from tests.test_routed_collection import Reader
    from cloudworkbench.routed_collection import collect_execution
    value,args = publication
    old=args['quiesced'].observation
    reader=Reader()
    reader.files['result']=old.result_json.encode()
    reader.files['events']=old.events_jsonl.encode().replace(b'done',b'\\u0064one')
    assert b'done' not in reader.files['events']
    observation=collect_execution(reader,launch_receipt_sha256=json.loads(old.result_json)['launch_receipt_sha256'])
    quiesced=replace(args['quiesced'],observation=observation)
    args['collector'].quiesced=quiesced
    with pytest.raises(PublicationError,match='^publication_secret_rejected$'):
        publish(publication,quiesced=quiesced,forbidden_values=(b'done',))
    assert counts(publication)==(0,0)
    assert not list(args['publication_root'].iterdir())


def test_publication_validates_secret_policy_before_context():
    with pytest.raises(PublicationError,match='^publication_secret_policy_invalid$'):
        publish_observation(None,prepared=None,spec=None,quiesced=None,execution_profile=None,
                            collector=None,cleanup=None,publication_root=None,forbidden_values=(b'',))
