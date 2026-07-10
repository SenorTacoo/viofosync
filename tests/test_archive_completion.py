"""_apply_completion attaches per-journey data-completeness (downloaded
duration / total, front clips only, skipped excluded)."""
from __future__ import annotations

from web.db import Database
from web.routers import archive

DATE = "2026-06-18"


def _payload(*windows):
    return {"journeys": [
        {"start_ts": gs, "end_ts": ge,
         "group_start_ts": gs, "group_end_ts": ge} for gs, ge in windows
    ]}


def _clip(db, ts, cam="F", *, dur=60.0, seq=1):
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index (path, basename, group_name, timestamp, "
            "camera, sequence, event_type, duration_s, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"/rec/{ts}{cam}.MP4", f"{ts}{cam}.MP4", DATE, ts, cam, seq,
             "normal", dur, ts),
        )


def _queued(db, ts, cam="F", *, state="pending", triaged=1, skip=None):
    fn = f"2026_0618_{ts:06d}_0001{cam}.MP4"
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, camera, "
            "event_type, state, enqueued_at, recorded_at, triaged_at, "
            "gps_points, skip_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fn, "/DCIM", cam, "normal", state, ts, ts,
             (ts if triaged else None), (5 if triaged else None), skip),
        )


def test_all_downloaded_is_complete(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _clip(db, 1000, dur=60.0)
    _clip(db, 1060, dur=60.0)
    p = _payload((1000, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion"] == 1.0
    assert p["journeys"][0]["completion_detail"]["downloaded_clips"] == 2


def test_skipped_clip_excluded_from_denominator(tmp_path):
    # An intentionally-skipped (e.g. geofenced) clip is absent from the map
    # and must not drag completion down: a downloaded clip + a skipped one
    # reads 100%.
    db = Database(str(tmp_path / "v.db"))
    _clip(db, 1000, dur=60.0)
    _queued(db, 1060, state="skipped", skip="geofence")
    p = _payload((1000, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion"] == 1.0
    assert p["journeys"][0]["completion_detail"]["total_clips"] == 1


def test_none_downloaded_is_zero(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _queued(db, 1000)
    _queued(db, 1060)
    p = _payload((1000, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion"] == 0.0
    assert p["journeys"][0]["completion_detail"]["total_clips"] == 2


def test_mixed_is_fraction_by_duration(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _clip(db, 1000, dur=60.0)
    _queued(db, 1060)
    p = _payload((1000, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion"] == 0.5


def test_rear_clip_ignored(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _clip(db, 1000, cam="F", dur=60.0)
    _clip(db, 1000, cam="R", dur=60.0, seq=2)
    p = _payload((1000, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion_detail"]["total_clips"] == 1


def test_prefixed_front_counts(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _clip(db, 1000, cam="PF", dur=60.0)
    p = _payload((1000, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion_detail"]["downloaded_clips"] == 1


def test_duration_s_null_uses_gap(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _clip(db, 1000, dur=None)
    _clip(db, 1090, dur=None)
    p = _payload((1000, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion"] == 1.0
    assert p["journeys"][0]["completion_detail"]["total_s"] == 90 + 60


def test_only_clips_in_window_count(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _clip(db, 1000, dur=60.0)
    _clip(db, 5000, dur=60.0)
    p = _payload((900, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion_detail"]["total_clips"] == 1


def test_empty_journey_reads_complete(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    p = _payload((1000, 1200))
    archive._apply_completion(db, DATE, p)
    # Empty window (no clips at all) -> no pie, not a misleading 100%.
    assert p["journeys"][0]["completion"] is None
    assert p["journeys"][0]["completion_detail"]["total_clips"] == 0


def test_untriaged_pending_excluded(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _queued(db, 1000, triaged=0)          # triaged_at NULL -> not on the map
    p = _payload((900, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion_detail"]["total_clips"] == 0
    # Empty window (nothing counted) -> no pie, not a misleading 100%.
    assert p["journeys"][0]["completion"] is None


def test_geofenced_only_window_has_no_pie(tmp_path):
    # A journey recovered purely from geofence-skipped skeletons has no
    # downloadable clips in its window: the pie is omitted (None), not a
    # misleading 100%.
    db = Database(str(tmp_path / "v.db"))
    _queued(db, 1060, state="skipped", skip="geofence")
    p = _payload((1000, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion"] is None


def test_pending_without_gps_lock_excluded(tmp_path):
    # triaged but gps_points=0 (no lock) -> absent from the journey map,
    # so excluded from the denominator.
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, camera, "
            "event_type, state, enqueued_at, recorded_at, triaged_at, "
            "gps_points) VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026_0618_001000_0001F.MP4", "/DCIM", "F", "normal",
             "pending", 1000, 1000, 1000, 0),
        )
    p = _payload((900, 1200))
    archive._apply_completion(db, DATE, p)
    assert p["journeys"][0]["completion_detail"]["total_clips"] == 0
