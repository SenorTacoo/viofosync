"""next_pending holds every lens of the newest (possibly-recording) capture
until remote_complete=1; older captures and superseded captures are free."""
from __future__ import annotations

import time

from web.db import Database
from web.services import queue as q


def _seed(db, filename, *, state="pending", remote_complete=None,
          triaged_at=1):
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at, "
            " triaged_at, remote_complete) VALUES (?,?,?,?,?,?,?,?)",
            (filename, "/DCIM", filename[-5], "normal", state, now,
             triaged_at, remote_complete),
        )


def test_newest_capture_front_is_held(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0628_120000_0001F.MP4")           # older capture
    _seed(db, "2026_0628_133416_0002F.MP4")           # newest, unconfirmed
    item = q.next_pending(db, active_guard=True)
    assert item is not None
    assert item.filename == "2026_0628_120000_0001F.MP4"  # older one only


def test_newest_capture_rear_sibling_is_held(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0628_133416_0002F.MP4")           # newest front
    _seed(db, "2026_0628_133416_0003R.MP4")           # newest rear (diff seq)
    assert q.next_pending(db, active_guard=True) is None


def test_confirmed_newest_is_released(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0628_133416_0002F.MP4", remote_complete=1)
    assert q.next_pending(db, active_guard=True) is not None


def test_supersession_releases_previous(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0628_133416_0002F.MP4")           # was newest, unconfirmed
    _seed(db, "2026_0628_140000_0009F.MP4")           # now newest, unconfirmed
    item = q.next_pending(db, active_guard=True)
    assert item is not None
    assert item.filename == "2026_0628_133416_0002F.MP4"  # previous now free


def test_guard_off_hands_out_newest(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0628_133416_0002F.MP4")
    assert q.next_pending(db, active_guard=False) is not None
