# tests/test_range_reader.py
"""RangeReader feeds parse_moov; verified against the doc's §9 vector."""
from __future__ import annotations

import http.server
import struct
import threading

from viofosync_lib import parse_moov
from viofosync_lib._archive import Recording
from viofosync_lib._protocol import (
    RangeReader,
    extract_remote_gps_points,
)

# §9 verified freeGPS payload (block offset 16 onward), little-endian.
# lat bytes 9af7a545 = struct.pack('<f', 5310.95); lon bytes 66e67b43 = struct.pack('<f', 251.90).
# The task brief listed different bytes (69f6a545 / a4e77b43) which decode to 5310.80/251.90
# and produce 53.18/−2.8651 — off by f32 rounding. Corrected to match the doc's §9 assertions
# (53.1825, −2.865) which are the source of truth.
_PAYLOAD = bytes.fromhex(
    "14000000" "24000000" "2b000000" "1a000000"   # hour=20 min=36 sec=43 year=26
    "06000000" "12000000" "414e5700"               # month=6 day=18 'A' 'N' 'W' pad
    "9af7a545"                                       # lat f32 LE = 5310.95
    "66e67b43"                                       # lon f32 LE = 251.90
    "00000000" "00000000"                           # speed=0.0 heading=0.0
)


def _synthetic_mp4() -> bytes:
    """moov{ gps-index } followed by one freeGPS block at the indexed offset.

    parse_moov: finds the 'gps ' box, reads (offset,size) u32-BE pairs from
    box+16 until the box ends, and for each seeks to `offset` expecting a
    `free`/`GPS ` atom of `size` bytes, then decodes payload[12:].
    """
    block_body = _PAYLOAD
    block_size = 12 + len(block_body)
    free_block = struct.pack(">I4s4s", block_size, b"free", b"GPS ") + block_body

    gps_box_size = 8 + 4 + 4 + 8
    moov_size = 8 + gps_box_size
    block_offset = moov_size  # absolute file offset of free_block

    gps_box = (
        struct.pack(">I4s", gps_box_size, b"gps ")
        + struct.pack(">I", 0x00000101)
        + struct.pack(">I", 1)
        + struct.pack(">II", block_offset, block_size)
    )
    moov = struct.pack(">I4s", moov_size, b"moov") + gps_box
    return moov + free_block


def test_parse_moov_decodes_section9_vector_via_bytesio():
    data = _synthetic_mp4()
    reader = RangeReader(lambda a, b: data[a : b + 1], len(data), head_len=64)
    pts = parse_moov(reader)
    assert len(pts) == 1
    p = pts[0]
    assert p["DT"]["DT"] == "2026-06-18T20:36:43Z"
    assert round(p["Loc"]["Lat"]["Float"], 4) == 53.1825
    assert round(p["Loc"]["Lon"]["Float"], 4) == -2.865


def test_range_reader_seek_read_eof():
    data = b"0123456789"
    r = RangeReader(lambda a, b: data[a : b + 1], len(data), head_len=4)
    r.seek(2)
    assert r.read(3) == b"234"
    r.seek(8)
    assert r.read(8) == b"89"
    r.seek(10)
    assert r.read(4) == b""


def test_extract_remote_gps_points_over_http():
    data = _synthetic_mp4()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

        def do_GET(self):
            rng = self.headers.get("Range")
            if rng:
                a, b = rng.replace("bytes=", "").split("-")
                a = int(a)
                b = int(b) if b else len(data) - 1
                b = min(b, len(data) - 1)
                chunk = data[a : b + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        rec = Recording(
            filename="2026_0618_203643_0001F.MP4",
            filepath="/DCIM/Movie/2026_0618_203643_0001F.MP4",
            size=len(data),
            timecode=None,
            datetime=None,
            attr=None,
        )
        pts = extract_remote_gps_points(
            f"http://127.0.0.1:{port}", rec, timeout=5.0, head_len=64
        )
        assert len(pts) == 1
        assert pts[0]["DT"]["DT"] == "2026-06-18T20:36:43Z"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5.0)
        assert not t.is_alive()
