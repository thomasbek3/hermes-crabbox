from dataclasses import replace
import json
import os
from pathlib import Path
import stat

import pytest

from cloudworkbench.store import Store
from cloudworkbench.scheduler import RoleScheduler
from cloudworkbench.workflow_revisions import (
    RevisionBinding, RevisionError, RevisionLimits, capture_revision,
    verify_revision, materialize_stage, discard_unlaunched_materialization,
)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'state.db')
    owner = store.add_client('revision fixture', 'x' * 40, ['submit'], ['demo'])
    store.migrate_scheduler()
    scheduler = RoleScheduler(store, None)
    frozen = {'accounts': {'synthetic': 'owner'}, 'role_plans': {}, 'provenance': {'synthetic': True}}
    root = scheduler.enqueue_root(owner, {'project_id': 'demo', 'agent': 'hermes', 'goal': 'revision test'}, 'root', frozen=frozen)
    binding = RevisionBinding(owner['id'], 'demo', root['session_id'], root['turn_id'], root['attempt_id'], 1, root['attempt_id'], 1)
    work, revisions = tmp_path / 'work', tmp_path / 'revisions'
    work.mkdir(); revisions.mkdir()
    (work / 'src').mkdir()
    (work / 'src/code.py').write_text('original\n')
    return store, binding, work, revisions, scheduler, owner, frozen


def capture(setup, **kwargs):
    store, binding, work, revisions, *_ = setup
    return capture_revision(store, binding, work, revisions, selected_paths=kwargs.pop('selected_paths', ('src',)),
                            controller_attests_quiesced=True, **kwargs)


def child_binding(setup):
    store, binding, *_ = setup
    # Identity-only fixture: real queued child rows satisfy scheduler immutable identity guards.
    with store._tx() as db:
        db.execute("INSERT INTO workflow_requests VALUES('request','pending',?,1,'implementation','{}','[]',1)", (binding.root_attempt_id,))
        db.execute("INSERT INTO workflow_seats VALUES('seat','request',0,'{}')")
        db.execute("""INSERT INTO attempts(id,session_id,turn_id,agent,generation,state,created_at,updated_at,
          execution_kind,workflow_root_id,workflow_parent_id,workflow_parent_generation,role_seat_id)
          VALUES('child',?,?,'hermes',1,'queued','now','now','hermes_child',?,?,1,'seat')""",
                   (binding.session_id, binding.turn_id, binding.root_attempt_id, binding.root_attempt_id))
    return replace(binding, attempt_id='child')


def test_capture_is_identity_bound_selected_and_deterministic(setup):
    store, binding, work, _, *_ = setup
    (work / 'native').mkdir(); (work / 'native/auth.json').write_text('excluded secret')
    (work / 'unselected.txt').write_text('outside selection')
    before = store.get_attempt(binding.attempt_id)
    first = capture(setup)
    assert capture(setup) == first
    manifest = verify_revision(store, first)
    assert manifest['binding']['root_attempt_id'] == binding.root_attempt_id
    assert manifest['selected_paths'] == ['src']
    assert [f['path'] for f in manifest['files']] == ['src/code.py']
    assert store.get_attempt(binding.attempt_id) == before
    (work / 'src/code.py').write_text('new candidate\n')
    second = capture(setup)
    assert first.sha256 != second.sha256
    assert (first.path / 'files/src/code.py').read_text() == 'original\n'


def test_materialize_child_review_and_writer_are_separate_copies(setup, tmp_path):
    store, _, _, _, *_ = setup
    revision = capture(setup)
    child = child_binding(setup)
    review = materialize_stage(store, revision, child, tmp_path / 'review')
    builder = materialize_stage(store, revision, child, tmp_path / 'builder', readonly=False)
    assert review.mounts == [{'source': str(review.source), 'target': '/workspace', 'readonly': True},
                             {'source': str(review.scratch), 'target': '/scratch', 'readonly': False}]
    assert builder.mounts[0]['readonly'] is False
    assert not stat.S_IMODE((review.source / 'src/code.py').stat().st_mode) & 0o222
    assert stat.S_IMODE((builder.source / 'src/code.py').stat().st_mode) & 0o220 == 0o220
    (builder.source / 'src/code.py').write_text('isolated edit')
    (review.scratch / 'notes').write_text('review findings')
    assert (review.source / 'src/code.py').read_text() == 'original\n'
    assert (revision.path / 'files/src/code.py').read_text() == 'original\n'
    with pytest.raises(RevisionError, match='stage_exists'):
        materialize_stage(store, revision, child, tmp_path / 'review')


