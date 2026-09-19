import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat

import pytest


SPEC = importlib.util.spec_from_file_location(
    'patch_hermes_pstack', Path(__file__).parents[1] / 'scripts/patch-hermes-pstack.py')
patcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patcher)


@pytest.fixture
def source(monkeypatch):
    data = (b'import time\nclass Emitter:\n    _start = time.time()\n'
            b'    def emit(self, data):\n        payload = {\n' + patcher.OLD + b'\n        return payload\n')
    monkeypatch.setattr(patcher, 'ORIGINAL_SHA256', hashlib.sha256(data).hexdigest())
    return data


@pytest.mark.parametrize('value,expected', [(0, 0), (7, 7), (1000, 1000),
                                          (True, None), (-1, None), ('7', None), (1.2, None), (None, None)])
def test_emitted_count_is_actual_nonnegative_integer(source, value, expected):
    updated, changed = patcher.patched_content(source)
    assert changed
    namespace = {}
    exec(compile(updated, '<patched emitter>', 'exec'), namespace)
    assert namespace['Emitter']().emit({'api_calls': value})['api_calls'] == expected
    assert namespace['Emitter']().emit({})['api_calls'] is None


def test_atomic_patch_idempotent_and_preserves_mode(source, tmp_path):
    path = tmp_path / 'stream_json.py'
    path.write_bytes(source)
    path.chmod(0o644)
    first = patcher.patch_file(path)
    content = path.read_bytes()
    second = patcher.patch_file(path)
    assert first['changed'] and not second['changed']
    assert first['patched_sha256'] == second['patched_sha256']
    assert path.read_bytes() == content
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert not list(tmp_path.glob('.pstack-patch-*'))


def test_refuses_drift_even_when_anchor_exists(source, tmp_path):
    path = tmp_path / 'stream_json.py'
    drifted = source + b'\n# unreviewed drift\n'
    path.write_bytes(drifted)
    with pytest.raises(ValueError, match='source_mismatch'):
        patcher.patch_file(path)
    assert path.read_bytes() == drifted
    patched, _ = patcher.patched_content(source)
    with pytest.raises(ValueError, match='source_mismatch'):
        patcher.patched_content(patched + b'\n# drift\n')


def test_refuses_symlink(source, tmp_path):
    target = tmp_path / 'target.py'
    target.write_bytes(source)
    link = tmp_path / 'stream_json.py'
    link.symlink_to(target)
    with pytest.raises(OSError):
        patcher.patch_file(link)
    assert target.read_bytes() == source


def test_actual_pinned_emitter_contract(monkeypatch):
    source = Path(__file__).parents[2] / 'work/hermes-pstack-image-context/hermes/hermes_cli/stream_json.py'
    if not source.is_file():
        pytest.skip('pinned Hermes build source is not present')
    data = source.read_bytes()
    assert hashlib.sha256(data).hexdigest() == patcher.ORIGINAL_SHA256
    updated, changed = patcher.patched_content(data)
    assert changed
    namespace = {}
    exec(compile(updated, str(source), 'exec'), namespace)
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr('sys.stdout', stdout)
    monkeypatch.setattr('sys.stderr', stderr)
    emitter = namespace['StreamJsonEmitter'](model='fixed', session_id='test-session')
    emitter.emit_result({'final_response': 'done', 'api_calls': 8, 'input_tokens': 22})
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert events[0]['type'] == 'system'
    assert events[-1]['api_calls'] == 8 and events[-1]['text'] == 'done'
    assert events[-1]['tokens']['input'] == 22
    assert events[-1]['exit_code'] == 0
