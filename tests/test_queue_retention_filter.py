"""The retention download filter.

RETENTION_MAX_DAYS deletes clips older than N days from the archive. Without
this filter the worker still downloaded them from the camera, so an old clip
was fetched in full and swept seconds later. Two mechanisms, tested here:

* ``retention_sweep_queue`` parks aged-out rows in ``skipped``/``retention``
  (and hands them back when the window widens)
* ``next_pending``'s gate never hands out a row the sweep would delete
"""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.services import queue

DAY = 86400
NOW = 1_800_000_000


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _add(
    db: Database, *, filename: str, age_days: float,
    source_dir: str = "/DCIM/Movie", locked: int = 0,
    state: str = "pending", enq: int = 1,
) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, locked, recorded_at, enqueued_at) "
            "VALUES (?,?,?,?,?,?)",
            (filename, source_dir, state, locked,
             int(NOW - age_days * DAY), enq),
        )


def _row(db: Database, filename: str) -> dict:
    with db.conn() as c:
        return dict(c.execute(
            "SELECT * FROM download_queue WHERE filename=?", (filename,),
        ).fetchone())


# ── the queue pass ──────────────────────────────────────────────────────────

def test_sweep_skips_clips_older_than_the_window(db: Database) -> None:
    _add(db, filename="OLD.MP4", age_days=5)
    _add(db, filename="NEW.MP4", age_days=1)
    res = queue.retention_sweep_queue(db, max_days=3, _now=NOW)
    assert res == {"skipped": 1, "released": 0}
    assert _row(db, "OLD.MP4")["state"] == "skipped"
    assert _row(db, "OLD.MP4")["skip_reason"] == "retention"
    assert _row(db, "NEW.MP4")["state"] == "pending"


def test_sweep_skips_failed_rows_too(db: Database) -> None:
    """A failed old clip would only be retried into an immediate delete."""
    _add(db, filename="OLD.MP4", age_days=5, state="failed")
    assert queue.retention_sweep_queue(db, max_days=3, _now=NOW)["skipped"] == 1
    assert _row(db, "OLD.MP4")["state"] == "skipped"


def test_sweep_is_a_noop_when_the_rule_is_off(db: Database) -> None:
    _add(db, filename="ANCIENT.MP4", age_days=900)
    assert queue.retention_sweep_queue(db, max_days=0, _now=NOW) == {
        "skipped": 0, "released": 0,
    }
    assert _row(db, "ANCIENT.MP4")["state"] == "pending"


def test_sweep_leaves_pinned_clips_alone(db: Database) -> None:
    """`locked` survives the sweep, so it must survive the filter too."""
    _add(db, filename="PIN.MP4", age_days=90, locked=1)
    assert queue.retention_sweep_queue(db, max_days=3, _now=NOW)["skipped"] == 0
    assert _row(db, "PIN.MP4")["state"] == "pending"


def test_sweep_respects_protect_ro(db: Database) -> None:
    _add(db, filename="RO.MP4", age_days=90, source_dir="/DCIM/Movie/RO")
    assert queue.retention_sweep_queue(
        db, max_days=3, protect_ro=True, _now=NOW,
    )["skipped"] == 0
    assert _row(db, "RO.MP4")["state"] == "pending"


def test_sweep_skips_ro_when_protection_is_off(db: Database) -> None:
    _add(db, filename="RO.MP4", age_days=90, source_dir="/DCIM/Movie/RO")
    assert queue.retention_sweep_queue(
        db, max_days=3, protect_ro=False, _now=NOW,
    )["skipped"] == 1
    assert _row(db, "RO.MP4")["state"] == "skipped"


def test_sweep_ignores_rows_without_a_timestamp(db: Database) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at) "
            "VALUES ('NOTS.MP4', '/DCIM/Movie', 'pending', 1)"
        )
    assert queue.retention_sweep_queue(db, max_days=3, _now=NOW)["skipped"] == 0


def test_widening_the_window_releases_skipped_clips(db: Database) -> None:
    _add(db, filename="OLD.MP4", age_days=5)
    queue.retention_sweep_queue(db, max_days=3, _now=NOW)
    res = queue.retention_sweep_queue(db, max_days=30, _now=NOW)
    assert res == {"skipped": 0, "released": 1}
    row = _row(db, "OLD.MP4")
    assert row["state"] == "pending"
    assert row["skip_reason"] is None


