"""A started, not-paused worker must 'report in' so the badge shows the real
state, not the 'worker hasn't reported yet → paused' default.

Regression for the bug where, on first app start, the status badge showed
'paused' even though the worker was running and not paused — so the toggle's
first click (acting on the true not-paused state) paused for real and it took
two clicks to resume. The fix makes start() broadcast a sync_state event, which
the hub folds into its snapshot and uses to recompute sync_status.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

from web.db import Database
from web.services.hub import Hub
from web.services.sync_status import compute_sync_status
from web.services.sync_worker import SyncWorker


def _snap(tmp_path):
    return types.SimpleNamespace(
        address="1.2.3.4", recordings=str(tmp_path), disk_critical_pct=95,
    )


async def test_started_worker_reports_running_not_paused(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    snap = _snap(tmp_path)
    provider = types.SimpleNamespace(get=lambda: snap)
    hub = Hub(settings_provider=provider)
    w = SyncWorker(db, provider, hub)
    w.bind_loop(asyncio.get_running_loop())
    # Stub the cycle + background sweeps so start() doesn't reach for a real
    # dashcam or run retention/geofence; we only care that start() makes the
    # worker report its (running, not-paused) state.
    w._cycle = AsyncMock(return_value=False)
    w._run_retention_sweep = AsyncMock()
    w._run_geofence_pass = AsyncMock()
    w.start()
    try:
        # Let the start()-scheduled sync_state broadcast run.
        for _ in range(10):
            await asyncio.sleep(0)
        ss = hub.last_state.get("sync_state")
        assert isinstance(ss, dict), "start() did not report sync_state"
        assert ss["running"] is True
        assert ss["paused"] is False
        # And the computed badge is 'waiting', not the 'paused' default.
        state, _ = compute_sync_status(hub, None, snap)
        assert state == "waiting"
    finally:
        await w.stop()


async def test_started_paused_worker_reports_paused(tmp_path):
    # A worker that restored a real pause still reports paused on start.
    db = Database(str(tmp_path / "t.db"))
    db.kv_set("sync_paused", "1")
    snap = _snap(tmp_path)
    provider = types.SimpleNamespace(get=lambda: snap)
    hub = Hub(settings_provider=provider)
    w = SyncWorker(db, provider, hub)
    w.bind_loop(asyncio.get_running_loop())
    w._cycle = AsyncMock(return_value=False)
    assert w.paused is True
    w.start()
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        ss = hub.last_state.get("sync_state")
        assert isinstance(ss, dict) and ss["paused"] is True
        state, _ = compute_sync_status(hub, None, snap)
        assert state == "paused"
    finally:
        await w.stop()
