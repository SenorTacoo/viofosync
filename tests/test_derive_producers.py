# tests/test_derive_producers.py
from __future__ import annotations

from web.db import Database
from web.services import derive_queue as dq
from web.services import retention, scanner


def test_scan_enqueues_indexed_clips(tmp_path, tmp_recordings_dir):
    db = Database(str(tmp_path / "t.db"))
    # "daily" grouping puts files under <recordings>/YYYY-MM-DD/
    day = tmp_recordings_dir / "2026-06-16"
    day.mkdir(parents=True)
    (day / "2026_0616_120000_0001F.MP4").write_bytes(b"x")
    scanner.scan(db, str(tmp_recordings_dir), "daily", None, None)
    assert dq.pending_count(db) >= 1


def test_retention_removes_derive_row(tmp_path):
    """When retention prunes a clip, its derive_queue row must be removed."""
    db = Database(str(tmp_path / "t.db"))
    clip_path = str(tmp_path / "2024_0101_120000_0001F.MP4")
    open(clip_path, "wb").close()  # create an actual file so _delete_clip_files can run

    # Insert a clip_index row with an old timestamp (epoch 0 → always older than max_days=1)
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index (id, path, basename, timestamp, camera, "
            "sequence, scanned_at) VALUES (42, ?, '2024_0101_120000_0001F.MP4', "
            "0, 'F', 1, 0)",
            (clip_path,),
        )

    dq.enqueue(db, 42, priority=1, now=1)
    assert dq.pending_count(db) == 1

    retention.sweep(
        db,
        str(tmp_path),
        max_days=1,
        disk_pct=0,
        protect_ro=False,
        _now=9999999999,  # far future so epoch-0 clip is definitely old
    )

    assert dq.pending_count(db) == 0
