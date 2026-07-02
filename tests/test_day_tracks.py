"""day_gpx_paths: the shared GPX path set for a day (sidecars + skeletons)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from web.db import Database
from web.services import day_tracks


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "v.db"))


def _index_downloaded(db: Database, path: str, day: str, ts: int) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " has_gpx, scanned_at) VALUES (?,?,?,?,?,?,?,0)",
            (path, os.path.basename(path), day, ts, "F", 1, 1),
        )


def _queue_triaged(db: Database, filename: str, *, state="pending") -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, triaged_at, gps_points, enqueued_at) "
            "VALUES (?,?,?,?,?,0)",
            (filename, "/DCIM/Movie", state, 1, 5),
        )


def test_includes_sidecars_and_skeletons(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    (rec / ".triage").mkdir(parents=True)
    # A downloaded clip's sidecar.
    _index_downloaded(db, str(rec / "2026_0618_200000_0001F.MP4"),
                      "2026-06-18", 1_000)
    # A queued, triaged clip's skeleton.
    sk = rec / ".triage" / "2026_0618_200100_0002F.MP4.gpx"
    sk.write_text("<gpx/>")
    _queue_triaged(db, "2026_0618_200100_0002F.MP4")

    paths = day_tracks.day_gpx_paths(db, str(rec), "2026-06-18")
    assert str(rec / "2026_0618_200000_0001F.MP4.gpx") in paths
    assert str(sk) in paths


def test_skipped_skeletons_only_with_broader_states(db: Database, tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    (rec / ".triage").mkdir(parents=True)
    sk = rec / ".triage" / "2026_0618_200100_0002F.MP4.gpx"
    sk.write_text("<gpx/>")
    _queue_triaged(db, "2026_0618_200100_0002F.MP4", state="skipped")

    # Default (pending/failed) excludes a skipped clip's skeleton…
    assert str(sk) not in day_tracks.day_gpx_paths(db, str(rec), "2026-06-18")
    # …but the geofence state set includes it.
    paths = day_tracks.day_gpx_paths(
        db, str(rec), "2026-06-18",
        queue_states=("pending", "failed", "skipped", "downloading"),
    )
    assert str(sk) in paths


def test_day_parking_spans_unions_clip_index_and_queue(db: Database) -> None:
    """Parking spans come from BOTH downloaded clips (clip_index, with real
    duration) and queued-not-yet-downloaded clips (download_queue, a point at
    recorded_at) — so the journey-window parking bound works in triage too."""
    day = "2026-06-18"
    with db.write() as c:
        # Downloaded parking clip: span = (ts, ts + duration_s)
        c.execute(
            "INSERT INTO clip_index (path, basename, group_name, timestamp, "
            " camera, sequence, event_type, duration_s, has_gpx, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,0,0)",
            ("/x/2026_0618_010000_0001PF.MP4", "2026_0618_010000_0001PF.MP4",
             day, 1000, "PF", 1, "parking", 1800.0),
        )
        # Queued (not downloaded) parking clip: point span at recorded_at
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, "
            " event_type, recorded_at, enqueued_at) VALUES (?,?,?,?,?,0)",
            ("2026_0618_020000_0009PF.MP4", "/DCIM/Movie", "pending",
             "parking", 5000),
        )
        # Non-parking rows in each table must be excluded.
        c.execute(
            "INSERT INTO clip_index (path, basename, group_name, timestamp, "
            " camera, sequence, event_type, has_gpx, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,0,0)",
            ("/x/2026_0618_030000_0003F.MP4", "2026_0618_030000_0003F.MP4",
             day, 9000, "F", 3, "normal"),
        )
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, "
            " event_type, recorded_at, enqueued_at) VALUES (?,?,?,?,?,0)",
            ("2026_0618_040000_0004F.MP4", "/DCIM/Movie", "pending",
             "normal", 12000),
        )

    spans = day_tracks.day_parking_spans(db, day)
    assert (1000, 2800.0) in spans      # clip_index: ts .. ts+duration
    assert (5000, 5000) in spans        # queue: point at recorded_at
    assert len(spans) == 2              # only the two parking clips
