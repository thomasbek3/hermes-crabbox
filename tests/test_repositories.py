import subprocess
from pathlib import Path

import pytest

from cloudworkbench.repositories import RepositoryError, snapshot, text_patch


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path/'repo'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Qualification')
    git(root, 'config', 'user.email', 'qualification@example.invalid')
    (root/'booking.py').write_text('def valid_date(value):\n    return True\n')
    (root/'binary.dat').write_bytes(b'\x00\xffsample')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Known broken base')
    return root, git(root, 'rev-parse', 'HEAD')


def test_immutable_snapshot_and_applicable_patch(repository, tmp_path):
    root, commit = repository
    baseline = snapshot(root, commit)
    (root/'booking.py').write_text('uncommitted changes must not enter baseline\n')
    assert baseline['files']['booking.py']['content'].endswith(b'return True\n')
    delivered = {name:item['content'] for name,item in baseline['files'].items()}
    delivered['booking.py'] = b'def valid_date(value):\n    return False\n'
    delivered['README.md'] = b'# Date checker\n'
    result = text_patch(baseline, delivered)
    clone = tmp_path/'apply'
    subprocess.run(['git', 'clone', '-q', '--no-hardlinks', str(root), str(clone)], check=True)
    patch = tmp_path/'change.patch'
    patch.write_bytes(result['patch'])
    git(clone, 'apply', '--check', str(patch))
    git(clone, 'apply', str(patch))
    assert (clone/'booking.py').read_bytes() == delivered['booking.py']
    assert (clone/'README.md').read_bytes() == delivered['README.md']
    assert result['complete_text_patch']


@pytest.mark.parametrize('ref', ['HEAD', 'main', '--output=/tmp/x', 'a'*39, 'f'*40+';pwd'])
def test_only_full_commit_ids(repository, ref):
    with pytest.raises(RepositoryError, match='immutable_commit'):
        snapshot(repository[0], ref)


def test_rejects_links_and_enforces_bounds(repository):
    root, commit = repository
    with pytest.raises(RepositoryError, match='snapshot_limit'):
        snapshot(root, commit, max_bytes=1)
    with pytest.raises(RepositoryError, match='snapshot_limit'):
        snapshot(root, commit, max_files=1)
    (root/'outside').symlink_to('/etc/passwd')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Link must be rejected')
    with pytest.raises(RepositoryError, match='links_or_special'):
        snapshot(root, git(root, 'rev-parse', 'HEAD'))


def test_binary_and_nonrepresentable_changes_explicit(repository):
    root, commit = repository
    baseline = snapshot(root, commit)
    delivered = {name:item['content'] for name,item in baseline['files'].items()}
    delivered.update({'binary.dat':b'\x00changed', 'empty':b'', 'odd name.md':b'ok\n', 'tail.txt':b'no newline'})
    result = text_patch(baseline, delivered)
    assert not result['complete_text_patch']
    assert len(result['excluded_from_text_patch']) == 3
    assert len(result['changed_files']) == 4


def test_patch_rejects_escape_and_limits(repository):
    baseline = snapshot(*repository)
    with pytest.raises(RepositoryError, match='path_rejected'):
        text_patch(baseline, {'../escape':b'x\n'})
    result=text_patch(baseline, {'new':b'x\n'}, max_bytes=1)
    assert result['patch']==b'' and not result['complete_text_patch']
    assert any(x['reason'].startswith('patch_byte_limit') for x in result['excluded_from_text_patch'])


def test_baseline_persistence_and_partial_initialization(repository,tmp_path):
    from cloudworkbench.repositories import save_baseline,load_baseline,initialize_workspace
    baseline=snapshot(*repository)
    path=tmp_path/'baseline.json'
    save_baseline(path,baseline)
    save_baseline(path,baseline)
    loaded=load_baseline(path)
    assert loaded==baseline
    workspace=tmp_path/'workspace';workspace.mkdir()
    (workspace/'booking.py').write_bytes(baseline['files']['booking.py']['content'])
    initialize_workspace(workspace,loaded)
    assert (workspace/'binary.dat').read_bytes()==b'\x00\xffsample'
    (workspace/'booking.py').write_text('agent work must be preserved')
    with pytest.raises(RepositoryError,match='initialization_conflict'):
        initialize_workspace(workspace,loaded)
    assert (workspace/'booking.py').read_text()=='agent work must be preserved'


def test_baseline_tampering_rejected(repository,tmp_path):
    import json
    from cloudworkbench.repositories import save_baseline,load_baseline
    path=tmp_path/'baseline.json';save_baseline(path,snapshot(*repository))
    data=json.loads(path.read_text());data['files']['booking.py']['sha256']='0'*64
    path.write_text(json.dumps(data))
    with pytest.raises(RepositoryError,match='baseline_invalid'):load_baseline(path)


@pytest.mark.parametrize('separator',['\f','\v','\r','\x85','\u2028'])
def test_non_lf_text_records_apply_exactly(repository,tmp_path,separator):
    root,commit=repository;baseline=snapshot(root,commit)
    delivered={name:item['content'] for name,item in baseline['files'].items()}
    delivered['booking.py']=('one'+separator+'two\nlast\n').encode()
    result=text_patch(baseline,delivered)
    patch=tmp_path/'delta.patch';patch.write_bytes(result['patch'])
    git(root,'apply','--check',str(patch));git(root,'apply',str(patch))
    assert (root/'booking.py').read_bytes()==delivered['booking.py']


def test_executable_and_empty_file_changes_apply(repository,tmp_path):
    root,commit=repository;baseline=snapshot(root,commit)
    delivered={name:item['content'] for name,item in baseline['files'].items()}
    delivered['empty']=b''
    modes={name:False for name in delivered};modes['booking.py']=True
    result=text_patch(baseline,delivered,delivered_modes=modes)
    patch=tmp_path/'delta.patch';patch.write_bytes(result['patch'])
    git(root,'apply','--check',str(patch));git(root,'apply',str(patch))
    assert (root/'booking.py').stat().st_mode & 0o100
    assert (root/'empty').read_bytes()==b''
    assert result['complete_text_patch'] and result['mode_tracking']


def test_excluded_baseline_not_reported_as_deleted(repository):
    from cloudworkbench.artifacts import DEPENDENCY_EXCLUSIONS
    baseline=snapshot(*repository)
    baseline['files']['node_modules/tracked.js']={'content':b'x\n','executable':False}
    delivered={name:item['content'] for name,item in baseline['files'].items() if not name.startswith('node_modules/')}
    result=text_patch(baseline,delivered,excluded_names=DEPENDENCY_EXCLUSIONS)
    assert not result['changed_files'] and not result['patch']
    assert result['baseline_paths_not_exported']==[{'path':'node_modules/tracked.js','reason':'outside_export_policy'}]
    assert not result['complete_text_patch']
