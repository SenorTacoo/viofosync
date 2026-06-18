"""Thumbnail fail-cache: mark_failed / failed_recently behaviour.

Regression: ``ensure_thumb`` returned None on ffmpeg failure and left no
marker, so un-thumbable clips (short/corrupt/partial) were re-selected and
re-run through ffmpeg on every sweep. With a sweep after every working
cycle (and on pause) that was a recurring CPU storm.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from web.services import thumbs


def test_mark_failed_then_skipped(tmp_path: Path):
    rec = tmp_path / "rec"
    rec.mkdir()
    video = rec / "clip.MP4"
    video.write_bytes(b"not a real video")
    # A fresh failure marker (recorded after the video was written) means
    # "don't bother trying again until the file changes".
    thumbs.mark_failed(str(rec), 1)
    assert thumbs.failed_recently(str(rec), 1, str(video)) is True


def test_stale_marker_retried_after_file_changes(tmp_path: Path):
    rec = tmp_path / "rec"
    rec.mkdir()
    video = rec / "clip.MP4"
    video.write_bytes(b"old")
    thumbs.mark_failed(str(rec), 1)
    # The clip is later rewritten (e.g. a partial import got redone) — its
    # mtime moves past the marker, so the thumb is worth another attempt.
    time.sleep(0.01)
    os.utime(str(video), None)
    assert thumbs.failed_recently(str(rec), 1, str(video)) is False