def test_legitimate_dotfiles_and_executable_bit_preserved(setup, tmp_path):
    store, binding, work, *_ = setup
    (work / '.github').mkdir(); (work / '.github/check.yml').write_text('test: true')
    (work / '.gitignore').write_text('build/')
    (work / 'run.sh').write_text('echo fixture'); (work / 'run.sh').chmod(0o755)
    revision = capture(setup, selected_paths=('.github', '.gitignore', 'run.sh'))
    stage = materialize_stage(store, revision, binding, tmp_path / 'stage', readonly=False)
    assert (stage.source / '.github/check.yml').is_file()
    assert (stage.source / 'run.sh').stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'src/../secret', 'src//x', '.', 'src\\x', 'src/\x00x'])
def test_path_escape_and_ambiguous_selection_refused(setup, path):
    with pytest.raises(RevisionError, match='unsafe_path'):
        capture(setup, selected_paths=(path,))


@pytest.mark.parametrize('name', ['.git', '.hermes', '.codex', '.claude', '.env', '.env.local',
                                 'native', 'auth', '.credentials', 'credential_state', 'auth.json'])
def test_private_state_inside_selected_tree_refused(setup, name):
    work = setup[2]
    (work / 'src' / name).write_text('synthetic state')
    with pytest.raises(RevisionError, match='private_state_refused'):
        capture(setup)
    assert list(setup[3].iterdir()) == []


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory_symlink'])
def test_unsafe_entries_refused(setup, kind):
    work = setup[2]
    target = work / 'src/unsafe'
    if kind == 'symlink': target.symlink_to(work / 'src/code.py')
    elif kind == 'directory_symlink': target.symlink_to(work, target_is_directory=True)
    elif kind == 'hardlink': os.link(work / 'src/code.py', target)
    else: os.mkfifo(target)
    with pytest.raises(RevisionError, match='unsafe_entry'):
        capture(setup)


@pytest.mark.parametrize('field', ['owner_id', 'project_id', 'session_id', 'turn_id', 'root_attempt_id',
                                 'attempt_id', 'root_generation', 'generation'])
def test_binding_substitution_refused(setup, field):
    store, binding, work, revisions, *_ = setup
    invalid = replace(binding, **{field: 2 if 'generation' in field else 'wrong'})
    with pytest.raises(RevisionError, match='binding_mismatch'):
        capture_revision(store, invalid, work, revisions, selected_paths=('src',), controller_attests_quiesced=True)


def test_valid_different_root_cannot_consume_revision(setup, tmp_path):
    store, binding, _, _, scheduler, owner, frozen = setup
    revision = capture(setup)
    second = scheduler.enqueue_root(owner, {'project_id': 'demo', 'agent': 'hermes', 'goal': 'other'}, 'other', frozen=frozen)
    other = replace(binding, session_id=second['session_id'], turn_id=second['turn_id'],
                    root_attempt_id=second['attempt_id'], attempt_id=second['attempt_id'])
    with pytest.raises(RevisionError, match='cross_root_revision_refused'):
        materialize_stage(store, revision, other, tmp_path / 'cross-root')
    assert not (tmp_path / 'cross-root').exists()


@pytest.mark.parametrize('change', ['bytes', 'extra', 'manifest', 'symlink'])
def test_revision_storage_drift_refused_before_materialization(setup, tmp_path, change):
    store, binding, *_ = setup
    revision = capture(setup)
    source = revision.path / 'files/src/code.py'
    if change == 'bytes': source.chmod(0o600); source.write_text('tampered')
    elif change == 'extra':
        source.parent.chmod(0o700); (source.parent / 'extra').write_text('unexpected')
    elif change == 'manifest':
        path = revision.path / 'manifest.json'; path.chmod(0o600); path.write_text('{}')
    else:
        source.parent.chmod(0o700); source.unlink(); source.symlink_to(setup[2] / 'src/code.py')
    with pytest.raises(RevisionError):
        materialize_stage(store, revision, binding, tmp_path / 'tampered-stage')
    assert not (tmp_path / 'tampered-stage').exists()


