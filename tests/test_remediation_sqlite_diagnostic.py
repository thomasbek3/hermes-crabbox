"""Test-only observer preserves SQLite behavior without capturing parameters."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest

if not (Path(__file__).resolve().parents[1]/'evidence/diagnose-remediation-sqlite.py').is_file():
    pytest.skip('Optional historical diagnostic fixture is not distributed', allow_module_level=True)

spec=importlib.util.spec_from_file_location('remediation_diagnostic',Path(__file__).resolve().parents[1]/'evidence/diagnose-remediation-sqlite.py')
diag=importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)


def test_boundary_never_contains_sql_literal_or_parameter(tmp_path):
    path=tmp_path/'test.db';sqlite3.connect(path).close()
    observer=diag.Observer();db=observer.connect(path)
    db.execute('CREATE TABLE example(value TEXT)')
    db.execute('INSERT INTO example VALUES(?)',('synthetic-private-value',))
    assert db.execute('SELECT value FROM example').fetchone()[0]=='synthetic-private-value'
    db.close()
    assert 'synthetic-private-value' not in json.dumps(observer.records)
    assert diag.boundary("SELECT 'secret-literal' FROM example")['table']=='example'
    assert 'secret-literal' not in json.dumps(diag.boundary("SELECT 'secret-literal' FROM example"))


def test_exact_interrupt_error_is_preserved_and_captured(tmp_path):
    path=tmp_path/'test.db';sqlite3.connect(path).close()
    observer=diag.Observer();db=observer.connect(path)
    db.set_progress_handler(lambda:1,1)
    with pytest.raises(sqlite3.OperationalError) as error:
        db.execute('SELECT 1')
    assert error.value.sqlite_errorname=='SQLITE_INTERRUPT'
    db.set_progress_handler(None,0);db.close()
    row=observer.records[0]
    assert row['errors'][0]['name']=='SQLITE_INTERRUPT' and row['errors'][0]['phase']=='execute'
    assert row['progress_interrupts'] and observer.injected is False


def test_non_timeout_sql_error_remains_distinct(tmp_path):
    path=tmp_path/'test.db';sqlite3.connect(path).close()
    observer=diag.Observer();db=observer.connect(path)
    with pytest.raises(sqlite3.OperationalError):db.execute('SELECT * FROM absent')
    db.close()
    row=observer.records[0]
    assert row['errors'][0]['name']=='SQLITE_ERROR' and not row['progress_interrupts']
