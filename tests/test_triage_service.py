# tests/test_triage_service.py
"""Triage service: select F clips, write skeleton sidecars, track state."""
from __future__ import annotations

import time

from web.db import Database
from web.services import triage


def _seed(db, filename, *, camera, source="/DCIM/Movie", state="pending"):
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at) "
            "VALUES (?,?,?,?,?,?)",
            (filename, source, camera, "normal", state, now),
        )


def test_select_targets_front_only_untriaged(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0618_203643_0001F.MP4", camera="F")
    _seed(db, "2026_0618_203644_0002R.MP4", camera="R")
    targets = triage.select_targets(db, limit=10)
    names = [t["filename"] for t in targets]
    assert names == ["2026_0618_203643_0001F.MP4"]   # R excluded


def test_triage_one_writes_sidecar_and_sets_columns(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    fn = "2026_0618_203643_0001F.MP4"
    _seed(db, fn, camera="F")

    point = {
        "DT": {"DT": "2026-06-18T20:36:43Z"},
        "Loc": {
            "Lat": {"Float": 53.1825}, "Lon": {"Float": -2.865},
            "Speed": 0.0, "Bearing": 0.0,
        },
    }
    monkeypatch.setattr(
        triage.vfs, "extract_remote_gps_points",
        lambda *a, **k: [point],
    )

    row = triage.select_targets(db, limit=1)[0]
    triage.triage_one(db, "http://cam", row, str(rec_dir), timeout=5.0)

    sidecar = rec_dir / ".triage" / (fn + ".gpx")
    assert sidecar.exists()
    assert "53.1825" in sidecar.read_text()
    with db.conn() as c:
        r = c.execute(
            "SELECT triaged_at, gps_points FROM download_queue WHERE filename=?",
            (fn,),
        ).fetchone()
    assert r["triaged_at"] is not None
    assert r["gps_points"] == 1


def test_triage_one_no_fix_sets_zero_points_no_sidecar(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    fn = "2026_0618_203643_0001F.MP4"
    _seed(db, fn, camera="F")
    monkeypatch.setattr(
        triage.vfs, "extract_remote_gps_points", lambda *a, **k: []
    )
    row = triage.select_targets(db, limit=1)[0]
    triage.triage_one(db, "http://cam", row, str(rec_dir), timeout=5.0)
    assert not (rec_dir / ".triage" / (fn + ".gpx")).exists()
    with db.conn() as c:
        r = c.execute(
            "SELECT triaged_at, gps_points FROM download_queue WHERE filename=?",
            (fn,),
        ).fetchone()
    assert r["triaged_at"] is not None
    assert r["gps_points"] == 0


def test_sweep_orphans_removes_skeletons_for_done_and_absent(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "recordings"
    triage_dir = rec_dir / ".triage"
    triage_dir.mkdir(parents=True)
    keep = "2026_0618_203643_0001F.MP4"
    drop_done = "2026_0618_203644_0002F.MP4"
    drop_absent = "2026_0618_203645_0003F.MP4"
    for fn in (keep, drop_done, drop_absent):
        (triage_dir / (fn + ".gpx")).write_text("x")
    _seed(db, keep, camera="F", state="pending")
    _seed(db, drop_done, camera="F", state="done")
    removed = triage.sweep_orphans(db, str(rec_dir))
    assert removed == 2
    assert (triage_dir / (keep + ".gpx")).exists()
    assert not (triage_dir / (drop_done + ".gpx")).exists()
    assert not (triage_dir / (drop_absent + ".gpx")).exists()


def test_purge_all_removes_dir_and_clears_columns(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "recordings"
    (rec_dir / ".triage").mkdir(parents=True)
    (rec_dir / ".triage" / "x.gpx").write_text("x")
    _seed(db, "2026_0618_203643_0001F.MP4", camera="F")
    with db.write() as c:
        c.execute("UPDATE download_queue SET triaged_at=1, gps_points=1")
    triage.purge_all(db, str(rec_dir))
    assert not (rec_dir / ".triage").exists()
    with db.conn() as c:
        r = c.execute(
            "SELECT triaged_at, gps_points FROM download_queue"
        ).fetchone()
    assert r["triaged_at"] is None and r["gps_points"] is None


def test_sweep_orphans_keeps_downloading(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "recordings"
    (rec_dir / ".triage").mkdir(parents=True)
    fn = "2026_0618_203643_0001F.MP4"
    (rec_dir / ".triage" / (fn + ".gpx")).write_text("x")
    _seed(db, fn, camera="F", state="downloading")
    removed = triage.sweep_orphans(db, str(rec_dir))
    assert removed == 0
    assert (rec_dir / ".triage" / (fn + ".gpx")).exists()


def test_triage_one_read_error_leaves_row_untriaged(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    fn = "2026_0618_203643_0001F.MP4"
    _seed(db, fn, camera="F")

    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(triage.vfs, "extract_remote_gps_points", _boom)
    row = triage.select_targets(db, limit=1)[0]
    rc = triage.triage_one(db, "http://cam", row, str(rec_dir), timeout=5.0)
    assert rc == -1
    with db.conn() as c:
        r = c.execute(
            "SELECT triaged_at, gps_points FROM download_queue WHERE filename=?",
            (fn,),
        ).fetchone()
    assert r["triaged_at"] is None          # untriaged -> retried next cycle
    assert r["gps_points"] is None


def test_run_pass_triages_and_counts(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    _seed(db, "2026_0618_203643_0001F.MP4", camera="F")
    _seed(db, "2026_0618_203644_0002F.MP4", camera="F")
    _seed(db, "2026_0618_203644_0003R.MP4", camera="R")  # ignored (rear)

    point = {
        "DT": {"DT": "2026-06-18T20:36:43Z"},
        "Loc": {
            "Lat": {"Float": 53.1825}, "Lon": {"Float": -2.865},
            "Speed": 0.0, "Bearing": 0.0,
        },
    }
    monkeypatch.setattr(
        triage.vfs, "extract_remote_gps_points", lambda *a, **k: [point]
    )
    summary = triage.run_pass(db, "http://cam", str(rec_dir), timeout=5.0)
    assert summary == {"triaged": 2, "with_gps": 2}
    with db.conn() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM download_queue WHERE triaged_at IS NOT NULL"
        ).fetchone()["n"]
    assert n == 2   # only the two F clips


def test_run_pass_triages_entire_backlog_not_capped(tmp_path, monkeypatch):
    """A single pass must triage the WHOLE pending front backlog, not a fixed
    batch — otherwise the long download drain starves triage and the journey
    map never fills in (the reported bug: ~50 triaged of ~900 pending)."""
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    # 45 front clips — deliberately more than the old 40-per-pass cap.
    for i in range(45):
        _seed(db, f"2026_0619_2030{i:02d}_{1000 + i:04d}F.MP4", camera="F")
    point = {
        "DT": {"DT": "2026-06-19T20:30:00Z"},
        "Loc": {
            "Lat": {"Float": 53.1}, "Lon": {"Float": -2.0},
            "Speed": 0.0, "Bearing": 0.0,
        },
    }
    monkeypatch.setattr(
        triage.vfs, "extract_remote_gps_points", lambda *a, **k: [point]
    )
    summary = triage.run_pass(db, "http://cam", str(rec_dir), timeout=5.0)
    assert summary["triaged"] == 45     # all of them, not capped at 40
    with db.conn() as c:
        untriaged = c.execute(
            "SELECT COUNT(*) AS n FROM download_queue "
            "WHERE state='pending' AND triaged_at IS NULL "
            "AND upper(substr(filename,-5,1))='F'"
        ).fetchone()["n"]
    assert untriaged == 0               # nothing left for the drain to starve


def test_run_pass_reports_incremental_progress(tmp_path, monkeypatch):
    """A long full-backlog pass must report progress as it goes (every
    PROGRESS_EVERY clips) so the UI can fill the map in, not only at the end."""
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    for i in range(45):
        _seed(db, f"2026_0619_2031{i:02d}_{2000 + i:04d}F.MP4", camera="F")
    point = {
        "DT": {"DT": "2026-06-19T20:31:00Z"},
        "Loc": {
            "Lat": {"Float": 53.1}, "Lon": {"Float": -2.0},
            "Speed": 0.0, "Bearing": 0.0,
        },
    }
    monkeypatch.setattr(
        triage.vfs, "extract_remote_gps_points", lambda *a, **k: [point]
    )
    calls = []
    triage.run_pass(
        db, "http://cam", str(rec_dir), timeout=5.0,
        progress_cb=lambda t, total, eta: calls.append((t, total, eta)),
    )
    # start (0/45, no ETA) + reports at 20 and 40, all against total=45.
    assert [c[0] for c in calls] == [0, 20, 40]
    assert all(c[1] == 45 for c in calls)
    assert calls[0][2] is None                       # no ETA at the start
    assert all(isinstance(c[2], int) for c in calls[1:])   # ETA computed after


def test_run_pass_aborts_when_camera_drops(tmp_path, monkeypatch):
    """If reads keep failing (camera offline mid-pass), the breaker stops the
    pass early instead of timing out on every remaining clip."""
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    for i in range(30):
        _seed(db, f"2026_0619_2032{i:02d}_{3000 + i:04d}F.MP4", camera="F")

    attempts = {"n": 0}

    def _boom(*a, **k):
        attempts["n"] += 1
        raise OSError("camera offline")

    monkeypatch.setattr(triage.vfs, "extract_remote_gps_points", _boom)
    summary = triage.run_pass(db, "http://cam", str(rec_dir), timeout=5.0)
    assert summary["triaged"] == 0
    # Aborted well before attempting all 30 (breaker trips at 8 consecutive,
    # plus at most a couple already in flight).
    assert attempts["n"] < 30
