import base64
import json
import os
from pathlib import Path
import time
import types
import uuid

import pytest

from cloudworkbench import crabbox_capture as capture
from cloudworkbench import crabbox_runtime as runtime
from cloudworkbench.hermes_coordinator_runtime import HERMES_ARGV
from cloudworkbench.runtime import RuntimeError


IMAGE = 'sha256:' + 'a' * 64
GROK = 'grok-synthetic-access-key'
CLAUDE = 'claude-synthetic-access-key'
JEV = 'jev-synthetic-access-key'
REFRESH = 'refresh-must-not-be-copied'
ID_TOKEN = 'id-token-must-not-be-copied'


def private(path, data):
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    path.chmod(0o600)
    return path


def codex_auth(path, *, remaining=10000, account='account-test', claimed='account-test'):
    claims = {'exp': time.time() + remaining,
              'https://api.openai.com/auth': {'chatgpt_account_id': claimed}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    access = 'header.' + payload + '.signature'
    private(path, {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
                   'tokens': {'access_token': access, 'refresh_token': REFRESH,
                              'id_token': ID_TOKEN, 'account_id': account}})
    return access


@pytest.fixture
def configured(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    source = tmp_path / 'source'
    source.mkdir()
    grok = private(tmp_path / 'grok', GROK.encode())
    claude = private(tmp_path / 'claude', (CLAUDE + '\n').encode())
    jev = private(tmp_path / 'jev', JEV.encode())
    codex = tmp_path / 'codex'
    access = codex_auth(codex)
    config = dict(root=tmp_path/'workspaces', image=IMAGE, test_path_workspace=True,
        hermes_source_root=source, hermes_grok_auth=grok, hermes_journal_root=tmp_path/'journal',
        docker_socket=tmp_path/'docker.sock', coordinator_uid=os.geteuid(),
        approved_mount_roots=[tmp_path], approved_writable_mount_roots=[tmp_path],
        uid=958, gid=959, tool_network='bridge', tool_image_allowlist=[IMAGE],
        crabbox_binary=tmp_path/'crabbox', crabbox_image=IMAGE, crabbox_images=[IMAGE],
        crabbox_desktop_images=[IMAGE], crabbox_pstack_images=[IMAGE],
        crabbox_pstack_claude_token=claude, crabbox_pstack_codex_auth=codex,
        crabbox_pstack_jev_key=jev)
    rt = runtime.CrabboxRuntime(config)
    attempt, session = str(uuid.uuid4()), str(uuid.uuid4())
    work, native = rt.make_workspace(session), rt.native_state(session)
    native.chmod(0o700)
    taskdir = tmp_path/'task'
    taskdir.mkdir()
    task = dict(schema_version=2, attempt_id=attempt, generation=1, session_id=session,
        runtime_owner=rt.owner, workspace_host=str(work), state_host=str(native),
        tool_image=IMAGE, tool_network='bridge', inputs=[], prompt='test',
        event_spool=attempt+'.1.jsonl', resume_session_id=None, continuation_summary='')
    private(taskdir/'task.json', task)
    mounts = [dict(source=str(taskdir), target='/run/task', readonly=True),
              dict(source=str(native), target='/state', readonly=False)]
    monkeypatch.setattr(runtime.os, 'chown', lambda *args: None)
    monkeypatch.setattr(runtime, 'read_auth', lambda path: (GROK, ()))
    monkeypatch.setattr(runtime, 'process_identity', lambda pid: 'fake-start')
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *args, **kwargs: types.SimpleNamespace(pid=12345))

    def allocate(record):
        record.update(phase='allocated', container='b'*64)
        rt.cb_save(record)

    monkeypatch.setattr(rt, 'cb_allocate', allocate)
    return rt, config, attempt, session, mounts, access


def launch(case):
    rt, config, attempt, session, mounts, access = case
    rid = rt.launch(attempt, session, HERMES_ARGV, {}, mounts, generation=1)
    return rt.crabbox_root/rid


def test_opt_in_payload_contains_access_only_and_capture_tracks_all(configured):
    folder = launch(configured)
    rt, _, attempt, _, _, access = configured
    request = json.loads((folder/'payload/request.json').read_text())
    assert request['pstack'] == {'credentials': {'anthropic': CLAUDE, 'openai-codex': access, 'jev': JEV}}
    assert request['api_key'] == GROK
    assert REFRESH not in json.dumps(request) and ID_TOKEN not in json.dumps(request)
    driver = json.loads((folder/'capture.json').read_text())
    assert driver['additional_secret_file'] == str(folder/'pstack-secrets.json')
    assert all(value not in json.dumps(driver) for value in (GROK, CLAUDE, access, JEV, REFRESH))
    assert set(rt.forbidden_for_attempt({'id': attempt, 'generation': 1, 'state': 'running'})) == {
        value.encode() for value in (GROK, CLAUDE, access, JEV)}
    assert (folder/'pstack-secrets.json').stat().st_mode & 0o777 == 0o600
    assert rt.cb_record(folder.name)['desktop'] and rt.cb_record(folder.name)['pstack']


def test_old_image_does_not_load_pstack_credentials(configured, monkeypatch):
    rt = configured[0]
    rt.crabbox_pstack_images = []
    monkeypatch.setattr(rt, 'pstack_credentials', lambda: pytest.fail('not opted in'))
    folder = launch(configured)
    assert 'pstack' not in json.loads((folder/'payload/request.json').read_text())
    assert 'additional_secret_file' not in json.loads((folder/'capture.json').read_text())
    assert not (folder/'pstack-secrets.json').exists()


def test_pstack_capability_requires_allowed_image(configured):
    config = {**configured[1], 'crabbox_pstack_images': ['sha256:'+'c'*64]}
    with pytest.raises(RuntimeError, match='capability policy'):
        runtime.CrabboxRuntime(config)
    config.pop('crabbox_pstack_images')
    assert runtime.CrabboxRuntime(config).crabbox_pstack_images == []


@pytest.mark.parametrize('remaining', [-1, 120, 7200, 7319])
def test_codex_expiry_requires_whole_job_budget(tmp_path, remaining):
    path = tmp_path.resolve()/'codex'
    codex_auth(path, remaining=remaining)
    with pytest.raises(RuntimeError, match='expired for job budget'):
        runtime._codex_access(path)


@pytest.mark.parametrize('account,claimed', [('account-one','account-two'), (None,None), ('account',None)])
def test_codex_account_claims_fail_closed(tmp_path, account, claimed):
    path = tmp_path.resolve()/'codex'
    codex_auth(path, account=account, claimed=claimed)
    with pytest.raises(RuntimeError, match='credential unavailable'):
        runtime._codex_access(path)


def test_credentials_rechecked_after_provisioning(configured, monkeypatch):
    rt = configured[0]
    original = rt.cb_allocate

    def expires(record):
        original(record)
        codex_auth(rt.pstack_credential_paths['openai-codex'], remaining=60)

    monkeypatch.setattr(rt, 'cb_allocate', expires)
    with pytest.raises(RuntimeError, match='expired for job budget'):
        launch(configured)


def test_snapshot_rotation_preserves_old_and_new_filters(configured, monkeypatch):
    rt = configured[0]
    original = rt.cb_allocate
    replacement = JEV+'-rotated'

    def rotates(record):
        original(record)
        private(rt.pstack_credential_paths['jev'], replacement.encode())

    monkeypatch.setattr(rt, 'cb_allocate', rotates)
    folder = launch(configured)
    assert json.loads((folder/'payload/request.json').read_text())['pstack']['credentials']['jev'] == replacement
    values = capture.read_additional_secrets(folder/'pstack-secrets.json')
    assert JEV.encode() in values and replacement.encode() in values


def test_cleanup_refuses_live_and_removes_all_snapshots(configured, monkeypatch):
    folder = launch(configured)
    rt, _, attempt, _, _, _ = configured
    row = {'id': attempt, 'generation': 1, 'state': 'completed'}
    with pytest.raises(RuntimeError, match='before stop'):
        rt.cleanup_terminal_secrets(row)
    record = rt.cb_record(folder.name)
    record['phase'] = 'stopped'
    rt.cb_save(record)
    rt.cleanup_terminal_secrets(row)
    assert all(not (folder/name).exists() for name in ('model-key','pstack-secrets.json','payload/request.json'))
    assert rt.forbidden_for_attempt(row) == ()


def test_missing_filters_fail_collection(configured):
    folder = launch(configured)
    rt, _, attempt, _, _, _ = configured
    (folder/'pstack-secrets.json').unlink()
    with pytest.raises(RuntimeError, match='safe collection'):
        rt.forbidden_for_attempt({'id': attempt, 'generation': 1, 'state': 'running'})


def test_secret_source_rejects_symlink_public_mode_and_invalid(tmp_path):
    path = private(tmp_path.resolve()/'secret', CLAUDE.encode())
    link = path.with_name('link')
    link.symlink_to(path)
    for candidate in (link, path.with_name('missing')):
        with pytest.raises(RuntimeError, match='credential unavailable'):
            runtime._pstack_secret(candidate)
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match='credential unavailable'):
        runtime._pstack_secret(path)
    private(path, b'short')
    with pytest.raises(RuntimeError, match='credential unavailable'):
        runtime._pstack_secret(path)


