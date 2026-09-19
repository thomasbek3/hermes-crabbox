import importlib.util
import io
import json
from pathlib import Path
import zipfile
import pytest
from cloudworkbench.result_bundle import build_bundle

spec = importlib.util.spec_from_file_location('bundle_qualifier', Path(__file__).parents[1]/'scripts/qualify-result-bundle.py')
qualifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualifier)


def test_qualifier_checks_actual_bundle_and_wrong_attempt(tmp_path):
    session = {'id': 's', 'attempts': [{'id': 'a', 'generation': 1, 'state': 'cancelled'}]}
    blob = build_bundle(session, [], tmp_path)
    receipt = qualifier.inspect_archive(blob, 's', 'a')
    assert receipt['entries'] == 4 and receipt['artifact_count'] == 0
    with pytest.raises(ValueError, match='identity'):
        qualifier.inspect_archive(blob, 's', 'wrong')


def test_qualifier_rejects_changed_manifest_binding(tmp_path):
    blob = build_bundle({'id': 's', 'attempts': [{'id': 'a', 'generation': 1, 'state': 'cancelled'}]}, [], tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as original, zipfile.ZipFile(output, 'w') as changed:
        for entry in original.infolist():
            data = original.read(entry.filename)
            if entry.filename == 'verification.json':
                value = json.loads(data); value['artifact_manifest_sha256'] = '0'*64
                data = json.dumps(value).encode()
            changed.writestr(entry, data)
    with pytest.raises(ValueError, match='manifest binding'):
        qualifier.inspect_archive(output.getvalue(), 's', 'a')
