import json
from pathlib import Path
import pytest

from cloudworkbench.runner import Runner
from cloudworkbench.credential_state import CredentialState
from test_runner import setup, FakeRuntime


def configured(setup,tmp_path,*,token=b'sk-ant-oat-synthetic-first',initialize=True):
    store,owner,runtime,prior=setup
    path=tmp_path/'dedicated-token'
    if token is not None:path.write_bytes(token);path.chmod(0o640)
    cfg=prior.config
    cfg.update(claude_token=str(path),claude_enabled=True,agents=['claude'])
    cfg['projects']['project']['allowed_agents'].append('claude')
    runner=Runner(store,cfg,runtime=runtime);runner.capacity=lambda:0
    if initialize and token:runner.credential_state.initialize()
    return store,owner,runtime,runner,path


def submit_auth(store,owner,key):
    return store.create_session(owner,{'project_id':'project','goal':'synthetic auth','agent':'claude','environment_version':'fixture-v1'},key)


def test_missing_auth_stays_blocked_without_reservation(setup,tmp_path):
    store,owner,runtime,runner,path=configured(setup,tmp_path,token=None)
    created=submit_auth(store,owner,'missing')
    for _ in range(3):runner.tick()
    a=store.get_attempt(created['attempt_id'])
    assert a['state']=='queued' and a['reason']=='auth_missing'
    assert not store.active_attempts() and not runtime.launches
    heartbeat=json.loads((runner.root/'heartbeat.json').read_text())
    assert heartbeat['blocked_agents']['claude']=='auth_missing' and heartbeat['available_agents']==[]
    assert len([e for e in store.events(owner,a['session_id']) if e['type']=='attempt.blocked'])==1


def test_uninitialized_private_state_blocks_provider(setup,tmp_path):
    store,owner,runtime,runner,path=configured(setup,tmp_path,initialize=False)
    a=submit_auth(store,owner,'uninitialized');runner.tick()
    assert store.get_attempt(a['attempt_id'])['reason']=='credential_state_invalid'
    assert not runtime.launches


@pytest.mark.parametrize('exit_code',[0,1])
def test_terminal_auth_rejection_quarantines_then_replacement_restart_allows_queued(setup,tmp_path,exit_code):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    first=submit_auth(store,owner,'first');second=submit_auth(store,owner,'second');runner.tick()
    a=store.get_attempt(first['attempt_id']);assert a['state']=='running'
    staged=runner.staged_credential_path(a)
    assert staged.read_bytes()==path.read_bytes() and staged.stat().st_mode&0o777==0o640
    assert not staged.is_relative_to(runner.task_dir(a))
    runtime.complete(a['runtime_id'],[{'type':'result','is_error':True,'api_error_status':401}],exit_code=exit_code)
    runner.tick();failed=store.get_attempt(a['id'])
    assert failed['state']=='failed' and failed['reason']=='provider_auth_rejected'
    assert failed['result']['usage_status']['reason']=='provider_did_not_report'
    assert not store.active_attempts()
    assert store.get_attempt(second['attempt_id'])['state']=='queued'
    assert store.get_attempt(second['attempt_id'])['reason']=='provider_auth_rejected'
    restarted=Runner(store,runner.config,runtime=runtime);restarted.capacity=lambda:0;restarted.tick()
    assert len(runtime.launches)==1
    path.write_bytes(b'sk-ant-oat-synthetic-replacement')
    runner.tick();assert runner.blocked()['claude']=='auth_changed_restart_required'
    replacement=Runner(store,runner.config,runtime=runtime);replacement.capacity=lambda:0;replacement.tick()
    assert store.get_attempt(second['attempt_id'])['state']=='running' and len(runtime.launches)==2
    assert store.get_attempt(first['attempt_id'])['state']=='failed'


def test_rate_limit_is_stable_terminal_without_quarantine_or_replay(setup,tmp_path):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'limited');runner.tick();a=store.get_attempt(created['attempt_id'])
    runtime.complete(a['runtime_id'],[{'type':'result','is_error':True,'api_error_status':429}],exit_code=1)
    runner.tick();runner.tick()
    assert store.get_attempt(a['id'])['reason']=='rate_limited'
    assert 'claude' not in runner.blocked() and len(runtime.launches)==1
    assert not store.active_attempts()


