"""The archive's opt-in "GPS-excluded" view: geofence-skipped clips as
tiles (remote_day_clips) — user-skipped clips stay invisible."""
from __future__ import annotations

import datetime as _dt
import time
import types

from web.db import Database
from web.routers import archive


def _req(db, *, gps_triage):
    """Minimal stand-in for a FastAPI Request: the archive endpoints only
    read request.app.state.db and .settings_provider.get()."""
    snap = types.SimpleNamespace(gps_triage=gps_triage)
    provider = types.SimpleNamespace(get=lambda: snap)
    state = types.SimpleNamespace(db=db, settings_provider=provider)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def _seed_queue(db, filename, *, camera, state="pending", skip_reason=None,
                recorded_at=None, triaged=True, gps_points=1):
    triaged_at = int(time.time()) if triaged else None
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, skip_reason, "
            " enqueued_at, recorded_at, triaged_at, gps_points) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (filename, "/DCIM/Movie", camera, "normal", state, skip_reason,
             int(time.time()), recorded_at, triaged_at, gps_points),
        )


def test_remote_day_clips_geofenced_only_with_flag(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed_queue(db, "2026_0618_080000_0001F.MP4", camera="F",
                state="skipped", skip_reason="geofence", recorded_at=1_000)
    _seed_queue(db, "2026_0618_090000_0002F.MP4", camera="F",
                state="skipped", skip_reason="user", recorded_at=2_000)

    # Default: no skipped rows at all (pins today's behaviour).
    assert archive.remote_day_clips(db, "2026-06-18") == []

    clips = archive.remote_day_clips(db, "2026-06-18", include_geofenced=True)
    assert [c["front"]["filename"] for c in clips] == [
        "2026_0618_080000_0001F.MP4"
    ]
    assert clips[0]["front"]["state"] == "skipped"
    assert clips[0]["front"]["skip_reason"] == "geofence"


def test_remote_day_clips_flag_keeps_pending(tmp_path):
    # The flag widens the set; it must not replace it.
    db = Database(str(tmp_path / "v.db"))
    _seed_queue(db, "2026_0618_080000_0001F.MP4", camera="F",
                recorded_at=1_000)
    _seed_queue(db, "2026_0618_090000_0002F.MP4", camera="F",
                state="skipped", skip_reason="geofence", recorded_at=2_000)
    clips = archive.remote_day_clips(db, "2026-06-18", include_geofenced=True)
    assert len(clips) == 2


def _moving_gpx(lat0, lon0, n, start, step_lon=0.0008, dt_s=60):
    pts = []
    for i in range(n):
        t = start + _dt.timedelta(seconds=i * dt_s)
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        pts.append(
            f'<trkpt lat="{lat0}" lon="{lon0 + i * step_lon:.6f}">'
            f"<time>{iso}</time><speed>13</speed><course>90</course></trkpt>"
        )
    return (
        '<?xml version="1.0"?><gpx version="1.0" '
        'xmlns="http://www.topografix.com/GPX/1/0">'
        "<trk><trkseg>" + "".join(pts) + "</trkseg></trk></gpx>"
    )


def _seed_geofenced_skeleton(db, rec, fn, start):
    (rec / ".triage").mkdir(parents=True, exist_ok=True)
    (rec / ".triage" / (fn + ".gpx")).write_text(
        _moving_gpx(53.0, -2.0, 6, start)
    )
    ts = int(start.replace(tzinfo=_dt.UTC).timestamp())
    _seed_queue(db, fn, camera="F", state="skipped",
                skip_reason="geofence", recorded_at=ts)
    return ts


def test_route_flag_recovers_geofenced_journey(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    fn = "2026_0618_080000_0001F.MP4"
    _seed_geofenced_skeleton(db, rec, fn,
                             _dt.datetime(2026, 6, 18, 8, 0, 0))

    off = archive.build_route_payload(db, str(rec), "2026-06-18", None)
    assert off["journeys"] == []
    assert "geofenced_tracks" not in off

    on = archive.build_route_payload(
        db, str(rec), "2026-06-18", None, include_geofenced=True,
    )
    assert len(on["journeys"]) == 1
    assert [t["filename"] for t in on["geofenced_tracks"]] == [fn]
    track = on["geofenced_tracks"][0]
    coords = track["geojson"]["geometry"]["coordinates"]
    assert len(coords) == 6
    # Each track carries per-point timestamps so the client can scope it to
    # the owning journey's window (otherwise every journey map would draw
    # every track). One timestamp per coordinate, ascending.
    assert len(track["times"]) == len(coords)
    assert track["times"] == sorted(track["times"])
    # No downloadable clip in the window -> no pie (completion is None).
    assert on["journeys"][0]["completion"] is None


def test_geofenced_tracks_scoped_to_separate_journeys(tmp_path):
    # Two journeys hours apart, each with its own geofence-skipped clip. Each
    # track's times must fall within exactly one journey's window, so the
    # client can draw each recovered stretch only on the map it belongs to.
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    fn_a = "2026_0618_080000_0001F.MP4"
    fn_b = "2026_0618_140000_0002F.MP4"
    _seed_geofenced_skeleton(db, rec, fn_a, _dt.datetime(2026, 6, 18, 8, 0, 0))
    _seed_geofenced_skeleton(db, rec, fn_b, _dt.datetime(2026, 6, 18, 14, 0, 0))

    on = archive.build_route_payload(
        db, str(rec), "2026-06-18", None, include_geofenced=True,
    )
    assert len(on["journeys"]) == 2
    tracks = {t["filename"]: t["times"] for t in on["geofenced_tracks"]}
    windows = [(j["start_ts"], j["end_ts"]) for j in on["journeys"]]
    # Every track sits wholly inside exactly one journey window.
    for _fn, times in tracks.items():
        in_window = [
            (lo, hi) for lo, hi in windows
            if all(lo <= t <= hi for t in times)
        ]
        assert len(in_window) == 1, f"{_fn} not scoped to one journey"


def _route_req(db, rec):
    snap = types.SimpleNamespace(
        gps_triage=True, recordings=str(rec), locations=(),
    )
    provider = types.SimpleNamespace(get=lambda: snap)
    state = types.SimpleNamespace(db=db, settings_provider=provider)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def test_get_day_show_geofenced_param(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed_queue(db, "2026_0618_080000_0001F.MP4", camera="F",
                state="skipped", skip_reason="geofence", recorded_at=1_000)
    req = _req(db, gps_triage=True)

    off = archive.get_day(req, "2026-06-18", None, None, True, True, True,
                          False)
    assert off["clips"] == []

    on = archive.get_day(req, "2026-06-18", None, None, True, True, True,
                         True)
    assert len(on["clips"]) == 1
    assert on["clips"][0]["front"]["skip_reason"] == "geofence"


def test_get_route_show_geofenced_param(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    _seed_geofenced_skeleton(db, rec, "2026_0618_080000_0001F.MP4",
                             _dt.datetime(2026, 6, 18, 8, 0, 0))
    req = _route_req(db, rec)

    off = archive.get_route(req, "2026-06-18", False)
    assert "geofenced_tracks" not in off

    on = archive.get_route(req, "2026-06-18", True)
    assert len(on["geofenced_tracks"]) == 1
    assert len(on["journeys"]) == 1
