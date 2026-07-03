"""Tests for build_route_payload + GET /api/archive/timeline."""
from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path

import pytest

from web.db import Database
from web.routers import archive


def test_build_route_payload_empty_day(tmp_path: Path):
    """No gpx clips for the date -> empty journeys/stops, point_count 0."""
    db = Database(str(tmp_path / "t.db"))
    payload = archive.build_route_payload(db, str(tmp_path), "2026-06-02", None)
    assert payload["date"] == "2026-06-02"
    assert payload["point_count"] == 0
    assert payload["journeys"] == []
    assert payload["stops"] == []


class _FakeMqttService:
    def __init__(self, **kwargs): pass
    def start(self): pass
    async def stop(self): pass
    async def on_settings_changed(self, keys, snap): pass
    def get_status(self):
        return {"state": "idle", "detail": None, "last_published_at": None}


@pytest.fixture
def logged_in_client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    import bcrypt
    from fastapi.testclient import TestClient

    from web import settings as settings_mod
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    digest = bcrypt.hashpw(b"pw" * 8, bcrypt.gensalt()).decode()
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = digest
    p._store.write(data)
    settings_mod.reset_for_tests()

    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqttService)

    app = create_app()
    c = TestClient(app)
    c.__enter__()
    c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
    yield c
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def _insert_clip(
    app, clip_id, ts, camera, duration_s, date="2026-06-02",
    event_type="normal",
):
    with app.state.db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at, duration_s) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?,?)",
            (clip_id, f"/rec/{clip_id}.MP4", f"{clip_id}.MP4", date,
             ts, camera, clip_id, event_type, ts, duration_s),
        )


def test_timeline_bad_date_400(logged_in_client):
    r = logged_in_client.get("/api/archive/timeline?date=nonsense")
    assert r.status_code == 400


def test_timeline_day_mode_channels_clips_bounds(logged_in_client):
    app = logged_in_client.app
    _insert_clip(app, 1, 1_717_312_440, "F", 60.0)
    _insert_clip(app, 2, 1_717_312_440, "R", 60.0)
    _insert_clip(app, 3, 1_717_312_500, "F", 60.0)

    r = logged_in_client.get("/api/archive/timeline?date=2026-06-02")
    assert r.status_code == 200
    body = r.json()
    assert [ch["key"] for ch in body["channels"]] == ["front", "rear"]
    assert body["channels"][0]["label"] == "Front"
    assert len(body["clips"]) == 3
    assert body["bounds"]["start_ts"] == 1_717_312_440
    assert body["bounds"]["end_ts"] == 1_717_312_560
    assert body["gps"] is None


def test_timeline_third_camera_channels(logged_in_client):
    """Tele and interior clips each get their own channel, in
    CHANNEL_ORDER after rear. A real device has only one of the
    two, but the endpoint must order any mix it finds — e.g. an
    archive that spans both a telephoto and an interior setup."""
    app = logged_in_client.app
    _insert_clip(app, 1, 1_717_312_440, "F", 60.0)
    _insert_clip(app, 2, 1_717_312_440, "R", 60.0)
    _insert_clip(app, 3, 1_717_312_440, "T", 60.0)
    _insert_clip(app, 4, 1_717_312_440, "I", 60.0)

    r = logged_in_client.get("/api/archive/timeline?date=2026-06-02")
    assert r.status_code == 200
    body = r.json()
    assert [ch["key"] for ch in body["channels"]] == [
        "front", "rear", "tele", "interior",
    ]
    labels = {ch["key"]: ch["label"] for ch in body["channels"]}
    assert labels["tele"] == "Tele"
    assert labels["interior"] == "Interior"
    by_channel = {}
    for c in body["clips"]:
        by_channel.setdefault(c["channel"], []).append(c)
    assert len(by_channel["tele"]) == 1
    assert len(by_channel["interior"]) == 1


