import json
import os
from pathlib import Path
import pytest
from cloudworkbench.adapters import read_credential
from cloudworkbench.credential_state import CredentialState, CredentialStateError


def token_file(path, token=b'sk-ant-oat-synthetic-valid-token'):
    path.write_bytes(token);path.chmod(0o640)
    return token


def test_read_credential_missing_invalid_and_unsafe(tmp_path):
    path=tmp_path/'token'
    assert read_credential(path)==(b'','auth_missing')
    for value in (b'wrong',b'x'*5000,b'sk-ant-oat\ninline',b'sk-ant-oat\xff'):
        token_file(path,value)
        assert read_credential(path)==(b'','auth_invalid')
    value=token_file(path)
    assert read_credential(path)==(value,None)
    path.chmod(0o644)
    assert read_credential(path)==(b'','auth_invalid')
    path.chmod(0o640)
    link=tmp_path/'link';link.symlink_to(path)
    assert read_credential(link)==(b'','auth_invalid')
    hard=tmp_path/'hard';os.link(path,hard)
    assert read_credential(hard)==(b'','auth_invalid')


def test_quarantine_persists_and_replacement_requires_new_supervisor(tmp_path):
    path=tmp_path/'token';first=token_file(path);statepath=tmp_path/'private'/'claude.json'
    state=CredentialState(statepath,first)
    state.initialize()
    assert state.blocked_reason(path) is None
    state.quarantine('provider_auth_rejected')
    assert state.blocked_reason(path)=='provider_auth_rejected'
    assert CredentialState(statepath,first).blocked_reason(path)=='provider_auth_rejected'
    assert first not in statepath.read_bytes()
    assert statepath.stat().st_mode & 0o777==0o600
    assert statepath.parent.stat().st_mode & 0o777==0o700
    replacement=token_file(path,b'sk-ant-oat-synthetic-replacement')
    assert state.blocked_reason(path)=='auth_changed_restart_required'
    assert CredentialState(statepath,replacement).blocked_reason(path) is None
    token_file(path,first)
    assert CredentialState(statepath,first).blocked_reason(path)=='provider_auth_rejected'


def test_quarantine_refuses_rate_limit_or_missing_identity(tmp_path):
    state=CredentialState(tmp_path/'private'/'state',b'sk-ant-oat-synthetic')
    with pytest.raises(CredentialStateError):state.quarantine('rate_limited')
    missing=CredentialState(tmp_path/'private'/'state',b'')
    assert missing.blocked_reason()=='auth_missing'
    with pytest.raises(CredentialStateError):missing.quarantine('provider_auth_rejected')


def test_corrupt_or_unprotected_state_fails_closed(tmp_path):
    path=tmp_path/'private'/'state';path.parent.mkdir(mode=0o700);path.write_text('invalid');path.chmod(0o600)
    state=CredentialState(path,b'sk-ant-oat-synthetic')
    assert state.blocked_reason()=='credential_state_invalid'
    path.write_text(json.dumps({'schema_version':1,'quarantined':{}}));path.chmod(0o644)
    assert state.blocked_reason()=='credential_state_invalid'
    path.chmod(0o600);path.parent.chmod(0o750)
    assert state.blocked_reason()=='credential_state_invalid'


def test_loss_of_initialized_state_fails_closed_and_operator_recovery_is_explicit(tmp_path):
    path=tmp_path/'private'/'state';state=CredentialState(path,b'sk-ant-oat-synthetic')
    assert state.blocked_reason()=='credential_state_invalid'
    state.initialize();state.quarantine('provider_auth_rejected')
    state.initialize()
    assert state.blocked_reason()=='provider_auth_rejected'
    with pytest.raises(CredentialStateError):state.clear_quarantine()
    state.clear_quarantine(operator_confirmed=True)
    assert state.blocked_reason() is None
    path.unlink();path.parent.rmdir()
    assert state.blocked_reason()=='credential_state_invalid'
    assert CredentialState(path,b'sk-ant-oat-synthetic').blocked_reason()=='credential_state_invalid'


def test_collection_block_survives_restart_and_token_replacement_until_operator_recovery(tmp_path):
    path=tmp_path/'private'/'claude.json';state=CredentialState(path,b'sk-ant-oat-first')
    state.initialize();state.block_collection()
    assert state.blocked_reason()=='attempt_credential_unavailable'
    replacement=CredentialState(path,b'sk-ant-oat-second');replacement.initialize()
    assert replacement.blocked_reason()=='attempt_credential_unavailable'
    with pytest.raises(CredentialStateError):replacement.clear_collection_block()
    replacement.clear_collection_block(operator_confirmed=True)
    assert replacement.blocked_reason() is None
    assert 'sk-ant' not in path.read_text()


def test_collection_block_refuses_unsafe_marker(tmp_path):
    state=CredentialState(tmp_path/'private'/'state',b'sk-ant-oat-first');state.initialize();state.block_collection()
    marker=state.path.parent/state.collection_block_name
    marker.chmod(0o644)
    assert state.blocked_reason()=='credential_state_invalid'


def test_private_directory_inherited_setgid_keeps_owner_only_access(tmp_path):
    folder=tmp_path/'private';folder.mkdir();folder.chmod(0o2700)
    state=CredentialState(folder/'claude.json',b'sk-ant-oat-synthetic')
    state.initialize()
    assert state.blocked_reason() is None
    state.quarantine('provider_auth_rejected')
    assert state.blocked_reason()=='provider_auth_rejected'
    assert folder.stat().st_mode & 0o777==0o700
    assert state.path.stat().st_mode & 0o777==0o600
    folder.chmod(0o2750)
    assert state.blocked_reason()=='credential_state_invalid'
