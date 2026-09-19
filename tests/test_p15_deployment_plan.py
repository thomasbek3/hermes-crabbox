import copy
import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('p15_deploy',Path(__file__).parents[1]/'scripts/deploy-p15-api-checkpoint.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)

def setup(tmp_path):
    names=set(deploy.PUBLISH)|{'src/cloudworkbench/runner.py'}
    hashes={}
    for name in names:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('source');hashes[name]=deploy.sha(path)
    manifest={'files':hashes,'p15_api_overlay':{'publish_files':sorted(deploy.PUBLISH)}}
    base={'passed':True,'host':'omarchy','deployed_source_sha256':hashes.copy()}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest));return manifest,base

def test_exact_host_only_publication_scope(tmp_path):
    manifest,base=setup(tmp_path)
    assert deploy.validate(tmp_path,base)==manifest
    manifest['p15_api_overlay']['publish_files'].append('src/cloudworkbench/runner.py')
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='scope'):deploy.validate(tmp_path,base)

def test_unrelated_source_or_unbound_file_blocks(tmp_path):
    manifest,base=setup(tmp_path)
    base['deployed_source_sha256']['src/cloudworkbench/runner.py']='changed'
    with pytest.raises(ValueError,match='Unrelated'):deploy.validate(tmp_path,base)
    (tmp_path/'src/cloudworkbench/api.py').write_text('tampered')
    with pytest.raises(ValueError,match='snapshot changed'):deploy.validate(tmp_path,base)

def test_snapshot_symlink_and_traversal_refused(tmp_path):
    manifest,base=setup(tmp_path)
    path=tmp_path/'src/cloudworkbench/api.py';path.unlink();path.symlink_to(tmp_path/'src/cloudworkbench/cli.py')
    with pytest.raises(ValueError,match='snapshot changed'):deploy.validate(tmp_path,base)
    manifest['files']={'../escape':'invalid'};(tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='Invalid source'):deploy.validate(tmp_path,base)
