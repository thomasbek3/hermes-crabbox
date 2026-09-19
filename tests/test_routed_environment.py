import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from cloudworkbench.environments import EnvironmentRegistry, canonical
from cloudworkbench import routed_environment as re
from tests.test_environments import passed

IMAGE = 'sha256:' + '1' * 64
SCRIPT = b'print("protected check")\n'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def source(tmp_path):
    root = tmp_path.resolve()
    script = root / 'check.py'
    script.write_bytes(SCRIPT)
    script.chmod(0o600)
    manifest = {'project_id':'demo', 'version':'v1', 'architecture':'amd64',
        'base_image_digest':IMAGE, 'image_digest':IMAGE, 'cli_versions':{'hermes':'0.21.3'},
        'readiness_probes':[{'id':'python', 'argv':['python3','--version']}],
        'checks':[{'id':'check', 'description':'Protected check.',
            'argv':['/usr/bin/python3','/run/task/check.py'], 'script_id':'protected-check',
            'script_name':'check.py', 'script_sha256':digest(SCRIPT)}]}
    registry = EnvironmentRegistry(root / 'registry.db')
    return registry, manifest, script


def resolve(source, *, qualify=True, **overrides):
    registry, manifest, script = source
    if qualify:
        registry.register(manifest)
        registry.qualify('demo', 'v1', passed)
    args = dict(project_id='demo', version='v1', allowed_versions=('v1',),
        script_sources={'protected-check':script}, forbidden_values=())
    args.update(overrides)
    return re.resolve_routed_environment(registry, **args)


def load(value, script, **overrides):
    args = dict(project_id='demo', allowed_versions=('v1',),
        script_sources={'protected-check':script}, forbidden_values=())
    args.update(overrides)
    return re.load_routed_environment(value.snapshot if hasattr(value,'snapshot') else value, **args)


def resign(snapshot):
    snapshot['qualification_sha256'] = digest(canonical(snapshot['qualification']).encode())
    snapshot['snapshot_sha256'] = digest(canonical({k:v for k,v in snapshot.items() if k != 'snapshot_sha256'}).encode())
    return snapshot


def test_resolve_read_only_detached_and_no_private_paths(source):
    registry, _, script = source
    first = resolve(source)
    before = registry.path.read_bytes()
    assert resolve(source, qualify=False).snapshot == first.snapshot
    assert registry.path.read_bytes() == before
    assert first.scripts == {'protected-check':SCRIPT}
    assert str(script) not in canonical(first.snapshot)
    assert SCRIPT.decode() not in repr(first)
    mutated = first.snapshot; mutated['manifest']['checks'].clear()
    first.scripts.clear()
    assert len(first.manifest['checks']) == 1 and first.scripts
    assert re.validate_routed_environment_snapshot(first.snapshot, project_id='demo', allowed_versions=['v1']) == first.snapshot


def test_frozen_load_never_uses_registry_default_or_read(source, monkeypatch):
    registry, manifest, script = source
    first = resolve(source)
    registry.activate('demo','v1',expected_active=None)
    registry.register({**manifest,'version':'v2'})
    registry.qualify('demo','v2',passed)
    registry.activate('demo','v2',expected_active='v1')
    monkeypatch.setattr(EnvironmentRegistry, 'get', lambda *args: pytest.fail('registry read on frozen load'))
    assert load(first,script).snapshot == first.snapshot


@pytest.mark.parametrize('version', [None, '', 'latest/name', True])
def test_explicit_version_required(source,version):
    with pytest.raises(re.RoutedEnvironmentError, match='explicit_version_required'):
        resolve(source, version=version)


def test_allowlist_and_project_remain_authoritative_on_load(source):
    first=resolve(source)
    with pytest.raises(re.RoutedEnvironmentError,match='version_not_allowed'):
        load(first,source[2],allowed_versions=['v2'])
    with pytest.raises(re.RoutedEnvironmentError,match='project_mismatch'):
        load(first,source[2],project_id='other')


def test_unqualified_manifest_refused(source):
    source[0].register(source[1])
    with pytest.raises(re.RoutedEnvironmentError,match='not_qualified'):
        resolve(source,qualify=False)


@pytest.mark.parametrize('change', [
    {'image_digest':'sha256:'+'2'*64}, {'architecture':'arm64'}, {'cli_versions':{'hermes':'wrong'}},
    {'probes':[{'id':'python','exit_code':1}]}, {'probes':[{'id':'other','exit_code':0}]},
    {'qualification_id':'not-an-id'}, {'qualified_at':'not-a-date'}, {'unexpected':True},
])
def test_frozen_qualification_revalidated_not_just_self_hash(source,change):
    value=resolve(source).snapshot
    value['qualification'].update(change); resign(value)
    with pytest.raises(re.RoutedEnvironmentError,match='qualification'):
        load(value,source[2])


