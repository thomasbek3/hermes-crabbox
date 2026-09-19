import os
import pytest

from tests.test_routed_stage import stage
from tests.test_routed_runtime import setup
from tests.test_routed_driver import driven
from tests.test_routed_cleanup import cleanup, request, ready
from cloudworkbench.routed_cleanup import _publish_exclusive, ChildCleanupError


def test_interrupted_evidence_write_can_retry_without_manual_file_deletion(cleanup, monkeypatch):
    request(cleanup)
    collector=ready(cleanup)
    original=os.fdopen
    class PartialWrite:
        def __init__(self, stream):self.stream=stream
        def __enter__(self):return self
        def __exit__(self,*args):self.stream.close()
        def write(self, raw):
            self.stream.write(raw[:len(raw)//2])
            self.stream.flush()
            raise OSError('synthetic interrupted write')
    monkeypatch.setattr(os,'fdopen',lambda *args,**kw:PartialWrite(original(*args,**kw)))
    with pytest.raises(OSError,match='synthetic interrupted write'):collector.collect()
    monkeypatch.setattr(os,'fdopen',original)
    evidence=collector.collect()
    assert evidence.evidence_path.is_file()


def test_atomic_publication_does_not_replace_existing_evidence(tmp_path):
    source=tmp_path/'source';source.write_bytes(b'new')
    target=tmp_path/'target';target.write_bytes(b'existing')
    with pytest.raises(FileExistsError):_publish_exclusive(source,target)
    assert source.read_bytes()==b'new' and target.read_bytes()==b'existing'


def test_crash_after_publication_before_directory_fsync_retries(cleanup,monkeypatch):
    request(cleanup);collector=ready(cleanup)
    original=os.fsync;calls=[]
    def directory_crash(fd):
        calls.append(fd)
        if len(calls)==2:raise OSError('synthetic directory fsync interruption')
        original(fd)
    monkeypatch.setattr(os,'fsync',directory_crash)
    with pytest.raises(OSError,match='synthetic directory'):collector.collect()
    monkeypatch.setattr(os,'fsync',original)
    evidence=collector.collect()
    assert evidence.evidence_path.stat().st_nlink==1


def test_corrupted_preexisting_final_evidence_is_preserved_and_refused(cleanup):
    request(cleanup);collector=ready(cleanup)
    evidence=collector.collect();evidence.evidence_path.write_bytes(b'corrupted')
    with pytest.raises(ChildCleanupError,match='evidence_changed'):collector.collect()
    assert evidence.evidence_path.read_bytes()==b'corrupted'
