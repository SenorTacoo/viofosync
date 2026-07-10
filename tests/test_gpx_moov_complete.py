"""has_final_moov detects a reachable top-level moov (finalized clip) vs a
recording-in-progress file that has only a growing mdat and no moov."""
from __future__ import annotations

import io
import struct

from viofosync_lib import TruncatedRead
from viofosync_lib._gpx import has_final_moov


def _atom(atom_type: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + atom_type + payload


def test_reachable_moov_is_complete() -> None:
    # NB: this ftyp,mdat,moov (moov-at-end) fixture is illustrative of the atom
    # walk only. Real A229/A329 clips write moov at the FRONT and keep growing
    # it while recording, so a reachable moov does NOT imply finalized on that
    # hardware — the active-recording guard uses the cmd=3014 record flag, not
    # this check. See has_final_moov's docstring.
    data = _atom(b"ftyp", b"isom") + _atom(b"mdat", b"\x00" * 32) + _atom(b"moov")
    assert has_final_moov(io.BytesIO(data)) is True


def test_mdat_only_is_incomplete() -> None:
    data = _atom(b"ftyp", b"isom") + _atom(b"mdat", b"\x00" * 4096)
    assert has_final_moov(io.BytesIO(data)) is False


def test_zero_size_header_stops_walk() -> None:
    data = _atom(b"ftyp", b"isom") + struct.pack(">I", 0) + b"junk"
    assert has_final_moov(io.BytesIO(data)) is False


def test_truncated_read_means_incomplete() -> None:
    class _ShortReader(io.BytesIO):
        def read(self, n=-1):
            raise TruncatedRead("short read")

    assert has_final_moov(_ShortReader(b"")) is False
