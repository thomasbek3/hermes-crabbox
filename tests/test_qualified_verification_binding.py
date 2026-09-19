"""Qualified provenance enforced at legacy composition entry points; no live runtime."""
from dataclasses import replace
import copy
import hashlib
import json

import pytest

from cloudworkbench.environments import EnvironmentRegistry
from cloudworkbench.workflow_submission import enqueue_qualified_workflow
from cloudworkbench import routed_verification as verification
from tests.test_environments import passed
from tests import test_routed_stage as stage_tests
from tests.test_routed_verification import (
    composed, result_phase, cleanup, driven, setup, prepare, execute, protected_artifacts,
)


@pytest.fixture
def stage(tmp_path,monkeypatch):
    script=b'print("protected check")\n'
    script_path=tmp_path.resolve()/'operator-check.py';script_path.write_bytes(script);script_path.chmod(0o600)
    manifest={'project_id':'demo','version':'v1','architecture':'amd64',
        'base_image_digest':'sha256:'+'a'*64,'image_digest':'sha256:'+'a'*64,
        'cli_versions':{'python':'3'},'readiness_probes':[{'id':'python','argv':['python3','--version']}],
        'checks':[{'id':'check','description':'Expected behavior',
            'argv':['/usr/bin/python3','/run/task/check.py'],'script_id':'protected',
            'script_name':'check.py','script_sha256':hashlib.sha256(script).hexdigest()}]}
    registry=EnvironmentRegistry(tmp_path/'qualified.db');registry.register(manifest);registry.qualify('demo','v1',passed)
    def qualified(scheduler,principal,request,key,**workflow):
        return enqueue_qualified_workflow(scheduler,principal,{**request,'environment_version':'v1'},key,
            registry=registry,allowed_versions=('v1',),script_sources={'protected':script_path},forbidden_values=(),**workflow)
    monkeypatch.setattr(stage_tests,'enqueue_workflow',qualified)
    return stage_tests.stage.__wrapped__(tmp_path)


def frozen(composed):
    d=composed.data
    return d['scheduler'].child_assignment(d['spec'].attempt_id,expected_generation=1)['workflow_snapshot']['provenance']['environment']


def load(composed,prepared):
    d=composed.data
    return verification.load_published_verification(d['scheduler'],d['runtime'],d['spec'],composed.results,
        prepared,composed.verifier,services=(d['service'],),forbidden_values=())


def test_historical_helper_exact_manifest_allowed_and_replays(composed):
    prepared=prepare(composed)
    assert prepared.plan.environment_sha256==frozen(composed)['manifest_sha256']
    result=execute(composed,prepared)
    assert result.outcome=='passed' and load(composed,prepared)==result
    assert len(composed.launched)==1 and len(protected_artifacts(composed))==1


@pytest.mark.parametrize('change',['empty','description','version','image','resources','script'])
def test_historical_prepare_cannot_replace_qualified_manifest_before_writes(composed,change):
    manifest=copy.deepcopy(composed.environment);scripts=dict(composed.scripts)
    if change=='empty':manifest['checks']=[];scripts={}
    elif change=='description':manifest['checks'][0]['description']='Trivial alternative'
    elif change=='version':manifest['version']='v2'
    elif change=='image':manifest['image_digest']='sha256:'+'b'*64
    elif change=='resources':manifest['resources']={'cpus':1}
    elif change=='script':
        scripts['protected']=b'print("always passes")\n'
        manifest['checks'][0]['script_sha256']=hashlib.sha256(scripts['protected']).hexdigest()
    with pytest.raises(verification.VerificationPhaseError,match='qualified_environment_changed'):
        prepare(composed,environment=manifest,scripts=scripts)
    assert not list(composed.storage.iterdir()) and not list(composed.material.iterdir())
    assert not composed.calls and not protected_artifacts(composed)


def test_preexisting_wrong_preparation_refused_before_run_but_after_cleanup(composed,monkeypatch):
    # Model an artifact prepared by the historical implementation before this fix.
    original=verification._qualified_environment_binding
    with monkeypatch.context() as old:
        old.setattr(verification,'_qualified_environment_binding',lambda *args,**kwargs:None)
        prepared=prepare(composed,environment={**composed.environment,'version':'v2'})
    assert verification._qualified_environment_binding is original
    with pytest.raises(verification.VerificationPhaseError,match='qualified_environment_changed'):
        execute(composed,prepared)
    assert [kind for kind,_ in composed.calls]==['reconcile']
    assert not composed.launched and not protected_artifacts(composed)


def test_preexisting_wrong_published_receipt_refused_on_load(composed,monkeypatch):
    with monkeypatch.context() as old:
        old.setattr(verification,'_qualified_environment_binding',lambda *args,**kwargs:None)
        prepared=prepare(composed,environment={**composed.environment,'version':'v2'})
        execute(composed,prepared)
    composed.calls.clear()
    with pytest.raises(verification.VerificationPhaseError,match='qualified_environment_changed'):
        load(composed,prepared)
    assert [kind for kind,_ in composed.calls]==['reconcile']
    assert len(composed.launched)==1


@pytest.mark.parametrize('change',['image','checks'])
def test_plan_cannot_lie_about_manifest_digest(composed,change):
    prepared=prepare(composed)
    d=composed.data
    with d['scheduler'].store._connect() as db:
        root=dict(db.execute('SELECT * FROM workflow_roots').fetchone())
    authority={'root':root,'child':d['scheduler'].store.get_attempt(d['spec'].attempt_id)}
    changed=replace(prepared.plan,**({'image':'sha256:'+'b'*64} if change=='image' else {'checks':()}))
    assert changed.environment_sha256==frozen(composed)['manifest_sha256']
    with pytest.raises(verification.VerificationPhaseError,match='qualified_environment_changed'):
        verification._qualified_environment_binding(authority,(),plan=changed)


def test_publication_revalidates_fresh_authoritative_environment(composed,monkeypatch):
    prepared=prepare(composed);original=verification._qualified_environment_binding;seen=[]
    def record(authority,secrets,**kwargs):
        seen.append(authority)
        return original(authority,secrets,**kwargs)
    monkeypatch.setattr(verification,'_qualified_environment_binding',record)
    execute(composed,prepared)
    assert len(seen)==2 and seen[0] is not seen[1]
    assert json.loads(seen[1]['root']['frozen'])['provenance']['environment']==frozen(composed)
