"""Named locations (e.g. "Home").

A location is any object exposing ``name``, ``lat``, ``lon``, ``radius_m`` (and,
for exclusion, ``exclude_recordings``; and ``is_home`` for the designated Home)
— the ``Place`` snapshot dataclass. Two consumers: the archive label override
(``match_for_point``, with ``name_for_point`` as a name-only convenience wrapper)
and the geofence exclusion (``exclusion_zones``). Pure; no DB, no settings import.
"""
from __future__ import annotations

from collections.abc import Sequence

from . import gps


def match_for_point(places: Sequence, lat: float, lon: float):
    """First place whose centre is within its radius (metres) of (lat, lon),
    else None. The returned object exposes ``.name`` and ``.is_home``."""
    for p in places:
        if gps._haversine_ll(p.lat, p.lon, lat, lon) <= p.radius_m:
            return p
    return None


def name_for_point(places: Sequence, lat: float, lon: float) -> str | None:
    """Name of the first place matching (lat, lon), else None."""
    p = match_for_point(places, lat, lon)
    return p.name if p is not None else None


def exclusion_zones(places: Sequence) -> list:
    """Places flagged ``exclude_recordings`` — the geofence's input zones."""
    return [p for p in places if p.exclude_recordings]
