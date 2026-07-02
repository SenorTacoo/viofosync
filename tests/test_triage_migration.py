"""download_queue gains triage columns, idempotently."""
from __future__ import annotations

import time

from web.db import Database


def _columns(db, table):
    with db.conn() as c:
        return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def test_triage_columns_present_on_fresh_db(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    cols = _columns(db, "download_queue")
    assert "triaged_at" in cols
    assert "gps_points" in cols


def test_migration_idempotent_on_existing_db(tmp_path):
    p = str(tmp_path / "v.db")
    Database(p)            # creates + migrates once
    db2 = Database(p)      # re-open: ALTERs must be no-ops, not raise
    cols = _columns(db2, "download_queue")
    assert {"triaged_at", "gps_points"} <= cols


def test_triage_retry_columns_present(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    cols = _columns(db, "download_queue")
    assert "triage_attempts" in cols
    assert "triage_last_attempt_at" in cols


def test_triage_attempts_defaults_to_zero(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at) "
            "VALUES (?,?,?,?,?,?)",
            ("2026_0618_203643_0001F.MP4", "/DCIM", "F", "normal", "pending", now),
        )
    with db.conn() as c:
        r = c.execute(
            "SELECT triage_attempts FROM download_queue"
        ).fetchone()
    assert r["triage_attempts"] == 0
