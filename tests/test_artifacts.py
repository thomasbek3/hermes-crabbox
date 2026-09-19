import hashlib
import os
from pathlib import Path
import socket
import stat

import pytest

from cloudworkbench.artifacts import ExportError, export_workspace


@pytest.fixture
def roots(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = tmp_path / "storage"
    storage.mkdir()
    return workspace, storage / "result"


def assert_unpublished(destination):
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_binary_roundtrip_hash_permissions_and_nested_paths(roots):
    workspace, destination = roots
    data = bytes(range(256)) * 1024
    (workspace / "nested").mkdir()
    (workspace / "nested" / "data.bin").write_bytes(data)
    (workspace / "empty.txt").touch()
    result = export_workspace(workspace, destination)
    assert [record["path"] for record in result] == ["empty.txt", "nested/data.bin"]
    record = result[1]
    assert record["sha256"] == hashlib.sha256(data).hexdigest()
    assert record["bytes"] == len(data)
    assert record["mime"] == "application/octet-stream"
    assert Path(record["storage_path"]).read_bytes() == data
    assert stat.S_IMODE(Path(record["storage_path"]).stat().st_mode) == 0o400
    assert stat.S_IMODE(destination.stat().st_mode) == 0o500
    assert (workspace / "nested" / "data.bin").read_bytes() == data


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo", "socket"])
def test_reject_special_nodes_without_reading_them(roots, kind, monkeypatch):
    workspace, destination = roots
    outside = workspace.parent / "private"
    outside.write_bytes(b"not for export")
    (workspace / "first.txt").write_text("valid")
    evil = workspace / "evil"
    sock = None
    if kind == "symlink":
        evil.symlink_to(outside)
    elif kind == "directory_symlink":
        evil.symlink_to(workspace.parent, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside, evil)
    elif kind == "fifo":
        os.mkfifo(evil)
    else:
        sock = socket.socket(socket.AF_UNIX)
        monkeypatch.chdir(workspace)
        sock.bind(evil.name)
    try:
        with pytest.raises(ExportError):
            export_workspace(workspace, destination)
        assert_unpublished(destination)
        assert outside.read_bytes() == b"not for export"
    finally:
        if sock:
            sock.close()


def test_reject_symlink_workspace_ancestor(roots):
    workspace, destination = roots
    alias = workspace.parent / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ExportError):
        export_workspace(alias, destination)
    assert_unpublished(destination)


@pytest.mark.parametrize("limits", [{"max_bytes": 2}, {"max_files": 1}, {"max_depth": 0}, {"max_seconds": 1e-12}])
def test_export_bounds_are_atomic(roots, limits):
    workspace, destination = roots
    (workspace / "nested").mkdir()
    (workspace / "nested" / "first").write_text("123")
    (workspace / "second").write_text("45")
    with pytest.raises(ExportError):
        export_workspace(workspace, destination, **limits)
    assert_unpublished(destination)


def test_secret_split_at_stream_boundary_rejects_entire_bundle(roots):
    workspace, destination = roots
    secret = b"sensitive-test-value"
    (workspace / "one").write_bytes(b"a" * (128 * 1024 - 4) + secret + b"tail")
    with pytest.raises(ExportError, match="protected credential") as error:
        export_workspace(workspace, destination, forbidden_values=(secret,))
    assert secret.decode() not in str(error.value)
    assert_unpublished(destination)


def test_existing_destination_is_preserved(roots):
    workspace, destination = roots
    destination.mkdir()
    (destination / "valuable").write_text("keep")
    with pytest.raises(ExportError, match="already exists"):
        export_workspace(workspace, destination)
    assert (destination / "valuable").read_text() == "keep"


def test_export_inside_source_is_rejected(roots):
    workspace, _ = roots
    with pytest.raises(ExportError, match="inside workspace"):
        export_workspace(workspace, workspace / "result")
    assert list(workspace.iterdir()) == []


