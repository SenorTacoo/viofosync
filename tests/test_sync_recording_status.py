"""run_recording_status_release sets remote_complete=1 on every lens of the
newest capture group only when the camera reports record=0; record=1 or an
unknown/None state releases nothing, so unknown-command cameras hold the newest
segment by default."""
from __future__ import annotations

import time

from web.db import Database
from web.services import sync_worker as sw


def _seed(db, filename):
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at) "
            "VALUES (?,?,?,?,?,?)",
            (filename, "/DCIM", filename[-5], "normal", "pending", now),
        )


def _complete(db):
    with db.conn() as c:
        return {
            r["filename"]: r["remote_complete"]
            for r in c.execute(
                "SELECT filename, remote_complete FROM download_queue"
            )
        }


def test_stopped_releases_whole_newest_group(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0628_120000_0001F.MP4")           # older capture
    _seed(db, "2026_0628_133416_0002F.MP4")           # newest front
    _seed(db, "2026_0628_133416_0003R.MP4")           # newest rear (diff seq)
    n = sw.run_recording_status_release(db, recording_state=0)
    assert n == 2
    flags = _complete(db)
    assert flags["2026_0628_133416_0002F.MP4"] == 1
    assert flags["2026_0628_133416_0003R.MP4"] == 1
    assert flags["2026_0628_120000_0001F.MP4"] is None   # older untouched


def test_recording_releases_nothing(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0628_133416_0002F.MP4")
    assert sw.run_recording_status_release(db, recording_state=1) == 0
    assert _complete(db)["2026_0628_133416_0002F.MP4"] is None


def test_unknown_state_releases_nothing(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0628_133416_0002F.MP4")
    assert sw.run_recording_status_release(db, recording_state=None) == 0
    assert _complete(db)["2026_0628_133416_0002F.MP4"] is None