def test_timeline_journey_mode_windows_clips(logged_in_client, monkeypatch):
    app = logged_in_client.app
    _insert_clip(app, 1, 1_717_312_440, "F", 60.0)
    _insert_clip(app, 2, 1_717_312_500, "F", 60.0)
    _insert_clip(app, 3, 1_717_313_040, "F", 60.0)

    cap = archive.MAX_JOURNEY_BUFFER_S
    fake_route = {
        "date": "2026-06-02",
        "point_count": 5,
        "journeys": [{
            "start_ts": 1_717_312_440,
            "end_ts": 1_717_312_560,
            "group_start_ts": 1_717_312_440 - cap,
            "group_end_ts": 1_717_312_560 + cap,
        }],
        "stops": [],
    }
    monkeypatch.setattr(
        "web.routers.archive.build_route_payload",
        lambda db, recordings, date, geocoder, places=(): fake_route,
    )

    r = logged_in_client.get("/api/archive/timeline?date=2026-06-02&journey=0")
    assert r.status_code == 200
    body = r.json()
    ids = sorted(c["id"] for c in body["clips"])
    assert ids == [1, 2]
    assert [ch["key"] for ch in body["channels"]] == ["front"]
    assert body["bounds"]["start_ts"] == 1_717_312_440
    assert body["bounds"]["end_ts"] == 1_717_312_560
    assert body["gps"] is not None


def test_timeline_journey_out_of_range_404(logged_in_client, monkeypatch):
    app = logged_in_client.app
    _insert_clip(app, 1, 1_717_312_440, "F", 60.0)
    monkeypatch.setattr(
        "web.routers.archive.build_route_payload",
        lambda db, recordings, date, geocoder, places=(): {
            "date": "2026-06-02", "point_count": 0, "journeys": [], "stops": [],
        },
    )
    r = logged_in_client.get("/api/archive/timeline?date=2026-06-02&journey=0")
    assert r.status_code == 404


def test_timeline_open_logs_clip_count(logged_in_client, caplog):
    """Opening the editor logs how many clips (= filmstrip jobs) it will
    drive, so a NAS CPU spike is traceable from the Logs tab."""
    app = logged_in_client.app
    _insert_clip(app, 1, 1_717_312_440, "F", 60.0)
    _insert_clip(app, 2, 1_717_312_440, "R", 60.0)

    with caplog.at_level(logging.INFO, logger="viofosync.archive"):
        r = logged_in_client.get("/api/archive/timeline?date=2026-06-02")
    assert r.status_code == 200

    msgs = [r.getMessage() for r in caplog.records]
    assert any("timeline: date=2026-06-02" in m and "2 clip(s)" in m for m in msgs)


# --- fallback durations: the editor needs a non-zero duration per clip to
# render blocks and resolve footage at the playhead. Until ffprobe has filled
# duration_s, derive it from the gap to the next clip on the same channel so
# the editor works immediately instead of showing empty tracks.


def test_timeline_fills_missing_duration_from_gap(logged_in_client):
    app = logged_in_client.app
    base = 1_717_312_440
    _insert_clip(app, 1, base, "F", None)
    _insert_clip(app, 2, base + 45, "F", None)
    body = logged_in_client.get("/api/archive/timeline?date=2026-06-02").json()
    clips = sorted(body["clips"], key=lambda c: c["start_ts"])
    assert clips[0]["duration_s"] == 45                       # gap to next clip
    assert clips[1]["duration_s"] == archive.FALLBACK_DEFAULT_S  # last -> default


def test_timeline_caps_fallback_for_large_gap(logged_in_client):
    app = logged_in_client.app
    base = 1_717_312_440
    _insert_clip(app, 1, base, "F", None)
    _insert_clip(app, 2, base + 99_999, "F", None)   # parking-sized gap
    body = logged_in_client.get("/api/archive/timeline?date=2026-06-02").json()
    clips = sorted(body["clips"], key=lambda c: c["start_ts"])
    assert clips[0]["duration_s"] == archive.FALLBACK_MAX_S   # capped


