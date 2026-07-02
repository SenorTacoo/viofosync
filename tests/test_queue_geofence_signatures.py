"""queue.geofence_day_signatures: per-day triaged-skeleton counts."""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.services import queue

DETECT = ("pending", "failed", "skipped", "downloading")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "v.db"))


def _seed(db, filename, *, state="pending", triaged=True, gps=5):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, recorded_at, triaged_at, "
            " gps_points, enqueued_at) VALUES (?,?,?,?,?,?,0)",
            (filename, "/DCIM/Movie", state, 0,
             1 if triaged else None, gps if triaged else None),
        )


def test_counts_triaged_with_gps_per_day(db: Database) -> None:
    _seed(db, "2026_0618_200000_0001F.MP4")            # day A, counts
    _seed(db, "2026_0618_200100_0002F.MP4")            # day A, counts
    _seed(db, "2026_0618_200200_0003F.MP4", gps=0)     # triaged, no fix -> excluded
    _seed(db, "2026_0618_200300_0004F.MP4", triaged=False)  # untriaged -> excluded
    _seed(db, "2026_0619_080000_0005F.MP4")            # day B, counts
    sigs = queue.geofence_day_signatures(db, DETECT)
    assert sigs == {"2026-06-18": 2, "2026-06-19": 1}


def test_skipped_clip_still_counts(db: Database) -> None:
    _seed(db, "2026_0618_200000_0001F.MP4", state="skipped")
    assert queue.geofence_day_signatures(db, DETECT) == {"2026-06-18": 1}


def test_empty_db_is_empty(db: Database) -> None:
    assert queue.geofence_day_signatures(db, DETECT) == {}
