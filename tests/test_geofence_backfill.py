"""Pure decision helper for the geofence backfill subscriber."""
from __future__ import annotations

from types import SimpleNamespace

from web.app import _geofence_backfill_needed


def _snap(**kw):
    return SimpleNamespace(
        gps_triage=kw.get("gps_triage", True),
        locations=kw.get(
            "locations", (SimpleNamespace(exclude_recordings=True),)
        ),
    )


def test_triggers_on_locations_change() -> None:
    assert _geofence_backfill_needed({"LOCATIONS"}, _snap()) is True


def test_ignores_unrelated_keys() -> None:
    assert _geofence_backfill_needed({"ADDRESS"}, _snap()) is False


def test_requires_triage_on() -> None:
    assert _geofence_backfill_needed({"LOCATIONS"}, _snap(gps_triage=False)) is False


def test_requires_an_exclusion_location() -> None:
    assert _geofence_backfill_needed(
        {"LOCATIONS"}, _snap(locations=(SimpleNamespace(exclude_recordings=False),))
    ) is False
    assert _geofence_backfill_needed({"LOCATIONS"}, _snap(locations=())) is False
