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


def test_assemble_route_persists_merged_points():
    """The cached payload carries the day's merged points (lon, lat, ts),
    sorted ascending by ts, so the on-read reframe can slice them."""
    import datetime as _dt

    from web.routers import archive
    from web.services.gps import Point

    base = _dt.datetime(2026, 6, 18, 8, 0, 0, tzinfo=_dt.UTC)
    pts = [
        Point(base + _dt.timedelta(seconds=i * 60), 53.0, -2.0 + i * 0.0008, 13, 90)
        for i in range(6)
    ]
    payload = archive._assemble_route("2026-06-18", pts, [], [])

    assert "points" in payload
    assert len(payload["points"]) == 6
    assert payload["points"][0] == [pts[0].lon, pts[0].lat, pts[0].t.timestamp()]
    ts_col = [p[2] for p in payload["points"]]
    assert ts_col == sorted(ts_col)


def _dwell_then_drive_gpx(lat0, lon0, start, dwell_n=7, drive_n=6,
                          step_lon=0.0008, dt_s=60):
    """``dwell_n`` stationary fixes (a >5-min stop) followed by ``drive_n``
    fixes moving east. aggregate_day yields one stop + one journey; the last
    stationary fixes sit just before the raw journey start, inside the pad."""
    import datetime as _dt
    rows = []
    t = start
    for _ in range(dwell_n):
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(f'<trkpt lat="{lat0}" lon="{lon0}"><time>{iso}</time>'
                    f"<speed>0</speed><course>0</course></trkpt>")
        t += _dt.timedelta(seconds=dt_s)
    for i in range(drive_n):
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(
            f'<trkpt lat="{lat0}" lon="{lon0 + (i + 1) * step_lon:.6f}">'
            f"<time>{iso}</time><speed>13</speed><course>90</course></trkpt>")
        t += _dt.timedelta(seconds=dt_s)
    return ('<?xml version="1.0"?><gpx version="1.0" '
            'xmlns="http://www.topografix.com/GPX/1/0"><trk><trkseg>'
            + "".join(rows) + "</trkseg></trk></gpx>")


def test_reframe_moves_start_back_into_swallowed_dwell(tmp_path):
    """A drive out of a dwell: the stop detector trims the first ~min of
    movement, but the reframe pulls the journey start back to the fixes inside
    the padded window."""
    import datetime as _dt
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    day = "2026-06-18"
    daydir = rec / day
    daydir.mkdir(parents=True, exist_ok=True)
    fn = "2026_0618_080000_0001F.MP4"
    (daydir / fn).write_bytes(b"x")
    (daydir / (fn + ".gpx")).write_text(
        _dwell_then_drive_gpx(53.0, -2.0, _dt.datetime(2026, 6, 18, 8, 0, 0)))
    ts = int(_dt.datetime(2026, 6, 18, 8, 0, 0, tzinfo=_dt.UTC).timestamp())
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, gps_examined, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,1,?)",
            (str(daydir / fn), fn, day, ts, "F", 1, "normal", 1, ts))

    payload = archive.build_route_payload(db, str(rec), day, None)
    j = payload["journeys"][0]
    base_ts = _dt.datetime(2026, 6, 18, 8, 0, 0, tzinfo=_dt.UTC).timestamp()
    # The raw GPS journey starts at the stop boundary (last in-radius fix, +360s);
    # reframing pulls the start back onto the padded window edge (+240s), proving
    # the swallowed dwell maneuvering is now included.
    assert j["start_ts"] == j["group_start_ts"] == base_ts + 240
    assert j["start_ts"] < base_ts + 360  # earlier than the raw (un-reframed) start
    # Reframed start sits at/after the group window start, and strictly before
    # the journey end (the swallowed dwell fixes were pulled in).
    assert j["start_ts"] >= j["group_start_ts"]
    assert j["start_ts"] < j["end_ts"]
    # geojson, times and the start marker all describe the same first point.
    coords = j["geojson"]["geometry"]["coordinates"]
    assert j["times"][0] == j["start_ts"]
    assert [j["start_lon"], j["start_lat"]] == coords[0]
    # start_time is the ISO form of the reframed start_ts.
    assert j["start_time"] == archive._iso_utc(j["start_ts"])
    # The merged-points scratch field is not shipped to the client.
    assert "points" not in payload


def test_reframe_leaves_cold_start_journey_untouched(tmp_path):
    """No fixes exist before the raw start (continuous movement from the first
    fix), so the reframe cannot move the start earlier."""
    import datetime as _dt
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    day = "2026-06-18"
    _seed_journey(db, rec, day, "2026_0618_080000_0001F.MP4",
                  _dt.datetime(2026, 6, 18, 8, 0, 0))

    payload = archive.build_route_payload(db, str(rec), day, None)
    j = payload["journeys"][0]
    # First fix is the raw start; nothing earlier to pull in.
    assert j["start_ts"] == j["times"][0]
    assert j["start_ts"] > j["group_start_ts"]  # window padded past the data


def _drive_dwell_drive_gpx(lat0, lon0, start, dt_s=60):
    """drive east (6) -> dwell in place (8 = 7 min) -> drive east (6). One
    session; aggregate_day yields two journeys split by one confirmed stop."""
    import datetime as _dt
    rows = []
    t = start
    lon = lon0
    def emit(lat, ln, sp, crs):
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(f'<trkpt lat="{lat}" lon="{ln:.6f}"><time>{iso}</time>'
                    f"<speed>{sp}</speed><course>{crs}</course></trkpt>")
    for _ in range(6):
        lon += 0.0008
        emit(lat0, lon, 13, 90)
        t += _dt.timedelta(seconds=dt_s)
    for _ in range(8):  # dwell in place, >5 min
        emit(lat0, lon, 0, 0)
        t += _dt.timedelta(seconds=dt_s)
    for _ in range(6):
        lon += 0.0008
        emit(lat0, lon, 13, 90)
        t += _dt.timedelta(seconds=dt_s)
    return ('<?xml version="1.0"?><gpx version="1.0" '
            'xmlns="http://www.topografix.com/GPX/1/0"><trk><trkseg>'
            + "".join(rows) + "</trkseg></trk></gpx>")


def test_adjacent_journey_windows_never_overlap(tmp_path):
    """Two drives separated by a >=5-min stop: padding is <=120 s per edge, and
    a confirmed stop is >=300 s, so the journeys' padded windows can't meet.
    Locks the invariant that lets the reframe slice points without a clamp."""
    import datetime as _dt
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    day = "2026-06-18"
    daydir = rec / day
    daydir.mkdir(parents=True, exist_ok=True)
    fn = "2026_0618_080000_0001F.MP4"
    (daydir / fn).write_bytes(b"x")
    (daydir / (fn + ".gpx")).write_text(
        _drive_dwell_drive_gpx(53.0, -2.0, _dt.datetime(2026, 6, 18, 8, 0, 0)))
    ts = int(_dt.datetime(2026, 6, 18, 8, 0, 0, tzinfo=_dt.UTC).timestamp())
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, gps_examined, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,1,?)",
            (str(daydir / fn), fn, day, ts, "F", 1, "normal", 1, ts))

    payload = archive.build_route_payload(db, str(rec), day, None)
    js = payload["journeys"]
    assert len(js) == 2, "expected two drives split by the dwell"
    a, b = sorted(js, key=lambda j: j["group_start_ts"])
    assert a["group_end_ts"] <= b["group_start_ts"], "padded windows overlap"
