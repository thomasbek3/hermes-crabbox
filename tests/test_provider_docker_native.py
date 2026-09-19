from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time
from types import SimpleNamespace

import pytest

from cloudworkbench.native_responses import NativeProfile
from cloudworkbench.provider_docker import DockerConfig, DockerError, ProviderDocker
from cloudworkbench.provider_dispatch import ProviderDispatch
from cloudworkbench.provider_leases import ProviderLeases
from cloudworkbench.store import Store
from test_provider_docker import FakeDocker


@pytest.fixture
def native_files(tmp_path, monkeypatch):
    profile = tmp_path / 'profile.json'
    credential = tmp_path / 'synthetic-auth.json'
    credential.write_bytes(b'synthetic')
    staging = tmp_path / 'staging'
    staging.mkdir(mode=0o700)
    original_stat, original_read = Path.lstat, Path.read_bytes
    metadata = {'size': 65536, 'mode': 0o640, 'links': 1}

    def lstat(path):
        if path in (profile, credential):
            return SimpleNamespace(st_mode=stat.S_IFREG | (0o444 if path == profile else metadata['mode']),
                                   st_uid=0, st_gid=959,
                                   st_nlink=1 if path == profile else metadata['links'],
                                   st_size=path.stat().st_size if path == profile else metadata['size'])
        return original_stat(path)

    def read(path):
        assert path != credential, 'controller must never read credential bytes'
        return original_read(path)

    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(Path, 'read_bytes', read)
    monkeypatch.setattr(os, 'fchown', lambda *args: None)

    def configure(value=None, raw=None, digest=None):
        value = value or {'transport': 'native-responses-v1', 'provider': 'openai-codex',
                          'model': 'gpt-6-astra', 'effort': 'high'}
        body = raw if raw is not None else json.dumps(value).encode()
        profile.write_bytes(body)
        digest = digest or NativeProfile('openai-codex', 'gpt-6-astra', 'high').digest
        config = DockerConfig('sha256:'+'a'*64, profile, hashlib.sha256(body).hexdigest(),
                              digest, credential, staging, ('chatgpt.com',))
        return config
    return configure, metadata


def admitted(tmp_path, runtime):
    store = Store(tmp_path / 'state.db')
    principal = store.add_client('test', 't'*40, ['submit'], ['project'])
    attempt = store.create_session(principal, {'project_id':'project', 'agent':'hermes', 'goal':'synthetic'}, 'task')
    store.claim_next()
    leases = ProviderLeases(store, cleanup_verifier=runtime.cleanup, inspector_id=runtime.config.inspector_id)
    leases.register_account('account', legacy_agent='claude', persistent_owner_id='owner')
    owner = leases.reserve('account', persistent_owner_id='owner')
    grant = leases.issue_grant(owner, attempt_id=attempt['attempt_id'], generation=1)
    dispatcher = ProviderDispatch(leases, runtime)
    payload = b'{"synthetic":true}'
    spec = dispatcher.admit(owner, grant, request_nonce=secrets.token_hex(32), payload=payload,
                            profile_digest=runtime.config.profile_digest)
    return dispatcher, spec, payload


@pytest.mark.parametrize('provider,model,effort', [
    ('openai-codex','gpt-6-astra','high'), ('openai-codex','gpt-5.6-sol','max'), ('xai-oauth','grok-4.6','xhigh')])
def test_native_create_inspect_cleanup_uses_exact_readonly_mount(native_files, tmp_path, provider, model, effort):
    configure, _ = native_files
    value = dict(transport='native-responses-v1', provider=provider, model=model, effort=effort)
    config = configure(value, digest=NativeProfile(provider,model,effort).digest)
    fake = FakeDocker()
    runtime = ProviderDocker(config, cancel_check=lambda _:False, command=fake)
    dispatcher, spec, payload = admitted(tmp_path, runtime)
    dispatcher.launch(spec.lease.request_id, payload)
    provider_object = next(obj for obj in fake.objects.values() if obj.get('Name') == '/'+spec.provider_name)
    mounts = provider_object['Mounts']
    assert {m['Destination'] for m in mounts} == {'/run/provider/profile.json','/run/provider/request.json','/run/secrets/native-auth.json'}
    assert all(not m['RW'] for m in mounts)
    assert all(obj.get('Mounts') == [] for obj in fake.objects.values() if obj.get('Name') == '/'+spec.gateway_name)
    assert not any('/run/secrets/claude-token' in str(command) for command in fake.commands)
    dispatcher.collect(spec.lease.request_id)
    dispatcher.cleanup(spec.lease.request_id)
    assert not fake.objects


