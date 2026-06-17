"""Tests for the timeline PiP overlay filter + partner-clip resolver."""
from __future__ import annotations

from web.services.exporter import _find_clip_at, _timeline_pip_filter


# --- _timeline_pip_filter (software dialect) ---

def _sw(coords: str) -> str:
    return (
        "[0:v]scale=1920:1080,setsar=1[base];"
        "[1:v]scale=480:270,setsar=1[pip];"
        f"[base][pip]overlay={coords}"
    )


def test_software_top_right_default():
    assert _timeline_pip_filter(1920, 1080, "top_right") == _sw("W-w-20:20")


def test_software_top_left():
    assert _timeline_pip_filter(1920, 1080, "top_left") == _sw("20:20")


def test_software_bottom_right():
    assert _timeline_pip_filter(1920, 1080, "bottom_right") == _sw("W-w-20:H-h-20")


def test_software_bottom_left():
    assert _timeline_pip_filter(1920, 1080, "bottom_left") == _sw("20:H-h-20")


def test_unknown_position_falls_back_to_top_right():
    assert _timeline_pip_filter(1920, 1080, "middle") == _sw("W-w-20:20")


def test_qsv_dialect_uses_gpu_filters_and_xy_coords():
    assert _timeline_pip_filter(1920, 1080, "top_right", encoder="qsv") == (
        "[0:v]scale_qsv=w=1920:h=1080[base];"
        "[1:v]scale_qsv=w=480:h=270[pip];"
        "[base][pip]overlay_qsv=x=W-w-20:y=20"
    )


# --- _find_clip_at ---

def _clips():
    return [
        {"path": "/f0.mp4", "channel": "front", "start_ts": 1000, "duration_s": 60},
        {"path": "/r0.mp4", "channel": "rear",  "start_ts": 1000, "duration_s": 60},
        {"path": "/r1.mp4", "channel": "rear",  "start_ts": 1060, "duration_s": 60},
    ]


def test_find_clip_at_returns_covering_clip():
    c = _find_clip_at(_clips(), "rear", 1075)
    assert c is not None and c["path"] == "/r1.mp4"


def test_find_clip_at_respects_channel():
    c = _find_clip_at(_clips(), "front", 1030)
    assert c is not None and c["path"] == "/f0.mp4"


def test_find_clip_at_none_when_no_coverage():
    assert _find_clip_at(_clips(), "rear", 5000) is None
    assert _find_clip_at(_clips(), "interior", 1030) is None