def test_timeline_gap_is_per_channel(logged_in_client):
    app = logged_in_client.app
    base = 1_717_312_440
    _insert_clip(app, 1, base, "F", None)
    _insert_clip(app, 2, base + 10, "R", None)       # other channel, ignored
    _insert_clip(app, 3, base + 60, "F", None)
    body = logged_in_client.get("/api/archive/timeline?date=2026-06-02").json()
    fronts = sorted(
        (c for c in body["clips"] if c["channel"] == "front"),
        key=lambda c: c["start_ts"],
    )
    assert fronts[0]["duration_s"] == 60   # gap to next FRONT, not the rear


def test_timeline_keeps_real_duration(logged_in_client):
    app = logged_in_client.app
    _insert_clip(app, 1, 1_717_312_440, "F", 42.0)
    body = logged_in_client.get("/api/archive/timeline?date=2026-06-02").json()
    assert body["clips"][0]["duration_s"] == 42.0


# --- journey buffer: a GPS journey window sits ~50m inside the real drive
# (the stop radius), so the pull-away clip at the start and the pull-in clip
# at the end fall outside it. Pad each edge outward, but no further than the
# nearest parking-mode clip (the real "car is parked" boundary) and never more
# than MAX_JOURNEY_BUFFER_S — the dashcam can be knocked out of parking mode by
# the car's electrics, so an arbitrary parking->driving switch isn't trusted.


def test_expand_journey_window_no_parking_uses_cap():
    cap = archive.MAX_JOURNEY_BUFFER_S
    s, e = archive.expand_journey_window(1000.0, 2000.0, [])
    assert s == 1000.0 - cap
    assert e == 2000.0 + cap


def test_expand_journey_window_parking_bounds_each_side():
    # Parking ends 30s before the window and starts 40s after it — both
    # inside the cap, so they bound the expansion instead of the cap.
    spans = [(800.0, 970.0), (2040.0, 2200.0)]
    s, e = archive.expand_journey_window(1000.0, 2000.0, spans)
    assert s == 970.0
    assert e == 2040.0


def test_expand_journey_window_distant_parking_falls_back_to_cap():
    cap = archive.MAX_JOURNEY_BUFFER_S
    # Parking is more than the cap away on both sides -> the cap wins.
    spans = [(0.0, 100.0), (5000.0, 5100.0)]
    s, e = archive.expand_journey_window(1000.0, 2000.0, spans)
    assert s == 1000.0 - cap
    assert e == 2000.0 + cap


def test_expand_journey_window_overlapping_parking_no_expansion():
    # A parking clip straddling an edge means we're already at the boundary;
    # don't expand into parked footage.
    spans = [(950.0, 1050.0), (1950.0, 2050.0)]
    s, e = archive.expand_journey_window(1000.0, 2000.0, spans)
    assert s == 1000.0
    assert e == 2000.0


def _fake_journey_route(j0, j1, group_start_ts=None, group_end_ts=None):
    """Return a minimal fake route payload for journey-mode tests.

    ``group_start_ts`` / ``group_end_ts`` default to the full-cap expansion
    when omitted, mirroring what ``_apply_group_windows`` would produce with
    no parking clips on the day.
    """
    cap = archive.MAX_JOURNEY_BUFFER_S
    return {
        "date": "2026-06-02",
        "point_count": 5,
        "journeys": [{
            "start_ts": j0,
            "end_ts": j1,
            "group_start_ts": j0 - cap if group_start_ts is None else group_start_ts,
            "group_end_ts": j1 + cap if group_end_ts is None else group_end_ts,
        }],
        "stops": [],
    }


