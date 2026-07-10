from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from starlette.requests import Request

from web.db import Database
from web.routers import imports as imports_router
from web.services import importer


def _seed_skipped(db, filename):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, enqueued_at) "
            "VALUES (?, '', 'skipped', ?)",
            (filename, int(time.time())),
        )


def _req(db, recordings):
    snap = SimpleNamespace(recordings=str(recordings), grouping="daily")
    provider = SimpleNamespace(get=lambda: snap)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, settings_provider=provider))
    )


def test_present_reports_skip_listed(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    (tmp_path / "rec").mkdir()
    skipped = "2026_0618_120000_0001F.MP4"
    wanted = "2026_0618_120100_0002F.MP4"
    _seed_skipped(db, skipped)
    body = imports_router._FilesBody(files=[
        imports_router._FileRef(name=skipped, size=0),
        imports_router._FileRef(name=wanted, size=0),
    ])
    out = imports_router.present(_req(db, tmp_path / "rec"), body)
    assert out["skipped"] == [skipped]
    assert wanted not in out["skipped"]
    assert out["present"] == []          # neither is on disk


def _fake_app(tmp_path):
    snap = MagicMock()
    snap.recordings = str(tmp_path / "rec")
    snap.grouping = "daily"
    snap.import_path = None
    snap.retention_disk_pct = 0
    snap.recordings_quota_gb = 0
    snap.retention_protect_ro = True
    provider = MagicMock()
    provider.get.return_value = snap
    db = Database(str(tmp_path / "t.db"))
    return SimpleNamespace(
        state=SimpleNamespace(settings_provider=provider, db=db)
    )


def _upload_request(app, name, body):
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0)

    scope = {
        "type": "http", "method": "POST", "path": "/api/import/upload",
        "query_string": b"", "app": app,
        "headers": [
            (b"x-import-path", name.encode()),
            (b"x-import-size", str(len(body)).encode()),
        ],
    }
    return Request(scope, receive)


async def test_upload_refuses_skip_listed_before_make_room(tmp_path, monkeypatch):
    app = _fake_app(tmp_path)
    fn = "2026_0618_120000_0001F.MP4"
    _seed_skipped(app.state.db, fn)
    make_room = MagicMock(return_value=True)
    monkeypatch.setattr(imports_router._retention, "make_room_for", make_room)

    res = await imports_router.upload(_upload_request(app, fn, b"x" * 32))

    assert res["status"] == "skipped"
    assert res["filename"] == fn
    make_room.assert_not_called()                    # refused before any eviction
    staging = tmp_path / "rec" / importer.STAGING_DIRNAME
    assert not staging.exists() or not any(staging.iterdir())   # nothing written


async def test_upload_non_skip_listed_reaches_make_room(tmp_path, monkeypatch):
    app = _fake_app(tmp_path)
    fn = "2026_0618_120000_0001F.MP4"                # NOT seeded as skipped
    # make_room refuses → over_quota_older proves we passed the skip check.
    monkeypatch.setattr(
        imports_router._retention, "make_room_for", lambda *a, **k: False
    )
    monkeypatch.setattr(
        imports_router._retention, "import_exclude_set", lambda *a, **k: set()
    )

    res = await imports_router.upload(_upload_request(app, fn, b"x" * 32))

    assert res["status"] == "over_quota_older"
