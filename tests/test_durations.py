"""Tests for clip duration probing."""
from __future__ import annotations

import pytest

from web.services import durations


async def test_probe_duration_parses_ffprobe(monkeypatch):
    class _P:
        async def communicate(self):
            return (b"60.05\n", b"")
    async def fake_exec(*a, **k):
        return _P()
    monkeypatch.setattr(durations.shutil, "which", lambda _n: "/usr/bin/ffprobe")
    monkeypatch.setattr(durations.asyncio, "create_subprocess_exec", fake_exec)
    assert await durations.probe_duration("/x.mp4") == pytest.approx(60.05)


async def test_probe_duration_none_without_ffprobe(monkeypatch):
    monkeypatch.setattr(durations.shutil, "which", lambda _n: None)
    assert await durations.probe_duration("/x.mp4") is None
