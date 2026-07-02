"""capture_key_sql yields the 14-digit YYYYMMDDHHMMSS capture key for both
filename layouts, so MAX() over it picks the true newest capture."""
from __future__ import annotations

import sqlite3

from web.services.naming import capture_key_sql


def _key(filename: str) -> str:
    c = sqlite3.connect(":memory:")
    try:
        c.execute("CREATE TABLE clips (filename TEXT)")
        c.execute("INSERT INTO clips (filename) VALUES (?)", (filename,))
        row = c.execute(
            f"SELECT {capture_key_sql('filename')} FROM clips"
        ).fetchone()
    finally:
        c.close()
    return row[0]


def test_standard_layout_strips_underscore() -> None:
    assert _key("2026_0628_133416_104430R.MP4") == "20260628133416"


def test_compact_layout_passes_through() -> None:
    assert _key("20260628133416_000123.MP4") == "20260628133416"


def test_same_capture_across_layouts_matches() -> None:
    assert _key("2026_0628_133416_0001F.MP4") == _key("20260628133416_9.MP4")


def test_siblings_share_key_despite_sequence() -> None:
    assert _key("2026_0628_133416_020753PF.MP4") == _key(
        "2026_0628_133416_020755PR.MP4"
    )
