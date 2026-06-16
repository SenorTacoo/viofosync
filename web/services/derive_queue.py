"""Persistent per-clip derive queue + state machine.

One row per clip (clip_id is the PK): "this clip needs a derive pass".
What is actually *done* is read from disk / clip_index, so rows are
idempotent. Mirrors services/queue.py: pure DB functions, writes go
through Database.write(). States: pending -> running -> done; a transient
failure returns the row to pending (attempts++) until max_attempts, then
failed. Pass ``now`` (unix seconds) explicitly so tests are deterministic.
"""
from __future__ import annotations

from typing import Optional

from ..db import Database


def enqueue(db: Database, clip_id: int, *, priority: int, now: int) -> None:
    """Insert or reactivate a clip at ``priority``. A row that is not
    currently ``running`` is reset to ``pending`` (so a manual rescan
    re-runs a ``failed`` clip); a ``running`` row is left alone."""
    with db.write() as c:
        c.execute(
            """
            INSERT INTO derive_queue (
                clip_id, priority, state, attempts,
                last_error, enqueued_at, updated_at
            ) VALUES (?, ?, 'pending', 0, NULL, ?, ?)
            ON CONFLICT(clip_id) DO UPDATE SET
                priority = MIN(derive_queue.priority, excluded.priority),
                state = CASE WHEN derive_queue.state = 'running'
                             THEN derive_queue.state ELSE 'pending' END,
                attempts = CASE WHEN derive_queue.state = 'running'
                                THEN derive_queue.attempts ELSE 0 END,
                last_error = CASE WHEN derive_queue.state = 'running'
                                  THEN derive_queue.last_error ELSE NULL END,
                updated_at = excluded.updated_at
            """,
            (clip_id, priority, now, now),
        )


def claim_next(db: Database, *, now: int) -> Optional[int]:
    """Atomically move the highest-priority pending clip to ``running``
    and return its id, or None if nothing is pending."""
    with db.write() as c:
        row = c.execute(
            """
            SELECT clip_id FROM derive_queue
            WHERE state = 'pending'
            ORDER BY priority ASC, clip_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        clip_id = row["clip_id"]
        c.execute(
            "UPDATE derive_queue SET state='running', updated_at=? "
            "WHERE clip_id=?",
            (now, clip_id),
        )
        return clip_id


def mark_done(db: Database, clip_id: int, *, now: int) -> None:
    """Mark a running clip as done."""
    with db.write() as c:
        c.execute(
            "UPDATE derive_queue SET state='done', last_error=NULL, "
            "updated_at=? WHERE clip_id=?",
            (now, clip_id),
        )


def mark_transient_failure(
    db: Database, clip_id: int, error: str, *, max_attempts: int, now: int
) -> str:
    """Increment attempts; return to 'pending' unless the cap is hit, in
    which case 'failed'. Returns the new state."""
    with db.write() as c:
        row = c.execute(
            "SELECT attempts FROM derive_queue WHERE clip_id=?", (clip_id,)
        ).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        state = "failed" if attempts >= max_attempts else "pending"
        c.execute(
            "UPDATE derive_queue SET state=?, attempts=?, last_error=?, "
            "updated_at=? WHERE clip_id=?",
            (state, attempts, error, now, clip_id),
        )
        return state


def reconcile_orphans(db: Database, *, now: int) -> int:
    """Reset rows left 'running' by a dead process back to 'pending'.
    Returns the number reset."""
    with db.write() as c:
        cur = c.execute(
            "UPDATE derive_queue SET state='pending', updated_at=? "
            "WHERE state='running'",
            (now,),
        )
        return cur.rowcount


def remove_clip(db: Database, clip_id: int) -> None:
    """Delete a clip's queue row entirely."""
    with db.write() as c:
        c.execute("DELETE FROM derive_queue WHERE clip_id=?", (clip_id,))


def pending_count(db: Database) -> int:
    """Count clips that are pending or currently running."""
    with db.conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM derive_queue WHERE state IN ('pending','running')"
        ).fetchone()[0]


def enqueue_missing(db: Database, *, priority: int, now: int) -> int:
    """Enqueue every clip_index row that lacks a done/running derive row
    (new clips + previously-failed), in a single write. Leaves done/running
    rows untouched so a repeated scan doesn't re-derive the whole archive.
    Returns the number of rows inserted or reset."""
    with db.write() as c:
        cur = c.execute(
            """
            INSERT INTO derive_queue (
                clip_id, priority, state, attempts,
                last_error, enqueued_at, updated_at
            )
            SELECT ci.id, ?, 'pending', 0, NULL, ?, ?
            FROM clip_index ci
            LEFT JOIN derive_queue dq ON dq.clip_id = ci.id
            WHERE dq.clip_id IS NULL OR dq.state NOT IN ('done', 'running')
            ON CONFLICT(clip_id) DO UPDATE SET
                priority = MIN(derive_queue.priority, excluded.priority),
                state = 'pending',
                attempts = 0,
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (priority, now, now),
        )
        return cur.rowcount
