# web/services/triage.py
"""GPS Triage — skeleton-track extraction for queued, not-yet-downloaded clips.

When GPS_TRIAGE is on, the sync worker calls :func:`run_pass` each cycle
(before draining downloads). For each pending front-camera clip we read its
embedded GPS track straight off the camera over HTTP byte-range requests
(reusing ``parse_moov`` + the spike-filtering ``generate_gpx``), write a
skeleton ``$RECORDINGS/.triage/<filename>.gpx`` sidecar, and record the
result on the queue row (``triaged_at`` / ``gps_points``).

The skeletons feed the archive journey view (see web/routers/archive.py); the
real per-clip sidecar written on download supersedes them.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import viofosync_lib as vfs

from ..db import Database

log = logging.getLogger("viofosync.triage")

# The camera's HTTP server resets above ~3 concurrent connections.
MAX_CONCURRENCY = 3
# How often run_pass reports progress so the UI can fill the journey map in
# during a long full-backlog pass (one report per this many triaged clips).
PROGRESS_EVERY = 20
# Stop a full-backlog pass after this many consecutive read failures — that
# many in a row means the camera dropped (car drove off), and every remaining
# read would otherwise block to timeout. The skipped clips stay un-triaged and
# retry on the next cycle once the camera is back.
ABORT_AFTER_FAILURES = 8

# Don't triage a clip until its recording window has surely closed, or we read
# a half-written file with no GPS yet and cache a wrong gps_points=0 that's
# never revisited. Parking clips are time-lapse (~30 real min observed); normal
# and RO clips are <=60 s.
TRIAGE_SETTLE_S = 120            # normal / RO: 60 s clip + margin
TRIAGE_SETTLE_PARKING_S = 1860   # parking time-lapse window (~31 min)

# States whose ``.triage`` skeleton must be kept on disk. This is the single
# source of truth shared with the geofence detector (geofence._DETECT_STATES):
# a skipped clip keeps its skeleton so re-evaluation still sees the full home
# dwell. 'done'/'gone' clips are superseded by the real sidecar / never return,
# so their skeletons are swept.
SKELETON_KEEP_STATES = ("pending", "failed", "skipped", "downloading")


def triage_dir(recordings: str) -> str:
    return os.path.join(recordings, ".triage")


def skeleton_path(recordings: str, filename: str) -> str:
    return os.path.join(triage_dir(recordings), filename + ".gpx")


def select_targets(
    db: Database, *, limit: int | None = None, now: int | None = None,
) -> list[dict]:
    """Pending front-camera rows not yet triaged and past their recording
    settle window, newest first.

    Clips are deferred until their recording window has surely closed so we
    don't read a half-written file with no GPS yet and cache a wrong
    gps_points=0 that is never revisited (``triaged_at IS NULL`` prevents
    re-triage). NULL ``recorded_at`` bypasses the gate (legacy rows).

    ``limit=None`` (the default) returns the entire un-triaged backlog so one
    pass can triage everything before the download drain runs — otherwise the
    long drain starves triage and the journey map never fills in. SQLite
    treats ``LIMIT -1`` as unbounded.

    Front (`F`) and rear (`R`) share one GPS track, so triaging F only halves
    the request count; the rear clip still shows as a placeholder tile."""
    if now is None:
        now = int(time.time())
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, filename, source_dir, remote_size, recorded_at "
            "FROM download_queue "
            "WHERE state='pending' AND triaged_at IS NULL "
            "AND upper(substr(filename, -5, 1)) = 'F' "
            "AND (recorded_at IS NULL OR ? - recorded_at >= "
            "     (CASE WHEN event_type='parking' THEN ? ELSE ? END)) "
            "ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (now, TRIAGE_SETTLE_PARKING_S, TRIAGE_SETTLE_S,
             -1 if limit is None else limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _write_skeleton(recordings: str, filename: str, gpx: str) -> str:
    d = triage_dir(recordings)
    os.makedirs(d, exist_ok=True)
    final = skeleton_path(recordings, filename)
    tmp = final + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(gpx)
        os.replace(tmp, final)
    except OSError:
        # Don't leave a half-written .part behind (sweep_orphans only
        # cleans .gpx files).
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return final


def triage_one(
    db: Database, base_url: str, row: dict, recordings: str, *, timeout: float
) -> int:
    """Triage one queued clip. Returns the number of GPS points found
    (0 = no fix, -1 = read error). Sets ``triaged_at`` + ``gps_points`` on
    success/no-fix so a fixless clip is never re-attempted; a read error
    leaves the row untriaged for a later retry."""
    filename = row["filename"]
    rec = vfs.Recording(
        filename=filename,
        filepath=row["source_dir"] or "",
        size=row.get("remote_size"),
        timecode=None,
        datetime=None,
        attr=None,
    )
    try:
        points = vfs.extract_remote_gps_points(base_url, rec, timeout=timeout)
    except Exception as e:
        # A clip with no GPS lock / no index decodes to []; an actual I/O
        # error is logged and the row stays untriaged for a later retry.
        log.info("triage read failed for %s: %s", filename, e)
        return -1
    if points:
        gpx = vfs.generate_gpx(points, os.path.basename(filename) + ".gpx")
        _write_skeleton(recordings, filename, gpx)
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET triaged_at=?, gps_points=? WHERE id=?",
            (now, len(points), row["id"]),
        )
    return len(points)


def run_pass(
    db: Database, base_url: str, recordings: str, *, timeout: float,
    cancel_check=None, progress_cb=None,
) -> dict:
    """Triage every un-triaged pending front clip concurrently (<=3), so the
    journey map fully populates before the download drain runs. A read error
    leaves a clip un-triaged for retry next cycle. Returns a summary dict.

    ``cancel_check`` (optional) aborts between clips (pause/stop).
    ``progress_cb(triaged, total, eta_s)`` (optional) is invoked once at the
    start (``0, total, None``) and every ``PROGRESS_EVERY`` clips, so the UI can
    show live progress + ETA during a long full-backlog pass."""
    sweep_orphans(db, recordings)
    targets = select_targets(db)
    if not targets:
        return {"triaged": 0, "with_gps": 0}

    total = len(targets)
    started = time.perf_counter()
    log.info("triage: starting pass — %d clip(s) to triage", total)
    if progress_cb is not None:
        progress_cb(0, total, None)

    triaged = 0
    with_gps = 0

    # Circuit breaker for a mid-pass camera drop: count consecutive read
    # failures (triage_one returns -1 only on an I/O error — a clip with no GPS
    # returns 0 and counts as success). Once tripped, queued _work calls return
    # immediately so the pass finishes promptly instead of timing out on every
    # remaining clip.
    abort = threading.Event()
    fail_streak = {"n": 0}
    fail_lock = threading.Lock()

    def _work(row):
        if abort.is_set() or (cancel_check is not None and cancel_check()):
            return None
        n = triage_one(db, base_url, row, recordings, timeout=timeout)
        with fail_lock:
            if n < 0:
                fail_streak["n"] += 1
                if fail_streak["n"] >= ABORT_AFTER_FAILURES:
                    abort.set()
            else:
                fail_streak["n"] = 0
        return n

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as ex:
        for n in ex.map(_work, targets):
            if n is None or n < 0:
                continue
            triaged += 1
            if n > 0:
                with_gps += 1
            if triaged % PROGRESS_EVERY == 0:
                elapsed = time.perf_counter() - started
                log.info(
                    "triage: %d/%d clip(s) done (%.0fs elapsed, %.1fs/clip)",
                    triaged, total, elapsed, elapsed / triaged,
                )
                if progress_cb is not None:
                    eta_s = round((total - triaged) * (elapsed / triaged))
                    progress_cb(triaged, total, eta_s)
    elapsed = time.perf_counter() - started
    if abort.is_set():
        log.warning(
            "triage: stopped early after %d consecutive read failures "
            "(camera offline?); remaining clips retry next cycle",
            ABORT_AFTER_FAILURES,
        )
    log.info(
        "triage: pass complete — %d/%d clip(s), %d with GPS, in %.0fs",
        triaged, total, with_gps, elapsed,
    )
    return {"triaged": triaged, "with_gps": with_gps}


def remove_skeleton(recordings: str, filename: str) -> None:
    try:
        os.remove(skeleton_path(recordings, filename))
    except OSError:
        pass


def sweep_orphans(db: Database, recordings: str) -> int:
    """Remove skeleton sidecars whose queue row is no longer in a keep state
    (downloaded, gone, skipped-but-released, or absent). Returns the number
    removed."""
    d = triage_dir(recordings)
    if not os.path.isdir(d):
        return 0
    with db.conn() as c:
        ph = ",".join("?" * len(SKELETON_KEEP_STATES))
        live = {
            r["filename"]
            for r in c.execute(
                f"SELECT filename FROM download_queue "
                f"WHERE state IN ({ph})",
                SKELETON_KEEP_STATES,
            )
        }
    removed = 0
    for entry in os.listdir(d):
        if not entry.endswith(".gpx"):
            continue
        filename = entry[: -len(".gpx")]
        if filename in live:
            continue
        try:
            os.remove(os.path.join(d, entry))
            removed += 1
        except OSError:
            pass
    return removed


def purge_all(db: Database, recordings: str) -> None:
    """Drop the entire .triage cache and clear triage columns (used when
    GPS_TRIAGE is turned off)."""
    shutil.rmtree(triage_dir(recordings), ignore_errors=True)
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET triaged_at=NULL, gps_points=NULL "
            "WHERE triaged_at IS NOT NULL OR gps_points IS NOT NULL"
        )
