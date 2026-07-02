"""next_pending(triage_gate=True) holds clips whose front sibling is still
awaiting triage, and releases them once triaged / gave-up."""
from __future__ import annotations

import time

from web.db import Database
from web.services import queue as q
from web.services.triage import TRIAGE_MAX_ATTEMPTS


def _seed(db, filename, *, state="pending", triaged_at=None,
          triage_attempts=0, priority=0):
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at, "
            " priority, triaged_at, triage_attempts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (filename, "/DCIM", filename[-5], "normal", state, now,
             priority, triaged_at, triage_attempts),
        )


def test_untriaged_front_is_gated(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0618_203643_0001F.MP4")          # pending-triage
    assert q.next_pending(db, triage_gate=True) is None
    # Without the gate it would be handed out normally.
    assert q.next_pending(db, triage_gate=False) is not None


def test_rear_sibling_is_gated_with_front(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0618_203643_0001F.MP4")          # front pending-triage
    _seed(db, "2026_0618_203643_0001R.MP4")          # rear of the SAME pair
    # Rear must not be downloaded ahead of its front's triage.
    assert q.next_pending(db, triage_gate=True) is None


def test_prefixed_parking_pair_is_gated_then_released(tmp_path):
    # Parking/event prefixes (PF/PR) must not confuse the sibling match: the
    # prefixed rear is held until the prefixed front is triaged.
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0618_203643_0001PF.MP4")         # parking front, pending
    _seed(db, "2026_0618_203643_0001PR.MP4")         # parking rear of pair
    assert q.next_pending(db, triage_gate=True) is None
    # Triage the front → the whole parking pair is released.
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET triaged_at=? WHERE filename=?",
            (int(time.time()), "2026_0618_203643_0001PF.MP4"),
        )
    assert q.next_pending(db, triage_gate=True) is not None


def test_differing_sequence_pair_is_gated_then_released(tmp_path):
    # Real captures can give each lens its own sequence number (observed on
    # parking clips: …_020753PF + …_020755PR) — siblings share the timestamp,
    # not the full stem. The gate must pair by timestamp, not by rebuilding
    # the front's filename from the rear's stem.
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0515_023653_020753PF.MP4")       # front pending-triage
    _seed(db, "2026_0515_023653_020755PR.MP4")       # rear, its own sequence
    assert q.next_pending(db, triage_gate=True) is None
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET triaged_at=? WHERE filename=?",
            (int(time.time()), "2026_0515_023653_020753PF.MP4"),
        )
    assert q.next_pending(db, triage_gate=True) is not None


def test_released_once_triaged(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0618_203643_0001F.MP4",
          triaged_at=int(time.time()))               # fetched/no-fix
    _seed(db, "2026_0618_203643_0001R.MP4")
    item = q.next_pending(db, triage_gate=True)
    assert item is not None                            # pair released


def test_released_once_given_up(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0618_203643_0001F.MP4",
          triage_attempts=TRIAGE_MAX_ATTEMPTS)        # gave up
    item = q.next_pending(db, triage_gate=True)
    assert item is not None


def test_orphan_rear_not_gated(tmp_path):
    # A rear with no matching front has nothing to wait for.
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0618_203643_0009R.MP4")
    assert q.next_pending(db, triage_gate=True) is not None


def test_ungated_clip_still_returned_when_a_gated_clip_present(tmp_path):
    # An untriaged front is gated, but a co-queued triaged clip must still be
    # handed out — the gate must be selective, not block the whole queue.
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "2026_0618_203643_0001F.MP4")                       # gated
    _seed(db, "2026_0618_203644_0002F.MP4",
          triaged_at=int(time.time()))                            # ungated
    item = q.next_pending(db, triage_gate=True)
    assert item is not None
    assert item.filename == "2026_0618_203644_0002F.MP4"
