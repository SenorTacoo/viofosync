"""extract_remote_gps_points raises IncompleteRecording when the remote clip
has no reachable moov; remote_moov_reachable wraps the atom walk over a
RangeReader without downloading the whole clip."""
from __future__ import annotations

import struct

import pytest

from viofosync_lib import IncompleteRecording
from viofosync_lib._protocol import RangeReader
from viofosync_lib import _gpx


def _atom(atom_type: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + atom_type + payload


def _reader(data: bytes) -> RangeReader:
    def _read_range(a, b):
        return data[a : b + 1]

    return RangeReader(_read_range, len(data))


def test_has_final_moov_over_rangereader_true() -> None:
    data = _atom(b"ftyp", b"isom") + _atom(b"mdat", b"\x00" * 64) + _atom(b"moov")
    assert _gpx.has_final_moov(_reader(data)) is True


def test_has_final_moov_over_rangereader_false() -> None:
    data = _atom(b"ftyp", b"isom") + _atom(b"mdat", b"\x00" * 64)
    assert _gpx.has_final_moov(_reader(data)) is False


def test_extract_remote_raises_on_no_moov(monkeypatch) -> None:
    from viofosync_lib import _protocol

    data = _atom(b"ftyp", b"isom") + _atom(b"mdat", b"\x00" * 64)
    monkeypatch.setattr(
        _protocol, "open_remote_reader",
        lambda *a, **k: _reader(data),
    )
    rec = _protocol.Recording("x.MP4", "/DCIM", len(data), None, None, None)
    with pytest.raises(IncompleteRecording):
        _protocol.extract_remote_gps_points("http://cam", rec)


def test_extract_remote_returns_empty_for_moov_without_gps(monkeypatch) -> None:
    from viofosync_lib import _protocol

    data = _atom(b"ftyp", b"isom") + _atom(b"moov", _atom(b"udta"))
    monkeypatch.setattr(
        _protocol, "open_remote_reader",
        lambda *a, **k: _reader(data),
    )
    rec = _protocol.Recording("x.MP4", "/DCIM", len(data), None, None, None)
    assert _protocol.extract_remote_gps_points("http://cam", rec) == []
