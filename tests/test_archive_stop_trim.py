"""_trim_stops_to_journeys shrinks each parking stop's window (and its shown
duration) so it no longer overlaps an adjacent journey's PADDED window — the
padded journey claims the pull-away/pull-in clips, so the stop should not also
advertise that time. A stop entirely absorbed by journey padding is dropped."""
from __future__ import annotations

import datetime as _dt

from web.routers.archive import _trim_stops_to_journeys


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


def _journey(gs: float, ge: float) -> dict:
    # Only the padded window fields matter to the trim.
    return {"start_ts": gs, "end_ts": ge,
            "group_start_ts": gs, "group_end_ts": ge}


def _stop(ss: float, se: float) -> dict:
    return {
        "start_ts": ss, "end_ts": se,
        "start_time": _iso(ss), "end_time": _iso(se),
        "duration_s": int(se - ss),
        "lat": 1.0, "lon": 2.0, "label": None,
    }


def test_front_overlap_trims_stop_start():
    # Journey padded end (260) reaches 60 s into the following stop [200, 500].
    payload = {"journeys": [_journey(100, 260)], "stops": [_stop(200, 500)]}
    _trim_stops_to_journeys(payload)
    s = payload["stops"][0]
    assert s["start_ts"] == 260
    assert s["end_ts"] == 500
    assert s["duration_s"] == 240
    assert s["start_time"] == _iso(260)


def test_back_overlap_trims_stop_end():
    # Following journey's padded start (540) reaches back into stop [200, 560].
    payload = {"journeys": [_journey(540, 800)], "stops": [_stop(200, 560)]}
    _trim_stops_to_journeys(payload)
    s = payload["stops"][0]
    assert s["start_ts"] == 200
    assert s["end_ts"] == 540
    assert s["duration_s"] == 340
    assert s["end_time"] == _iso(540)


def test_stop_between_two_journeys_trimmed_both_ends():
    payload = {
        "journeys": [_journey(50, 260), _journey(540, 800)],
        "stops": [_stop(200, 560)],
    }
    _trim_stops_to_journeys(payload)
    s = payload["stops"][0]
    assert (s["start_ts"], s["end_ts"]) == (260, 540)
    assert s["duration_s"] == 280


def test_fully_absorbed_stop_is_dropped():
    # Short stop [200, 260]; the two flanking paddings meet inside it.
    payload = {
        "journeys": [_journey(50, 258), _journey(255, 400)],
        "stops": [_stop(200, 260)],
    }
    _trim_stops_to_journeys(payload)
    assert payload["stops"] == []


def test_isolated_stop_is_untouched():
    payload = {"journeys": [_journey(50, 260)], "stops": [_stop(1000, 1300)]}
    before = dict(payload["stops"][0])
    _trim_stops_to_journeys(payload)
    assert payload["stops"][0] == before


def test_no_journeys_leaves_stops_alone():
    payload = {"journeys": [], "stops": [_stop(200, 500)]}
    before = dict(payload["stops"][0])
    _trim_stops_to_journeys(payload)
    assert payload["stops"][0] == before
