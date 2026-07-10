"""Named-location helpers: name_for_point + exclusion_zones."""
from __future__ import annotations

from types import SimpleNamespace

from web.services import locations


def _p(name, lat, lon, radius, exclude=False, is_home=False):
    return SimpleNamespace(
        name=name, lat=lat, lon=lon, radius_m=radius,
        exclude_recordings=exclude, is_home=is_home,
    )


def test_name_for_point_inside_radius() -> None:
    places = [_p("Home", 53.1, -2.0, 30)]
    assert locations.name_for_point(places, 53.1, -2.0) == "Home"


def test_name_for_point_outside_radius() -> None:
    places = [_p("Home", 53.1, -2.0, 30)]
    # ~11 km north — well outside 30 m.
    assert locations.name_for_point(places, 53.2, -2.0) is None


def test_name_for_point_first_match_wins() -> None:
    places = [_p("Big", 53.1, -2.0, 100_000), _p("Small", 53.1, -2.0, 30)]
    assert locations.name_for_point(places, 53.1, -2.0) == "Big"


def test_name_for_point_empty() -> None:
    assert locations.name_for_point([], 53.1, -2.0) is None


def test_exclusion_zones_filters_by_flag() -> None:
    a = _p("A", 0, 0, 30, exclude=True)
    b = _p("B", 0, 0, 30, exclude=False)
    assert locations.exclusion_zones([a, b]) == [a]
    assert locations.exclusion_zones([]) == []


def test_match_for_point_returns_place_with_is_home() -> None:
    places = [_p("Home", 53.0, -2.0, 5000, is_home=True)]
    p = locations.match_for_point(places, 53.01, -2.0)
    assert p is not None and p.name == "Home" and p.is_home is True


def test_match_for_point_first_match_wins() -> None:
    places = [_p("Big", 53.0, -2.0, 5000), _p("Small", 53.0, -2.0, 50)]
    assert locations.match_for_point(places, 53.01, -2.0).name == "Big"


def test_match_for_point_none_outside() -> None:
    assert locations.match_for_point([_p("Home", 53.0, -2.0, 30)], 60.0, 0.0) is None
