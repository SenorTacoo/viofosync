"""download_queue gains triage columns, idempotently."""
from __future__ import annotations

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