def test_swap_file_to_symlink_between_stat_and_open_is_rejected(roots, monkeypatch):
    workspace, destination = roots
    victim = workspace / "file"
    victim.write_text("public")
    secret = workspace.parent / "secret"
    secret.write_text("SECRET")
    original = os.open
    def swapping_open(path, flags, *args, **kwargs):
        if path == "file" and kwargs.get("dir_fd") is not None:
            victim.unlink()
            victim.symlink_to(secret)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises(ExportError):
        export_workspace(workspace, destination)
    assert_unpublished(destination)


def test_unsafe_filename_rejected(roots):
    workspace, destination = roots
    (workspace / "bad\nname").touch()
    with pytest.raises(ExportError, match="unsafe"):
        export_workspace(workspace, destination)
    assert_unpublished(destination)


def test_secret_in_filename_is_not_exported(roots):
    workspace, destination = roots
    (workspace / "test-canary-secret.txt").touch()
    with pytest.raises(ExportError, match="protected credential"):
        export_workspace(workspace, destination, forbidden_values=(b"canary-secret",))
    assert_unpublished(destination)


def test_file_mutation_during_read_rejects_whole_export(roots, monkeypatch):
    workspace, destination = roots
    changing = workspace / "file"
    changing.write_bytes(b"first")
    original = os.read
    mutated = False
    def mutating_read(fd, count):
        nonlocal mutated
        data = original(fd, count)
        if data and not mutated:
            mutated = True
            changing.write_bytes(b"replaced content")
        return data
    monkeypatch.setattr(os, "read", mutating_read)
    with pytest.raises(ExportError, match="changed"):
        export_workspace(workspace, destination)
    assert_unpublished(destination)


def test_explicit_group_read_for_separate_api_identity(tmp_path):
    import stat
    source=tmp_path/'source';source.mkdir();(source/'result.txt').write_text('result')
    output=tmp_path/'published'
    export_workspace(source,output,shared_group=True)
    assert stat.S_IMODE(output.stat().st_mode)==0o550
    assert stat.S_IMODE((output/'result.txt').stat().st_mode)==0o440


def test_dependency_policy_exports_deliverables_and_reports_skipped_trees(roots):
    import json
    from cloudworkbench.artifacts import DEPENDENCY_EXCLUSIONS
    workspace, destination = roots
    (workspace / '.venv' / 'bin').mkdir(parents=True)
    (workspace / '.venv' / 'bin' / 'python').symlink_to('/not/a/deliverable/python')
    (workspace / 'node_modules' / '.bin').mkdir(parents=True)
    (workspace / 'node_modules' / '.bin' / 'tool').symlink_to('../tool/bin.js')
    (workspace / '.git').mkdir()
    (workspace / '.git' / 'config').write_text('private build metadata')
    (workspace / 'src').mkdir()
    (workspace / 'src' / 'answer.py').write_text('print(42)')
    for name in ('dist', 'build', 'target'):
        (workspace / name).mkdir()
        (workspace / name / 'deliverable.txt').write_text('keep this build output')
    report = {}
    exported = export_workspace(workspace, destination, excluded_names=DEPENDENCY_EXCLUSIONS, export_report=report)
    assert {item['path'] for item in exported} == {'src/answer.py', 'dist/deliverable.txt', 'build/deliverable.txt', 'target/deliverable.txt'}
    assert report['scope'] == 'selected_workspace_files'
    assert report['complete_workspace'] is False
    assert report['omitted_count'] == 3
    assert report['omitted_by_name'] == {'.git': 1, '.venv': 1, 'node_modules': 1}
    assert {entry['path'] for entry in report['omissions']} == {'.git', '.venv', 'node_modules'}
    assert report['omissions_truncated'] is False
    assert report['exported_file_count'] == 4
    assert len(json.dumps(report).encode()) < 65536
    assert not (destination / '.venv').exists()


def test_actual_python_venv_does_not_block_deliverable_export(roots):
    import subprocess
    import sys
    from cloudworkbench.artifacts import DEPENDENCY_EXCLUSIONS
    workspace, destination = roots
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(workspace / '.venv')], check=True, capture_output=True)
    (workspace / 'result.txt').write_text('a verified deliverable')
    report = {}
    result = export_workspace(workspace, destination, excluded_names=DEPENDENCY_EXCLUSIONS, export_report=report)
    assert [item['path'] for item in result] == ['result.txt']
    assert report['omitted_by_name'] == {'.venv': 1}


