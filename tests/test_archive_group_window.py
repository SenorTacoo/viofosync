"""The archive day-grid groups clips into a journey using a PADDED window, so
pull-away / pull-in clips that sit just outside the raw GPS journey (the GPS
stop boundary lands ~STOP_RADIUS_M inside the real drive) attach to the journey
card instead of the neighbouring stop. The route payload carries the padded
window (``group_start_ts``/``group_end_ts``), reusing the same
``_expand_journey_window`` the timeline editor uses."""
from __future__ import annotations

import datetime as _dt

from web.db import Database
from web.routers import archive


def _moving_gpx(lat0, lon0, n, start, step_lon=0.0008, dt_s=60):
    """A GPX track of ``n`` points moving steadily east — continuous movement,
    >200 m, no 5-min dwell — so aggregate_day yields exactly one journey."""
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


def _seed_journey(db, rec, day, fn, start):
    daydir = rec / day
    daydir.mkdir(parents=True, exist_ok=True)
    path = daydir / fn
    path.write_bytes(b"x")
    (daydir / (fn + ".gpx")).write_text(_moving_gpx(53.0, -2.0, 6, start))
    ts = int(start.replace(tzinfo=_dt.UTC).timestamp())
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, gps_examined, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,1,?)",
            (str(path), fn, day, ts, "F", 1, "normal", 1, ts),
        )


def test_journey_carries_padded_group_window(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    day = "2026-06-18"
    _seed_journey(db, rec, day, "2026_0618_080000_0001F.MP4",
                  _dt.datetime(2026, 6, 18, 8, 0, 0))

    payload = archive.build_route_payload(db, str(rec), day, None)
    assert payload["journeys"], "expected one journey from the moving track"
    j = payload["journeys"][0]
    # No parking clips on the day → padded by the full cap on each edge.
    assert j["group_start_ts"] == j["start_ts"] - archive.MAX_JOURNEY_BUFFER_S
    assert j["group_end_ts"] == j["end_ts"] + archive.MAX_JOURNEY_BUFFER_S


def test_group_window_bounded_by_parking_clip(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    day = "2026-06-18"
    _seed_journey(db, rec, day, "2026_0618_080000_0001F.MP4",
                  _dt.datetime(2026, 6, 18, 8, 0, 0))

    # Drop a parking clip 30 s after the journey end (inside the 120 s pad) so
    # the trailing edge clamps to it instead of running to the full cap.
    end_ts = archive.build_route_payload(db, str(rec), day, None)["journeys"][0]["end_ts"]
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, gps_examined, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,0,1,?)",
            (str(rec / day / "park.MP4"), "2026_0618_080600_0009PF.MP4", day,
             int(end_ts) + 30, "PF", 9, "parking", 1, int(end_ts)),
        )

    j = archive.build_route_payload(db, str(rec), day, None)["journeys"][0]
    assert j["group_end_ts"] == j["end_ts"] + 30
    assert j["group_end_ts"] < j["end_ts"] + archive.MAX_JOURNEY_BUFFER_S


def test_group_window_bounded_by_queued_parking_clip(tmp_path):
    """A parking clip that is still queued (not downloaded) must also bound the
    pad — under GPS triage the day's parking clips live only in download_queue,
    so a clip_index-only bound would never engage."""
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    day = "2026-06-18"
    _seed_journey(db, rec, day, "2026_0618_080000_0001F.MP4",
                  _dt.datetime(2026, 6, 18, 8, 0, 0))
    end_ts = archive.build_route_payload(db, str(rec), day, None)["journeys"][0]["end_ts"]
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, "
            " event_type, recorded_at, enqueued_at) VALUES (?,?,?,?,?,0)",
            ("2026_0618_080530_0009PF.MP4", "/DCIM/Movie", "pending",
             "parking", int(end_ts) + 30),
        )

    j = archive.build_route_payload(db, str(rec), day, None)["journeys"][0]
    assert j["group_end_ts"] == j["end_ts"] + 30
    assert j["group_end_ts"] < j["end_ts"] + archive.MAX_JOURNEY_BUFFER_S