@pytest.mark.parametrize('sql', [
    "UPDATE qualification_attempts SET status='failed'",
    "UPDATE qualification_attempts SET receipt='{}'",
    "UPDATE qualification_attempts SET version='other'",
    "UPDATE qualification_attempts SET finished_at='2099-01-01T00:00:00+00:00'",
    "DELETE FROM qualification_attempts",
])
def test_qualified_boolean_cannot_replace_actual_attempt(source,sql):
    resolve(source)
    with source[0].transaction() as db: db.execute(sql)
    assert source[0].get('demo','v1')['qualified'] is True
    with pytest.raises(re.RoutedEnvironmentError,match='qualification_record_mismatch'):
        resolve(source,qualify=False)


def test_first_success_survives_later_failed_qualification(source):
    first=resolve(source)
    with pytest.raises(ValueError):
        source[0].qualify('demo','v1', lambda record: {**passed(record),'probes':[{'id':'python','exit_code':1}]})
    assert resolve(source,qualify=False).snapshot == first.snapshot


@pytest.mark.parametrize('kind', ['symlink','ancestor_symlink','hardlink','fifo','writable','writable_parent','foreign_owner'])
def test_descriptor_source_safety(source,kind,monkeypatch):
    first=resolve(source); script=source[2]
    if kind=='symlink':
        target=script.with_name('target.py'); script.rename(target); script.symlink_to(target)
    elif kind=='ancestor_symlink':
        alias=script.parent/'alias'; alias.symlink_to(script.parent, target_is_directory=True); script=alias/script.name
    elif kind=='hardlink': os.link(script,script.with_name('hardlink'))
    elif kind=='fifo': script.unlink(); os.mkfifo(script)
    elif kind=='writable': script.chmod(0o660)
    elif kind=='writable_parent': script.parent.chmod(0o770)
    elif kind=='foreign_owner':
        original=re.os.fstat
        def fake(fd):
            info=original(fd)
            if info.st_ino==script.stat().st_ino:
                values=list(info);values[4]=123456
                return os.stat_result(values)
            return info
        monkeypatch.setattr(re.os,'fstat',fake)
    with pytest.raises(re.RoutedEnvironmentError,match='environment_script_'):
        load(first,script)


def test_changed_script_exact_hash_refused(source):
    first=resolve(source); source[2].write_bytes(b'print("different")\n')
    with pytest.raises(re.RoutedEnvironmentError,match='digest_mismatch'): load(first,source[2])


def test_source_mutation_during_read_refused(source,monkeypatch):
    first=resolve(source); original=re.os.read; changed=False
    def read(fd,size):
        nonlocal changed
        part=original(fd,size)
        if part and not changed:
            changed=True;source[2].write_bytes(SCRIPT+b'# altered')
        return part
    monkeypatch.setattr(re.os,'read',read)
    with pytest.raises(re.RoutedEnvironmentError,match='script_changed'): load(first,source[2])


def test_source_same_content_inode_replacement_refused(source,monkeypatch):
    first=resolve(source); original=re.os.read; changed=False
    def read(fd,size):
        nonlocal changed
        part=original(fd,size)
        if part and not changed:
            changed=True
            replacement=source[2].with_name('replacement');replacement.write_bytes(SCRIPT)
            replacement.chmod(0o600);replacement.replace(source[2])
        return part
    monkeypatch.setattr(re.os,'read',read)
    with pytest.raises(re.RoutedEnvironmentError,match='script_changed'): load(first,source[2])


@pytest.mark.parametrize('where',['script','metadata','escaped_metadata'])
def test_stronger_known_secret_policy_refuses_without_disclosing(source,where):
    secret= b'protected check' if where=='script' else b'Protected check.'
    if where=='escaped_metadata':
        source[1]['checks'][0]['description']='private\nmarker';secret=b'private\nmarker'
    first=resolve(source)
    with pytest.raises(re.RoutedEnvironmentError,match='known_secret_refused') as error:
        load(first,source[2],forbidden_values=(secret,))
    assert secret.decode() not in str(error.value)


def test_unsupported_runtime_argv_refused_before_script_read(source,monkeypatch):
    source[1]['checks'][0]['argv']=['python3','/run/task/check.py']
    monkeypatch.setattr(re,'_read_script',lambda *args:pytest.fail('unexpected read'))
    with pytest.raises(re.RoutedEnvironmentError,match='argv_unsupported'): resolve(source)


def test_caller_resource_limits_are_not_verifier_limits(source):
    source[1]['resources']={'cpus':6,'memory_mib':16384,'pids':1024,'workspace_mib':20480}
    assert resolve(source).manifest['resources']['cpus']==6


def test_check_count_bound(source):
    check=source[1]['checks'][0]
    source[1]['checks']=[{**check,'id':f'check{i}'} for i in range(33)]
    with pytest.raises(re.RoutedEnvironmentError,match='check_limit'): resolve(source)