def test_strict_default_still_rejects_dependency_links(roots):
    workspace, destination = roots
    (workspace / '.venv').mkdir()
    (workspace / '.venv' / 'python').symlink_to('/outside')
    with pytest.raises(ExportError, match='links'):
        export_workspace(workspace, destination)
    assert_unpublished(destination)


@pytest.mark.parametrize('excluded', [('node_modules',), ('../outside',), ('*',), ('',), ('a/b',), ('.',), ('foo\\bar',), ('line\nname',), ('x' * 81,), 'node_modules'])
def test_exclusions_require_valid_explicit_policy_and_report(roots, excluded):
    workspace, destination = roots
    with pytest.raises(ExportError):
        export_workspace(workspace, destination, excluded_names=excluded)
    assert_unpublished(destination)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo'])
def test_dependency_exclusions_do_not_relax_deliverable_validation(roots, kind):
    from cloudworkbench.artifacts import DEPENDENCY_EXCLUSIONS
    workspace, destination = roots
    (workspace / 'node_modules').mkdir()
    (workspace / 'node_modules' / 'allowed-to-skip').symlink_to('/outside')
    private = workspace.parent / 'outside'
    private.write_text('PRIVATE')
    victim = workspace / 'output'
    if kind == 'symlink':
        victim.symlink_to(private)
    elif kind == 'hardlink':
        os.link(private, victim)
    else:
        os.mkfifo(victim)
    report = {'previous': 'preserved on failure'}
    with pytest.raises(ExportError, match='links and special'):
        export_workspace(workspace, destination, excluded_names=DEPENDENCY_EXCLUSIONS, export_report=report)
    assert report == {'previous': 'preserved on failure'}
    assert_unpublished(destination)


def test_excluded_symlink_is_not_followed_and_is_reported(roots, monkeypatch):
    from cloudworkbench.artifacts import DEPENDENCY_EXCLUSIONS
    workspace, destination = roots
    outside = workspace.parent / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('not read')
    (workspace / 'node_modules').symlink_to(outside, target_is_directory=True)
    (workspace / 'result').write_text('read this')
    original = os.open
    def guarded_open(path, flags, *args, **kwargs):
        assert path != 'node_modules'
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', guarded_open)
    report = {}
    exported = export_workspace(workspace, destination, excluded_names=DEPENDENCY_EXCLUSIONS, export_report=report)
    assert [item['path'] for item in exported] == ['result']
    assert report['omissions'] == [{'path': 'node_modules', 'type': 'symlink', 'reason': 'configured_exclusion'}]


def test_worker_ignore_files_do_not_control_export(roots):
    from cloudworkbench.artifacts import DEPENDENCY_EXCLUSIONS
    workspace, destination = roots
    (workspace / '.gitignore').write_text('important.txt\n')
    (workspace / 'important.txt').write_text('still deliver')
    result = export_workspace(workspace, destination, excluded_names=DEPENDENCY_EXCLUSIONS, export_report={})
    assert {item['path'] for item in result} == {'.gitignore', 'important.txt'}


def test_many_nested_exclusions_report_aggregate_and_truncation(roots):
    import json
    from cloudworkbench.artifacts import DEPENDENCY_EXCLUSIONS
    workspace, destination = roots
    for number in range(300):
        path = workspace / ('package-' + str(number)) / 'node_modules'
        path.mkdir(parents=True)
        (path / 'unread').symlink_to('/outside')
    report = {}
    exported = export_workspace(workspace, destination, excluded_names=DEPENDENCY_EXCLUSIONS, export_report=report)
    assert exported == []
    assert report['omitted_count'] == 300
    assert report['omitted_by_name'] == {'node_modules': 300}
    assert report['omissions_truncated'] is True
    assert len(report['omissions']) <= 128
    assert len(json.dumps(report).encode()) < 65536


