"""SyncWorker wiring for the retention download filter.

The pass has to run inside the cycle *before* triage and the drain — a
clip that retention will delete on arrival should not cost a GPS read
either — and it must survive a snapshot that predates the settings.
"""
from __future__ import annotations

import time

from web.db import Database
from web.services import queue as q
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker

DAY = 86400


class _Provider:
    def __init__(self, snap):
        self._snap = snap

    def get(self):
        return self._snap


class _Snap:
    def __init__(self, recordings, *, max_days=3):
        self.recordings = recordings
        self.gps_triage = False
        self.sync_ro_only = False
        self.disk_critical_pct = 95
        self.timeout = 5.0
        self.retention_max_days = max_days
        self.retention_protect_ro = True


def _seed(db, fn, *, age_days: float) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, recorded_at, "
            " enqueued_at) VALUES (?,?,?,?,?,?,?)",
            (fn, "/DCIM/Movie", "F", "normal", "pending",
             int(time.time() - age_days * DAY), int(time.time())),
        )


def _worker(tmp_path, snap):
    db = Database(str(tmp_path / "v.db"))
    return db, SyncWorker(db, _Provider(snap), Hub())


async def test_pass_skips_out_of_window_clips(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    db, sw = _worker(tmp_path, _Snap(str(rec), max_days=3))
    _seed(db, "2026_0618_203643_0001F.MP4", age_days=10)
    _seed(db, "2026_0620_203643_0001F.MP4", age_days=1)
    await sw._run_retention_queue_pass()
    with db.conn() as c:
        states = {
            r["filename"]: (r["state"], r["skip_reason"]) for r in
            c.execute(
                "SELECT filename, state, skip_reason FROM download_queue"
            ).fetchall()
        }
    assert states["2026_0618_203643_0001F.MP4"] == ("skipped", "retention")
    assert states["2026_0620_203643_0001F.MP4"] == ("pending", None)


async def test_pass_tolerates_a_snapshot_without_retention_fields(tmp_path):
    """Older/partial snapshots must not blow up the cycle — the filter
    just stays off."""
    class _Bare:
        recordings = str(tmp_path)
        gps_triage = False
        timeout = 5.0

    db, sw = _worker(tmp_path, _Bare())
    _seed(db, "2026_0618_203643_0001F.MP4", age_days=900)
    await sw._run_retention_queue_pass()
    with db.conn() as c:
        assert c.execute(
            "SELECT state FROM download_queue"
        ).fetchone()["state"] == "pending"


async def test_cycle_runs_the_pass_before_triage(tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()
    db, sw = _worker(tmp_path, _Snap(str(rec)))
    order: list[str] = []

    async def _noop(*a, **k):
        return None

    async def _true(*a, **k):
        return True

    async def _addr(*a, **k):
        return ("1.2.3.4", "primary")

    monkeypatch.setattr(sw, "_emit_disk_pct", _noop)
    monkeypatch.setattr(sw, "_check_recordings_writable", _true)
    monkeypatch.setattr(sw, "_select_active_address", _addr)
    monkeypatch.setattr(sw, "_refresh_listing_and_reconcile", _true)
    monkeypatch.setattr(sw, "_run_recording_status_pass", _noop)
    monkeypatch.setattr(sw, "_run_geofence_pass", _noop)

    async def _triage(*a, **k):
        order.append("triage")
        return 0

    monkeypatch.setattr(sw, "_run_triage_pass", _triage)
    monkeypatch.setattr(
        q, "retention_sweep_queue",
        lambda *a, **k: order.append("retention") or {
            "skipped": 0, "released": 0,
        },
    )
    monkeypatch.setattr(q, "next_pending", lambda *a, **k: None)

    await sw._cycle()
    assert order == ["retention", "triage"]
