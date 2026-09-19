"""A protected decision can load evidence but cannot manufacture a missing check run."""
import json

import pytest

from tests.test_routed_verification import (composed, result_phase, cleanup, driven, stage, setup,
    prepare, execute, cancel, protected_artifacts)
from cloudworkbench import routed_verification as verification
from cloudworkbench.routed_publication import PublicationError


def load(value, prepared):
    data=value.data
    return verification.load_published_verification(data['scheduler'],data['runtime'],data['spec'],
        value.results,prepared,value.verifier,services=(data['service'],),forbidden_values=())


def test_unpublished_verification_cannot_be_started_by_loading(composed):
    prepared=prepare(composed)
    with pytest.raises(verification.VerificationPhaseError,match='verification_publication_missing'):
        load(composed,prepared)
    assert all(action=='reconcile' for action,_ in composed.calls)
    assert not composed.launched and protected_artifacts(composed)==[]
    assert not (prepared.storage/'phase-budget.json').exists()


def test_published_verification_load_is_exact_and_never_calls_run(composed,monkeypatch):
    prepared=prepare(composed);expected=execute(composed,prepared)
    before=protected_artifacts(composed);composed.calls.clear()
    monkeypatch.setattr(composed.verifier,'run',lambda *a,**k:pytest.fail('loader executed a check'))
    assert load(composed,prepared)==expected
    assert [action for action,_ in composed.calls]==['reconcile','load']
    assert protected_artifacts(composed)==before


def test_published_load_does_not_recreate_deleted_receipt(composed):
    prepared=prepare(composed);execute(composed,prepared)
    metadata,=protected_artifacts(composed)
    from pathlib import Path
    destination=Path(metadata['storage_path']);destination.unlink()
    with pytest.raises(FileNotFoundError):load(composed,prepared)
    assert not destination.exists()
    assert len(composed.launched)==1


def test_publication_removed_during_load_is_not_reinserted(composed):
    prepared=prepare(composed);published=execute(composed,prepared)
    def remove(_):
        with composed.data['scheduler'].store._tx() as db:
            db.execute('DELETE FROM artifacts WHERE id=?',(published.artifact_id,))
    composed.before_load=remove
    with pytest.raises(verification.VerificationPhaseError,match='verification_publication_missing'):
        load(composed,prepared)
    assert protected_artifacts(composed)==[] and len(composed.launched)==1


def test_cancelled_load_only_reconciles_without_reading_a_result(composed):
    prepared=prepare(composed);execute(composed,prepared);composed.calls.clear();cancel(composed)
    with pytest.raises(PublicationError):load(composed,prepared)
    assert [action for action,_ in composed.calls]==['reconcile']
    assert len(composed.launched)==1
