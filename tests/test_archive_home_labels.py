"""Named-location label override in _apply_labels + the /geocode route."""
from __future__ import annotations

from types import SimpleNamespace

from web.routers import archive


def _place(name, lat, lon, r, is_home=False):
    return SimpleNamespace(name=name, lat=lat, lon=lon, radius_m=r,
                           exclude_recordings=False, is_home=is_home)


class _Geo:
    def cache_lookup(self, lat, lon):
        return "Somewhere"


def test_apply_labels_home_override() -> None:
    payload = {
        "journeys": [{"start_lat": 53.1, "start_lon": -2.0,
                      "end_lat": 50.0, "end_lon": 0.0}],
        "stops": [{"lat": 53.1, "lon": -2.0}],
    }
    archive._apply_labels(payload, _Geo(), (_place("Home", 53.1, -2.0, 30, is_home=True),))
    j = payload["journeys"][0]
    assert j["start_label"] == "Home" and j["start_home"] is True
    assert j["start_named"] is True
    assert j["end_label"] == "Somewhere" and j["end_home"] is False
    assert j["end_named"] is False
    s = payload["stops"][0]
    assert s["label"] == "Home" and s["home"] is True


def test_apply_labels_no_places_falls_back() -> None:
    payload = {"journeys": [], "stops": [{"lat": 53.1, "lon": -2.0}]}
    archive._apply_labels(payload, _Geo(), ())
    assert payload["stops"][0]["label"] == "Somewhere"
    assert payload["stops"][0]["home"] is False


def test_apply_labels_home_without_geocoder() -> None:
    payload = {"journeys": [], "stops": [{"lat": 53.1, "lon": -2.0}]}
    archive._apply_labels(payload, None, (_place("Home", 53.1, -2.0, 30, is_home=True),))
    assert payload["stops"][0]["label"] == "Home"
    assert payload["stops"][0]["home"] is True


async def test_geocode_route_non_home_shape() -> None:
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings_provider=SimpleNamespace(get=lambda: SimpleNamespace(locations=())),
        geocode=None,
    )))
    out = await archive.geocode(req, 10.0, 10.0)
    assert out["label"] is None
    assert out["home"] is False


async def test_geocode_route_returns_home() -> None:
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings_provider=SimpleNamespace(
            get=lambda: SimpleNamespace(locations=(_place("Home", 53.1, -2.0, 30, is_home=True),))
        ),
        geocode=None,
    )))
    out = await archive.geocode(req, 53.1, -2.0)
    assert out["label"] == "Home"
    assert out["home"] is True
    assert out["named"] is True


def test_apply_labels_named_non_home() -> None:
    payload = {"journeys": [], "stops": [{"lat": 53.1, "lon": -2.0}]}
    archive._apply_labels(payload, _Geo(), (_place("Office", 53.1, -2.0, 30),))
    s = payload["stops"][0]
    assert s["label"] == "Office"
    assert s["home"] is False
    assert s["named"] is True


def test_apply_labels_home_is_also_named() -> None:
    payload = {"journeys": [], "stops": [{"lat": 53.1, "lon": -2.0}]}
    archive._apply_labels(payload, _Geo(), (_place("Home", 53.1, -2.0, 30, is_home=True),))
    s = payload["stops"][0]
    assert s["home"] is True and s["named"] is True


def test_apply_labels_geocode_not_named() -> None:
    payload = {"journeys": [], "stops": [{"lat": 53.1, "lon": -2.0}]}
    archive._apply_labels(payload, _Geo(), ())
    s = payload["stops"][0]
    assert s["label"] == "Somewhere" and s["home"] is False and s["named"] is False


async def test_geocode_route_named_non_home() -> None:
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings_provider=SimpleNamespace(
            get=lambda: SimpleNamespace(locations=(_place("Office", 53.1, -2.0, 30),))
        ),
        geocode=None,
    )))
    out = await archive.geocode(req, 53.1, -2.0)
    assert out["label"] == "Office" and out["home"] is False and out["named"] is True
