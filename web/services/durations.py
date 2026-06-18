"""Populate ``clip_index.duration_s`` via ffprobe.

The scanner indexes clips from filenames but never measures their
length. ``duration_s`` drives filmstrip frame counts and the
timeline layout. :func:`probe_duration` / :func:`probe_and_store`
are called per-clip by the DeriveWorker; :func:`ensure_gps` marks
GPS extraction as examined so clips are not re-probed.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil

import viofosync_lib as vfs

from ..db import Database

_FFPROBE_TIMEOUT_S = 15.0

log = logging.getLogger("viofosync.durations")


# mvhd ``duration`` sentinel meaning "unknown" (all bits set), per the
# ISO base media format — 32-bit for a v0 header, 64-bit for v1.
_MVHD_UNKNOWN = {0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}


def _read_box_header(f):
    """Read an ISO-BMFF box header at the current offset.

    Returns ``(size, type, header_len)`` where ``size`` is the total box
    length including the header (or ``None`` for the size==0 "to EOF" form),
    or ``None`` at EOF / on a short read.
    """
    hdr = f.read(8)
    if len(hdr) < 8:
        return None
    size = int.from_bytes(hdr[:4], "big")
    btype = hdr[4:8]
    header_len = 8
    if size == 1:                       # 64-bit largesize follows (big mdat)
        ext = f.read(8)
        if len(ext) < 8:
            return None
        size = int.from_bytes(ext, "big")
        header_len = 16
    elif size == 0:                     # extends to end of file
        size = None
    return size, btype, header_len


def _find_box(f, target: bytes, region_end: int):
    """Scan sibling boxes from the current offset up to ``region_end`` and
    return ``(payload_start, box_end)`` of the first box of ``target`` type,
    or ``None``. On a match the file is left positioned at ``payload_start``.
    Bails out (None) on a malformed/truncated box rather than looping."""
    while f.tell() + 8 <= region_end:
        start = f.tell()
        hdr = _read_box_header(f)
        if hdr is None:
            return None
        size, btype, header_len = hdr
        box_end = region_end if size is None else start + size
        # ``==`` is a valid empty box (e.g. ffmpeg's zero-payload ``free``);
        # only a box claiming to be smaller than its own header, or running
        # past the parent, is malformed.
        if box_end < start + header_len or box_end > region_end:
            return None
        if btype == target:
            return start + header_len, box_end
        f.seek(box_end)
    return None


def _probe_duration_mvhd(path: str) -> float | None:
    """Clip duration in seconds read directly from the MP4 ``moov/mvhd``
    box — no subprocess. Returns ``None`` when the file isn't a parseable
    MP4, ``mvhd`` is absent, or the duration is unknown, so the caller can
    fall back to ffprobe.

    Only a handful of box headers plus the ~108-byte ``mvhd`` are read; the
    huge ``mdat`` is seeked past, so this is cheap even when ``moov`` is at
    the end of a large file on a slow NAS volume.
    """
    try:
        end = os.path.getsize(path)
        with open(path, "rb") as f:
            moov = _find_box(f, b"moov", end)
            if moov is None:
                return None
            moov_start, moov_end = moov
            f.seek(moov_start)
            mvhd = _find_box(f, b"mvhd", moov_end)
            if mvhd is None:
                return None
            f.seek(mvhd[0])
            version_flags = f.read(4)
            if len(version_flags) < 4:
                return None
            if version_flags[0] == 1:
                buf = f.read(28)        # ctime(8) mtime(8) timescale(4) dur(8)
                if len(buf) < 28:
                    return None
                timescale = int.from_bytes(buf[16:20], "big")
                duration = int.from_bytes(buf[20:28], "big")
            else:
                buf = f.read(16)        # ctime(4) mtime(4) timescale(4) dur(4)
                if len(buf) < 16:
                    return None
                timescale = int.from_bytes(buf[8:12], "big")
                duration = int.from_bytes(buf[12:16], "big")
    except (OSError, ValueError):
        return None
    if timescale <= 0 or duration in _MVHD_UNKNOWN:
        return None
    secs = duration / timescale
    return secs if secs > 0 else None


async def _probe_with_method(path: str) -> tuple[float | None, str | None]:
    """``(duration, method)`` where ``method`` is ``"mvhd"``, ``"ffprobe"``
    or ``None``. The sweep uses this to report how clips were resolved;
    :func:`probe_duration` is the value-only wrapper."""
    secs = await asyncio.to_thread(_probe_duration_mvhd, path)
    if secs is not None:
        return secs, "mvhd"
    secs = await _probe_duration_ffprobe(path)
    return (secs, "ffprobe") if secs is not None else (None, None)


async def probe_duration(path: str) -> float | None:
    """Clip length in seconds. Fast path parses the MP4 ``mvhd`` box
    directly (no subprocess); falls back to ffprobe for anything that
    doesn't parse (odd containers, damaged moov, non-MP4)."""
    secs, _ = await _probe_with_method(path)
    return secs


async def _probe_duration_ffprobe(path: str) -> float | None:
    """Clip length in seconds via ffprobe, or None if ffprobe is
    missing / the probe fails / the value is non-positive."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_FFPROBE_TIMEOUT_S,
        )
    except TimeoutError:
        # Kill and reap — abandoning the child left it running
        # (possibly stuck on NAS I/O) and a zombie once it exited.
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    except OSError:
        return None
    try:
        d = float(out.decode().strip())
    except ValueError:
        return None
    return d if d > 0 else None



async def probe_and_store(db: Database, clip_id: int, path: str) -> float | None:
    """Probe one clip's duration (probe_duration) and persist it. Returns
    the duration or None on failure. Idempotent: callers gate on a missing
    duration_s, so this only runs when needed."""
    dur = await probe_duration(path)
    if dur and dur > 0:
        with db.write() as c:
            c.execute(
                "UPDATE clip_index SET duration_s=? WHERE id=?", (dur, clip_id)
            )
    return dur


async def ensure_gps(db: Database, clip_id: int, path: str) -> None:
    """Extract GPS if not yet examined; mark examined either way so the
    clip isn't re-probed. Best-effort; clips without a GPS lock are normal."""
    with db.conn() as c:
        row = c.execute(
            "SELECT gps_examined FROM clip_index WHERE id=?", (clip_id,)
        ).fetchone()
    if row and row["gps_examined"]:
        return
    try:
        await asyncio.to_thread(vfs.extract_gps_data, path)
    except Exception:
        pass  # no GPS lock / unreadable atom — still mark examined
    # scanner._iter_clips uses ``path + ".gpx"`` (appended to full path,
    # extension included, e.g. foo.MP4.gpx) — match that exactly.
    sidecar = path + ".gpx"
    with db.write() as c:
        c.execute(
            "UPDATE clip_index SET has_gpx=?, gps_examined=1 WHERE id=?",
            (1 if os.path.exists(sidecar) else 0, clip_id),
        )