def test_excluded_path_secret_checks_run_before_report_population(roots):
    from cloudworkbench.artifacts import DEPENDENCY_EXCLUSIONS
    workspace, destination = roots
    (workspace / 'known-secret' / 'node_modules').mkdir(parents=True)
    report = {}
    with pytest.raises(ExportError, match='protected credential') as exc:
        export_workspace(workspace, destination, excluded_names=DEPENDENCY_EXCLUSIONS, export_report=report, forbidden_values=(b'known-secret',))
    assert report == {}
    assert 'known-secret' not in str(exc.value)
    assert_unpublished(destination)


def test_explicit_policy_cannot_leak_secret_in_report(roots):
    workspace, destination = roots
    with pytest.raises(ExportError, match='protected credential'):
        export_workspace(workspace, destination, excluded_names=('secret-policy',), export_report={}, forbidden_values=(b'secret-policy',))
    assert_unpublished(destination)


@pytest.mark.parametrize('policy', [('../outside',), ('*',), ('',), ('a/b',), ('.',), ('foo\\bar',), ('line\nname',), ('x' * 81,), 'node_modules'])
def test_invalid_policy_rejected_even_with_report_sink(roots, policy):
    workspace, destination = roots
    with pytest.raises(ExportError, match='exclusion policy'):
        export_workspace(workspace, destination, excluded_names=policy, export_report={})
    assert_unpublished(destination)


def test_exclusion_policy_keeps_included_file_secret_checks(roots):
    from cloudworkbench.artifacts import DEPENDENCY_EXCLUSIONS
    workspace, destination = roots
    (workspace / '.venv').mkdir()
    (workspace / '.venv' / 'ignored').write_bytes(b'known-secret')
    (workspace / 'deliverable').write_bytes(b'known-secret')
    with pytest.raises(ExportError, match='protected credential'):
        export_workspace(workspace, destination, excluded_names=DEPENDENCY_EXCLUSIONS, export_report={}, forbidden_values=(b'known-secret',))
    assert_unpublished(destination)


def test_export_adoption_validates_hashes_and_rejects_mutation(tmp_path):
    from cloudworkbench.artifacts import adopt_export
    source = tmp_path/'source'; source.mkdir()
    (source/'x.txt').write_text('original')
    target = tmp_path/'export'
    receipt = tmp_path/'receipt.json'
    records = export_workspace(source,target,receipt_path=receipt)
    adopted, report = adopt_export(target,receipt)
    assert adopted == records and report['exported_file_count'] == 1
    (target/'x.txt').chmod(0o600)
    (target/'x.txt').write_text('modified')
    with pytest.raises(ExportError, match='changed'):
        adopt_export(target,receipt)


def test_export_error_diagnostic_has_specific_reason():
    from cloudworkbench.runner import Runner
    assert Runner.error_diagnostic(ExportError('artifact byte limit exceeded')) == {'code':'artifact_byte_limit','operation':'artifact_export'}


def test_export_receipt_is_bound_to_attempt_generation(tmp_path):
    from cloudworkbench.artifacts import adopt_export
    source=tmp_path/'source';source.mkdir();(source/'result').write_text('old generation')
    destination=tmp_path/'destination';receipt=tmp_path/'receipt.json'
    context={'id':'attempt','generation':1,'session_id':'session'}
    export_workspace(source,destination,receipt_path=receipt,receipt_context=context)
    assert adopt_export(destination,receipt,expected_context=context)[0]
    with pytest.raises(ExportError,match='generation or identity'):
        adopt_export(destination,receipt,expected_context={**context,'generation':2})


def test_export_records_source_executable_without_executable_publication(tmp_path):
    from cloudworkbench.artifacts import export_workspace
    source=tmp_path/'source';source.mkdir()
    script=source/'run.sh';script.write_text('#!/bin/sh\n');script.chmod(0o750)
    records=export_workspace(source,tmp_path/'output')
    assert records[0]['executable'] is True
    assert not (tmp_path/'output/run.sh').stat().st_mode & 0o111
