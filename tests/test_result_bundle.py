import hashlib
import io
import json
import zipfile
import pytest
from cloudworkbench.result_bundle import build_bundle,BundleError


@pytest.fixture
def bundle(tmp_path):
    root=tmp_path/'artifacts';root.mkdir();path=root/'output.bin';path.write_bytes(b'\x00\xffbinary')
    session={'id':'s','bundle_lifecycle':{'running_at':'2026-01-01T00:00:00+00:00'},'attempts':[{'id':'a','generation':1,'state':'completed','outcome':'verified','agent':'claude',
       'started_at':'2026-01-01T00:00:00+00:00','updated_at':'2026-01-01T00:00:03+00:00',
       'result':{'summary':'Done','checks':[{'id':'test','passed':True}],
                 'environment':{'manifest':{'resources':{'cpus':1}}}}}]}
    artifacts=[{'id':'file','session_id':'s','attempt_id':'a','path':'output.bin','storage_path':str(path),
                'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'executable':False}]
    return session,artifacts,root


def test_bundle_roundtrip_hashes_timing_and_unknowns(bundle):
    blob=build_bundle(*bundle)
    assert build_bundle(*bundle)==blob
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert set(archive.namelist())=={'result.json','summary.md','verification.json','artifact-manifest.json','files/output.bin'}
        assert archive.read('files/output.bin')==b'\x00\xffbinary'
        data=json.loads(archive.read('result.json'))
        assert data['timing']['execution_and_verification']['seconds']==3
        assert data['resources']['measured_usage'] is None and data['resources']['reason']
        assert data['usage'] is None and data['usage_status']['state']=='unknown'
        assert str(bundle[2]).encode() not in archive.read('artifact-manifest.json')


def test_latest_attempt_does_not_return_old_result(bundle):
    session,artifacts,root=bundle
    session['attempts'].append({'id':'b','generation':2,'state':'running'})
    with pytest.raises(BundleError,match='not terminal'):build_bundle(*bundle)
    session['attempts'][-1].update(state='failed',reason='provider_rate_limited')
    blob=build_bundle(*bundle)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert 'files/output.bin' not in archive.namelist()
        assert json.loads(archive.read('result.json'))['unresolved_issues']


@pytest.mark.parametrize('change',[{'session_id':'foreign'},{'path':'../escape'},{'path':'/absolute'},
   {'sha256':'0'*64},{'bytes':100},{'path':'bad\\path'}])
def test_invalid_artifact_refuses_entire_bundle(bundle,change):
    bundle[1][0].update(change)
    with pytest.raises(BundleError):build_bundle(*bundle)


def test_links_and_escape_storage_rejected(bundle,tmp_path):
    item=bundle[1][0];source=tmp_path/'outside';source.write_bytes(b'\x00\xffbinary')
    item['storage_path']=str(source)
    with pytest.raises(BundleError):build_bundle(*bundle)
    link=bundle[2]/'link';link.symlink_to(source);item['storage_path']=str(link)
    with pytest.raises(BundleError):build_bundle(*bundle)


def test_budget_and_duplicate_fail_closed(bundle):
    with pytest.raises(BundleError,match='byte limit'):build_bundle(*bundle,max_bytes=1)
    with pytest.raises(BundleError,match='file limit'):build_bundle(*bundle,max_files=0)
    bundle[1].append(dict(bundle[1][0]))
    with pytest.raises(BundleError,match='Duplicate'):build_bundle(*bundle)


def test_partial_patch_and_invalid_time_are_explicit(bundle):
    attempt=bundle[0]['attempts'][0]
    attempt.update(started_at='bad',outcome='unverified')
    bundle[0]['bundle_lifecycle']['running_at']='bad'
    attempt['result']['delivery']={'complete_text_patch':False}
    with zipfile.ZipFile(io.BytesIO(build_bundle(*bundle))) as archive:
        result=json.loads(archive.read('result.json'))
        assert result['timing']['execution_and_verification']=={'seconds':None,'reason':'invalid_timestamp'}
        assert len(result['unresolved_issues'])==2
        assert b'Outcome: unverified' in archive.read('summary.md')


def result_of(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return json.loads(archive.read('result.json'))


def test_reported_usage_cost_and_exact_requested_resolved_provenance(bundle):
    session, artifacts, root = bundle
    attempt = session['attempts'][0]
    attempt['request'] = {'model': 'requested-alias', 'environment_version': 'v1'}
    attempt['result'].update(usage={'input_tokens': 4}, provider_result={'reported_cost_usd': 0.25})
    session['bundle_provenance'] = {'model': 'resolved-version', 'cli_version': '2.1.274', 'event_sequence': 6}
    value = result_of(build_bundle(*bundle))
    assert value['usage_status'] == {'state': 'reported', 'reason': None}
    assert value['cost']['value'] is None and value['cost']['provider_reported_usd'] == 0.25
    assert value['provider_provenance']['requested_model']['value'] == 'requested-alias'
    assert value['provider_provenance']['resolved_model']['value'] == 'resolved-version'
    assert value['provider_provenance']['cli_version']['value'] == '2.1.274'
    assert value['resources']['measured_usage'] is None


def test_missing_provenance_never_invents_exact_version_or_model(bundle):
    value = result_of(build_bundle(*bundle))
    for key in ('requested_model', 'resolved_model', 'cli_version', 'expected_cli_version'):
        assert value['provider_provenance'][key]['value'] is None
        assert value['provider_provenance'][key]['reason']


def test_zip_actual_size_limit_includes_headers_and_generated_files(bundle):
    blob = build_bundle(*bundle)
    assert build_bundle(*bundle, max_bytes=len(blob)) == blob
    with pytest.raises(BundleError, match='byte limit'):
        build_bundle(*bundle, max_bytes=len(blob)-1)
    with pytest.raises(BundleError, match='file limit'):
        build_bundle(*bundle, max_files=4)
    assert build_bundle(*bundle, max_files=5) == blob


@pytest.mark.parametrize('shape', [[], 42, 'invalid'])
def test_malformed_result_shapes_are_controlled(bundle, shape):
    bundle[0]['attempts'][0]['result'] = shape
    with pytest.raises(BundleError, match='metadata'):
        build_bundle(*bundle)


def test_authoritative_outcome_overrides_result_claims(bundle):
    attempt = bundle[0]['attempts'][0]
    attempt.update(outcome='rejected', state='completed')
    attempt['result'].update(outcome='verified', state='completed', remaining_criteria=['manual-review'])
    value = result_of(build_bundle(*bundle))
    assert value['outcome'] == 'rejected' and value['remaining_criteria'] == ['manual-review']
    assert len(value['unresolved_issues']) >= 2


def test_hardlinks_and_special_files_fail_without_partial_output(bundle):
    import os
    session, artifacts, root = bundle
    path = root / 'output.bin'
    alias = root / 'alias'
    os.link(path, alias)
    with pytest.raises(BundleError, match='integrity'):
        build_bundle(*bundle)
    alias.unlink()
    path.unlink()
    os.mkfifo(path)
    artifacts[0]['bytes'] = 0
    with pytest.raises(BundleError, match='integrity'):
        build_bundle(*bundle)


def test_no_exec_bits_private_metadata_or_path_conflicts(bundle):
    session, artifacts, root = bundle
    session['attempts'][0]['result']['environment']['manifest']['secret_refs'] = ['DO_NOT_INCLUDE_REF']
    session['attempts'][0]['result']['environment']['credential_fingerprint'] = 'DO_NOT_INCLUDE_FP'
    with zipfile.ZipFile(io.BytesIO(build_bundle(*bundle))) as archive:
        assert all((item.external_attr >> 16) & 0o777 == 0o600 for item in archive.infolist())
        assert b'DO_NOT_INCLUDE' not in archive.read('result.json')
    artifacts.append({**artifacts[0], 'id': 'nested', 'path': 'output.bin/child'})
    with pytest.raises(BundleError, match='name conflict'):
        build_bundle(*bundle)


def test_patch_is_included_as_registered_bytes_not_reconstructed(bundle):
    session, artifacts, root = bundle
    patch = root / 'patch'; patch.write_bytes(b'diff --git a/a b/a\n')
    artifacts.append({'id': 'patch', 'session_id': 's', 'attempt_id': 'a', 'path': '@delivery/changes.patch', 'storage_path': str(patch), 'bytes': patch.stat().st_size, 'sha256': hashlib.sha256(patch.read_bytes()).hexdigest()})
    with zipfile.ZipFile(io.BytesIO(build_bundle(*bundle))) as archive:
        assert archive.read('files/@delivery/changes.patch') == patch.read_bytes()
        receipt = json.loads(archive.read('verification.json'))
        assert receipt['artifact_manifest_sha256'] == hashlib.sha256(archive.read('artifact-manifest.json')).hexdigest()


def test_preparation_time_is_not_mislabeled_as_agent_execution(bundle):
    session, artifacts, root = bundle
    attempt = session['attempts'][0]
    attempt.update(created_at='2026-01-01T00:00:00Z', started_at='2026-01-01T00:00:10Z', updated_at='2026-01-01T00:01:00Z')
    session['bundle_lifecycle'] = {'running_at': '2026-01-01T00:00:30Z'}
    attempt['result']['verification_started_at'] = '2026-01-01T00:00:50Z'
    timing = result_of(build_bundle(*bundle))['timing']
    assert timing['queue']['seconds'] == 10
    assert timing['preparation']['seconds'] == 20
    assert timing['execution']['seconds'] == 20
    assert timing['verification']['seconds'] == 10
    assert timing['execution_and_verification']['seconds'] == 30
    session['bundle_lifecycle'] = {}
    timing = result_of(build_bundle(*bundle))['timing']
    assert timing['execution']['seconds'] is None and timing['execution_and_verification']['seconds'] is None
    assert timing['created_to_terminal']['seconds'] == 60


def test_metadata_allowlists_exclude_urls_commands_private_fields_and_error_text(bundle):
    raw = bundle[0]['attempts'][0]['result']
    secret = 'BUNDLE_PRIVATE_SENTINEL'
    raw.update(repository={'repository_id': 'repo', 'url': 'https://token:'+secret+'@example/repo'},
               artifact_error='cannot read /srv/artifacts/'+secret, verification_error=secret,
               delivery={'complete_text_patch': True, 'private': {'api_key': secret}},
               export_report={'scope': 'selected_workspace_files', 'private': secret})
    raw['environment'].update(env={'api_key': secret}, mounts=[secret])
    raw['environment']['manifest'].update(install_commands=[{'argv': [secret]}])
    raw['checks'][0].update(stdout=secret, stderr=secret, unexpected={'credential': secret})
    with zipfile.ZipFile(io.BytesIO(build_bundle(*bundle))) as archive:
        assert all(secret.encode() not in archive.read(name) for name in ('result.json', 'summary.md', 'verification.json'))
        assert json.loads(archive.read('result.json'))['repository'] == {'repository_id': 'repo'}


@pytest.mark.parametrize('names', [('A.txt', 'a.txt'), ('caf\u00e9.txt', 'cafe\u0301.txt'), ('A.txt', 'a.txt/child')])
def test_portable_name_collisions_fail_closed(bundle, names):
    artifacts = bundle[1]
    artifacts[0]['path'] = names[0]
    artifacts.append({**artifacts[0], 'id': 'other', 'path': names[1]})
    with pytest.raises(BundleError, match='name conflict'):
        build_bundle(*bundle)


def test_provenance_mismatch_visible_without_claiming_alias_is_wrong(bundle):
    session, artifacts, root = bundle
    session['attempts'][0]['request'] = {'model': 'alias'}
    session['attempts'][0]['result']['environment']['manifest']['cli_versions'] = {'claude': '2'}
    session['bundle_provenance'] = {'model': 'exact', 'cli_version': '1'}
    issues = result_of(build_bundle(*bundle))['unresolved_issues']
    assert any('alias resolution' in issue for issue in issues)
    assert any('CLI version differs' in issue for issue in issues)


def test_directory_and_root_symlinks_refused_relative_storage_supported(bundle, tmp_path):
    session, artifacts, root = bundle
    artifacts[0]['storage_path'] = 'output.bin'
    assert build_bundle(*bundle)
    (root/'link').symlink_to(root, target_is_directory=True)
    artifacts[0]['storage_path'] = 'link/output.bin'
    with pytest.raises(BundleError, match='integrity'):
        build_bundle(*bundle)
    artifacts[0]['storage_path'] = 'output.bin'
    alias = tmp_path/'root-alias'; alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(BundleError, match='integrity'):
        build_bundle(session, artifacts, alias)
