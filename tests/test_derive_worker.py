# tests/test_derive_worker.py
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from web.db import Database
from web.services import derive_queue as dq
from web.services import derive_worker
from web.services.derive_worker import DeriveWorker


def _index_clip(db, clip_id, path):
    # scanned_at is NOT NULL with no default, so it must be supplied.
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index (id, path, basename, timestamp, camera, "
            "sequence, duration_s, scanned_at) VALUES (?,?,?,?,?,?,?,?)",
            (clip_id, path, "c.mp4", 0, "F", 0, 60.0, 0),
        )


async def test_boot_backfill_enqueues_existing(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    v = tmp_path / "c.mp4"
    v.write_bytes(b"x")
    _index_clip(db, 1, str(v))
    from web.services.derive_worker import enqueue_backfill
    n = enqueue_backfill(db, now=1)
    assert n == 1
    from web.services import derive_queue as dq
    assert dq.pending_count(db) == 1


async def test_worker_drains_queue(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    v = tmp_path / "c.mp4"
    v.write_bytes(b"x")
    _index_clip(db, 1, str(v))
    dq.enqueue(db, 1, priority=0, now=1)

    monkeypatch.setattr(derive_worker, "derive_one",
                        AsyncMock(return_value=derive_worker.DeriveResult()))
    provider = MagicMock()
    provider.get.return_value = SimpleNamespace(
        recordings=str(tmp_path),
        derive_thumbs_eager=True, derive_filmstrips_eager=True,
        gps_extract=False, max_attempts=3,
    )
    hub = SimpleNamespace(broadcast=AsyncMock())
    w = DeriveWorker(db, provider, hub)
    w.bind_loop(asyncio.get_running_loop())
    w.start()
    for _ in range(50):
        if dq.pending_count(db) == 0:
            break
        await asyncio.sleep(0.02)
    await w.stop()
    assert dq.pending_count(db) == 0
    derive_worker.derive_one.assert_awaited()
    hub.broadcast.assert_awaited()  # clip_derived emitted
