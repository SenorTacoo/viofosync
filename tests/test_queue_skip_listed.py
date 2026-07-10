from __future__ import annotations

import time

from web.db import Database
from web.services import queue as q


def _seed(db, filename, state):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, enqueued_at) "
            "VALUES (?, '', ?, ?)",
            (filename, state, int(time.time())),
        )


def test_skip_listed_names_returns_only_skipped(tmp_path):
    db = Database(str(tmp_path / "q.db"))
    _seed(db, "2026_0618_120000_0001F.MP4", "skipped")   # geofence/user skip
    _seed(db, "2026_0618_120100_0002F.MP4", "pending")
    _seed(db, "2026_0618_120200_0003F.MP4", "done")
    _seed(db, "2026_0618_120300_0004F.MP4", "skipped")
    names = [
        "2026_0618_120000_0001F.MP4",   # skipped
        "2026_0618_120100_0002F.MP4",   # pending
        "2026_0618_120200_0003F.MP4",   # done
        "2026_0618_120300_0004F.MP4",   # skipped
        "2026_0618_999999_9999F.MP4",   # not in queue at all
    ]
    assert q.skip_listed_names(db, names) == {
        "2026_0618_120000_0001F.MP4",
        "2026_0618_120300_0004F.MP4",
    }


def test_skip_listed_names_empty_input_does_no_query(tmp_path):
    db = Database(str(tmp_path / "q.db"))
    assert q.skip_listed_names(db, []) == set()