def test_source_mutation_during_read_refuses_entire_revision(setup, monkeypatch):
    import cloudworkbench.workflow_revisions as revisions
    source = setup[2] / 'src/code.py'
    original_read = revisions.os.read
    changed = False
    def mutate(fd, size):
        nonlocal changed
        data = original_read(fd, size)
        if not changed:
            changed = True
            source.write_text('changed during capture')
        return data
    monkeypatch.setattr(revisions.os, 'read', mutate)
    with pytest.raises(RevisionError, match='source_changed'):
        capture(setup)
    assert not list(setup[3].iterdir())


def test_explicit_attestation_bounds_secrets_and_overlap(setup):
    store, binding, work, revisions, *_ = setup
    with pytest.raises(RevisionError, match='attestation'):
        capture_revision(store, binding, work, revisions, selected_paths=('src',))
    with pytest.raises(RevisionError, match='byte_limit'):
        capture(setup, limits=RevisionLimits(max_bytes=1))
    with pytest.raises(RevisionError, match='entry_limit'):
        capture(setup, limits=RevisionLimits(max_entries=1))
    with pytest.raises(RevisionError, match='secret_refused'):
        capture(setup, forbidden_values=(b'original',))
    with pytest.raises(RevisionError, match='overlapping_selection'):
        capture(setup, selected_paths=('src', 'src/code.py'))
    with pytest.raises(RevisionError, match='storage_overlap'):
        capture_revision(store, binding, work, work / 'src', selected_paths=('src',), controller_attests_quiesced=True)
    assert not list(revisions.iterdir())


def test_selected_nested_file_omits_siblings_and_retains_ancestors(setup):
    (setup[2] / 'src/unselected').write_text('not copied')
    revision = capture(setup, selected_paths=('src/code.py',))
    manifest = verify_revision(setup[0], revision)
    assert manifest['directories'] == ['src']
    assert [f['path'] for f in manifest['files']] == ['src/code.py']


def test_stale_generation_during_capture_is_not_published(setup, monkeypatch):
    import cloudworkbench.workflow_revisions as revisions
    store, binding, *_ = setup
    original = revisions._scan
    def advance(*args, **kwargs):
        value = original(*args, **kwargs)
        with store._tx() as db:
            db.execute('UPDATE attempts SET generation=2 WHERE id=?', (binding.attempt_id,))
        return value
    monkeypatch.setattr(revisions, '_scan', advance)
    with pytest.raises(RevisionError, match='binding_mismatch'):
        capture(setup)
    assert list(setup[3].iterdir()) == []


def test_directory_swap_during_nested_read_is_refused(setup, monkeypatch):
    import cloudworkbench.workflow_revisions as revisions
    original = revisions.os.read
    swapped = False
    def swap(fd, size):
        nonlocal swapped
        data = original(fd, size)
        if not swapped:
            swapped = True
            source = setup[2] / 'src'
            source.rename(setup[2] / 'old-src')
            source.mkdir()
            (source / 'code.py').write_text('replacement')
        return data
    monkeypatch.setattr(revisions.os, 'read', swap)
    with pytest.raises(RevisionError, match='source_changed'):
        capture(setup, selected_paths=('src/code.py',))


def test_concurrent_existing_destination_is_never_replaced(setup, tmp_path, monkeypatch):
    import cloudworkbench.workflow_revisions as revisions
    store, binding, *_ = setup
    revision = capture(setup)
    destination = tmp_path / 'stage'
    original = revisions._publish
    def conflict(stage, final):
        final.mkdir()
        (final / 'sentinel').write_text('preserve')
        return original(stage, final)
    monkeypatch.setattr(revisions, '_publish', conflict)
    with pytest.raises(RevisionError, match='publication_conflict'):
        materialize_stage(store, revision, binding, destination)
    assert (destination / 'sentinel').read_text() == 'preserve'
    assert not list(tmp_path.glob('.materialize-*'))


