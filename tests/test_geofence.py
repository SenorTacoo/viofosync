"""Geofence detection: home_stops, evaluate_day, sweep_all."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from web.db import Database
from web.services import geofence
from web.services.gps import Stop
from web.settings import Place


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "v.db"))


def _stop(lat, lon, start, end):
    return Stop(
        start_time=_dt.datetime.fromtimestamp(start, _dt.UTC),
        end_time=_dt.datetime.fromtimestamp(end, _dt.UTC),
        center_lat=lat, center_lon=lon, point_count=10,
    )


def test_home_stops_filters_by_radius() -> None:
    home = _stop(53.1000, -2.0000, 0, 600)         # at home
    away = _stop(53.2000, -2.0000, 0, 600)         # ~11 km north
    zones = (Place("Home", 53.1000, -2.0000, 30, True),)
    out = geofence.home_stops([home, away], zones)
    assert out == [home]


def test_home_stops_empty_zones() -> None:
    assert geofence.home_stops([_stop(0, 0, 0, 1)], ()) == []


# ---- evaluate_day fixtures -------------------------------------------------

HOME_LAT, HOME_LON = 53.1000, -2.0000


def _gpx(points) -> str:
    pts = "".join(
        f'<trkpt lat="{lat}" lon="{lon}">'
        f"<time>{t}</time><speed>0</speed><course>0</course></trkpt>"
        for t, lat, lon in points
    )
    return (
        '<?xml version="1.0"?><gpx version="1.0" '
        'xmlns="http://www.topografix.com/GPX/1/0">'
        f"<trk><trkseg>{pts}</trkseg></trk></gpx>"
    )


def _utc(h, m, s=0):
    return _dt.datetime(2026, 6, 18, h, m, s, tzinfo=_dt.UTC)


def _seed(db, filename, *, state="pending", source_dir="/DCIM/Movie",
          recorded_at=None, triaged=True, released=None) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, recorded_at, triaged_at, "
            " gps_points, geofence_released_at, enqueued_at) VALUES (?,?,?,?,?,?,?,0)",
            (filename, source_dir, state, recorded_at,
             1 if triaged else None, 5 if triaged else None, released),
        )


def _write_skeleton(rec: Path, filename: str, points) -> None:
    (rec / ".triage").mkdir(parents=True, exist_ok=True)
    (rec / ".triage" / (filename + ".gpx")).write_text(_gpx(points))


def test_evaluate_day_skips_in_home_dwell(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    # A 7-minute dwell at home, split across the queued clip's skeleton.
    dwell = [
        (_utc(20, 0).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON),
        (_utc(20, 1).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON),
        (_utc(20, 3).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON),
        (_utc(20, 5).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON),
        (_utc(20, 7).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON),
    ]
    fn_f = "2026_0618_200200_0001F.MP4"
    fn_r = "2026_0618_200200_0002R.MP4"
    _write_skeleton(rec, fn_f, dwell)
    _seed(db, fn_f, recorded_at=int(_utc(20, 2).timestamp()))
    # Rear clip shares the time window but has no skeleton of its own.
    _seed(db, fn_r, recorded_at=int(_utc(20, 2).timestamp()), triaged=False)

    zones = (Place("Home", HOME_LAT, HOME_LON, 30, True),)
    skipped = geofence.evaluate_day(db, str(rec), "2026-06-18", zones)
    assert set(skipped) == {fn_f, fn_r}
    with db.conn() as c:
        states = {
            r["filename"]: (r["state"], r["skip_reason"])
            for r in c.execute("SELECT filename, state, skip_reason FROM download_queue")
        }
    assert states[fn_f] == ("skipped", "geofence")
    assert states[fn_r] == ("skipped", "geofence")


def test_evaluate_day_keeps_before_window_and_guarded(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    dwell = [
        (_utc(20, 0).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON),
        (_utc(20, 2).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON),
        (_utc(20, 4).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON),
        (_utc(20, 6).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON),
    ]
    fn_in = "2026_0618_200300_0001F.MP4"
    fn_before = "2026_0618_195000_0002F.MP4"   # before the dwell starts
    fn_ro = "2026_0618_200300_0003F.MP4"        # in window but RO
    fn_rel = "2026_0618_200300_0004F.MP4"       # in window but released
    _write_skeleton(rec, fn_in, dwell)
    _seed(db, fn_in, recorded_at=int(_utc(20, 3).timestamp()))
    _seed(db, fn_before, recorded_at=int(_utc(19, 50).timestamp()))
    _seed(db, fn_ro, source_dir="/DCIM/Movie/RO",
          recorded_at=int(_utc(20, 3).timestamp()))
    _seed(db, fn_rel, recorded_at=int(_utc(20, 3).timestamp()), released=1)

    zones = (Place("Home", HOME_LAT, HOME_LON, 30, True),)
    skipped = geofence.evaluate_day(db, str(rec), "2026-06-18", zones)
    assert set(skipped) == {fn_in}


def test_sweep_all_covers_pending_days(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    for day, hh in (("2026_0618", 20), ("2026_0619", 8)):
        dwell = [
            (_dt.datetime(2026, int(day[5:7]), int(day[7:9]), hh, m,
                          tzinfo=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
             HOME_LAT, HOME_LON)
            for m in (0, 2, 4, 6)
        ]
        fn = f"{day}_{hh:02d}0300_0001F.MP4"
        _write_skeleton(rec, fn, dwell)
        _seed(db, fn, recorded_at=int(
            _dt.datetime(2026, int(day[5:7]), int(day[7:9]), hh, 3,
                         tzinfo=_dt.UTC).timestamp()))
    zones = (Place("Home", HOME_LAT, HOME_LON, 30, True),)
    assert geofence.sweep_all(db, str(rec), zones) == 2


def test_sweep_all_no_zones_is_noop(db: Database, tmp_path: Path) -> None:
    assert geofence.sweep_all(db, str(tmp_path), ()) == 0


def test_detect_states_match_skeleton_keep_states() -> None:
    # The states the detector reads MUST equal the states whose skeletons the
    # orphan sweep keeps, or skipped dwells get deleted out from under it.
    from web.services import triage
    assert set(geofence._DETECT_STATES) == set(triage.SKELETON_KEEP_STATES)


def test_evaluate_day_skips_leading_clip_before_first_fix(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    # Dwell skeleton: first GPS fix at 20:00 (this defines the stop's start).
    dwell = [
        (_utc(20, m).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON)
        for m in (0, 2, 4, 6)
    ]
    fn_dwell = "2026_0618_200000_0001F.MP4"
    _write_skeleton(rec, fn_dwell, dwell)
    _seed(db, fn_dwell, recorded_at=int(_utc(20, 0).timestamp()))

    # Leading clip: recorded 30 s BEFORE the first fix (GPS-lock lag) -> must
    # now be skipped (its footage is the start of the same home dwell).
    fn_lead = "2026_0618_195930_0009F.MP4"
    _seed(db, fn_lead, recorded_at=int(_utc(19, 59, 30).timestamp()))

    # A clip 5 min before the dwell is genuinely outside it -> must be kept.
    fn_far = "2026_0618_195500_0010F.MP4"
    _seed(db, fn_far, recorded_at=int(_utc(19, 55, 0).timestamp()))

    zones = (Place("Home", HOME_LAT, HOME_LON, 30, True),)
    skipped = set(geofence.evaluate_day(db, str(rec), "2026-06-18", zones))
    assert fn_dwell in skipped
    assert fn_lead in skipped      # leading clip now caught by the pad
    assert fn_far not in skipped   # pad is bounded — far clip untouched


def test_sweep_all_seen_skips_unchanged_then_reevaluates(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    dwell = [
        (_utc(20, m).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON)
        for m in (0, 2, 4, 6)
    ]
    fn_a = "2026_0618_200300_0001F.MP4"
    _write_skeleton(rec, fn_a, dwell)
    _seed(db, fn_a, recorded_at=int(_utc(20, 3).timestamp()))
    zones = (Place("Home", HOME_LAT, HOME_LON, 30, True),)

    seen: dict[str, int] = {}
    assert geofence.sweep_all(db, str(rec), zones, seen=seen) == 1
    assert seen == {"2026-06-18": 1}
    # Nothing new triaged -> day skipped (no re-eval, no new skips).
    assert geofence.sweep_all(db, str(rec), zones, seen=seen) == 0
    # A newly-triaged home clip bumps the signature -> day re-evaluated.
    fn_b = "2026_0618_200400_0002F.MP4"
    _write_skeleton(rec, fn_b, dwell)
    _seed(db, fn_b, recorded_at=int(_utc(20, 4).timestamp()))
    assert geofence.sweep_all(db, str(rec), zones, seen=seen) == 1
    assert seen == {"2026-06-18": 2}


def test_sweep_all_seen_none_is_full_sweep(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    dwell = [
        (_utc(20, m).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON)
        for m in (0, 2, 4, 6)
    ]
    fn = "2026_0618_200300_0001F.MP4"
    _write_skeleton(rec, fn, dwell)
    _seed(db, fn, recorded_at=int(_utc(20, 3).timestamp()))
    zones = (Place("Home", HOME_LAT, HOME_LON, 30, True),)
    assert geofence.sweep_all(db, str(rec), zones) == 1


def test_evaluate_day_keeps_departure_clip_in_journey(db: Database, tmp_path: Path) -> None:
    """A clip that dwells in the home zone but also falls inside a journey's
    padded window is the pull-away/pull-in footage — the journey wins over the
    home dwell, so it must NOT be skipped (mirrors the archive grid)."""
    rec = tmp_path / "rec"
    # Home dwell 20:00-20:06 (one skeleton), then a drive moving east from 20:06:30.
    dwell = [(_utc(20, m).strftime("%Y-%m-%dT%H:%M:%SZ"), HOME_LAT, HOME_LON)
             for m in range(0, 7)]
    drive = []
    for i in range(8):
        t = _utc(20, 6, 30) + _dt.timedelta(seconds=30 * i)   # 20:06:30..20:10:00
        drive.append((t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      HOME_LAT, HOME_LON + 0.001 * (i + 1)))   # ~67 m steps east
    _write_skeleton(rec, "2026_0618_200000_0001F.MP4", dwell)
    _write_skeleton(rec, "2026_0618_200630_0005F.MP4", drive)
    _seed(db, "2026_0618_200000_0001F.MP4", recorded_at=int(_utc(20, 0).timestamp()))
    _seed(db, "2026_0618_200630_0005F.MP4", recorded_at=int(_utc(20, 6, 30).timestamp()))

    fn_dwell = "2026_0618_200100_0002F.MP4"        # deep in the dwell -> skip
    fn_departure = "2026_0618_200530_0003F.MP4"    # dwell edge + in journey pad -> keep
    _seed(db, fn_dwell, recorded_at=int(_utc(20, 1).timestamp()))
    _seed(db, fn_departure, recorded_at=int(_utc(20, 5, 30).timestamp()))

    zones = (Place("Home", HOME_LAT, HOME_LON, 30, True),)
    skipped = geofence.evaluate_day(db, str(rec), "2026-06-18", zones)

    assert fn_dwell in skipped              # genuinely parked at home
    assert fn_departure not in skipped      # part of the drive -> journey wins
