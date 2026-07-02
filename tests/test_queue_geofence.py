"""Geofence queue provenance: skip_reason, geofence_skip, release stamping."""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.services import queue as q


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "v.db"))


def test_download_queue_has_geofence_columns(db: Database) -> None:
    with db.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(download_queue)")}
    assert "skip_reason" in cols
    assert "geofence_released_at" in cols


def test_migration_is_idempotent(tmp_path: Path) -> None:
    p = str(tmp_path / "v.db")
    Database(p)            # creates + migrates
    db2 = Database(p)      # second open re-runs _migrate; must not raise
    with db2.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(download_queue)")}
    assert "skip_reason" in cols
    assert "geofence_released_at" in cols


def _rows(db: Database) -> dict:
    with db.conn() as c:
        return {
            r["filename"]: dict(r)
            for r in c.execute("SELECT * FROM download_queue").fetchall()
        }


def _seed(db: Database, filename: str, *, state: str = "pending",
          skip_reason=None, source_dir="/DCIM/Movie", recorded_at=1_000,
          released=None) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, skip_reason, recorded_at, "
            " geofence_released_at, enqueued_at) VALUES (?,?,?,?,?,?,0)",
            (filename, source_dir, state, skip_reason, recorded_at, released),
        )


def test_skip_sets_user_reason(db: Database) -> None:
    _seed(db, "p_0001F.MP4", state="pending")
    q.skip(db, ["p_0001F.MP4"])
    row = _rows(db)["p_0001F.MP4"]
    assert row["state"] == "skipped"
    assert row["skip_reason"] == "user"


def test_unskip_stamps_release_only_for_geofence(db: Database) -> None:
    _seed(db, "g_0001F.MP4", state="skipped", skip_reason="geofence")
    _seed(db, "u_0001F.MP4", state="skipped", skip_reason="user")
    q.unskip(db, ["g_0001F.MP4", "u_0001F.MP4"])
    rows = _rows(db)
    assert rows["g_0001F.MP4"]["state"] == "pending"
    assert rows["g_0001F.MP4"]["skip_reason"] is None
    assert rows["g_0001F.MP4"]["geofence_released_at"] is not None
    assert rows["u_0001F.MP4"]["state"] == "pending"
    assert rows["u_0001F.MP4"]["geofence_released_at"] is None


def test_geofence_skip_flips_only_pending_unreleased(db: Database) -> None:
    _seed(db, "a_0001F.MP4", state="pending")
    _seed(db, "b_0001F.MP4", state="done")
    _seed(db, "c_0001F.MP4", state="pending", released=12345)
    n = q.geofence_skip(db, ["a_0001F.MP4", "b_0001F.MP4", "c_0001F.MP4"])
    rows = _rows(db)
    assert n == 1
    assert rows["a_0001F.MP4"]["state"] == "skipped"
    assert rows["a_0001F.MP4"]["skip_reason"] == "geofence"
    assert rows["b_0001F.MP4"]["state"] == "done"      # not pending
    assert rows["c_0001F.MP4"]["state"] == "pending"   # released, untouched


def test_geofence_candidates_day_and_guards(db: Database) -> None:
    # Real Viofo-prefixed filenames so the day expression matches.
    _seed(db, "2026_0618_200000_0001F.MP4", state="pending", recorded_at=6_000)
    _seed(db, "2026_0618_200000_0002EF.MP4", state="pending", recorded_at=6_000)
    _seed(db, "2026_0618_200000_0003F.MP4", state="pending",
          source_dir="/DCIM/Movie/RO", recorded_at=6_000)
    _seed(db, "2026_0618_200000_0004F.MP4", state="pending",
          recorded_at=6_000, released=1)
    _seed(db, "2026_0618_200000_0005F.MP4", state="done", recorded_at=6_000)
    names = {c["filename"] for c in q.geofence_candidates(db, "2026-06-18")}
    assert names == {"2026_0618_200000_0001F.MP4"}   # only the plain pending clip


def test_pending_days(db: Database) -> None:
    _seed(db, "2026_0618_200000_0001F.MP4", state="pending")
    _seed(db, "2026_0619_080000_0001F.MP4", state="pending")
    _seed(db, "2026_0617_080000_0001F.MP4", state="done")
    assert set(q.pending_days(db)) == {"2026-06-18", "2026-06-19"}


def test_geofence_skipped_by_day_counts_front_geofence_only(db: Database) -> None:
    from web.routers.archive import geofence_skipped_by_day
    # Two geofence-skipped pairs on 2026-06-19 (front clips counted only).
    _seed(db, "2026_0619_082048_001F.MP4", state="skipped", skip_reason="geofence")
    _seed(db, "2026_0619_082048_001R.MP4", state="skipped", skip_reason="geofence")
    _seed(db, "2026_0619_082148_002F.MP4", state="skipped", skip_reason="geofence")
    # A user skip and a pending clip must NOT count.
    _seed(db, "2026_0619_083000_003F.MP4", state="skipped", skip_reason="user")
    _seed(db, "2026_0619_084000_004F.MP4", state="pending")
    # A different day.
    _seed(db, "2026_0620_090000_005F.MP4", state="skipped", skip_reason="geofence")
    assert geofence_skipped_by_day(db) == {"2026-06-19": 2, "2026-06-20": 1}


def test_geofence_skipped_by_day_respects_date_range(db: Database) -> None:
    from web.routers.archive import geofence_skipped_by_day
    _seed(db, "2026_0619_082048_001F.MP4", state="skipped", skip_reason="geofence")
    _seed(db, "2026_0620_090000_002F.MP4", state="skipped", skip_reason="geofence")
    assert geofence_skipped_by_day(db, "2026-06-20", None) == {"2026-06-20": 1}
    assert geofence_skipped_by_day(db, None, "2026-06-19") == {"2026-06-19": 1}