@pytest.mark.parametrize('mutation', [
    {'transport':'native-responses-v2'}, {'provider':'anthropic'}, {'model':'auto'},
    {'effort':'low'}, {'credential':'untrusted'}, {'effort':None}])
def test_native_invalid_profile_refused_without_docker(native_files, mutation):
    configure, _ = native_files
    value = dict(transport='native-responses-v1',provider='openai-codex',model='gpt-6-astra',effort='high')
    value.update(mutation)
    fake = FakeDocker()
    with pytest.raises(DockerError, match='profile_binding_mismatch'):
        ProviderDocker(configure(value), cancel_check=lambda _:False, command=fake)
    assert fake.commands == []


@pytest.mark.parametrize('body', [b'[]', b'null', b'{"transport":"native-responses-v1"}',
    b'{"transport":"native-responses-v1","provider":"xai-oauth","provider":"openai-codex","model":"gpt-6-astra","effort":"high"}'])
def test_native_malformed_or_duplicate_fields_refused(native_files, body):
    configure, _ = native_files
    with pytest.raises(DockerError, match='profile_binding_mismatch'):
        ProviderDocker(configure(raw=body), cancel_check=lambda _:False, command=FakeDocker())


def test_native_digest_mismatch_refused(native_files):
    configure, _ = native_files
    with pytest.raises(DockerError, match='profile_binding_mismatch'):
        ProviderDocker(configure(digest='f'*64), cancel_check=lambda _:False, command=FakeDocker())


@pytest.mark.parametrize('field,value', [('size',65537),('size',0),('mode',0o644),('links',2)])
def test_native_credential_metadata_limits(native_files, field, value):
    configure, metadata = native_files
    metadata[field] = value
    with pytest.raises(DockerError, match='unsafe_credential_mount'):
        ProviderDocker(configure(), cancel_check=lambda _:False, command=FakeDocker())


@pytest.mark.parametrize('tamper', ['legacy_target','writable','extra','duplicate_replacement','wrong_source'])
def test_native_inspection_rejects_credential_mount_drift(native_files, tmp_path, tamper):
    configure, _ = native_files
    fake = FakeDocker()
    runtime = ProviderDocker(configure(), cancel_check=lambda _:False, command=fake)
    _, spec, payload = admitted(tmp_path, runtime)
    resources = runtime.create(spec,payload,deadline=time.monotonic()+10)
    mounts = fake.objects[resources.provider_id]['Mounts']
    credential = next(m for m in mounts if m['Destination']=='/run/secrets/native-auth.json')
    if tamper=='legacy_target':credential['Destination']='/run/secrets/claude-token'
    elif tamper=='writable':credential['RW']=True
    elif tamper=='wrong_source':credential['Source']='/untrusted'
    elif tamper=='duplicate_replacement':mounts[0]=dict(credential)
    else:mounts.append(dict(credential))
    with pytest.raises(DockerError,match='container_mount_mismatch'):
        runtime.inspect(spec,resources,deadline=time.monotonic()+5)


def test_profile_change_after_create_refuses_start_but_cleanup_still_works(native_files, tmp_path):
    configure, _ = native_files
    fake = FakeDocker()
    runtime = ProviderDocker(configure(), cancel_check=lambda _:False, command=fake)
    _, spec, payload = admitted(tmp_path,runtime)
    resources = runtime.create(spec,payload,deadline=time.monotonic()+10)
    runtime.config.profile_path.write_bytes(b'{}')
    with pytest.raises(DockerError,match='profile_file_changed'):
        runtime.start(spec,resources,deadline=time.monotonic()+5)
    assert not any(command[0]=='start' for command in fake.commands)
    # Inspection remains bound to validated config, allowing cleanup despite a damaged profile.
    runtime.inspect(spec,resources,deadline=time.monotonic()+5)

    runtime._cleanup_known(spec,runtime._load(spec),time.monotonic()+10)
    assert not fake.objects


def test_new_adapter_reconstructs_native_mount_binding(native_files, tmp_path):
    configure, _ = native_files
    config = configure()
    fake = FakeDocker()
    runtime = ProviderDocker(config, cancel_check=lambda _:False, command=fake)
    _, spec, payload = admitted(tmp_path,runtime)
    resources = runtime.create(spec,payload,deadline=time.monotonic()+10)
    restarted = ProviderDocker(config, cancel_check=lambda _:False, command=fake)
    assert restarted.inspect(spec,resources,deadline=time.monotonic()+5)
    mount = next(m for m in fake.objects[resources.provider_id]['Mounts'] if m['Destination']=='/run/secrets/native-auth.json')
    mount['Destination']='/run/secrets/claude-token'
    with pytest.raises(DockerError,match='container_mount_mismatch'):
        restarted.inspect(spec,resources,deadline=time.monotonic()+5)
