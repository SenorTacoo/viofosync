# tests/test_compact_filenames.py
"""Compact single-channel Viofo filenames: ``YYYYMMDDHHMMSS_NNNNNN.MP4``.

Some units list recordings without datetime separator underscores or a
camera suffix. Those files are single-channel: the sole lens is the
GPS-bearing one, so suffix-less names default to the registry's GPS
camera letter and 'normal' event metadata, and every filename-derived
SQL expression (day key, camera letter, GPS-lens test) must understand
both layouts.
"""
from __future__ import annotations

import datetime as _dt
import time

import viofosync_lib as vfs
from viofosync_lib.cameras import GPS_CAMERA_LETTER

from web.db import Database
from web.routers import archive
from web.services import queue as q
from web.services import scanner
from web.services import triage

COMPACT = "20260618203643_000123.MP4"


# --- parsing layer -------------------------------------------------------


def test_regex_parses_compact():
    m = vfs.downloaded_filename_re.match(COMPACT)
    assert m is not None
    assert m.group("year") == "2026"
    assert m.group("month") == "06"
    assert m.group("day") == "18"
    assert m.group("hour") == "20"
    assert m.group("minute") == "36"
    assert m.group("second") == "43"
    assert m.group("sequence") == "000123"
    assert m.group("camera") == ""


def test_regex_still_parses_standard():
    m = vfs.downloaded_filename_re.match("2026_0618_203643_0001PF.MP4")
    assert m is not None
    assert m.group("sequence") == "0001"
    assert m.group("camera") == "PF"


def test_glob_finds_compact_on_disk(tmp_path):
    d = tmp_path / "2026-06-18"
    d.mkdir()
    (d / COMPACT).write_bytes(b"x")
    got = vfs.get_downloaded_recordings(str(tmp_path), "daily")
    assert (COMPACT, _dt.date(2026, 6, 18)) in got


def test_camera_and_event_default_to_gps_lens():
    assert q._camera_from_filename(COMPACT) == GPS_CAMERA_LETTER
    assert q._event_from_filename(COMPACT) == "normal"


def test_scanner_meta_defaults_front(tmp_path):
    d = tmp_path / "2026-06-18"
    d.mkdir()
    (d / COMPACT).write_bytes(b"x")
    meta = scanner._clip_meta_for(str(tmp_path), "daily", COMPACT, "")
    assert meta is not None
    assert meta.camera == GPS_CAMERA_LETTER
    assert meta.event_type == "normal"
    assert meta.group_name == "2026-06-18"
    assert meta.sequence == 123


def test_importer_scan_item_defaults_front():
    from web.services import importer
    m = vfs.downloaded_filename_re.match(COMPACT)
    item = importer.scan_item_from_match(
        m, COMPACT, source_rel_path=COMPACT, size=1, src_path="",
    )
    assert item.camera == GPS_CAMERA_LETTER
    assert item.event_type == "normal"
    assert item.sequence == 123


# --- SQL layer -----------------------------------------------------------


def _seed(db, filename, *, state="pending", recorded_at=None,
          triaged_at=None, gps_points=None, triage_attempts=0):
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at, "
            " recorded_at, triaged_at, gps_points, triage_attempts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (filename, "/DCIM/Movie", q._camera_from_filename(filename),
             q._event_from_filename(filename), state, now,
             recorded_at, triaged_at, gps_points, triage_attempts),
        )


def test_queue_day_key_not_malformed(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 6, 18, 20, 36, 43).timestamp())
    _seed(db, COMPACT, recorded_at=ts)
    days = q.list_days(db)
    assert [d["day"] for d in days] == ["2026-06-18"]
    items = q.list_day_items(db, day="2026-06-18")
    assert [it["filename"] for it in items] == [COMPACT]
    assert items[0]["kind_camera"] == GPS_CAMERA_LETTER
    assert items[0]["kind_event"] == "normal"


def test_select_targets_includes_compact(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    old = int(time.time()) - 3600            # long past the settle window
    _seed(db, COMPACT, recorded_at=old)
    targets = triage.select_targets(db)
    assert [t["filename"] for t in targets] == [COMPACT]


def test_gate_holds_compact_until_triaged(tmp_path):
    # A compact clip IS the GPS-bearing lens: like a front, it must gate
    # itself until triage reaches it, then be released.
    db = Database(str(tmp_path / "v.db"))
    _seed(db, COMPACT)
    assert q.next_pending(db, triage_gate=True) is None
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET triaged_at=?, gps_points=5 "
            "WHERE filename=?",
            (int(time.time()), COMPACT),
        )
    item = q.next_pending(db, triage_gate=True)
    assert item is not None and item.filename == COMPACT


def test_remote_day_clips_shows_compact_as_front(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 6, 18, 20, 36, 43).timestamp())
    _seed(db, COMPACT, recorded_at=ts,
          triaged_at=int(time.time()), gps_points=5)
    clips = archive.remote_day_clips(db, "2026-06-18")
    assert len(clips) == 1
    assert clips[0]["front"] is not None
    assert clips[0]["front"]["basename"] == COMPACT
    assert clips[0]["event_type"] == "normal"


def test_gps_state_derived_from_own_columns(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 6, 18, 20, 36, 43).timestamp())
    _seed(db, COMPACT, recorded_at=ts,
          triaged_at=int(time.time()), gps_points=5)
    items = q.list_page(db, per_page=10)["items"]
    assert items[0]["gps_state"] == "ok"
