"""The camera registry designates exactly one GPS-bearing lens (front)."""
from __future__ import annotations

from viofosync_lib.cameras import (
    CAMERAS,
    GPS_CAMERA_LETTER,
    is_gps_camera,
)


def test_exactly_one_gps_camera_and_it_is_front():
    gps_cams = [c for c in CAMERAS if c.gps]
    assert len(gps_cams) == 1
    assert gps_cams[0].channel == "front"
    assert GPS_CAMERA_LETTER == gps_cams[0].letter


def test_is_gps_camera_matches_front_only():
    assert is_gps_camera("F") is True
    assert is_gps_camera("f") is True          # case-insensitive
    assert is_gps_camera("PF") is True         # parking prefix, last letter F
    assert is_gps_camera("EF") is True         # event prefix
    assert is_gps_camera("R") is False
    assert is_gps_camera("T") is False
    assert is_gps_camera("PI") is False        # parking interior
    assert is_gps_camera("") is False
    assert is_gps_camera(None) is False
