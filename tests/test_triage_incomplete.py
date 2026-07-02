"""Triage never mis-marks the unconfirmed newest capture: select_targets
excludes it, and an IncompleteRecording during triage_one leaves it untriaged
without burning an attempt, while an older truncated clip is marked no-GPS."""
from __future__ import annotations

import time

from viofosync_lib import IncompleteRecording
from web.db import Database
from web.services import triage


def _seed(db, filename, *, recorded_at):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at, "
            " recorded_at) VALUES (?,?,?,?,?,?,?)",
            (filename, "/DCIM", filename[-5], "normal", "pending",
             int(time.time()), recorded_at),
        )


def _row(db, filename):
    with db.conn() as c:
        return dict(c.execute(
            "SELECT * FROM download_queue WHERE filename=?", (filename,)
        ).fetchone())


def test_select_targets_excludes_unconfirmed_newest(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    old = int(time.time()) - 10_000        # past the settle window
    _seed(db, "2026_0628_120000_0001F.MP4", recorded_at=old)   # older
    _seed(db, "2026_0628_133416_0002F.MP4", recorded_at=old)   # newest, unconfirmed
    names = {t["filename"] for t in triage.select_targets(db)}
    assert "2026_0628_120000_0001F.MP4" in names
    assert "2026_0628_133416_0002F.MP4" not in names


def test_triage_one_incomplete_newest_defers_without_attempt(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    old = int(time.time()) - 10_000
    _seed(db, "2026_0628_133416_0002F.MP4", recorded_at=old)   # newest
    monkeypatch.setattr(
        triage.vfs, "extract_remote_gps_points",
        lambda *a, **k: (_ for _ in ()).throw(IncompleteRecording("x")),
    )
    row = _row(db, "2026_0628_133416_0002F.MP4")
    n = triage.triage_one(db, "http://cam", row, str(tmp_path), timeout=5)
    assert n == -1
    after = _row(db, "2026_0628_133416_0002F.MP4")
    assert after["triaged_at"] is None
    assert after["triage_attempts"] == 0
    assert after["triage_last_attempt_at"] is not None


def test_triage_one_incomplete_old_marks_no_gps(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    old = int(time.time()) - 10_000
    _seed(db, "2026_0628_120000_0001F.MP4", recorded_at=old)   # older
    _seed(db, "2026_0628_140000_0009F.MP4", recorded_at=old)   # newest (so 1st is old)
    monkeypatch.setattr(
        triage.vfs, "extract_remote_gps_points",
        lambda *a, **k: (_ for _ in ()).throw(IncompleteRecording("x")),
    )
    row = _row(db, "2026_0628_120000_0001F.MP4")
    n = triage.triage_one(db, "http://cam", row, str(tmp_path), timeout=5)
    after = _row(db, "2026_0628_120000_0001F.MP4")
    assert after["triaged_at"] is not None
    assert after["gps_points"] == 0