@pytest.mark.parametrize('additional', [False, True])
def test_capture_redacts_all_and_preserves_old_records(tmp_path, monkeypatch, additional):
    tmp_path = tmp_path.resolve()
    home = tmp_path/'home'
    home.mkdir(mode=0o700)
    key = private(tmp_path/'key', GROK.encode())
    record = {'command': ['/bin/false'], 'env': {'HOME': str(home)},
              'event_spool': str(tmp_path/'events'), 'secret_file': str(key),
              'resume_session_id': None, 'receipt': str(tmp_path/'receipt')}
    values = [GROK]
    if additional:
        record['additional_secret_file'] = str(private(tmp_path/'more-secrets', [CLAUDE, JEV]))
        values += [CLAUDE, JEV]
    path = private(tmp_path/'capture', record)

    def fake_capture(command, env, writer, **kwargs):
        assert set(writer.forbidden) == {value.encode() for value in values}
        for secret in values:
            writer.append({'type':'assistant.message','payload':{'text':secret}})
        return 0

    monkeypatch.setattr(capture, 'capture', fake_capture)
    assert capture.main([str(path)]) == 0
    assert all(value not in (tmp_path/'events').read_text() for value in values)
    assert json.loads((tmp_path/'receipt').read_text())['exit_code'] == 0


def test_capture_rejects_additional_secret_in_environment(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    home = tmp_path/'home'
    home.mkdir(mode=0o700)
    record = {'command':['/bin/false'], 'env':{'HOME':str(home),'LEAK':CLAUDE},
              'event_spool':str(tmp_path/'events'), 'secret_file':str(private(tmp_path/'key', GROK.encode())),
              'additional_secret_file':str(private(tmp_path/'more', [CLAUDE])),
              'resume_session_id':None, 'receipt':str(tmp_path/'receipt')}
    monkeypatch.setattr(capture, 'capture', lambda *args, **kwargs: pytest.fail('must not launch'))
    assert capture.main([str(private(tmp_path/'capture', record))]) == 1
    assert not (tmp_path/'events').exists()
