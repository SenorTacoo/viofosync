"""Sync pause state survives a restart.

The pause toggle used to live only in an in-memory ``threading.Event``,
so pausing sync and restarting the app (or container) silently resumed
it. It's now persisted in the ``kv`` table and restored when the worker
is constructed, so the last-used state is remembered. ``ENABLE_SCHEDULED_SYNC``
still governs whether the schedule runs at all — this only remembers the
pause toggle.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from web.db import Database
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker


def _worker(db: Database) -> SyncWorker:
    # A fresh SyncWorker on the same DB stands in for a process restart.
    return SyncWorker(db, MagicMock(), Hub())


def test_fresh_db_defaults_to_running(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    assert not _worker(db).paused


def test_pause_persists_across_restart(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    w1 = _worker(db)
    assert not w1.paused
    w1.pause()
    assert w1.paused

    # A new worker on the same DB restores the paused state.
    assert _worker(db).paused


def test_resume_persists_across_restart(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    w1 = _worker(db)
    w1.pause()
    w1.resume()
    assert not w1.paused

    # And the resumed state is remembered too.
    assert not _worker(db).paused
