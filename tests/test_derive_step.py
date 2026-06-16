# tests/test_derive_step.py
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from web.services import derive_worker, filmstrip


def test_filmstrip_failed_marker_roundtrip(tmp_path, monkeypatch):
    recordings = str(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    cid = 42
    assert filmstrip.failed_recently(recordings, cid, str(video)) is False
    filmstrip.mark_failed(recordings, cid)
    assert filmstrip.failed_recently(recordings, cid, str(video)) is True
    # A newer video (mtime moves past the marker) clears the skip.
    os.utime(video, (1 << 31, 1 << 31))
    assert filmstrip.failed_recently(recordings, cid, str(video)) is False


def test_derive_settings_defaults(tmp_config_dir):
    from web.settings import SettingsProvider
    snap = SettingsProvider().get()
    # Thumbs pre-generate by default; filmstrips are opt-in (heavy).
    assert snap.derive_thumbs_eager is True
    assert snap.derive_filmstrips_eager is False


def test_every_editable_key_is_projected_to_the_ui(tmp_config_dir):
    # _editable_values is a hand-written projection separate from
    # EDITABLE_KEYS; a key validated/persisted on write but absent here
    # is invisible in the UI (the checkbox can't reflect its value). Pin
    # the invariant so the two lists can't drift again.
    from web.routers.settings import _editable_values
    from web.settings import SettingsProvider
    from web.settings_schema import EDITABLE_KEYS
    snap = SettingsProvider().get()
    projected = set(_editable_values(snap).keys())
    missing = set(EDITABLE_KEYS) - projected
    assert not missing, f"editable keys not projected to the UI: {missing}"


class MagicMockDB:
    """Minimal stand-in: derive_one only writes via the durations helpers,
    which these tests avoid by pre-setting duration_s and disabling GPS."""
    def write(self):  # pragma: no cover - not exercised here
        raise AssertionError("derive_one should not write in these tests")


def _snap(recordings, **over):
    base = dict(
        recordings=recordings, gps_extract=False,
        derive_thumbs_eager=True, derive_filmstrips_eager=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _clip(tmp_path, dur=60.0):
    v = tmp_path / "c.mp4"
    v.write_bytes(b"x")
    return {"id": 1, "path": str(v), "duration_s": dur}


async def test_derive_one_generates_enabled_artifacts(tmp_path, monkeypatch):
    snap = _snap(str(tmp_path))
    clip = _clip(tmp_path)
    thumb = AsyncMock(return_value="thumb.jpg")
    film = AsyncMock(return_value=object())
    monkeypatch.setattr(derive_worker.thumbs, "ensure_thumb", thumb)
    monkeypatch.setattr(derive_worker.filmstrip, "ensure_filmstrip", film)
    res = await derive_worker.derive_one(MagicMockDB(), snap, clip)
    assert res.transient_failed is False
    thumb.assert_awaited_once()
    film.assert_awaited_once()


async def test_derive_one_respects_toggles(tmp_path, monkeypatch):
    snap = _snap(str(tmp_path), derive_filmstrips_eager=False)
    clip = _clip(tmp_path)
    thumb = AsyncMock(return_value="thumb.jpg")
    film = AsyncMock(return_value=object())
    monkeypatch.setattr(derive_worker.thumbs, "ensure_thumb", thumb)
    monkeypatch.setattr(derive_worker.filmstrip, "ensure_filmstrip", film)
    await derive_worker.derive_one(MagicMockDB(), snap, clip)
    thumb.assert_awaited_once()
    film.assert_not_awaited()


async def test_derive_one_missing_file_is_gone(tmp_path):
    snap = _snap(str(tmp_path))
    clip = {"id": 1, "path": str(tmp_path / "nope.mp4"), "duration_s": 60.0}
    res = await derive_worker.derive_one(MagicMockDB(), snap, clip)
    assert res.gone is True
