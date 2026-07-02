"""list_page / list_day_items expose a derived gps_state per row."""
from __future__ import annotations

import time

from web.db import Database
from web.services import queue as q
from web.services.triage import TRIAGE_MAX_ATTEMPTS


def _seed(db, filename, **cols):
    now = int(time.time())
    base = dict(source_dir="/DCIM", camera=filename[-5], event_type="normal",
                state="pending", enqueued_at=now, recorded_at=now)
    base.update(cols)
    keys = ",".join(base)
    qmarks = ",".join("?" * len(base))
    with db.write() as c:
        c.execute(
            f"INSERT INTO download_queue (filename,{keys}) "
            f"VALUES (?,{qmarks})",
            (filename, *base.values()),
        )


def _state_for(items, fn):
    return next(it["gps_state"] for it in items if it["filename"] == fn)


def test_gps_state_ok_none_pending(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    now = int(time.time())
    _seed(db, "2026_0618_203643_0001F.MP4", triaged_at=now, gps_points=42)  # ok
    _seed(db, "2026_0618_203644_0002F.MP4", triaged_at=now, gps_points=0)   # none
    _seed(db, "2026_0618_203645_0003F.MP4")                                  # pending
    _seed(db, "2026_0618_203646_0004F.MP4",
          triage_attempts=TRIAGE_MAX_ATTEMPTS)                              # none (gave up)

    items = q.list_page(db, per_page=100)["items"]
    assert _state_for(items, "2026_0618_203643_0001F.MP4") == "ok"
    assert _state_for(items, "2026_0618_203644_0002F.MP4") == "none"
    assert _state_for(items, "2026_0618_203645_0003F.MP4") == "pending"
    assert _state_for(items, "2026_0618_203646_0004F.MP4") == "none"


def test_gps_state_present_in_day_items(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    now = int(time.time())
    fn = "2026_0618_203643_0001F.MP4"
    _seed(db, fn, triaged_at=now, gps_points=7)
    items = q.list_day_items(db, day="2026-06-18")
    assert _state_for(items, fn) == "ok"


def test_gps_state_none_for_orphan_non_gps_lenses(tmp_path):
    # GPS is a capture-level fact carried by the front lens. A rear/tele/
    # interior row with NO front sibling at its timestamp has no GPS fact to
    # inherit — no indicator (None), regardless of its own triage columns.
    db = Database(str(tmp_path / "v.db"))
    now = int(time.time())
    _seed(db, "2026_0618_203643_0001R.MP4", triaged_at=now, gps_points=9)  # rear
    _seed(db, "2026_0618_203644_0001T.MP4")                                 # tele
    _seed(db, "2026_0618_203645_0001I.MP4")                                 # interior
    items = q.list_page(db, per_page=100)["items"]
    assert _state_for(items, "2026_0618_203643_0001R.MP4") is None
    assert _state_for(items, "2026_0618_203644_0001T.MP4") is None
    assert _state_for(items, "2026_0618_203645_0001I.MP4") is None


def test_gps_state_none_for_orphan_parking_prefixed_rear(tmp_path):
    # Parking/event prefixes (PR, ER) resolve via the registry's last-letter
    # rule, so a prefixed rear is still treated as a non-GPS lens.
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0618_203643_0001PR.MP4")
    items = q.list_page(db, per_page=100)["items"]
    assert _state_for(items, "2026_0618_203643_0001PR.MP4") is None


def test_gps_state_implied_from_front_sibling(tmp_path):
    # Sibling lenses inherit the front's GPS state — matched by timestamp
    # prefix (sequences differ per lens), mirroring the archive/triage-gate
    # sibling rule. Rear rows are never triaged themselves.
    db = Database(str(tmp_path / "v.db"))
    now = int(time.time())
    _seed(db, "2026_0618_203643_0001F.MP4", triaged_at=now, gps_points=42)
    _seed(db, "2026_0618_203643_0002R.MP4")                          # → ok
    _seed(db, "2026_0618_203744_0003F.MP4", triaged_at=now, gps_points=0)
    _seed(db, "2026_0618_203744_0004R.MP4")                          # → none
    _seed(db, "2026_0618_203845_0005F.MP4")
    _seed(db, "2026_0618_203845_0006R.MP4")                          # → pending
    _seed(db, "2026_0618_203946_0007F.MP4",
          triage_attempts=TRIAGE_MAX_ATTEMPTS)
    _seed(db, "2026_0618_203946_0008R.MP4")                          # → none

    items = q.list_page(db, per_page=100)["items"]
    assert _state_for(items, "2026_0618_203643_0002R.MP4") == "ok"
    assert _state_for(items, "2026_0618_203744_0004R.MP4") == "none"
    assert _state_for(items, "2026_0618_203845_0006R.MP4") == "pending"
    assert _state_for(items, "2026_0618_203946_0008R.MP4") == "none"
    # The fronts themselves are unaffected by the sibling join.
    assert _state_for(items, "2026_0618_203643_0001F.MP4") == "ok"
    assert _state_for(items, "2026_0618_203845_0005F.MP4") == "pending"


def test_gps_state_implied_in_day_items(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    now = int(time.time())
    _seed(db, "2026_0618_203643_0001F.MP4", triaged_at=now, gps_points=7)
    _seed(db, "2026_0618_203643_0002R.MP4")
    items = q.list_day_items(db, day="2026-06-18")
    assert _state_for(items, "2026_0618_203643_0002R.MP4") == "ok"