def test_restart_redacts_and_scans_attempt_token_after_operator_replacement(setup,tmp_path):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'old-token');runner.tick();a=store.get_attempt(created['attempt_id']);old=path.read_bytes()
    path.write_bytes(b'sk-ant-oat-synthetic-new')
    replacement=Runner(store,runner.config,runtime=runtime);replacement.capacity=lambda:0
    (runtime.make_workspace(a['session_id'])/'leak.txt').write_bytes(old)
    runtime.complete(a['runtime_id'],[{'type':'assistant','message':{'content':[{'type':'text','text':old.decode()}]}},{'type':'result','is_error':False,'result':'done'}])
    replacement.tick()
    assert store.get_attempt(a['id'])['state']=='failed'
    assert old.decode() not in json.dumps(store.events(owner,a['session_id']))
    assert old not in (runner.root/'logs'/(a['id']+'.log')).read_bytes()
    assert not store.list_artifacts(owner,a['session_id'])


def test_old_attempt_rejection_does_not_quarantine_replacement_identity(setup,tmp_path):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'old-reject');runner.tick();a=store.get_attempt(created['attempt_id']);old=path.read_bytes()
    path.write_bytes(b'sk-ant-oat-synthetic-new')
    replacement=Runner(store,runner.config,runtime=runtime);replacement.capacity=lambda:0
    runtime.complete(a['runtime_id'],[{'type':'result','is_error':True,'api_error_status':401}],exit_code=1)
    replacement.tick()
    assert 'claude' not in replacement.blocked()
    assert CredentialState(replacement.credential_state_path,old).blocked_reason()=='provider_auth_rejected'


def test_success_keeps_stage_through_verifier_then_deletes_after_durable_commit(setup,tmp_path,monkeypatch):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'success-cleanup');runner.tick();a=store.get_attempt(created['attempt_id'])
    staged=runner.staged_credential_path(a)
    runtime.complete(a['runtime_id'],[{'type':'result','is_error':False,'result':'done'}]);runner.tick()
    a=store.get_attempt(a['id']);assert a['state']=='verifying' and staged.exists()
    original=runner.cleanup_terminal_material
    def checked(attempt):
        assert store.get_attempt(attempt['id'])['state']=='completed' and staged.exists()
        return original(attempt)
    monkeypatch.setattr(runner,'cleanup_terminal_material',checked)
    runtime.complete(a['runtime_id'],[{'type':'verification','checks':[{'id':'protected','result':'passed'}]}]);runner.tick()
    assert store.get_attempt(a['id'])['state']=='completed' and not staged.exists()
    assert (runner.root/'results'/(a['id']+'.json')).exists()


def test_lost_stage_blocks_next_attempt_and_restart_without_reading_raw_logs(setup,tmp_path,monkeypatch):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    first=submit_auth(store,owner,'lost-first');second=submit_auth(store,owner,'lost-second');runner.tick()
    a=store.get_attempt(first['attempt_id']);runner.staged_credential_path(a).unlink()
    runtime.complete(a['runtime_id'],[{'type':'result','is_error':True,'api_error_status':401}],exit_code=1)
    monkeypatch.setattr(runtime,'logs',lambda *args,**kwargs:pytest.fail('must not read unredactable raw logs'))
    runner.tick()
    assert store.get_attempt(a['id'])['reason']=='attempt_credential_unavailable'
    assert store.get_attempt(second['attempt_id'])['state']=='queued' and len(runtime.launches)==1
    restarted=Runner(store,runner.config,runtime=runtime);restarted.capacity=lambda:0;restarted.tick()
    assert restarted.blocked()['claude']=='attempt_credential_unavailable' and len(runtime.launches)==1


def test_lost_stage_stops_live_runtime_and_guard_persistence_failure_retains_reservation(setup,tmp_path,monkeypatch):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'lost-running');runner.tick();a=store.get_attempt(created['attempt_id'])
    runner.staged_credential_path(a).unlink()
    original=runner.credential_state.block_collection
    monkeypatch.setattr(runner.credential_state,'block_collection',lambda: (_ for _ in ()).throw(OSError('test disk failure')))
    runner.tick()
    assert runtime.status(a['runtime_id'],a['generation'])['state'] in ('exited','missing')
    assert a['runtime_id'] in runtime.stops
    assert store.active_attempts() and runner.blocked()['claude']=='attempt_credential_unavailable'
    monkeypatch.setattr(runner.credential_state,'block_collection',original);runner.tick()
    assert store.get_attempt(a['id'])['state']=='failed' and not store.active_attempts()


def test_quarantine_failure_preserves_known_auth_reason_and_blocks_admission(setup,tmp_path,monkeypatch):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'quarantine-error');second=submit_auth(store,owner,'quarantine-next');runner.tick();a=store.get_attempt(created['attempt_id'])
    runtime.complete(a['runtime_id'],[{'type':'result','is_error':True,'api_error_status':401}],exit_code=1)
    monkeypatch.setattr(CredentialState,'quarantine',lambda *args: (_ for _ in ()).throw(OSError('test failure')))
    runner.tick();failed=store.get_attempt(a['id'])
    assert failed['reason']=='provider_auth_rejected' and failed['result']['failure_code']=='provider_auth_rejected'
    assert runner.blocked()['claude']=='attempt_credential_unavailable'
    assert store.get_attempt(second['attempt_id'])['state']=='queued'
    assert not runner.staged_credential_path(a).exists()


