"""Settings for the named-locations feature (replaces geofence settings)."""
from __future__ import annotations

import json
import tempfile

import pytest
from pydantic import ValidationError

from web.settings import Place, SettingsProvider
from web.settings_schema import (
    EDITABLE_KEYS,
    Location,
    SettingsModel,
    normalize_locations,
    validate_partial,
)


def test_defaults_empty() -> None:
    assert SettingsModel().LOCATIONS == []
    assert "LOCATIONS" in EDITABLE_KEYS
    # Old geofence keys are gone.
    assert "GEOFENCE_ENABLED" not in EDITABLE_KEYS
    assert "GEOFENCE_ZONES" not in EDITABLE_KEYS


def test_location_defaults_and_bounds() -> None:
    loc = Location(lat=53.1, lon=-2.0)
    assert loc.name == "Home"
    assert loc.radius_m == 30
    assert loc.exclude_recordings is False
    Location(lat=0, lon=0, radius_m=5)
    Location(lat=0, lon=0, radius_m=500)
    with pytest.raises(ValidationError):
        Location(lat=0, lon=0, radius_m=4)
    with pytest.raises(ValidationError):
        Location(lat=91, lon=0)
    with pytest.raises(ValidationError):
        Location(lat=0, lon=181)


def test_validate_partial_roundtrips_location_list() -> None:
    out = validate_partial(
        {"LOCATIONS": [{"lat": 53.1, "lon": -2.0, "exclude_recordings": True}]}
    )
    assert out["LOCATIONS"] == [
        {"name": "Home", "lat": 53.1, "lon": -2.0,
         "radius_m": 30, "exclude_recordings": True, "is_home": True}
    ]


def test_snapshot_exposes_locations() -> None:
    with tempfile.TemporaryDirectory() as d:
        sp = SettingsProvider(
            config_path=f"{d}/c.json", env_file_path=f"{d}/v.env", recordings_dir=d
        )
        assert sp.get().locations == ()


def test_location_persists_and_reinflates() -> None:
    with tempfile.TemporaryDirectory() as d:
        cfg = f"{d}/c.json"
        sp = SettingsProvider(
            config_path=cfg, env_file_path=f"{d}/v.env", recordings_dir=d
        )
        sp.update(
            {"LOCATIONS": [{"lat": 53.1, "lon": -2.0, "radius_m": 40,
                            "exclude_recordings": True}]},
            actor="test",
        )
        sp2 = SettingsProvider(
            config_path=cfg, env_file_path=f"{d}/v.env", recordings_dir=d
        )
        assert sp2.get().locations == (
            Place(name="Home", lat=53.1, lon=-2.0, radius_m=40,
                  exclude_recordings=True, is_home=True),
        )


def test_migrates_geofence_zones_to_locations() -> None:
    with tempfile.TemporaryDirectory() as d:
        cfg = f"{d}/c.json"
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump(
                {"WEB_PASSWORD_HASH": "x", "GEOFENCE_ENABLED": True,
                 "GEOFENCE_ZONES": [{"lat": 53.1, "lon": -2.0, "radius_m": 40}]},
                f,
            )
        sp = SettingsProvider(
            config_path=cfg, env_file_path=f"{d}/v.env", recordings_dir=d
        )
        assert sp.get().locations == (
            Place(name="Home", lat=53.1, lon=-2.0, radius_m=40,
                  exclude_recordings=True, is_home=True),
        )
        # Old keys removed from the rewritten config.
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
        assert "GEOFENCE_ZONES" not in data
        assert "GEOFENCE_ENABLED" not in data
        assert "LOCATIONS" in data


def test_migration_preserves_disabled_state() -> None:
    with tempfile.TemporaryDirectory() as d:
        cfg = f"{d}/c.json"
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump(
                {"WEB_PASSWORD_HASH": "x", "GEOFENCE_ENABLED": False,
                 "GEOFENCE_ZONES": [{"lat": 53.1, "lon": -2.0, "radius_m": 30}]},
                f,
            )
        sp = SettingsProvider(
            config_path=cfg, env_file_path=f"{d}/v.env", recordings_dir=d
        )
        locs = sp.get().locations
        assert len(locs) == 1
        assert locs[0].exclude_recordings is False


