import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from app import review_projection_receipts as receipts


@pytest.fixture(autouse=True)
def isolated_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts.settings, "review_projection_receipts_path", str(tmp_path / "receipts.sqlite3"))


def test_late_upsert_cannot_resurrect_deleted_review():
    applied = []
    assert receipts.apply_review_projection("r",2,{"op":"delete"},lambda:applied.append("delete"))
    assert not receipts.apply_review_projection("r",1,{"op":"upsert"},lambda:applied.append("stale"))
    assert applied == ["delete"]


def test_prepared_revision_survives_failed_io_and_rejects_older_delivery():
    def crash():
        raise RuntimeError("failed between receipt preparation and index completion")
    with pytest.raises(RuntimeError):
        receipts.apply_review_projection("r",2,{"op":"delete"},crash)
    assert not receipts.apply_review_projection("r",1,{"op":"upsert"},lambda:pytest.fail("stale write"))
    assert receipts.apply_review_projection("r",2,{"op":"delete"},lambda:None)
    assert not receipts.apply_review_projection("r",2,{"op":"delete"},lambda:pytest.fail("duplicate"))


def test_revision_collision_and_unversioned_overwrite_are_rejected():
    receipts.apply_review_projection("r",2,{"op":"delete"},lambda:None)
    with pytest.raises(ValueError,match="conflict"):
        receipts.apply_review_projection("r",2,{"op":"upsert"},lambda:None)
    with pytest.raises(ValueError,match="requires"):
        receipts.apply_review_projection("r",None,{"op":"upsert"},lambda:None)


def test_concurrent_newer_delete_finishes_after_inflight_upsert():
    started,release=threading.Event(),threading.Event()
    applied=[]
    def slow_upsert():
        started.set()
        assert release.wait(5)
        applied.append("upsert")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(receipts.apply_review_projection,"r",1,{"op":"upsert"},slow_upsert)
        assert started.wait(5)
        second=pool.submit(receipts.apply_review_projection,"r",2,{"op":"delete"},lambda:applied.append("delete"))
        release.set()
        assert first.result(timeout=5)
        assert second.result(timeout=5)
    assert applied == ["upsert","delete"]
