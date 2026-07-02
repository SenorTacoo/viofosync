# tests/test_queue_download_next.py
"""download_next: re-queue selected clips for immediate download."""
from __future__ import annotations

import time

from web.db import Database
from web.services import queue as q


def _seed(db, filename, *, state, priority=0):
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, priority, enqueued_at, attempts) "
            "VALUES (?,?,?,?,?,0)",
            (filename, "/DCIM/Movie", state, priority, now),
        )


def _row(db, filename):
    with db.conn() as c:
        return dict(c.execute(
            "SELECT state, priority FROM download_queue WHERE filename=?",
            (filename,),
        ).fetchone())


def test_download_next_requeues_and_prioritizes(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "skip.MP4", state="skipped")
    _seed(db, "fail.MP4", state="failed")
    _seed(db, "pend.MP4", state="pending")
    _seed(db, "done.MP4", state="done")
    _seed(db, "other.MP4", state="pending")   # not selected — must stay lower

    n = q.download_next(
        db, ["skip.MP4", "fail.MP4", "pend.MP4", "done.MP4"]
    )

    assert _row(db, "skip.MP4")["state"] == "pending"
    assert _row(db, "fail.MP4")["state"] == "pending"
    assert _row(db, "pend.MP4")["state"] == "pending"
    assert _row(db, "done.MP4")["state"] == "done"
    sel_prio = min(
        _row(db, f)["priority"] for f in ("skip.MP4", "fail.MP4", "pend.MP4")
    )
    assert sel_prio > _row(db, "other.MP4")["priority"]
    assert n == 3
