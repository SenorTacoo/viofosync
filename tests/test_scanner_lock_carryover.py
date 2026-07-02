"""scanner.scan must carry a queued locked=1 into clip_index.

Two invariants:
1. Carry-over: a download_queue row with locked=1 propagates into
   clip_index.locked=1 after a scan that indexes the matching file.
2. Preserve: if clip_index.locked is already 1 (set directly on the
   index, e.g. by the archive UI) a rescan must NOT clear it, even
   when download_queue.locked=0 for that file.
"""
from __future__ import annotations

from pathlib import Path

from web.db import Database
from web.services import scanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILENAME = "2026_0603_082421_0001F.MP4"
_GROUP = "2026-06-03"


def _make_clip_on_disk(dest: Path) -> Path:
    """Create a real Viofo-named file so the scanner can index it."""
    day = dest / _GROUP
    day.mkdir(parents=True, exist_ok=True)
    f = day / _FILENAME
    f.write_bytes(b"\x00" * 16)
    return f


def _insert_queue_row(db: Database, *, locked: int = 0) -> None:
    """Add a download_queue row for _FILENAME."""
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, priority, attempts, enqueued_at, locked) "
            "VALUES (?, '/Movie/Normal/', 'done', 0, 0, 0, ?)",
            (_FILENAME, locked),
        )


def _get_clip_locked(db: Database) -> int | None:
    with db.conn() as c:
        row = c.execute(
            "SELECT locked FROM clip_index WHERE basename = ?", (_FILENAME,)
        ).fetchone()
    return row["locked"] if row else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_scan_carries_queue_lock_into_clip_index(tmp_path: Path) -> None:
    """A queued clip with locked=1 must have clip_index.locked=1 after scan."""
    db = Database(str(tmp_path / "t.db"))
    dest = tmp_path / "recordings"
    _make_clip_on_disk(dest)
    _insert_queue_row(db, locked=1)

    scanner.scan(db, str(dest), "daily")

    assert _get_clip_locked(db) == 1, (
        "clip_index.locked should be 1 when download_queue.locked=1"
    )


def test_scan_preserves_index_lock_on_rescan(tmp_path: Path) -> None:
    """A lock set directly on clip_index must survive a rescan even when
    download_queue.locked=0 for that file."""
    db = Database(str(tmp_path / "t.db"))
    dest = tmp_path / "recordings"
    _make_clip_on_disk(dest)
    _insert_queue_row(db, locked=0)

    # First scan — indexes the clip with locked=0
    scanner.scan(db, str(dest), "daily")
    assert _get_clip_locked(db) == 0

    # Simulate an archive-UI lock set directly on clip_index
    with db.write() as c:
        c.execute(
            "UPDATE clip_index SET locked = 1 WHERE basename = ?", (_FILENAME,)
        )
    assert _get_clip_locked(db) == 1

    # Second scan — must NOT clear the lock
    scanner.scan(db, str(dest), "daily")

    assert _get_clip_locked(db) == 1, (
        "clip_index.locked must be preserved across rescans "
        "(rescan must never clear a lock set on the index)"
    )
