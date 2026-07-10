"""RangeReader raises on a truncated range response instead of silently
returning partial data (which parse_moov would commit as a valid track)."""
from __future__ import annotations

import pytest

from viofosync_lib import TruncatedRead
from viofosync_lib._protocol import RangeReader


def test_full_range_ok():
    # read_range returns exactly the requested bytes -> no error.
    data = bytes(range(256)) * 8        # 2048 bytes
    rr = RangeReader(lambda a, b: data[a : b + 1], len(data), head_len=16)
    rr.seek(100)
    assert rr.read(50) == data[100:150]


def test_short_range_raises():
    # read_range returns fewer bytes than requested, not at EOF -> raise.
    size = 2048

    def short(a, b):
        want = b - a + 1
        return b"\x00" * (want // 2)   # half what was asked for

    rr = RangeReader(short, size, head_len=16)
    rr.seek(100)
    with pytest.raises(TruncatedRead):
        rr.read(50)


def test_short_read_in_head_region_raises():
    # The head prefetch itself is truncated: head_len=16 but the source only
    # ever returns half, so the prefetched head is short. A read confined to
    # the head's intended region (offset 0, len 16) must still raise — the
    # camera closed the body early during the prefetch.
    #
    # Note: once a head buffer is populated, a pure `_head[start:end]` slice
    # can never be short, so the only way a read "inside the head" is short is
    # a truncated prefetch like this. With head_n=8 the read crosses into the
    # range branch, which is where the guard fires.
    def short(a, b):
        want = b - a + 1
        return b"\x00" * (want // 2)

    rr = RangeReader(short, 2048, head_len=16)
    rr.seek(0)
    with pytest.raises(TruncatedRead):
        rr.read(16)


def test_short_read_spanning_head_boundary_raises():
    size = 2048

    def part(a, b):
        want = b - a + 1
        if a == 0:
            return b"\x00" * want          # head prefetch is complete
        return b"\x00" * (want // 2)        # later range read is short

    rr = RangeReader(part, size, head_len=16)
    rr.seek(8)                              # start inside the 16-byte head
    with pytest.raises(TruncatedRead):
        rr.read(40)                         # ends well past head_n=16


def test_eof_clamp_is_not_short():
    # Reading past EOF clamps and returns "" — that's legitimate, not short.
    data = b"abcd"
    rr = RangeReader(lambda a, b: data[a : b + 1], len(data), head_len=16)
    rr.seek(4)
    assert rr.read(10) == b""
