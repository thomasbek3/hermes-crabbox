"""Install into disposable directories; never touch a real agent profile."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('skill_installer', ROOT / 'scripts/install-delegation-skill.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_installs_licensed_bundle_and_repeat_preserves_files(tmp_path):
    path, changed = installer.install(installer.SOURCE, tmp_path)
    assert changed and (path / 'LICENSE').read_bytes() == (ROOT / 'LICENSE').read_bytes()
    assert (path / 'scripts/omarchy_cloud.py').is_file()
    before = (path / 'SKILL.md').stat().st_mtime_ns
    assert installer.install(installer.SOURCE, tmp_path) == (path, False)
    assert (path / 'SKILL.md').stat().st_mtime_ns == before


def test_existing_user_edits_are_not_overwritten(tmp_path):
    path, _ = installer.install(installer.SOURCE, tmp_path)
    (path / 'SKILL.md').write_text('Local custom instructions')
    with pytest.raises(ValueError, match='existing skill differs'):
        installer.install(installer.SOURCE, tmp_path)
    assert (path / 'SKILL.md').read_text() == 'Local custom instructions'


def test_symlink_destination_refused(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / installer.NAME).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symbolic link'):
        installer.install(installer.SOURCE, tmp_path)
    assert not list(outside.iterdir())


def test_incomplete_source_creates_nothing(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    with pytest.raises(ValueError, match='incomplete'):
        installer.install(source, tmp_path / 'target')
    assert not (tmp_path / 'target').exists()