def test_cleanup_restarts_after_crash_between_terminal_commit_and_unlink(setup,tmp_path,monkeypatch):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'cleanup-crash');runner.tick();a=store.get_attempt(created['attempt_id'])
    runtime.complete(a['runtime_id'],[{'type':'result','is_error':True,'api_error_status':429}],exit_code=1)
    monkeypatch.setattr(runner,'cleanup_terminal_material',lambda *args: (_ for _ in ()).throw(OSError('simulated crash')))
    runner.tick();assert store.get_attempt(a['id'])['state']=='failed' and runner.staged_credential_path(a).exists()
    restarted=Runner(store,runner.config,runtime=runtime);restarted.reconcile()
    assert not restarted.staged_credential_path(a).exists()


def test_failed_completion_sidecar_removed_after_commit_in_shared_results_dir(setup,tmp_path):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'candidate-failed');runner.tick();a=store.get_attempt(created['attempt_id'])
    runtime.complete(a['runtime_id'],[],exit_code=1)
    sidecar=runner.root/'results'/(a['id']+'.json');sidecar.write_text('{"outcome":"verified"}');sidecar.parent.chmod(0o770)
    runner.tick();assert store.get_attempt(a['id'])['state']=='failed'
    assert not sidecar.exists() and not runner.staged_credential_path(a).exists()


def test_cleanup_refuses_live_or_stale_generation(setup,tmp_path):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'cleanup-fence');runner.tick();a=store.get_attempt(created['attempt_id'])
    with pytest.raises(ValueError):runner.cleanup_terminal_material(a)
    assert runner.staged_credential_path(a).exists()
    runtime.complete(a['runtime_id'],[],exit_code=1)
    committed=store.transition(a['id'],'failed',expected_generation=a['generation'])
    with pytest.raises(ValueError):runner.cleanup_terminal_material({**committed,'generation':a['generation']+1})
    assert runner.staged_credential_path(a).exists()


def test_terminal_sweep_rotates_and_skips_completed_sidecars_before_runtime_inspection(setup,tmp_path,monkeypatch):
    import uuid
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    rows={};deleted=[]
    for n in range(7):
        aid=str(uuid.uuid4());p=runner.root/'results'/(aid+'.json');p.write_text('{}')
        rows[aid]={'id':aid,'generation':1,'state':'completed' if n<6 else 'failed'}
    monkeypatch.setattr(store,'get_attempt',lambda aid:rows[aid])
    def cleanup(a):
        assert a['state']=='failed'
        deleted.append(a['id']);(runner.root/'results'/(a['id']+'.json')).unlink()
    monkeypatch.setattr(runner,'cleanup_terminal_material',cleanup)
    for _ in range(5):runner.cleanup_terminal_credentials(limit=2)
    assert len(deleted)==1


def test_terminal_sweep_runtime_uncertainty_preserves_token_without_raising(setup,tmp_path,monkeypatch):
    from cloudworkbench.runtime import RuntimeError as DockerRuntimeError
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    created=submit_auth(store,owner,'uncertain-cleanup');runner.tick();a=store.get_attempt(created['attempt_id'])
    runtime.complete(a['runtime_id'],[],exit_code=1)
    store.transition(a['id'],'failed',expected_generation=a['generation'])
    runtime.status_error=DockerRuntimeError('safe category only')
    runner.cleanup_terminal_credentials()
    assert runner.staged_credential_path(a).exists()


def test_inherited_setgid_capsule_directory_stages_and_cleans(setup,tmp_path):
    store,owner,runtime,runner,path=configured(setup,tmp_path)
    private=runner.root/'tasks'/'.credentials';private.mkdir();private.chmod(0o2700)
    created=submit_auth(store,owner,'setgid-capsule');runner.tick()
    attempt=store.get_attempt(created['attempt_id'])
    assert attempt['state']=='running'
    staged=runner.staged_credential_path(attempt)
    assert staged.is_file() and private.stat().st_mode&0o777==0o700
    runtime.complete(attempt['runtime_id'],[{'type':'result','is_error':True,'api_error_status':401}],exit_code=1)
    runner.tick()
    assert store.get_attempt(attempt['id'])['state']=='failed'
    assert not staged.exists()
    assert runner.blocked()['claude']=='provider_auth_rejected'