def test_timeline_journey_buffer_includes_adjacent_clips(
    logged_in_client, monkeypatch,
):
    """The pull-away and pull-in clips, which fall just outside the raw GPS
    window, are pulled in by the buffer and the bounds grow to cover them."""
    app = logged_in_client.app
    j0, j1 = 1_000_000, 1_000_300
    _insert_clip(app, 1, j0 - 60, "F", 30.0)   # pull-away [j0-60, j0-30]
    _insert_clip(app, 2, j0, "F", 300.0)       # the journey itself [j0, j1]
    _insert_clip(app, 3, j1 + 30, "F", 40.0)   # pull-in [j1+30, j1+70]
    monkeypatch.setattr(
        "web.routers.archive.build_route_payload",
        lambda db, recordings, date, geocoder, places=(): _fake_journey_route(j0, j1),
    )

    body = logged_in_client.get(
        "/api/archive/timeline?date=2026-06-02&journey=0"
    ).json()
    assert sorted(c["id"] for c in body["clips"]) == [1, 2, 3]
    assert body["bounds"]["start_ts"] == j0 - 60   # back to pull-away start
    assert body["bounds"]["end_ts"] == j1 + 70     # forward to pull-in end


def test_timeline_journey_buffer_stops_at_parking_clip(
    logged_in_client, monkeypatch,
):
    """A parking-mode clip in the group window caps how far the editor's start
    extends — the parking boundary (80 s back) is already baked into
    group_start_ts, so the editor picks it up without a second parking query."""
    app = logged_in_client.app
    j0, j1 = 1_000_000, 1_000_300
    _insert_clip(app, 1, j0 - 150, "F", 70.0, event_type="parking")  # [-150,-80]
    _insert_clip(app, 2, j0 - 80, "F", 50.0)   # pull-away [j0-80, j0-30]
    _insert_clip(app, 3, j0, "F", 300.0)       # the journey itself
    # group_start_ts is bounded by the parking clip (ends at j0-80); the route
    # payload carries this pre-computed value — the editor must not re-expand.
    monkeypatch.setattr(
        "web.routers.archive.build_route_payload",
        lambda db, recordings, date, geocoder, places=(): _fake_journey_route(
            j0, j1, group_start_ts=j0 - 80,
        ),
    )

    body = logged_in_client.get(
        "/api/archive/timeline?date=2026-06-02&journey=0"
    ).json()
    # Editor window starts at the parking boundary baked into group_start_ts,
    # then clamped to the earliest clip (also j0-80 here) — not the full cap.
    assert body["bounds"]["start_ts"] == j0 - 80


# ---------------------------------------------------------------------------
# Queued parking clip bounds the timeline editor window
# ---------------------------------------------------------------------------

def _moving_gpx(lat0, lon0, n, start, step_lon=0.0008, dt_s=60):
    """A GPX track of ``n`` points moving steadily east — no 5-min dwell —
    so aggregate_day yields exactly one journey."""
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


def _seed_gpx_clip(app, recordings_dir, clip_id, fn, day, start_dt):
    """Seed a clip in clip_index with a real GPX sidecar so build_route_payload
    detects a journey for the day."""
    daydir = Path(recordings_dir) / day
    daydir.mkdir(parents=True, exist_ok=True)
    path = daydir / fn
    path.write_bytes(b"x")
    (daydir / (fn + ".gpx")).write_text(
        _moving_gpx(53.0, -2.0, 6, start_dt)
    )
    ts = int(start_dt.replace(tzinfo=_dt.UTC).timestamp())
    with app.state.db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,1,?)",
            (clip_id, str(path), fn, day, ts, "F", clip_id, "normal", ts),
        )


