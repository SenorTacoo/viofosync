# tests/test_triage_archive_merge.py
"""Archive endpoints union queued (triaged) clips with clip_index."""
from __future__ import annotations

import datetime as _dt
import time
import types

from web.db import Database
from web.routers import archive


def _req(db, *, gps_triage):
    """Minimal stand-in for a FastAPI Request: the archive endpoints only read
    request.app.state.db and request.app.state.settings_provider.get()."""
    snap = types.SimpleNamespace(gps_triage=gps_triage)
    provider = types.SimpleNamespace(get=lambda: snap)
    state = types.SimpleNamespace(db=db, settings_provider=provider)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def _gpx(lat, lon, t="2026-06-18T20:36:43Z"):
    return (
        '<?xml version="1.0"?><gpx version="1.0" '
        'xmlns="http://www.topografix.com/GPX/1/0">'
        f'<trk><trkseg><trkpt lat="{lat}" lon="{lon}">'
        f"<time>{t}</time><speed>0</speed><course>0</course>"
        "</trkpt></trkseg></trk></gpx>"
    )


def _seed_queue(db, filename, *, camera, state="pending", recorded_at=None):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at, "
            " recorded_at, triaged_at, gps_points) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (filename, "/DCIM/Movie", camera, "normal", state,
             int(time.time()), recorded_at, int(time.time()), 1),
        )


def test_route_includes_skeleton_for_queued_clip(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    (rec / ".triage").mkdir(parents=True)
    fn = "2026_0618_203643_0001F.MP4"
    (rec / ".triage" / (fn + ".gpx")).write_text(_gpx(53.0, -2.0))
    ts = int(_dt.datetime(2026, 6, 18, 20, 36, 43).timestamp())
    _seed_queue(db, fn, camera="F", recorded_at=ts)

    payload = archive.build_route_payload(db, str(rec), "2026-06-18", None)
    assert payload["point_count"] >= 1


def test_route_excludes_downloaded_clip_skeleton(tmp_path):
    """A clip already downloaded (state done) must not also be counted from .triage."""
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    (rec / ".triage").mkdir(parents=True)
    fn = "2026_0618_203643_0001F.MP4"
    (rec / ".triage" / (fn + ".gpx")).write_text(_gpx(53.0, -2.0))
    _seed_queue(db, fn, camera="F", state="done")
    payload = archive.build_route_payload(db, str(rec), "2026-06-18", None)
    assert payload["point_count"] == 0


def test_days_includes_remote_only_day(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed_queue(db, "2026_0618_203643_0001F.MP4", camera="F")  # nothing in clip_index
    out = archive.remote_day_summaries(db)
    assert "2026-06-18" in {d["day"] for d in out}
    d = next(d for d in out if d["day"] == "2026-06-18")
    assert d["remote_count"] >= 1


def test_day_includes_remote_clip_placeholder(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 6, 18, 20, 36, 43).timestamp())
    _seed_queue(db, "2026_0618_203643_0001F.MP4", camera="F", recorded_at=ts)
    _seed_queue(db, "2026_0618_203643_0002R.MP4", camera="R", recorded_at=ts)
    clips = archive.remote_day_clips(db, "2026-06-18")
    assert len(clips) == 1                      # F+R paired
    pair = clips[0]
    assert pair["remote"] is True
    assert pair["front"]["remote"] is True
    assert pair["rear"]["remote"] is True
    assert "id" not in pair["front"]            # not downloaded → no clip id


def test_remote_day_clips_collapses_event_kind_to_normal(tmp_path):
    """Impact (E-prefix) clips are 'event' in the queue but 'normal' once
    downloaded (scanner._event_type_for). remote_day_clips must collapse to
    'normal' so get_day's de-dup key matches and no ghost placeholder remains."""
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 6, 18, 20, 36, 43).timestamp())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at, "
            " recorded_at, triaged_at, gps_points) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026_0618_203643_0001EF.MP4", "/DCIM/Movie", "F", "event",
             "pending", int(time.time()), ts, int(time.time()), 1),
        )
    clips = archive.remote_day_clips(db, "2026-06-18")
    assert len(clips) == 1
    assert clips[0]["event_type"] == "normal"


def test_remote_day_clips_includes_skipped_and_downloading(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 6, 18, 20, 36, 43).timestamp())
    _seed_queue(db, "2026_0618_203643_0001F.MP4", camera="F",
                state="pending", recorded_at=ts)
    _seed_queue(db, "2026_0618_203644_0002F.MP4", camera="F",
                state="skipped", recorded_at=ts + 60)
    _seed_queue(db, "2026_0618_203645_0003F.MP4", camera="F",
                state="downloading", recorded_at=ts + 120)
    _seed_queue(db, "2026_0618_203646_0004F.MP4", camera="F",
                state="done", recorded_at=ts + 180)
    _seed_queue(db, "2026_0618_203647_0005F.MP4", camera="F",
                state="gone", recorded_at=ts + 240)

    clips = archive.remote_day_clips(db, "2026-06-18")
    by_state = {c["front"]["state"]: c for c in clips if c.get("front")}
    assert set(by_state) == {"pending", "skipped", "downloading"}
    assert "done" not in by_state
    assert "gone" not in by_state


def test_get_day_hides_remote_clips_when_triage_off(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 6, 18, 20, 36, 43).timestamp())
    _seed_queue(db, "2026_0618_203643_0001F.MP4", camera="F",
                state="pending", recorded_at=ts)
    kw = dict(time_from=None, time_to=None, driving=True, parking=True, ro=True)

    # Triage off → no placeholder pairs in the day view (they would otherwise
    # snap onto the nearest journey with no GPS of their own).
    off = archive.get_day(_req(db, gps_triage=False), "2026-06-18", **kw)
    assert off["clips"] == []

    # Triage on → the queued clip appears as a remote placeholder pair.
    on = archive.get_day(_req(db, gps_triage=True), "2026-06-18", **kw)
    assert any(c.get("remote") for c in on["clips"])


def test_list_days_hides_remote_only_day_when_triage_off(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed_queue(db, "2026_0618_203643_0001F.MP4", camera="F", state="pending")
    kw = dict(date_from=None, date_to=None, driving=True, parking=True, ro=True,
              sort="desc", page=1, per_page=20)

    off = archive.list_days(_req(db, gps_triage=False), **kw)
    assert off["days"] == []

    on = archive.list_days(_req(db, gps_triage=True), **kw)
    assert "2026-06-18" in {d["day"] for d in on["days"]}
