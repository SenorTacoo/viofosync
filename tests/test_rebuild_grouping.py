"""Maintenance flush: un-skip geofence decisions + clear the route cache, then
re-evaluate the geofence (POST /api/archive/rebuild-grouping)."""
from __future__ import annotations

import os
import types

from web.db import Database
from web.routers import archive
from web.services import queue as q
from web.services import route_cache
from web.settings import Place


def _seed(db, filename, state, skip_reason=None, released=None):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, "
            " skip_reason, geofence_released_at, enqueued_at) VALUES (?,?,?,?,?,0)",
            (filename, "/DCIM/Movie", state, skip_reason, released),
        )


def test_unskip_geofence_resets_only_geofence_skips(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "a_geofence.MP4", "skipped", "geofence")              # -> pending
    _seed(db, "b_user.MP4", "skipped", "user")                     # keep (user skip)
    _seed(db, "c_released.MP4", "skipped", "geofence", released=1)  # keep (released)
    _seed(db, "d_pending.MP4", "pending")                          # keep
    _seed(db, "e_done.MP4", "done")                                # keep

    n = q.unskip_geofence(db)

    assert n == 1
    with db.conn() as c:
        st = {
            r["filename"]: (r["state"], r["skip_reason"], r["geofence_released_at"])
            for r in c.execute(
                "SELECT filename, state, skip_reason, geofence_released_at "
                "FROM download_queue"
            )
        }
    # Reset to pending, skip_reason cleared, and crucially NOT marked released
    # (so the next sweep can re-skip it).
    assert st["a_geofence.MP4"] == ("pending", None, None)
    assert st["b_user.MP4"] == ("skipped", "user", None)
    assert st["c_released.MP4"][0] == "skipped"
    assert st["d_pending.MP4"][0] == "pending"
    assert st["e_done.MP4"][0] == "done"


def test_clear_all_removes_route_cache(tmp_path):
    rec = str(tmp_path / "rec")
    route_cache.store(rec, "2026-06-18", "sig", {"x": 1})
    assert os.path.exists(route_cache._cache_path(rec, "2026-06-18"))

    route_cache.clear_all(rec)
    assert not os.path.exists(route_cache._cache_path(rec, "2026-06-18"))

    route_cache.clear_all(rec)  # idempotent when already gone


def _req(db, rec, *, gps_triage=True, locations=(), worker=None):
    snap = types.SimpleNamespace(
        recordings=str(rec), gps_triage=gps_triage, locations=locations
    )
    provider = types.SimpleNamespace(get=lambda: snap)
    state = types.SimpleNamespace(db=db, settings_provider=provider)
    if worker is not None:
        state.sync_worker = worker
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


async def test_rebuild_grouping_endpoint(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    _seed(db, "a_geofence.MP4", "skipped", "geofence")
    route_cache.store(str(rec), "2026-06-18", "sig", {"x": 1})

    # Stub the re-sweep (it needs real skeletons/zones otherwise).
    monkeypatch.setattr(
        archive.geofence_service, "sweep_all", lambda *a, **k: 4
    )
    worker = types.SimpleNamespace(_geofence_seen={"2026-06-18": 9})
    req = _req(
        db, rec,
        locations=(Place("Home", 53.1, -2.0, 30, True),),
        worker=worker,
    )

    out = await archive.rebuild_grouping(req)

    assert out == {"ok": True, "unskipped": 1, "reskipped": 4}
    # geofence clip is back to pending
    with db.conn() as c:
        row = c.execute(
            "SELECT state FROM download_queue WHERE filename='a_geofence.MP4'"
        ).fetchone()
    assert row["state"] == "pending"
    # route cache cleared and worker's seen-cache reset
    assert not os.path.exists(route_cache._cache_path(str(rec), "2026-06-18"))
    assert worker._geofence_seen == {}


async def test_rebuild_grouping_skips_sweep_without_zones(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    called = {"n": 0}
    monkeypatch.setattr(
        archive.geofence_service, "sweep_all",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 0,
    )

    out = await archive.rebuild_grouping(_req(db, rec, locations=()))

    assert out == {"ok": True, "unskipped": 0, "reskipped": 0}
    assert called["n"] == 0   # no zones -> no sweep
