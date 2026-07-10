"""Shared GPX path gathering for a day.

The archive route view and the geofence evaluator both need "the GPX tracks
for this day": downloaded clips' real ``.gpx`` sidecars plus the ``.triage/``
skeletons of queued, not-yet-downloaded clips. Defining it once keeps the
geofence's stop detection aligned with what the journey map shows.

The route view wants the in-flight skeletons (``pending``/``failed``/
``downloading``) so every clip shown as a grid tile has a matching track on the
map; skipped clips drop off the map unless the archive's opt-in "GPS-excluded"
view adds geofence-skipped skeletons back via
:func:`geofence_skipped_gpx_paths`. The geofence evaluator passes a broader
``queue_states`` so a clip already auto-skipped still contributes its track to
dwell detection.
"""
from __future__ import annotations

import os
from typing import Sequence

from ..db import Database
from . import triage as triage_service
from .naming import day_key_sql


def day_gpx_paths(
    db: Database,
    recordings: str,
    date: str,
    queue_states: Sequence[str] = ("pending", "failed", "downloading"),
) -> list[str]:
    """Return the GPX paths for ``date`` (``YYYY-MM-DD``): downloaded sidecars
    from ``clip_index`` plus triage skeletons for queued clips in
    ``queue_states`` (with a GPS fix), de-duped against downloaded clips by
    basename."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT path FROM clip_index "
            "WHERE group_name = ? AND has_gpx = 1 "
            "ORDER BY timestamp ASC",
            (date,),
        ).fetchall()
    gpx_paths = [r["path"] + ".gpx" for r in rows]
    downloaded_names = {os.path.basename(r["path"]) for r in rows}

    placeholders = ",".join("?" * len(queue_states))
    day_expr = day_key_sql()
    with db.conn() as c:
        q_rows = c.execute(
            f"SELECT filename FROM download_queue "
            f"WHERE state IN ({placeholders}) "
            f"  AND triaged_at IS NOT NULL AND gps_points > 0 "
            f"  AND {day_expr} = ?",
            (*queue_states, date),
        ).fetchall()
    for qr in q_rows:
        if qr["filename"] in downloaded_names:
            continue
        sk = triage_service.skeleton_path(recordings, qr["filename"])
        if os.path.exists(sk):
            gpx_paths.append(sk)
    return gpx_paths


def geofence_skipped_gpx_paths(
    db: Database, recordings: str, date: str,
) -> list[str]:
    """Triage-skeleton paths for ``date``'s geofence-skipped clips
    (``state='skipped' AND skip_reason='geofence'``, with a GPS fix).
    Powering the archive's opt-in "GPS-excluded" view; kept separate from
    :func:`day_gpx_paths` so the defaults every other caller relies on
    (geofence evaluator included) cannot drift. User-skipped clips are
    never returned — a manual skip means "don't show me this"."""
    day_expr = day_key_sql()
    with db.conn() as c:
        rows = c.execute(
            f"SELECT filename FROM download_queue "
            f"WHERE state = 'skipped' AND skip_reason = 'geofence' "
            f"  AND triaged_at IS NOT NULL AND gps_points > 0 "
            f"  AND {day_expr} = ?",
            (date,),
        ).fetchall()
    out: list[str] = []
    for r in rows:
        sk = triage_service.skeleton_path(recordings, r["filename"])
        if os.path.exists(sk):
            out.append(sk)
    return out


def day_parking_spans(db: Database, date: str) -> list[tuple[float, float]]:
    """``(start, end)`` spans for ``date``'s parking-mode clips, unioned across
    downloaded clips (``clip_index``, with their real ``duration_s``) and
    queued-but-not-yet-downloaded clips (``download_queue``, a *point* span at
    ``recorded_at`` since the queue has no duration). Feeds the journey-window
    parking bound (``gps.expand_journey_window``) so the "car is parked" hard
    edge is honoured even before the parking clips are downloaded — the common
    case under GPS triage, where the day's parking clips live only in the queue.
    Overlapping/duplicate spans are harmless: the consumer reduces with min/max.
    """
    day_expr = day_key_sql()
    spans: list[tuple[float, float]] = []
    with db.conn() as c:
        for r in c.execute(
            "SELECT timestamp, duration_s FROM clip_index "
            "WHERE group_name = ? AND event_type = 'parking'",
            (date,),
        ):
            spans.append((r["timestamp"], r["timestamp"] + (r["duration_s"] or 0.0)))
        for r in c.execute(
            f"SELECT recorded_at FROM download_queue "
            f"WHERE event_type = 'parking' AND recorded_at IS NOT NULL "
            f"  AND {day_expr} = ?",
            (date,),
        ):
            spans.append((r["recorded_at"], r["recorded_at"]))
    return spans
