"""Archived operational scripts cannot be mistaken for supported installers."""
from pathlib import Path
import subprocess
import sys

import pytest

ARCHIVE = Path(__file__).resolve().parents[1] / 'scripts/legacy'


@pytest.mark.parametrize('script', sorted(p.name for p in ARCHIVE.iterdir() if p.name.endswith(('.py', '.py.txt'))))
def test_archived_operation_refuses_before_imports_or_side_effects(tmp_path, script):
    # An empty cwd and environment prevent imports from the repository; the
    # refusal must happen before any dependency, credential or host operation.
    result = subprocess.run([sys.executable, '-I', str(ARCHIVE / script)],
                            cwd=tmp_path, env={}, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert 'Archived' in result.stderr and 'disabled' in result.stderr
    assert not result.stdout
    assert list(tmp_path.iterdir()) == []
