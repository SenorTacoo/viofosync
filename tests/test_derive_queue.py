"""Tests for the derive_queue table."""
from __future__ import annotations

from web.db import Database
from web.services import derive_queue as dq


def test_derive_queue_table_exists(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    with db.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(derive_queue)")}
    assert cols == {
        "clip_id", "priority", "state", "attempts",
        "last_error", "enqueued_at", "updated_at",
    }


def test_enqueue_and_claim_order(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    dq.enqueue(db, 10, priority=1, now=100)   # backfill
    dq.enqueue(db, 11, priority=0, now=100)   # live
    dq.enqueue(db, 12, priority=0, now=100)   # live, newer id
    # priority ASC, clip_id DESC -> live newest first, backfill last
    assert dq.claim_next(db, now=101) == 12
    assert dq.claim_next(db, now=102) == 11
    assert dq.claim_next(db, now=103) == 10
    assert dq.claim_next(db, now=104) is None  # all running


def test_mark_done(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    dq.enqueue(db, 5, priority=0, now=1)
    dq.claim_next(db, now=2)
    dq.mark_done(db, 5, now=3)
    with db.conn() as c:
        row = c.execute(
            "SELECT state FROM derive_queue WHERE clip_id=5"
        ).fetchone()
    assert row["state"] == "done"


def test_transient_failure_requeues_then_fails(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    dq.enqueue(db, 7, priority=0, now=1)
    dq.claim_next(db, now=2)
    # First failure -> back to pending, attempts=1
    assert dq.mark_transient_failure(db, 7, "boom", max_attempts=2, now=3) == "pending"
    assert dq.claim_next(db, now=4) == 7
    # Second failure hits the cap -> failed
    assert dq.mark_transient_failure(db, 7, "boom", max_attempts=2, now=5) == "failed"
    assert dq.claim_next(db, now=6) is None


def test_enqueue_reactivates_failed(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    dq.enqueue(db, 9, priority=0, now=1)
    dq.claim_next(db, now=2)
    dq.mark_transient_failure(db, 9, "x", max_attempts=1, now=3)  # -> failed
    dq.enqueue(db, 9, priority=0, now=4)  # manual re-enqueue resets it
    assert dq.claim_next(db, now=5) == 9


def test_reconcile_orphans(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    dq.enqueue(db, 3, priority=0, now=1)
    dq.claim_next(db, now=2)  # -> running
    assert dq.reconcile_orphans(db, now=3) == 1
    assert dq.claim_next(db, now=4) == 3  # back to pending, claimable


def test_remove_clip(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    dq.enqueue(db, 4, priority=0, now=1)
    dq.remove_clip(db, 4)
    with db.conn() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM derive_queue WHERE clip_id=4"
        ).fetchone()[0] == 0


def test_enqueue_missing_skips_done(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    with db.write() as c:
        for cid in (1, 2):
            c.execute(
                "INSERT INTO clip_index (id, path, basename, timestamp, "
                "camera, sequence, scanned_at) VALUES (?,?,?,?,?,?,?)",
                (cid, f"/x/{cid}.mp4", f"{cid}.mp4", 0, "F", 0, 0),
            )
    dq.enqueue(db, 1, priority=0, now=1)
    dq.mark_done(db, 1, now=2)          # clip 1 already derived
    n = dq.enqueue_missing(db, priority=1, now=3)
    assert n == 1                        # only clip 2 enqueued
    assert dq.claim_next(db, now=4) == 2


def test_enqueue_does_not_downgrade_priority(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    # A clip enqueued live (priority 0)...
    dq.enqueue(db, 1, priority=0, now=1)
    # ...must not be downgraded to backfill priority by a later scan.
    dq.enqueue(db, 1, priority=1, now=2)
    with db.conn() as c:
        p = c.execute(
            "SELECT priority FROM derive_queue WHERE clip_id=1"
        ).fetchone()["priority"]
    assert p == 0


def test_enqueue_missing_does_not_downgrade(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index (id, path, basename, timestamp, camera, "
            "sequence, scanned_at) VALUES (1,'/x/c.mp4','c.mp4',0,'F',0,0)"
        )
    dq.enqueue(db, 1, priority=0, now=1)          # live, still pending
    dq.enqueue_missing(db, priority=1, now=2)      # post-cycle scan
    with db.conn() as c:
        p = c.execute(
            "SELECT priority FROM derive_queue WHERE clip_id=1"
        ).fetchone()["priority"]
    assert p == 0