def test_migration_skips_malformed_zone() -> None:
    with tempfile.TemporaryDirectory() as d:
        cfg = f"{d}/c.json"
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump(
                {"WEB_PASSWORD_HASH": "x", "GEOFENCE_ENABLED": True,
                 "GEOFENCE_ZONES": [{"radius_m": 30}]},  # no lat/lon
                f,
            )
        # Must not raise at construction; the malformed zone is dropped.
        sp = SettingsProvider(
            config_path=cfg, env_file_path=f"{d}/v.env", recordings_dir=d
        )
        assert sp.get().locations == ()


def test_location_is_home_defaults_false() -> None:
    assert Location(lat=53.0, lon=-2.0).is_home is False


def test_normalize_empty_unchanged() -> None:
    assert normalize_locations([]) == []


def test_normalize_promotes_first_when_none_home() -> None:
    out = normalize_locations([
        {"name": "A", "lat": 1.0, "lon": 1.0, "radius_m": 30},
        {"name": "B", "lat": 2.0, "lon": 2.0, "radius_m": 30},
    ])
    assert [loc["is_home"] for loc in out] == [True, False]


def test_normalize_keeps_first_when_multiple_home() -> None:
    out = normalize_locations([
        {"name": "A", "lat": 1.0, "lon": 1.0, "radius_m": 30, "is_home": True},
        {"name": "B", "lat": 2.0, "lon": 2.0, "radius_m": 30, "is_home": True},
    ])
    assert [loc["is_home"] for loc in out] == [True, False]


def test_normalize_single_home_unchanged() -> None:
    out = normalize_locations([
        {"name": "A", "lat": 1.0, "lon": 1.0, "radius_m": 30, "is_home": False},
        {"name": "B", "lat": 2.0, "lon": 2.0, "radius_m": 30, "is_home": True},
    ])
    assert [loc["is_home"] for loc in out] == [False, True]


def test_validate_partial_normalizes_locations() -> None:
    out = validate_partial({"LOCATIONS": [
        {"name": "A", "lat": 1.0, "lon": 1.0, "radius_m": 30},
        {"name": "B", "lat": 2.0, "lon": 2.0, "radius_m": 30},
    ]})
    assert [loc["is_home"] for loc in out["LOCATIONS"]] == [True, False]


def test_snapshot_exposes_is_home(tmp_path) -> None:
    sp = SettingsProvider(config_path=tmp_path / "config.json")
    sp.update({"LOCATIONS": [
        {"name": "Home", "lat": 53.0, "lon": -2.0, "radius_m": 30, "is_home": True},
        {"name": "Office", "lat": 54.0, "lon": -1.0, "radius_m": 50},
    ]}, actor="test")
    snap = sp.get()
    assert snap.locations[0].is_home is True
    assert snap.locations[1].is_home is False


def test_legacy_location_without_is_home_migrates(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"LOCATIONS": [
        {"name": "Home", "lat": 53.0, "lon": -2.0, "radius_m": 30,
         "exclude_recordings": False},
    ]}))
    sp = SettingsProvider(config_path=cfg)
    assert sp.get().locations[0].is_home is True
    # the invariant is written back to disk, not just applied per-read
    on_disk = json.loads(cfg.read_text())
    assert on_disk["LOCATIONS"][0]["is_home"] is True


def test_editable_values_includes_is_home(tmp_path) -> None:
    from web.routers.settings import _editable_values
    sp = SettingsProvider(config_path=tmp_path / "config.json")
    sp.update({"LOCATIONS": [
        {"name": "Home", "lat": 53.0, "lon": -2.0, "radius_m": 30, "is_home": True},
    ]}, actor="t")
    vals = _editable_values(sp.get())
    assert vals["LOCATIONS"][0]["is_home"] is True
