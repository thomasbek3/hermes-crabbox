"""Import the exact copied packages, without falling back to the full checkout."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ('qualify-native-supervised-linux.py', 'qualify-supervised-provider-linux.py',
           'qualify-real-provider-guarded.py')


def modules(script):
    tree = ast.parse((ROOT / 'scripts' / script).read_text())
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == 'MODULES' for target in node.targets))


def isolated_import(folder, names):
    program = '''import importlib,json,pathlib,socket,subprocess,sys
root=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root))
def denied(*args,**kwargs): raise AssertionError('import attempted external operation')
socket.socket.connect=denied
socket.socket.connect_ex=denied
subprocess.Popen=denied
for name in json.loads(sys.argv[2]):
    importlib.import_module('cloudworkbench' if name=='__init__.py' else 'cloudworkbench.'+name[:-3])
loaded={name:str(pathlib.Path(module.__file__).resolve()) for name,module in sys.modules.items()
        if name=='cloudworkbench' or name.startswith('cloudworkbench.')}
assert all(pathlib.Path(path).is_relative_to(root) for path in loaded.values()),loaded
print(json.dumps(sorted(loaded)))
'''
    env = {'HOME': str(folder), 'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}
    return subprocess.run([sys.executable, '-I', '-c', program, str(folder), json.dumps(names)],
                          cwd=folder, env=env, capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize('script', SCRIPTS)
def test_qualification_snapshot_imports_without_checkout(tmp_path, script):
    names = modules(script)
    assert len(names) == len(set(names))
    assert {'provider_protocol.py', 'native_responses.py', 'provider_recovery.py'} <= set(names)
    package = tmp_path / 'cloudworkbench'
    package.mkdir()
    for name in names:
        (package / name).write_bytes((ROOT / 'src/cloudworkbench' / name).read_bytes())
    result = isolated_import(tmp_path, names)
    assert result.returncode == 0, result.stderr
    assert 'cloudworkbench.provider_protocol' in json.loads(result.stdout)


def test_missing_native_dependency_is_detected(tmp_path):
    names = modules(SCRIPTS[0])
    package = tmp_path / 'cloudworkbench'
    package.mkdir()
    for name in names:
        if name != 'native_responses.py':
            (package / name).write_bytes((ROOT / 'src/cloudworkbench' / name).read_bytes())
    result = isolated_import(tmp_path, ['provider_executor.py'])
    assert result.returncode != 0
    assert "No module named 'cloudworkbench.native_responses'" in result.stderr


def test_real_qualification_example_requires_exact_new_source_set():
    example = json.loads((ROOT / 'deploy/real-provider-qualification.example.json').read_text())
    assert example['controller_source_sha256'] == dict.fromkeys(modules(SCRIPTS[2]))
    assert example['authorization'] is None


def test_provider_context_manifest_and_isolated_import(tmp_path):
    path = ROOT / 'scripts/prepare-provider-image-context.py'
    spec = importlib.util.spec_from_file_location('prepare_provider_context', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / 'context'
    manifest = module.prepare(destination)
    builder = ast.parse((ROOT / 'scripts/build-provider-candidate.py').read_text())
    expected = next(ast.literal_eval(node.value) for node in builder.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == 'MODULES' for target in node.targets))
    assert set(manifest) == expected == set(module.MODULES)
    assert json.loads((destination / 'source-manifest.json').read_text()) == manifest
    assert (destination / 'Dockerfile').read_bytes() == (ROOT / 'deploy/Dockerfile.provider').read_bytes()
    assert {str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()} == {
        'Dockerfile', 'source-manifest.json', *('cloudworkbench/' + name for name in manifest)}
    for name, digest in manifest.items():
        assert hashlib.sha256((destination / 'cloudworkbench' / name).read_bytes()).hexdigest() == digest
    result = isolated_import(destination, list(manifest))
    assert result.returncode == 0, result.stderr
    assert 'cloudworkbench.provider_main' in json.loads(result.stdout)
    with pytest.raises(FileExistsError):
        module.prepare(destination)
