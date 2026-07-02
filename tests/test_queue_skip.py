"""Tests for queue.skip / queue.unskip and the skip/unskip routes."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _insert(c, filename, state, attempts=0, last_error=None):
    now = int(time.time())
    c.execute(
        "INSERT INTO download_queue "
        "(filename, source_dir, state, enqueued_at, attempts, last_error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (filename, "/DCIM", state, now, attempts, last_error),
    )


def test_skip_marks_pending_and_failed(tmp_path):
    from web.db import Database
    from web.services.queue import skip
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        _insert(c, "p.MP4", "pending")
        _insert(c, "f.MP4", "failed", attempts=3, last_error="boom")
        _insert(c, "d.MP4", "done")
        _insert(c, "g.MP4", "gone")
    n = skip(db, ["p.MP4", "f.MP4", "d.MP4", "g.MP4"])
    assert n == 2
    with db.conn() as c:
        rows = {r["filename"]: r["state"] for r in c.execute(
            "SELECT filename, state FROM download_queue").fetchall()}
    assert rows["p.MP4"] == "skipped"
    assert rows["f.MP4"] == "skipped"
    assert rows["d.MP4"] == "done"    # untouched
    assert rows["g.MP4"] == "gone"    # untouched


def test_skip_empty_is_noop(tmp_path):
    from web.db import Database
    from web.services.queue import skip
    db = Database(str(tmp_path / "v.db"))
    assert skip(db, []) == 0


def test_unskip_restores_to_pending_and_resets(tmp_path):
    from web.db import Database
    from web.services.queue import unskip
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        _insert(c, "s.MP4", "skipped", attempts=4, last_error="boom")
        _insert(c, "p.MP4", "pending")
    n = unskip(db, ["s.MP4", "p.MP4"])
    assert n == 1
    with db.conn() as c:
        rows = {r["filename"]: dict(r) for r in c.execute(
            "SELECT * FROM download_queue").fetchall()}
    assert rows["s.MP4"]["state"] == "pending"
    assert rows["s.MP4"]["attempts"] == 0
    assert rows["s.MP4"]["last_error"] is None
    assert rows["p.MP4"]["state"] == "pending"   # untouched


def test_unskip_empty_is_noop(tmp_path):
    from web.db import Database
    from web.services.queue import unskip
    db = Database(str(tmp_path / "v.db"))
    assert unskip(db, []) == 0


def test_skip_and_unskip_log_info_audit(tmp_path, caplog):
    # viofosync.queue INFO is persisted to app_log by DBLogHandler.
    from web.db import Database
    from web.services.queue import skip, unskip
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        _insert(c, "p.MP4", "pending")
    with caplog.at_level("INFO", logger="viofosync.queue"):
        skip(db, ["p.MP4"])
        unskip(db, ["p.MP4"])
    msgs = [r.getMessage() for r in caplog.records if r.name == "viofosync.queue"]
    assert any("archive skip" in m and "p.MP4" in m for m in msgs), msgs
    assert any("archive unskip" in m and "p.MP4" in m for m in msgs), msgs


def test_next_pending_ignores_skipped(tmp_path):
    from web.db import Database
    from web.services.queue import next_pending
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        _insert(c, "s.MP4", "skipped")
    assert next_pending(db) is None


def test_list_days_reports_skipped_count(tmp_path):
    from web.db import Database
    from web.services.queue import list_days
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        _insert(c, "p.MP4", "pending")
        _insert(c, "s.MP4", "skipped")
    days = list_days(db)
    assert days, "expected at least one day summary"
    assert days[0]["skipped_count"] == 1


@pytest.fixture
def authed_client(tmp_config_dir: Path, tmp_recordings_dir: Path, monkeypatch):
    from web import app as app_mod
    from web import settings as settings_mod
    monkeypatch.setenv("VIOFOSYNC_RESTART_DISABLED", "1")
    settings_mod.reset_for_tests()
    application = app_mod.create_app()
    with TestClient(application) as c:
        c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        })
        csrf = c.get("/api/auth/csrf").json()["csrf"]
        c.headers.update({"x-csrf-token": csrf})
        yield c


def _seed(client):
    with client.app.state.db.write() as c:
        _insert(c, "p.MP4", "pending")
        _insert(c, "f.MP4", "failed", attempts=3, last_error="boom")
        _insert(c, "s.MP4", "skipped")


def _states(client):
    with client.app.state.db.conn() as c:
        return {r["filename"]: r["state"] for r in c.execute(
            "SELECT filename, state FROM download_queue").fetchall()}


def test_skip_endpoint(authed_client):
    _seed(authed_client)
    r = authed_client.post("/api/queue/skip",
                           json={"filenames": ["p.MP4", "f.MP4"]})
    assert r.status_code == 200
    assert r.json()["updated"] == 2
    st = _states(authed_client)
    assert st["p.MP4"] == "skipped"
    assert st["f.MP4"] == "skipped"


def test_unskip_endpoint(authed_client):
    _seed(authed_client)
    r = authed_client.post("/api/queue/unskip", json={"filenames": ["s.MP4"]})
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    assert _states(authed_client)["s.MP4"] == "pending"


def test_skip_endpoint_empty_body(authed_client):
    _seed(authed_client)
    r = authed_client.post("/api/queue/skip", json={})
    assert r.status_code == 200
    assert r.json()["updated"] == 0
