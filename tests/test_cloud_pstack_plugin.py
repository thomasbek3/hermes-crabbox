import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'integrations' / 'hermes-cloud-pstack'


@pytest.fixture
def plugin(tmp_path):
    spec = importlib.util.spec_from_file_location('cloud_pstack_under_test', PLUGIN / '__init__.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.WORKSPACE = tmp_path.resolve()
    return module


def read(plugin, path, **kwargs):
    return json.loads(plugin.cloud_read_workspace({'path': path, **kwargs}))


def test_registration_keeps_review_tools_separate(plugin):
    class Context:
        def __init__(self):
            self.tools, self.skills = {}, {}

        def register_tool(self, name, toolset, schema, handler):
            assert name not in self.tools
            self.tools[name] = toolset, schema, handler

        def register_skill(self, name, path):
            self.skills[name] = path

    context = Context()
    plugin.register(context)
    assert {name: item[0] for name, item in context.tools.items()} == {
        'cloud_route_task': 'cloud_pstack',
        'cloud_run_pstack_stage': 'cloud_pstack',
        'cloud_read_workspace': 'cloud_workspace_read',
    }
    for name, (_, schema, handler) in context.tools.items():
        assert schema['name'] == name
        assert schema['parameters']['additionalProperties'] is False
        assert callable(handler)
    assert context.skills['orchestrate'].is_file()


def test_wrappers_forward_only_allowed_arguments(plugin, monkeypatch):
    calls = []
    backend = types.ModuleType('cloudworkbench.agent_pstack')
    backend.route_task = lambda summary: calls.append(('route', summary)) or {'stages': ['s1']}
    backend.run_stage = lambda stage, instruction: calls.append(('stage', stage, instruction)) or {'status': 'blocked'}
    monkeypatch.setitem(sys.modules, 'cloudworkbench.agent_pstack', backend)
    assert json.loads(plugin.cloud_route_task({'summary': 'Build a page'})) == {'stages': ['s1']}
    assert json.loads(plugin.cloud_run_pstack_stage({'stage_id': 's1', 'instruction': 'Review page'})) == {'status': 'blocked'}
    assert calls == [('route', 'Build a page'), ('stage', 's1', 'Review page')]
    assert json.loads(plugin.cloud_route_task({'summary': 'Build', 'model': 'other'}))['error'] == 'invalid_arguments'
    assert json.loads(plugin.cloud_run_pstack_stage({'stage_id': '../x', 'instruction': 'Review'}))['error'] == 'invalid_arguments'
    assert len(calls) == 2


def test_backend_exception_does_not_expose_secrets(plugin, monkeypatch):
    backend = types.ModuleType('cloudworkbench.agent_pstack')

    def fail(*args):
        raise RuntimeError('private-token-value')

    backend.route_task = backend.run_stage = fail
    monkeypatch.setitem(sys.modules, 'cloudworkbench.agent_pstack', backend)
    for output in (plugin.cloud_route_task({'summary': 'x'}),
                   plugin.cloud_run_pstack_stage({'stage_id': 's1', 'instruction': 'x'})):
        assert json.loads(output) == {'ok': False, 'error': 'cloud_backend_unavailable'}


def test_bounded_pages_preserve_file(plugin):
    target = plugin.WORKSPACE / 'src' / 'module.py'
    target.parent.mkdir()
    target.write_text('hello world')
    before = target.stat()
    first = read(plugin, 'src/module.py', limit=5)
    second = read(plugin, 'src/module.py', offset=first['next_offset'])
    assert first['content'] == 'hello' and not first['eof']
    assert second['content'] == ' world' and second['eof']
    assert target.read_text() == 'hello world'
    assert target.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize('path', ['', '/etc/passwd', '../x', 'a/../x', './x', 'a//x', 'a/', 'a\\x', 'a\x00x'])
def test_rejects_path_escape(plugin, path):
    assert read(plugin, path) == {'ok': False, 'error': 'invalid_arguments'}


@pytest.mark.parametrize('kwargs', [{'offset': -1}, {'offset': True}, {'limit': 0},
                                   {'limit': 12001}, {'limit': True}, {'offset': 8 * 1024 * 1024 + 1},
                                   {'unexpected': 'value'}])
def test_rejects_unbounded_or_extra_arguments(plugin, kwargs):
    assert not read(plugin, 'file', **kwargs)['ok']


def test_symlinks_and_root_symlinks_are_not_followed(plugin, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (outside / 'secret').write_text('private text')
    (plugin.WORKSPACE / 'leaf').symlink_to(outside / 'secret')
    (plugin.WORKSPACE / 'dir').symlink_to(outside, target_is_directory=True)
    assert read(plugin, 'leaf')['error'] == 'workspace_file_unavailable'
    assert read(plugin, 'dir/secret')['error'] == 'workspace_file_unavailable'
    plugin.WORKSPACE = plugin.WORKSPACE / 'dir'
    assert read(plugin, 'secret')['error'] == 'workspace_file_unavailable'


def test_hardlinks_special_files_binary_and_missing_are_refused(plugin):
    target = plugin.WORKSPACE / 'original'
    target.write_text('content')
    os.link(target, plugin.WORKSPACE / 'hardlink')
    os.mkfifo(plugin.WORKSPACE / 'fifo')
    (plugin.WORKSPACE / 'directory').mkdir()
    (plugin.WORKSPACE / 'binary').write_bytes(b'a\x00b')
    for name in ('original', 'hardlink', 'fifo', 'directory', 'missing'):
        assert read(plugin, name)['error'] == 'workspace_file_unavailable'
    assert read(plugin, 'binary')['error'] == 'workspace_file_not_text'


def test_large_files_refused_and_default_output_bounded(plugin):
    target = plugin.WORKSPACE / 'file'
    target.write_bytes(b'x' * 13000)
    result = read(plugin, 'file')
    assert len(result['content']) == 12000 and not result['eof']
    with target.open('wb') as stream:
        stream.truncate(plugin.MAX_FILE_BYTES + 1)
    assert read(plugin, 'file')['error'] == 'workspace_file_too_large'


def test_directory_swap_does_not_redirect_file_read(plugin, monkeypatch):
    folder = plugin.WORKSPACE / 'directory'
    folder.mkdir()
    (folder / 'file').write_text('workspace')
    outside = plugin.WORKSPACE.parent / (plugin.WORKSPACE.name + '-race')
    outside.mkdir()
    (outside / 'file').write_text('outside')
    original_open = os.open

    def swapping_open(path, flags, **kwargs):
        fd = original_open(path, flags, **kwargs)
        if path == 'directory':
            folder.rename(plugin.WORKSPACE / 'old-directory')
            folder.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(plugin.os, 'open', swapping_open)
    assert read(plugin, 'directory/file')['content'] == 'workspace'


def test_actual_pinned_hermes_registration_when_runtime_available(tmp_path):
    import os
    if not os.environ.get('CWB_HERMES_SOURCE') or not os.environ.get('CWB_HERMES_DEPENDENCY_PYTHON'):
        pytest.skip('Set CWB_HERMES_SOURCE and CWB_HERMES_DEPENDENCY_PYTHON for the optional pinned runtime test')
    source = Path(os.environ['CWB_HERMES_SOURCE'])
    python = Path(os.environ['CWB_HERMES_DEPENDENCY_PYTHON'])
    if not source.is_dir() or not python.is_file():
        pytest.skip('Pinned source and local Hermes dependency runtime are not present')
    script = '''
import importlib.util, pathlib, socket, sys
def deny(*args, **kwargs):
    raise RuntimeError('network forbidden')
socket.socket.connect = deny
socket.socket.connect_ex = deny
from hermes_cli.plugins import PluginContext, PluginManager
import hermes_cli.plugins as hermes_plugins
from hermes_cli.plugins_manifest import parse_manifest_file
from tools.registry import registry
assert pathlib.Path(hermes_plugins.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[2]).resolve())
plugin = pathlib.Path(sys.argv[1])
manifest = parse_manifest_file(plugin / 'plugin.yaml', plugin, 'user', '')
manager = PluginManager()
context = PluginContext(manifest, manager)
spec = importlib.util.spec_from_file_location('cloud_pstack_isolated', plugin / '__init__.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.register(context)
for name, toolset in [('cloud_route_task','cloud_pstack'), ('cloud_run_pstack_stage','cloud_pstack'), ('cloud_read_workspace','cloud_workspace_read')]:
    entry = registry.get_entry(name, scope=manager.scope_key)
    assert entry is not None and entry.toolset == toolset
assert 'cloud-pstack:orchestrate' in manager._plugin_skills
print('actual_pinned_registration_passed')
'''
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path),
           'PYTHONPATH': str(source), 'HERMES_ENABLE_PROJECT_PLUGINS': '0',
           'HERMES_BUNDLED_PLUGINS': str(tmp_path / 'empty')}
    result = subprocess.run([str(python), '-c', script, str(PLUGIN), str(source)], env=env,
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'actual_pinned_registration_passed'