def test_legacy_attempt_layout_cannot_be_used_as_routed_revision(tmp_path):
    store = Store(tmp_path / 'state.db')
    owner = store.add_client('legacy', 'x' * 40, ['submit'], ['demo'])
    root = store.create_session(owner, {'project_id': 'demo', 'agent': 'claude', 'goal': 'legacy'}, 'legacy')
    binding = RevisionBinding(owner['id'], 'demo', root['session_id'], root['turn_id'], root['attempt_id'], 1, root['attempt_id'], 1)
    work = tmp_path / 'work'; work.mkdir()
    revisions = tmp_path / 'revisions'; revisions.mkdir()
    with pytest.raises(RevisionError, match='routed_schema_required'):
        capture_revision(store, binding, work, revisions, selected_paths=('file',), controller_attests_quiesced=True)


def test_required_tool_group_uses_setgid_parent_and_explicit_scratch_permissions(setup, tmp_path):
    store, binding, *_ = setup
    revision = capture(setup)
    parent = tmp_path / 'provisioned'
    parent.mkdir(); parent.chmod(0o2770)
    gid = parent.stat().st_gid
    old_umask = os.umask(0o077)
    try:
        value = materialize_stage(store, revision, binding, parent / 'stage', required_tool_gid=gid)
    finally:
        os.umask(old_umask)
    assert value.required_tool_gid == gid
    assert stat.S_IMODE(value.scratch.stat().st_mode) == 0o770
    assert all(p.lstat().st_gid == gid for p in (value.source.parent, *value.source.parent.rglob('*')))
    assert verify_revision(store, revision)['binding']['attempt_id'] == binding.attempt_id


@pytest.mark.parametrize('failure', ['missing_setgid', 'wrong_gid'])
def test_group_contract_rejects_unprovisioned_parent(setup, tmp_path, failure):
    store, binding, *_ = setup
    revision = capture(setup)
    parent = tmp_path / 'unprovisioned'
    parent.mkdir()
    if failure == 'wrong_gid': parent.chmod(0o2770)
    gid = parent.stat().st_gid + (1 if failure == 'wrong_gid' else 0)
    with pytest.raises(RevisionError, match='preprovisioned_setgid_parent_required'):
        materialize_stage(store, revision, binding, parent / 'stage', required_tool_gid=gid)
    assert not list(parent.iterdir())


