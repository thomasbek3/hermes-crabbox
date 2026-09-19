import os
import threading
import pytest
from cloudworkbench.inference_transport import TransportError
from cloudworkbench.provider_main import read_request, read_profile


def pipe_read(data, **kwargs):
    read, write = os.pipe()
    def writer():
        try:
            os.write(write, data)
        except BrokenPipeError:
            pass
        finally:
            os.close(write)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        return read_request(read, **kwargs)
    finally:
        os.close(read)
        thread.join()


def test_request_is_one_bounded_json_object():
    assert pipe_read(b'{"messages":[],"tools":[]}') == {'messages': [], 'tools': []}
    for data in [b'[]', b'{"x":1,"x":2}', b'{"x":NaN}', b'{}{}']:
        with pytest.raises(TransportError):
            pipe_read(data)
    with pytest.raises(TransportError, match='request_too_large'):
        pipe_read(b'12345', max_bytes=4)


def test_stalled_input_has_deadline():
    read, write = os.pipe()
    try:
        with pytest.raises(TransportError, match='request_read_timeout'):
            read_request(read, timeout=.02)
    finally:
        os.close(read)
        os.close(write)


def test_untrusted_and_linked_profile_refused(tmp_path):
    profile = tmp_path/'profile.json'
    profile.write_text('{}')
    profile.chmod(0o666)
    with pytest.raises(TransportError):
        read_profile(profile)
    link = tmp_path/'link'
    link.symlink_to(profile)
    with pytest.raises(OSError):
        read_profile(link)


def test_module_emits_fixed_envelope_for_untrusted_profile(tmp_path):
    import json
    import subprocess
    import sys
    profile = tmp_path/'profile.json'
    profile.write_text('sensitive-invalid-content')
    profile.chmod(0o666)
    result = subprocess.run([sys.executable, '-m', 'cloudworkbench.provider_main', '--profile', str(profile)],
                            input=b'{}', capture_output=True, timeout=5)
    assert result.returncode == 1
    envelope = json.loads(result.stdout)
    assert envelope['error'] == 'invalid_profile_file'
    assert envelope['container_cleanup_required'] is True
    assert envelope['credential_reuse_authorized'] is False
    assert b'sensitive-invalid-content' not in result.stdout + result.stderr


def test_valid_profile_and_typed_values(tmp_path, monkeypatch):
    import json
    import types
    from cloudworkbench import provider_main
    original = os.fstat
    def root_stat(fd):
        value = original(fd)
        return types.SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_nlink=value.st_nlink, st_size=value.st_size)
    monkeypatch.setattr(provider_main.os, 'fstat', root_stat)
    profile = tmp_path/'profile.json'
    data = {'path': '/opt/provider/claude', 'sha256': 'a'*64, 'version': '2.1.274', 'native_model': 'claude-fable-5-1', 'effort': 'high'}
    profile.write_text(json.dumps(data));profile.chmod(0o644)
    assert read_profile(profile).native_model == data['native_model']
    data['path'] = 12
    profile.write_text(json.dumps(data))
    with pytest.raises(TransportError, match='invalid_profile_file'):
        read_profile(profile)


@pytest.mark.parametrize('bad', [None, float('nan'), object()])
def test_main_success_and_serialization_fallback(tmp_path, monkeypatch, capsys, bad):
    import json
    from pathlib import Path
    from cloudworkbench import provider_main
    monkeypatch.setattr(provider_main, 'read_profile', lambda _: object())
    monkeypatch.setattr(provider_main, 'read_request', lambda _: {})
    monkeypatch.setattr(provider_main.sys, 'stdin', type('Input', (), {'fileno': lambda self: 0})())
    monkeypatch.setattr(provider_main.tempfile, 'mkdtemp', lambda **_: str(tmp_path))
    def run(*args, **kwargs):
        assert (kwargs['home'].stat().st_mode & 0o777) == 0o700
        assert kwargs['proxy']=='http://10.77.0.2:8080'
        return {'version': 1, 'status': 'ok', 'error': None, 'result': bad, 'container_cleanup_required': True, 'credential_reuse_authorized': False}
    monkeypatch.setattr(provider_main, 'run_token_request', run)
    code = provider_main.main(['--proxy','http://10.77.0.2:8080'])
    result = json.loads(capsys.readouterr().out)
    assert code == (0 if bad is None else 1)
    assert result['error'] == (None if bad is None else 'response_serialization_failed')