def test_snapshot_size_refuses_no_truncation(source):
    check=source[1]['checks'][0]
    source[1]['checks']=[{**check,'id':f'check{i}','description':'a'*8000} for i in range(5)]
    with pytest.raises(re.RoutedEnvironmentError,match='snapshot_too_large'): resolve(source)


def test_empty_checks_preserved_for_later_needs_review_policy(source):
    source[1]['checks']=[]
    value=resolve(source,script_sources={})
    assert value.scripts=={} and value.manifest['checks']==[]


def test_snapshot_mutation_without_rehash_refused(source):
    value=resolve(source).snapshot;value['manifest']['version']='v2'
    with pytest.raises(re.RoutedEnvironmentError,match='digest_mismatch'): load(value,source[2])


def test_missing_mapping_does_not_resolve_job_paths(source):
    with pytest.raises(re.RoutedEnvironmentError,match='mapping_missing'):
        resolve(source,script_sources={'other':source[2]})


def test_script_deadline_fails_closed(source,monkeypatch):
    first=resolve(source); clock=iter([0,6])
    monkeypatch.setattr(re.time,'monotonic',lambda:next(clock))
    with pytest.raises(re.RoutedEnvironmentError,match='read_deadline'): load(first,source[2])


def test_corrupt_registry_errors_sanitized(source):
    source[0].path.write_bytes(b'private broken bytes')
    with pytest.raises(re.RoutedEnvironmentError,match='registry_unavailable') as error:
        resolve(source,qualify=False)
    assert 'private' not in str(error.value)


def test_root_sticky_shared_ancestor_requires_protected_descendants(source,monkeypatch):
    first=resolve(source); original=re.os.fstat
    root_inode=source[2].parent.stat().st_ino
    def synthetic_root_shared(fd):
        info=original(fd)
        if info.st_ino==root_inode:
            values=list(info);values[0]=0o41777;values[4]=0
            return os.stat_result(values)
        return info
    # Pure permission check proves only the root-owned sticky exception; descriptor
    # identity drift remains tested separately with the actual filesystem.
    monkeypatch.setattr(re.os,'fstat',synthetic_root_shared)
    fd=os.open(source[2].parent,os.O_RDONLY)
    try: re._trusted(re.os.fstat(fd),directory=True)
    finally: os.close(fd)
    values=list(source[2].parent.stat());values[0]=0o40777;values[4]=0
    with pytest.raises(re.RoutedEnvironmentError): re._trusted(os.stat_result(values),directory=True)
    values[0]=0o41777;values[4]=123456
    with pytest.raises(re.RoutedEnvironmentError): re._trusted(os.stat_result(values),directory=True)


@pytest.mark.parametrize('size',[0,re.MAX_SCRIPT_BYTES+1])
def test_script_size_bound_before_read(source,size,monkeypatch):
    first=resolve(source);source[2].write_bytes(b'a'*size)
    monkeypatch.setattr(re.os,'read',lambda *args:pytest.fail('file read before size bound'))
    with pytest.raises(re.RoutedEnvironmentError,match='size_or_links_invalid'):load(first,source[2])


def test_total_script_bytes_bound(source):
    registry,manifest,script=source
    mapping={};checks=[]
    for index in range(5):
        raw=b'#'+b'x'*900000+b'\n'
        path=script.with_name(f'check{index}.py');path.write_bytes(raw);path.chmod(0o600)
        identity=f'script{index}';mapping[identity]=path
        checks.append({**manifest['checks'][0], 'id':f'check{index}', 'script_id':identity,
            'script_name':path.name,'script_sha256':digest(raw),'argv':['/usr/bin/python3','/run/task/'+path.name]})
    manifest['checks']=checks
    with pytest.raises(re.RoutedEnvironmentError,match='total_limit'):resolve(source,script_sources=mapping)


def test_pure_snapshot_validation_never_reads_scripts(source,monkeypatch):
    value=resolve(source)
    monkeypatch.setattr(re,'_read_script',lambda *args:pytest.fail('script read during pure validation'))
    assert re.validate_routed_environment_snapshot(value.snapshot,project_id='demo',allowed_versions=['v1'])==value.snapshot


def test_real_root_sticky_ancestor_with_private_script_directory(source):
    import stat
    import tempfile
    shared=Path('/tmp').resolve()
    info=shared.stat()
    if info.st_uid!=0 or not info.st_mode & stat.S_ISVTX:
        pytest.skip('host has no root-owned sticky temporary directory')
    first=resolve(source)
    with tempfile.TemporaryDirectory(prefix='cwb-env-script-',dir=shared) as directory:
        path=Path(directory)/'check.py';path.write_bytes(SCRIPT);path.chmod(0o600)
        assert load(first,path).scripts==first.scripts
