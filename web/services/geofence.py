"""GPS geofence exclusion — auto-skip queued clips parked at home.

Reuses the stop detector in :mod:`web.services.gps` (a stop is already a
>= 5-min dwell) over a day's GPX tracks. A *home stop* is a detected stop
whose centre lies within a configured zone's radius; the queued clips whose
``recorded_at`` falls inside a home stop are marked ``skipped``/``geofence``.

Pure-ish: detection here, mutations delegated to :mod:`web.services.queue`.
Off / inert unless at least one location is flagged for exclusion (and
GPS_TRIAGE, checked by the caller — without triaged skeletons there are no
tracks).
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from ..db import Database
from . import day_tracks, gps
from . import queue as q
from . import triage as triage_service

log = logging.getLogger("viofosync.geofence")

# Geofence detection considers a clip's track even after it has been
# auto-skipped, so re-evaluation still sees the full home dwell. Shared with
# the orphan sweep so skipped skeletons aren't deleted out from under us.
_DETECT_STATES = triage_service.SKELETON_KEEP_STATES

# A clip's recorded_at (filename second) precedes its first GPS fix by the
# receiver's acquisition lag (≈1 s driving, tens of seconds for a parking clip
# that re-acquires). A home stop's window starts at that first fix, so the
# dwell's leading clip lands just before it. Pad the leading edge by one
# nominal clip length to pull it in. Larger would risk swallowing the
# preceding pull-in drive clip (which sits a full clip + lag earlier).
LEADING_EDGE_PAD_S = 60


def home_stops(stops: Sequence[gps.Stop], zones: Sequence) -> list[gps.Stop]:
    """Stops whose centre is within any zone's radius (metres)."""
    out: list[gps.Stop] = []
    for s in stops:
        for z in zones:
            if gps._haversine_ll(
                s.center_lat, s.center_lon, z.lat, z.lon
            ) <= z.radius_m:
                out.append(s)
                break
    return out


def evaluate_day(db: Database, recordings: str, date: str, zones: Sequence) -> list[str]:
    """Auto-skip queued clips on ``date`` that dwell inside a home zone.
    Returns the filenames skipped (empty when no zones / no home stop)."""
    if not zones:
        return []
    candidates = q.geofence_candidates(db, date)
    if not candidates:
        return []
    paths = day_tracks.day_gpx_paths(
        db, recordings, date, queue_states=_DETECT_STATES
    )
    _points, stops, journeys = gps.aggregate_day(paths)
    home = home_stops(stops, zones)
    if not home:
        return []
    windows = [(s.start_time.timestamp(), s.end_time.timestamp()) for s in home]
    # A clip that's part of a drive must not be skipped even if its timestamp
    # dwells in a home zone — the journey wins over the home dwell, mirroring the
    # archive grid's "journey beats stop" grouping. Use the same padded journey
    # windows (parking-bounded via gps.expand_journey_window) so the pull-away /
    # pull-in clips at the dwell edge survive; only genuinely-parked clips skip.
    parking = day_tracks.day_parking_spans(db, date)
    drives = [
        gps.expand_journey_window(
            j.start_time.timestamp(), j.end_time.timestamp(), parking
        )
        for j in journeys
    ]

    def _in_drive(ts: float) -> bool:
        return any(lo <= ts <= hi for lo, hi in drives)

    to_skip = [
        c["filename"]
        for c in candidates
        if any(
            lo - LEADING_EDGE_PAD_S <= c["recorded_at"] <= hi
            for lo, hi in windows
        )
        and not _in_drive(c["recorded_at"])
    ]
    if to_skip:
        q.geofence_skip(db, to_skip)
    return to_skip


def sweep_all(
    db: Database, recordings: str, zones: Sequence, *, seen: dict | None = None,
) -> int:
    """Evaluate every day that currently has pending clips. Returns the total
    number auto-skipped. Used for the per-cycle pass and the on-enable backfill.

    ``seen`` (optional) is a caller-owned ``{day: signature}`` cache. When
    given, a day is only re-evaluated if its triaged-skeleton signature changed
    since the cached value, so steady-state ticks skip the GPX re-parse. When
    ``None`` (a full sweep), every pending day is evaluated."""
    if not zones:
        return 0
    sigs = (
        q.geofence_day_signatures(db, _DETECT_STATES)
        if seen is not None else None
    )
    total = 0
    for day in q.pending_days(db):
        if seen is not None:
            sig = sigs.get(day, 0)
            if seen.get(day) == sig:
                continue
            seen[day] = sig
        total += len(evaluate_day(db, recordings, day, zones))
    if total:
        log.info("geofence: auto-skipped %d clip(s) parked at home", total)
    return total
