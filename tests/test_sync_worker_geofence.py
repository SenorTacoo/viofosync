"""SyncWorker geofence pass wiring."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from web.db import Database
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker
from web.settings import Place


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "v.db"))


def _utc(h, m, s=0):
    return _dt.datetime(2026, 6, 18, h, m, s, tzinfo=_dt.UTC)


HOME_LAT, HOME_LON = 53.1, -2.0


def _gpx(points) -> str:
    pts = "".join(
        f'<trkpt lat="{lat}" lon="{lon}"><time>{t}</time>'
        f"<speed>0</speed><course>0</course></trkpt>"
        for t, lat, lon in points
    )
    return (
        '<?xml version="1.0"?><gpx version="1.0" '
        'xmlns="http://www.topografix.com/GPX/1/0">'
        f"<trk><trkseg>{pts}</trkseg></trk></gpx>"
    )


def _provider(rec, **kw):
    snap = SimpleNamespace(
        recordings=str(rec),
        gps_triage=kw.get("gps_triage", True),
        locations=kw.get(
            "locations", (Place("Home", HOME_LAT, HOME_LON, 30, True),)
        ),
    )
    return SimpleNamespace(get=lambda: snap)


def _seed_home_clip(db, rec):
    dwell = [
        (_utc(20, m).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON)
        for m in (0, 2, 4, 6)
    ]
    (rec / ".triage").mkdir(parents=True, exist_ok=True)
    fn = "2026_0618_200300_0001F.MP4"
    (rec / ".triage" / (fn + ".gpx")).write_text(_gpx(dwell))
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, recorded_at, triaged_at, gps_points, "
            " enqueued_at) VALUES (?,?,?,?,?,?,0)",
            (fn, "/DCIM/Movie", "pending", int(_utc(20, 3).timestamp()), 1, 5),
        )
    return fn


def _state(db, fn):
    with db.conn() as c:
        return c.execute(
            "SELECT state FROM download_queue WHERE filename=?", (fn,)
        ).fetchone()["state"]


async def test_geofence_pass_skips_home_clip(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    fn = _seed_home_clip(db, rec)
    worker = SyncWorker(db, _provider(rec), Hub())
    await worker._run_geofence_pass()
    assert _state(db, fn) == "skipped"


async def test_geofence_pass_noop_when_not_excluded(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    fn = _seed_home_clip(db, rec)
    worker = SyncWorker(
        db, _provider(rec, locations=(Place("Home", HOME_LAT, HOME_LON, 30, False),)),
        Hub(),
    )
    await worker._run_geofence_pass()
    assert _state(db, fn) == "pending"


async def test_geofence_pass_noop_when_triage_off(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    fn = _seed_home_clip(db, rec)
    worker = SyncWorker(db, _provider(rec, gps_triage=False), Hub())
    await worker._run_geofence_pass()
    assert _state(db, fn) == "pending"


async def test_geofence_pass_incremental_caches_day(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    fn = _seed_home_clip(db, rec)
    worker = SyncWorker(db, _provider(rec), Hub())

    await worker._run_geofence_pass(seen=worker._geofence_seen)
    assert _state(db, fn) == "skipped"
    assert worker._geofence_seen == {"2026-06-18": 1}

    # Re-arm as pending; unchanged signature -> cached day skipped, not re-skipped.
    with db.write() as c:
        c.execute("UPDATE download_queue SET state='pending' WHERE filename=?", (fn,))
    await worker._run_geofence_pass(seen=worker._geofence_seen)
    assert _state(db, fn) == "pending"


async def test_full_pass_clears_cache(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    fn = _seed_home_clip(db, rec)
    worker = SyncWorker(db, _provider(rec), Hub())
    worker._geofence_seen["2026-06-18"] = 1  # stale entry

    await worker._run_geofence_pass()   # seen=None -> full sweep, clears cache
    assert _state(db, fn) == "skipped"
    assert worker._geofence_seen == {}
