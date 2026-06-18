"""Tests for the pure timeline-export piece builder."""
from __future__ import annotations

from web.services.exporter import build_switch_pieces


def _clips():
    return [
        {"path": "/f0.mp4", "channel": "front", "start_ts": 1000, "duration_s": 60},
        {"path": "/f1.mp4", "channel": "front", "start_ts": 1060, "duration_s": 60},
        {"path": "/r0.mp4", "channel": "rear",  "start_ts": 1000, "duration_s": 60},
    ]


def test_single_segment_within_one_clip():
    segs = [{"channel": "rear", "start_ts": 1010, "end_ts": 1040}]
    pieces = build_switch_pieces(segs, _clips())
    assert pieces == [
        {"path": "/r0.mp4", "ss": 10.0, "t": 30.0, "ts": 1010.0, "pip": None},
    ]


def test_segment_spans_two_clips():
    segs = [{"channel": "front", "start_ts": 1030, "end_ts": 1090}]
    pieces = build_switch_pieces(segs, _clips())
    assert pieces == [
        {"path": "/f0.mp4", "ss": 30.0, "t": 30.0, "ts": 1030.0, "pip": None},
        {"path": "/f1.mp4", "ss": 0.0, "t": 30.0, "ts": 1060.0, "pip": None},
    ]


def test_switch_between_cameras_in_order():
    segs = [
        {"channel": "rear", "start_ts": 1000, "end_ts": 1020},
        {"channel": "front", "start_ts": 1020, "end_ts": 1050},
    ]
    pieces = build_switch_pieces(segs, _clips())
    assert pieces == [
        {"path": "/r0.mp4", "ss": 0.0, "t": 20.0, "ts": 1000.0, "pip": None},
        {"path": "/f0.mp4", "ss": 20.0, "t": 30.0, "ts": 1020.0, "pip": None},
    ]


def test_zero_width_and_missing_channel_skipped():
    segs = [
        {"channel": "front", "start_ts": 1000, "end_ts": 1000.02},
        {"channel": "interior", "start_ts": 1000, "end_ts": 1030},
    ]
    assert build_switch_pieces(segs, _clips()) == []


def test_pip_channel_propagates_to_pieces():
    segs = [{"channel": "front", "start_ts": 1010, "end_ts": 1040, "pip": "rear"}]
    pieces = build_switch_pieces(segs, _clips())
    assert pieces == [
        {"path": "/f0.mp4", "ss": 10.0, "t": 30.0, "ts": 1010.0, "pip": "rear"},
    ]
