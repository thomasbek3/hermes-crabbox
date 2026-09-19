"""Exercise installation and HTTP diagnostics with temporary profiles and a local server."""
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'integrations/omarchy-mcp/skill/scripts'


def install(tmp_path, server, *extra):
    return subprocess.run([sys.executable, str(ROOT / 'scripts/install-delegation-skill.py'),
                           '--skills-dir', str(tmp_path), '--server', server, '--json', *extra],
                          capture_output=True, text=True)


def test_configured_install_uses_selected_host_and_preserves_changes(tmp_path):
    args = ['--project', 'my-project', '--environment-version', 'my-worker-v1']
    first = install(tmp_path, 'https://worker.example.ts.net', *args)
    assert first.returncode == 0, first.stderr
    receipt = json.loads(first.stdout)
    skill = Path(receipt['directory'])
    settings = json.loads((skill / 'scripts/connection.json').read_text())
    assert settings == {'server': 'https://worker.example.ts.net', 'project': 'my-project', 'environment': 'my-worker-v1'}
    assert json.loads((skill / 'cursor.mcp.json').read_text())['mcpServers']['hermes-crabbox']['url'] == settings['server'] + '/mcp'
    assert json.loads(install(tmp_path, settings['server'], *args).stdout)['status'] == 'unchanged'
    (skill / 'CONNECTION.md').write_text('My edits')
    assert install(tmp_path, settings['server'], *args).returncode == 1
    assert (skill / 'CONNECTION.md').read_text() == 'My edits'


@pytest.mark.parametrize('server', ['http://worker.example.com', 'https://secret@worker.example.com',
                                    'https://worker.example.com/mcp', 'https://worker.example.com?token=secret'])
def test_invalid_origins_create_nothing_and_do_not_echo_credentials(tmp_path, server):
    result = install(tmp_path, server)
    assert result.returncode == 1
    assert 'secret' not in result.stdout + result.stderr
    assert not (tmp_path / 'omarchy-cloud-delegate').exists()


def test_installed_clients_share_configuration_and_environment_override(tmp_path, monkeypatch):
    assert install(tmp_path, 'https://worker.example.ts.net').returncode == 0
    file = tmp_path / 'omarchy-cloud-delegate/scripts/omarchy_cloud.py'
    spec = importlib.util.spec_from_file_location('configured_caller', file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in ('SERVER', 'PROJECT', 'ENVIRONMENT'):
        monkeypatch.delenv('OMARCHY_CLOUD_' + key, raising=False)
    assert module.parser().parse_args(['list']).server == 'https://worker.example.ts.net'
    monkeypatch.setenv('OMARCHY_CLOUD_SERVER', 'https://override.example.ts.net')
    assert module.connection_defaults()['server'] == 'https://override.example.ts.net'
    file.with_name('connection.json').write_text('{invalid')
    with pytest.raises(module.ClientError):
        module.connection_defaults()


@pytest.mark.parametrize('status,payload,expected', [
    (200, {'sessions': [{'private': 'never print this'}]}, 'http_read_access'),
    (200, {'not_the_task_api': True}, 'response'),
    (401, {'message': 'secret'}, 'access'),
    (403, {'message': 'secret'}, 'access'),
    (302, {}, 'redirect'),
])
def test_connection_check_is_read_only_and_redacts(tmp_path, monkeypatch, status, payload, expected):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.command, self.path, self.headers.get('Authorization')))
            self.send_response(status)
            if status == 302:
                self.send_header('Location', '/must-not-follow')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('OMARCHY_CLOUD_TOKEN', 'secret')
    try:
        result = subprocess.run([sys.executable, str(SCRIPTS / 'check_connection.py'), '--server',
                                 f'http://127.0.0.1:{server.server_port}'], capture_output=True, text=True)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    report = json.loads(result.stdout)
    assert report['code'] == expected
    assert result.returncode == (0 if status == 200 and expected == 'http_read_access' else 1)
    assert seen == [('GET', '/v1/sessions?limit=1&offset=0', 'Bearer secret')]
    assert 'secret' not in result.stdout + result.stderr
    assert 'never print this' not in result.stdout + result.stderr


@pytest.mark.parametrize('agent,relative', [
    ('hermes', '.hermes/skills'), ('codex', '.agents/skills'),
    ('claude', '.claude/skills'), ('cursor', '.cursor/skills'),
])
def test_named_agent_installs_into_temporary_home(tmp_path, monkeypatch, capsys, agent, relative):
    spec = importlib.util.spec_from_file_location('onboarding_installer', ROOT / 'scripts/install-delegation-skill.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(sys, 'argv', ['install', '--agent', agent, '--server', 'https://worker.example.ts.net', '--json'])
    assert module.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert Path(receipt['directory']) == tmp_path / relative / 'omarchy-cloud-delegate'
    assert (Path(receipt['directory']) / 'CONNECTION.md').is_file()