def test_turning_the_rule_off_releases_skipped_clips(db: Database) -> None:
    _add(db, filename="OLD.MP4", age_days=5)
    queue.retention_sweep_queue(db, max_days=3, _now=NOW)
    assert queue.retention_sweep_queue(db, max_days=0, _now=NOW) == {
        "skipped": 0, "released": 1,
    }
    assert _row(db, "OLD.MP4")["state"] == "pending"


def test_sweep_never_releases_a_user_skip(db: Database) -> None:
    _add(db, filename="U.MP4", age_days=1)
    queue.skip(db, ["U.MP4"])
    assert queue.retention_sweep_queue(db, max_days=0, _now=NOW)["released"] == 0
    assert _row(db, "U.MP4")["state"] == "skipped"


def test_unskip_pins_a_retention_clip_so_it_stays_released(db: Database) -> None:
    """Asking for an out-of-window clip is asking to keep it — otherwise the
    next pass would re-skip it (or the sweep would delete it on arrival)."""
    _add(db, filename="OLD.MP4", age_days=5)
    queue.retention_sweep_queue(db, max_days=3, _now=NOW)
    assert queue.unskip(db, ["OLD.MP4"]) == 1
    row = _row(db, "OLD.MP4")
    assert row["state"] == "pending"
    assert row["locked"] == 1
    assert queue.retention_sweep_queue(db, max_days=3, _now=NOW)["skipped"] == 0
    assert _row(db, "OLD.MP4")["state"] == "pending"


def test_unskip_does_not_pin_a_user_skip(db: Database) -> None:
    _add(db, filename="U.MP4", age_days=1)
    queue.skip(db, ["U.MP4"])
    queue.unskip(db, ["U.MP4"])
    assert _row(db, "U.MP4")["locked"] == 0


def test_unskip_still_records_the_geofence_release(db: Database) -> None:
    _add(db, filename="G.MP4", age_days=1)
    queue.geofence_skip(db, ["G.MP4"])
    queue.unskip(db, ["G.MP4"])
    row = _row(db, "G.MP4")
    assert row["geofence_released_at"] is not None
    assert row["locked"] == 0


# ── the next_pending gate ───────────────────────────────────────────────────

def test_next_pending_never_returns_an_expired_clip(db: Database) -> None:
    _add(db, filename="OLD.MP4", age_days=5, enq=1)
    _add(db, filename="NEW.MP4", age_days=1, enq=2)
    item = queue.next_pending(db, retention_max_days=3, _now=NOW)
    assert item is not None and item.filename == "NEW.MP4"


def test_next_pending_gate_off_by_default(db: Database) -> None:
    _add(db, filename="OLD.MP4", age_days=900)
    item = queue.next_pending(db, _now=NOW)
    assert item is not None and item.filename == "OLD.MP4"


def test_next_pending_gate_honours_the_pin(db: Database) -> None:
    _add(db, filename="OLD.MP4", age_days=900, locked=1)
    item = queue.next_pending(db, retention_max_days=3, _now=NOW)
    assert item is not None and item.filename == "OLD.MP4"


def test_next_pending_gate_honours_protect_ro(db: Database) -> None:
    _add(db, filename="RO.MP4", age_days=900, source_dir="/DCIM/Movie/RO")
    assert queue.next_pending(
        db, retention_max_days=3, retention_protect_ro=True, _now=NOW,
    ) is not None
    assert queue.next_pending(
        db, retention_max_days=3, retention_protect_ro=False, _now=NOW,
    ) is None


def test_next_pending_gate_composes_with_the_triage_gate(db: Database) -> None:
    """Both filters bind parameters — a wrong order would silently mix up
    the cutoff and the attempt ceiling."""
    _add(db, filename="2026_0101_120000_0001F.MP4", age_days=5, enq=1)
    _add(db, filename="2026_0102_120000_0001F.MP4", age_days=1, enq=2)
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET triaged_at=1, gps_points=10 "
            "WHERE filename='2026_0102_120000_0001F.MP4'"
        )
    item = queue.next_pending(
        db, triage_gate=True, retention_max_days=3, _now=NOW,
    )
    assert item is not None
    assert item.filename == "2026_0102_120000_0001F.MP4"