def test_wrong_inherited_group_refuses_publication(setup, tmp_path, monkeypatch):
    import cloudworkbench.workflow_revisions as revisions
    from types import SimpleNamespace
    store, binding, *_ = setup
    revision = capture(setup)
    parent = tmp_path / 'provisioned'
    parent.mkdir(); parent.chmod(0o2770)
    gid = parent.stat().st_gid
    original = Path.lstat
    def wrong_group(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path.name == 'code.py' and '.materialize-' in str(path):
            return SimpleNamespace(st_gid=gid+1, st_mode=info.st_mode)
        return info
    monkeypatch.setattr(Path, 'lstat', wrong_group)
    with pytest.raises(RevisionError, match='materialization_group_mismatch'):
        materialize_stage(store, revision, binding, parent / 'stage', required_tool_gid=gid)
    assert not list(parent.iterdir())


@pytest.mark.parametrize('raises', [False, True])
def test_cancelled_authority_callback_cleans_private_copy(setup, tmp_path, raises):
    store, binding, *_ = setup
    revision = capture(setup)
    observations = []
    destination = tmp_path / 'cancelled-stage'
    def revoked(actual):
        observations.append(actual)
        assert not destination.exists()
        assert list(tmp_path.glob('.materialize-*/source/src/code.py'))
        if raises: raise RuntimeError('trusted cancellation')
        return False
    with pytest.raises((RevisionError, RuntimeError)):
        materialize_stage(store, revision, binding, destination, before_publish=revoked)
    assert observations == [binding]
    assert not destination.exists() and not list(tmp_path.glob('.materialize-*'))
    verify_revision(store, revision)


def test_exact_unlaunched_discard_removes_only_new_copy(setup, tmp_path):
    store, binding, *_ = setup
    revision = capture(setup)
    left = materialize_stage(store, revision, binding, tmp_path / 'left', before_publish=lambda _: True)
    right = materialize_stage(store, revision, binding, tmp_path / 'right')
    with pytest.raises(RevisionError, match='attestation'):
        discard_unlaunched_materialization(left)
    discard_unlaunched_materialization(left, controller_attests_unlaunched=True)
    assert not left.source.parent.exists()
    assert right.source.exists() and revision.path.exists()
    verify_revision(store, revision)
    with pytest.raises(RevisionError, match='discard_failed'):
        discard_unlaunched_materialization(left, controller_attests_unlaunched=True)


@pytest.mark.parametrize('change', ['root', 'source', 'metadata', 'contents', 'scratch', 'handle_binding', 'handle_digest'])
def test_discard_refuses_foreign_replacement_or_changed_work(setup, tmp_path, change):
    store, binding, *_ = setup
    revision = capture(setup)
    value = materialize_stage(store, revision, binding, tmp_path / 'stage', readonly=False)
    root = value.source.parent
    if change == 'root':
        root.rename(tmp_path / 'original')
        root.mkdir(); (root / 'foreign').write_text('preserve replacement')
    elif change == 'source':
        value.source.rename(tmp_path / 'old-source')
        value.source.mkdir(); (value.source / 'foreign').write_text('preserve replacement')
    elif change == 'metadata':
        metadata = root / 'materialization.json'; metadata.chmod(0o600); metadata.write_text('{}')
    elif change == 'contents':
        (value.source / 'src/code.py').write_text('possibly launched work')
    elif change == 'scratch':
        (value.scratch / 'notes').write_text('possibly launched work')
    elif change == 'handle_binding':
        value = replace(value, consumer=replace(binding, generation=2))
    else:
        value = replace(value, metadata_sha256='0'*64)
    with pytest.raises(RevisionError):
        discard_unlaunched_materialization(value, controller_attests_unlaunched=True)
    assert root.exists() and revision.path.exists()


def test_discard_refuses_source_symlink_without_touching_target(setup, tmp_path):
    store, binding, *_ = setup
    revision = capture(setup)
    value = materialize_stage(store, revision, binding, tmp_path / 'stage', readonly=False)
    value.source.rename(tmp_path / 'original-source')
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'marker').write_text('preserve')
    value.source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RevisionError):
        discard_unlaunched_materialization(value, controller_attests_unlaunched=True)
    assert (outside / 'marker').read_text() == 'preserve'


def test_material_publication_callback_receives_exact_final_identity_before_publish(setup, tmp_path):
    store, binding, *_ = setup; revision = capture(setup)
    destination = tmp_path / 'durable-stage'; seen = []
    def persist(value):
        seen.append(value)
        assert value.consumer == binding and value.revision_sha256 == revision.sha256
        assert value.source == destination / 'source' and value.scratch == destination / 'scratch'
        assert not destination.exists()
        private, = tmp_path.glob('.materialize-*')
        assert (private.stat().st_dev, private.stat().st_ino) == value.publication_identity
        assert stat.S_IMODE(private.stat().st_mode) == 0o750
        import hashlib
        assert hashlib.sha256((private / 'materialization.json').read_bytes()).hexdigest() == value.metadata_sha256
        return True
    value = materialize_stage(store, revision, binding, destination, before_publish_material=persist)
    assert seen == [value]
    assert (destination.stat().st_dev, destination.stat().st_ino) == value.publication_identity


@pytest.mark.parametrize('outcome', ['refuse', 'raise', 'truthy'])
def test_material_publication_callback_failure_leaves_no_destination(setup, tmp_path, outcome):
    store, binding, *_ = setup; revision = capture(setup)
    destination = tmp_path / 'unpublished'
    def persist(value):
        assert not destination.exists()
        if outcome == 'raise': raise RuntimeError('intent persistence failed')
        return 1 if outcome == 'truthy' else False
    with pytest.raises((RevisionError, RuntimeError)):
        materialize_stage(store, revision, binding, destination, before_publish_material=persist)
    assert not destination.exists() and not list(tmp_path.glob('.materialize-*'))
    verify_revision(store, revision)


def test_material_publication_callback_must_be_callable(setup, tmp_path):
    store, binding, *_ = setup; revision = capture(setup)
    with pytest.raises(RevisionError, match='invalid_material_publication_guard'):
        materialize_stage(store, revision, binding, tmp_path / 'unpublished', before_publish_material=True)
    assert not (tmp_path / 'unpublished').exists()
