import json
import sys
import time

import pytest

from cloudworkbench.entrypoint import run_verification_check


def test_verifier_classifies_exit_failure_and_start_error(tmp_path):
    passed = run_verification_check({'id': 'ok', 'argv': [sys.executable, '-c', 'print("checked")']})
    assert passed['result'] == 'passed' and passed['stdout'] == 'checked\n'
    failed = run_verification_check({'id': 'bad', 'argv': [sys.executable, '-c', 'raise SystemExit(2)']})
    assert failed['result'] == 'failed' and failed['exit_code'] == 2
    missing = run_verification_check({'id': 'missing', 'argv': [str(tmp_path / 'missing-secret-command')]})
    assert missing == {'id': 'missing', 'result': 'infrastructure_error', 'reason': 'check_execution_error'}
    assert 'missing-secret-command' not in json.dumps(missing)


@pytest.mark.parametrize('parent_waits', [True, False])
def test_verifier_reaps_process_group_on_timeout_or_parent_completion(tmp_path, parent_waits):
    marker = tmp_path / 'descendant-survived'
    child = 'import time; from pathlib import Path; time.sleep(.7); Path(' + repr(str(marker)) + ').write_text("survived")'
    parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); print("started",flush=True); '
    if parent_waits:
        parent += 'time.sleep(10)'
    started = time.monotonic()
    result = run_verification_check({'id': 'bounded', 'argv': [sys.executable, '-c', parent], 'timeout': .3})
    assert time.monotonic() - started < 2
    assert result['stdout'] == 'started\n'
    if parent_waits:
        assert result['result'] == 'failed' and result['reason'] == 'check_timeout'
    else:
        assert result['result'] == 'passed'
    time.sleep(.8)
    assert not marker.exists()


def test_verifier_output_file_and_public_tail_are_bounded():
    result = run_verification_check({'id': 'verbose', 'argv': [sys.executable, '-c', 'import os; os.write(1,b"x"*2000000)']})
    assert len(result['stdout'].encode()) <= 32000


def test_group_kill_permission_error_only_ignored_for_exited_child(monkeypatch):
    from cloudworkbench.entrypoint import kill_process_group
    from types import SimpleNamespace
    def denied(*args):raise PermissionError('already exited group')
    monkeypatch.setattr('os.killpg',denied)
    kill_process_group(SimpleNamespace(pid=123,poll=lambda:0))
    with pytest.raises(PermissionError):kill_process_group(SimpleNamespace(pid=123,poll=lambda:None))
