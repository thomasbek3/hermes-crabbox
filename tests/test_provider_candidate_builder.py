import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / ('scripts/legacy' if name == 'build-provider-candidate.py' else 'scripts') / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    builder = script('build-provider-candidate.py')
    context = tmp_path / 'context'
    script('prepare-provider-image-context.py').prepare(context)
    calls = []

    def check(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == 'hostname':
            return 'archived-worker.invalid\n'
        if '--format' in argv[-1]:
            return builder.BASE
        meta = {'RootFS': {'Layers': ['base']}, 'Id': 'sha256:candidate',
                'Config': {'Entrypoint': ['python'], 'User': '958:959'}}
        return json.dumps([meta])

    def run(argv, **kwargs):
        calls.append(argv)
        assert '--network none' in argv[-1]
        with tarfile.open(fileobj=io.BytesIO(kwargs['input']), mode='r:gz') as archive:
            assert set(archive.getnames()) == {
                'Dockerfile', 'source-manifest.json', *('cloudworkbench/' + n for n in builder.MODULES)}
        return subprocess.CompletedProcess(argv, 0, b'built\n', b'')

    monkeypatch.setattr(builder.subprocess, 'check_output', check)
    monkeypatch.setattr(builder.subprocess, 'run', run)
    return builder, context, calls


def test_build_explicit_fresh_context_and_outputs(candidate, tmp_path):
    builder, context, calls = candidate
    receipt, log = tmp_path / 'new.json', tmp_path / 'new.log'
    result = builder.build(context, receipt, log)
    assert result == json.loads(receipt.read_text())
    assert result['context'] == str(context)
    assert not result['activated'] and not result['provider_calls']
    assert log.read_text() == 'built\n'
    assert calls


@pytest.mark.parametrize('existing', ['receipt', 'log'])
def test_existing_evidence_refused_before_remote(candidate, tmp_path, existing):
    builder, context, calls = candidate
    receipt, log = tmp_path / 'new.json', tmp_path / 'new.log'
    target = receipt if existing == 'receipt' else log
    target.write_text('historical')
    with pytest.raises(FileExistsError):
        builder.build(context, receipt, log)
    assert target.read_text() == 'historical'
    assert not calls
    assert not (log if existing == 'receipt' else receipt).exists()


def test_stale_context_refused_before_remote(candidate, tmp_path):
    builder, context, calls = candidate
    (context / 'cloudworkbench/native_responses.py').write_text('stale')
    with pytest.raises(ValueError, match='drift'):
        builder.build(context, tmp_path / 'new.json', tmp_path / 'new.log')
    assert not calls


def test_failed_build_has_failure_receipt(candidate, tmp_path, monkeypatch):
    builder, context, calls = candidate
    monkeypatch.setattr(builder.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 1, b'', b'failed'))
    receipt, log = tmp_path / 'new.json', tmp_path / 'new.log'
    with pytest.raises(RuntimeError, match='build failed'):
        builder.build(context, receipt, log)
    assert json.loads(receipt.read_text())['status'] == 'failed'
    assert log.read_text() == 'failed'


def test_shared_output_path_refused(candidate, tmp_path):
    builder, context, calls = candidate
    with pytest.raises(ValueError, match='distinct'):
        builder.build(context, tmp_path / 'same', tmp_path / 'same')
    assert not calls


def test_timeout_retains_partial_output_and_candidate_identity(candidate, tmp_path, monkeypatch):
    builder, context, calls = candidate

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 120, output=b'partial build\n', stderr=b'partial error\n')

    monkeypatch.setattr(builder.subprocess, 'run', timeout)
    receipt, log = tmp_path / 'timeout.json', tmp_path / 'timeout.log'
    with pytest.raises(subprocess.TimeoutExpired):
        builder.build(context, receipt, log)
    result = json.loads(receipt.read_text())
    assert result['tag'].startswith('cwb-provider-candidate:')
    assert result['remote_build_outcome'] == 'unknown'
    assert len(result['context_archive_sha256']) == 64
    assert set(result['sources']) == builder.MODULES
    assert log.read_bytes() == b'partial build\npartial error\n'


def test_remote_inspection_is_bounded(candidate, tmp_path, monkeypatch):
    builder, context, calls = candidate

    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 30
        raise subprocess.TimeoutExpired(args[0], 30)

    monkeypatch.setattr(builder.subprocess, 'check_output', timeout)
    receipt = tmp_path / 'inspect.json'
    with pytest.raises(subprocess.TimeoutExpired):
        builder.build(context, receipt, tmp_path / 'inspect.log')
    result = json.loads(receipt.read_text())
    assert result['remote_build_outcome'] == 'not_started'
    assert result['tag'] is None
