"""queue.delete_from_camera: dashcam SD-card delete, skipping pinned clips.

Read-only (/RO) clips are attempted like any other — the camera enforces
its own write-protection, and a refusal is reported as an error.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _env(tmp_path: Path):
    from web.db import Database
    rec = tmp_path / "rec"
    rec.mkdir()
    return rec, Database(str(rec / "v.db"))


def _q(db, filename, source_dir, *, locked=0, state="pending"):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, locked, "
            "enqueued_at) VALUES (?,?,?,?,1)", (filename, source_dir, state, locked),
        )


def test_delete_from_camera_deletes_and_marks_gone(tmp_path):
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "A.MP4", "/DCIM/Movie")
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=True) as m:
        res = delete_from_camera(db, ["A.MP4"], "http://cam")
    m.assert_called_once_with("http://cam", "/DCIM/Movie", "A.MP4", timeout=10.0)
    assert res == {"deleted": 1, "skipped": 0, "errors": 0, "ro_errors": 0}
    with db.conn() as c:
        assert c.execute("SELECT state FROM download_queue WHERE filename='A.MP4'").fetchone()["state"] == "gone"


def test_delete_from_camera_skips_only_locked(tmp_path):
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "L.MP4", "/DCIM/Movie", locked=1)
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=True) as m:
        res = delete_from_camera(db, ["L.MP4"], "http://cam")
    m.assert_not_called()
    assert res == {"deleted": 0, "skipped": 1, "errors": 0, "ro_errors": 0}


@pytest.mark.parametrize("source_dir", ["/DCIM/Movie/RO", "/DCIM/Movie/RO/"])
def test_delete_from_camera_attempts_ro(tmp_path, source_dir):
    """A read-only clip is a delete request like any other — the camera,
    not the client, decides whether write-protection blocks it."""
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "R.MP4", source_dir)
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=True) as m:
        res = delete_from_camera(db, ["R.MP4"], "http://cam")
    m.assert_called_once_with("http://cam", source_dir, "R.MP4", timeout=10.0)
    assert res == {"deleted": 1, "skipped": 0, "errors": 0, "ro_errors": 0}
    with db.conn() as c:
        assert c.execute(
            "SELECT state FROM download_queue WHERE filename='R.MP4'"
        ).fetchone()["state"] == "gone"


def test_delete_from_camera_ro_refusal_is_an_error(tmp_path):
    """When the firmware refuses, the clip stays queued and the refusal is
    counted separately so the UI can say why."""
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "R.MP4", "/DCIM/Movie/RO")
    _q(db, "A.MP4", "/DCIM/Movie")
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=False):
        res = delete_from_camera(db, ["R.MP4", "A.MP4"], "http://cam")
    assert res == {"deleted": 0, "skipped": 0, "errors": 2, "ro_errors": 1}
    with db.conn() as c:
        states = {
            r["filename"]: r["state"] for r in
            c.execute("SELECT filename, state FROM download_queue").fetchall()
        }
    assert states == {"R.MP4": "pending", "A.MP4": "pending"}


def test_delete_from_camera_keeps_done_rows_done(tmp_path):
    """Clearing the card is the whole point of deleting a downloaded clip —
    but it must not erase the fact that we hold it in the archive."""
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "D.MP4", "/DCIM/Movie", state="done")
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=True):
        res = delete_from_camera(db, ["D.MP4"], "http://cam")
    assert res["deleted"] == 1
    with db.conn() as c:
        assert c.execute(
            "SELECT state FROM download_queue WHERE filename='D.MP4'"
        ).fetchone()["state"] == "done"


def test_delete_from_camera_counts_errors(tmp_path):
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "A.MP4", "/DCIM/Movie")
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=False):
        res = delete_from_camera(db, ["A.MP4"], "http://cam")
    assert res == {"deleted": 0, "skipped": 0, "errors": 1, "ro_errors": 0}
    with db.conn() as c:
        assert c.execute("SELECT state FROM download_queue WHERE filename='A.MP4'").fetchone()["state"] == "pending"


def test_delete_from_camera_empty(tmp_path):
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    assert delete_from_camera(db, [], "http://cam") == {
        "deleted": 0, "skipped": 0, "errors": 0, "ro_errors": 0,
    }


# ── endpoint tests ──────────────────────────────────────────────────────────

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


def test_delete_from_camera_endpoint_no_address(authed_client, monkeypatch):
    """Returns ok:False/error when no dashcam address is configured."""
    import dataclasses
    snap = authed_client.app.state.settings_provider.get()
    no_addr_snap = dataclasses.replace(snap, address=None)
    monkeypatch.setattr(
        authed_client.app.state.settings_provider, "get", lambda: no_addr_snap
    )
    r = authed_client.post(
        "/api/queue/delete-from-camera", json={"filenames": ["A.MP4"]}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "error" in data


def test_delete_from_camera_endpoint_success(authed_client):
    """With address configured + mocked vfs, returns ok:True deleted:1."""
    db = authed_client.app.state.db
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, locked, enqueued_at) "
            "VALUES ('A.MP4', '/DCIM/Movie', 'pending', 0, 1)"
        )
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=True):
        r = authed_client.post(
            "/api/queue/delete-from-camera", json={"filenames": ["A.MP4"]}
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["deleted"] == 1
    assert data["skipped"] == 0
    assert data["errors"] == 0
