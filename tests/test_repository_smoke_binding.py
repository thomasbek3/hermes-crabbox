"""Test the driver's pure binding guard without executing its live API calls."""
import ast
import copy
from pathlib import Path
import pytest


def guard():
    tree=ast.parse((Path(__file__).parents[1]/'scripts/repo-provider-smoke.py').read_text())
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='assert_continuation_binding')
    namespace={}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<binding-guard>','exec'),namespace)
    return namespace[function.name]


def receipt():
    return {'host':'omarchy','uid':0,'config_sha256':'a'*64,'image_digest':'sha256:'+'b'*64,
        'registered_commit':'c'*40,'source_sha256':{name:'d'*64 for name in
        ('runner.py','api.py','store.py','environments.py','repositories.py','artifacts.py')}}


def test_exact_boundary_can_reuse_execution():
    guard()(receipt(),receipt())


@pytest.mark.parametrize('field',['host','uid','config_sha256','image_digest','registered_commit','source_sha256'])
def test_changed_boundary_cannot_reuse_execution(field):
    prior=receipt();current=copy.deepcopy(prior)
    current[field]='changed'
    with pytest.raises(AssertionError,match='Changed continuation binding'):
        guard()(prior,current)


@pytest.mark.parametrize('field',['host','uid','config_sha256','image_digest','registered_commit','source_sha256'])
def test_missing_boundary_cannot_reuse_execution(field):
    prior=receipt();prior.pop(field)
    with pytest.raises(AssertionError,match='Missing continuation binding'):
        guard()(prior,receipt())


def test_incomplete_source_receipts_cannot_reuse_execution():
    prior=receipt();prior['source_sha256'].pop('runner.py')
    with pytest.raises(AssertionError):guard()(prior,copy.deepcopy(prior))