def test_timeline_queued_parking_bounds_editor_window(
    logged_in_client, tmp_recordings_dir, monkeypatch,
):
    """A parking clip queued but not yet downloaded (download_queue only, no
    clip_index entry, no local file) must bound the editor's journey window.

    Under the OLD code, get_timeline re-ran expand_journey_window using a
    clip_index-only parking query — the queued clip was invisible, so the
    window expanded the full 120 s cap backward past it.  Under the new code,
    get_timeline reads the journey's group_start_ts (computed by
    _apply_group_windows from the unioned day_parking_spans source) and uses
    it directly — one definition of the journey's edges, shared with the grid.
    """
    app = logged_in_client.app
    day = "2026-06-02"
    start_dt = _dt.datetime(2026, 6, 2, 8, 0, 0)
    fn = "2026_0602_080000_0001F.MP4"

    # Seed a driving clip with a real GPX sidecar so build_route_payload
    # detects exactly one journey.
    _seed_gpx_clip(app, tmp_recordings_dir, 1, fn, day, start_dt)

    # The journey's raw GPS start (before _apply_group_windows pads it).
    route0 = archive.build_route_payload(
        app.state.db, str(tmp_recordings_dir), day, None
    )
    assert route0["journeys"], "expected one journey from the moving track"
    raw_start_ts = route0["journeys"][0]["start_ts"]

    # Seed a parking clip in download_queue only (no clip_index row, no file).
    # Place it 60 s before the journey's GPS start — inside the 120 s cap —
    # so it bounds the group window instead of the cap taking effect.
    prk_recorded_at = int(raw_start_ts) - 60
    with app.state.db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, event_type, recorded_at, enqueued_at) "
            "VALUES (?,?,?,?,?,0)",
            ("2026_0602_075900_0009PF.MP4", "/DCIM/Movie",
             "pending", "parking", prk_recorded_at),
        )

    # Insert a normal clip that sits INSIDE the old (un-bounded) cap but
    # BEFORE the queued parking boundary — i.e. in the gap between
    # (raw_start_ts - 120) and prk_recorded_at.  Under the old code this clip
    # falls inside the re-padded window (start = raw_start_ts - 120) so
    # clip-coverage clamping pulls the editor start back to here.  Under the
    # new code the group window starts at prk_recorded_at so this clip is
    # excluded (ts + dur < prk_recorded_at) and the editor start clamps to
    # the earliest included clip (raw_start_ts), which is >= group_start_ts.
    clip_before_parking = int(raw_start_ts) - 110  # inside old cap, before queued parking
    with app.state.db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at, duration_s) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?,?)",
            (2, f"/rec/pre_{clip_before_parking}.MP4", "pre.MP4", day,
             clip_before_parking, "F", 2, "normal", clip_before_parking, 5.0),
        )

    # Step 3: group_start_ts must be bounded by the queued parking clip
    # (its recorded_at, which day_parking_spans treats as a point span).
    route = archive.build_route_payload(
        app.state.db, str(tmp_recordings_dir), day, None
    )
    j = route["journeys"][0]
    assert j["group_start_ts"] == prk_recorded_at, (
        f"group_start_ts ({j['group_start_ts']}) should equal the queued "
        f"parking clip's recorded_at ({prk_recorded_at}), not the full cap "
        f"({raw_start_ts - archive.MAX_JOURNEY_BUFFER_S})"
    )
    assert j["group_start_ts"] > raw_start_ts - archive.MAX_JOURNEY_BUFFER_S, (
        "group_start_ts must be bounded tighter than the full cap — "
        "the queued parking clip must be honoured"
    )

    # Step 4: get_timeline must use the group window, not re-expand raw ts.
    r = logged_in_client.get(
        f"/api/archive/timeline?date={day}&journey=0"
    )
    assert r.status_code == 200
    payload = r.json()
    editor_start = payload["bounds"]["start_ts"]
    # The editor's start_ts must not reach past the queued parking boundary.
    # Under the old code it would expand to raw_start_ts - 120 (the full cap),
    # which is beyond the parking boundary — this assertion catches that.
    assert editor_start >= j["group_start_ts"], (
        f"editor start_ts ({editor_start}) went behind group_start_ts "
        f"({j['group_start_ts']}), suggesting a raw re-pad bypassed the "
        f"queued parking boundary"
    )
