import io
from pathlib import Path
import runpy
import json
import os
import subprocess
import sys

import pytest

from cryptography.exceptions import InvalidTag
module=runpy.run_path(str(Path(__file__).parents[1]/'scripts/seal-backup.py'))


def test_authenticated_backup_roundtrip_and_tampering(tmp_path):
    key=module['read_key'](tmp_path/'key',True)
    data=b'\x00\xffSynthetic backup\n'*50000
    envelope=tmp_path/'backup.sealed'
    module['transform'](io.BytesIO(data),envelope,key)
    restored=tmp_path/'restored.tar'
    with envelope.open('rb') as source:module['transform'](source,restored,key,True)
    assert restored.read_bytes()==data
    tampered=bytearray(envelope.read_bytes());tampered[len(tampered)//2]^=1
    with pytest.raises(InvalidTag):module['transform'](io.BytesIO(tampered),tmp_path/'bad',key,True)
    assert not (tmp_path/'bad').exists()
    with pytest.raises(InvalidTag):module['transform'](io.BytesIO(envelope.read_bytes()[:-1]),tmp_path/'short',key,True)
    assert not (tmp_path/'short').exists()
    assert not list(tmp_path.glob('.sealed-backup-*'))


def test_key_permissions_and_existing_destination(tmp_path):
    keyfile=tmp_path/'key';key=module['read_key'](keyfile,True)
    keyfile.chmod(0o644)
    with pytest.raises(ValueError):module['read_key'](keyfile)
    destination=tmp_path/'existing';destination.write_text('preserve')
    with pytest.raises(ValueError):module['transform'](io.BytesIO(b'x'),destination,key)
    assert destination.read_text()=='preserve'


@pytest.mark.parametrize('case', ['wrong-key', 'tag', 'magic'])
def test_authentication_failure_never_publishes_or_leaves_temporary(tmp_path, case):
    key = os.urandom(32)
    envelope = tmp_path / 'sealed'
    module['transform'](io.BytesIO(b'private synthetic data'), envelope, key)
    content = bytearray(envelope.read_bytes())
    expected = InvalidTag
    if case == 'wrong-key':
        key = os.urandom(32)
    elif case == 'tag':
        content[-1] ^= 1
    else:
        content[0] ^= 1
        expected = ValueError
    with pytest.raises(expected):
        module['transform'](io.BytesIO(content), tmp_path / 'plaintext', key, True)
    assert not (tmp_path / 'plaintext').exists()
    assert not list(tmp_path.glob('.sealed-backup-*'))


def test_exact_size_limits_on_both_paths(tmp_path, monkeypatch):
    key = os.urandom(32)
    oversized = tmp_path / 'oversized'
    module['transform'](io.BytesIO(b'x' * 101), oversized, key)
    monkeypatch.setitem(module['transform'].__globals__, 'LIMIT', 100)
    exact = tmp_path / 'exact'
    module['transform'](io.BytesIO(b'x' * 100), exact, key)
    with exact.open('rb') as source:
        module['transform'](source, tmp_path / 'exact-restored', key, True)
    assert (tmp_path / 'exact-restored').read_bytes() == b'x' * 100
    with pytest.raises(ValueError, match='limit'):
        module['transform'](io.BytesIO(b'x' * 101), tmp_path / 'seal-too-big', key)
    with oversized.open('rb') as source, pytest.raises(ValueError, match='limit'):
        module['transform'](source, tmp_path / 'open-too-big', key, True)
    assert not (tmp_path / 'seal-too-big').exists()
    assert not (tmp_path / 'open-too-big').exists()
    assert not list(tmp_path.glob('.sealed-backup-*'))


def test_dangling_destination_symlink_preserved(tmp_path):
    target = tmp_path / 'output'
    target.symlink_to(tmp_path / 'absent')
    with pytest.raises(ValueError, match='Destination'):
        module['transform'](io.BytesIO(b'x'), target, os.urandom(32))
    assert target.is_symlink()
    assert not (tmp_path / 'absent').exists()
    assert not list(tmp_path.glob('.sealed-backup-*'))


def test_key_short_read_and_short_direct_key_refused(tmp_path, monkeypatch):
    keyfile = tmp_path / 'key'
    module['read_key'](keyfile, True)
    monkeypatch.setattr(os, 'read', lambda fd, amount: b'x' * 16)
    with pytest.raises(ValueError, match='exactly 32'):
        module['read_key'](keyfile)
    with pytest.raises(ValueError, match='32-byte'):
        module['transform'](io.BytesIO(b'x'), tmp_path / 'output', b'x' * 16)
    assert not (tmp_path / 'output').exists()


def test_cli_stdin_roundtrip_and_wrong_key_have_no_key_output(tmp_path):
    script = str(Path(__file__).parents[1] / 'scripts/seal-backup.py')
    keyfile, wrong = tmp_path / 'key', tmp_path / 'wrong'
    key = module['read_key'](keyfile, True)
    other = module['read_key'](wrong, True)
    envelope, restored = tmp_path / 'sealed', tmp_path / 'restored'
    content = b'CLI synthetic content' * 4096
    sealed = subprocess.run([sys.executable, script, 'seal', '--key-file', str(keyfile), '--output', str(envelope)], input=content, capture_output=True, timeout=30)
    assert sealed.returncode == 0
    assert json.loads(sealed.stdout)['input_bytes'] == len(content)
    opened = subprocess.run([sys.executable, script, 'open', '--key-file', str(keyfile), '--input', str(envelope), '--output', str(restored)], capture_output=True, timeout=30)
    assert opened.returncode == 0 and restored.read_bytes() == content
    failed = subprocess.run([sys.executable, script, 'open', '--key-file', str(wrong), '--input', str(envelope), '--output', str(tmp_path / 'bad')], capture_output=True, timeout=30)
    assert failed.returncode != 0 and not (tmp_path / 'bad').exists()
    assert not list(tmp_path.glob('.sealed-backup-*'))
    for result in (sealed, opened, failed):
        for protected in (key, other, key.hex().encode(), other.hex().encode()):
            assert protected not in result.stdout + result.stderr
