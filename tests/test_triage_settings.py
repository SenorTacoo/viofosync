# tests/test_triage_settings.py
"""GPS_TRIAGE is an opt-in boolean exposed through the settings provider."""
from __future__ import annotations

from web.settings import SettingsProvider
from web.settings_schema import DEFAULT_VALUES, EDITABLE_KEYS, validate_partial


def test_default_off_and_editable():
    assert DEFAULT_VALUES["GPS_TRIAGE"] is False
    assert "GPS_TRIAGE" in EDITABLE_KEYS
    assert validate_partial({"GPS_TRIAGE": True}) == {"GPS_TRIAGE": True}


def test_snapshot_exposes_gps_triage(tmp_config_dir, tmp_recordings_dir):
    p = SettingsProvider()
    assert p.get().gps_triage is False
    snap = p.update({"GPS_TRIAGE": True}, actor="test")
    assert snap.gps_triage is True
