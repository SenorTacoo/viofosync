"""Tests for the dashcam delete HTTP helper.

We mock urllib.request.urlopen so the tests don't hit a real
dashcam. The helper builds the URL — the assertions check we
hit /?custom=1&cmd=4003&str=<path> and propagate success/failure
correctly.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from viofosync_lib import (
    _dashcam_native_path,
    _dashcam_posix_path,
    delete_dashcam_file,
    is_ro_path,
)


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_delete_returns_true_on_success() -> None:
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req if isinstance(req, str) else req.full_url
        captured["timeout"] = timeout
        return _FakeResponse(status=200)

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.230",
            "/DCIM/Movie",
            "2026_0508_104020_001234F.MP4",
            timeout=5.0,
        )
    assert ok is True
    assert captured["url"] == (
        "http://192.168.1.230/?custom=1&cmd=4003"
        "&str=A%3A%5CDCIM%5CMovie%5C2026_0508_104020_001234F.MP4"
    )
    assert captured["timeout"] == 5.0


@pytest.mark.parametrize(
    "path, expected",
    [
        ("A:\\DCIM\\Movie\\RO\\X.MP4", True),
        ("/DCIM/Movie/RO/X.MP4", True),
        ("/DCIM/Movie/RO", True),
        ("/DCIM/Movie/RO/", True),
        ("A:\\DCIM\\Movie\\ro\\X.MP4", True),
        ("A:\\DCIM\\Movie\\X.MP4", False),
        ("/DCIM/Movie/Parking/X.MP4", False),
        # 'RO' as part of a longer name is not the RO folder.
        ("/DCIM/Movie/ROAD/X.MP4", False),
        ("", False),
        (None, False),
    ],
)
def test_is_ro_path(path, expected: bool) -> None:
    assert is_ro_path(path) is expected


@pytest.mark.parametrize(
    "source_dir, expected",
    [
        # What the queue actually stores: the listing's FPATH, i.e. the
        # full file path. Appending the filename again produced
        # "...\X.MP4\X.MP4" and the camera answered "no such file".
        ("A:\\DCIM\\Movie\\RO\\X.MP4", "A:\\DCIM\\Movie\\RO\\X.MP4"),
        ("A:\\DCIM\\Movie\\X.MP4", "A:\\DCIM\\Movie\\X.MP4"),
        # The SSD drive letter is preserved, not assumed to be A:.
        ("B:\\DCIM\\Movie\\X.MP4", "B:\\DCIM\\Movie\\X.MP4"),
        # HTML-scrape form: URL-style and drive-less — converted.
        ("/DCIM/Movie/RO/X.MP4", "A:\\DCIM\\Movie\\RO\\X.MP4"),
        # Directory forms — legacy rows and direct callers.
        ("/DCIM/Movie", "A:\\DCIM\\Movie\\X.MP4"),
        ("/DCIM/Movie/RO/", "A:\\DCIM\\Movie\\RO\\X.MP4"),
        ("DCIM/Movie", "A:\\DCIM\\Movie\\X.MP4"),
        # A directory that merely shares the name is still a directory.
        ("/DCIM/Movie/x.mp4/", "A:\\DCIM\\Movie\\x.mp4"),
    ],
)
def test_native_path_building(source_dir: str, expected: str) -> None:
    assert _dashcam_native_path(source_dir, "X.MP4") == expected


@pytest.mark.parametrize(
    "source_dir, expected",
    [
        ("A:\\DCIM\\Movie\\RO\\X.MP4", "/DCIM/Movie/RO/X.MP4"),
        ("/DCIM/Movie/RO/X.MP4", "/DCIM/Movie/RO/X.MP4"),
        ("/DCIM/Movie", "/DCIM/Movie/X.MP4"),
    ],
)
def test_posix_fallback_path_building(source_dir: str, expected: str) -> None:
    assert _dashcam_posix_path(source_dir, "X.MP4") == expected


def test_delete_uses_the_cameras_native_path() -> None:
    """The exact URL shape verified by hand against an A229 — drive
    letter and backslashes, percent-encoded. The URL-style path is
    rejected with Status -5 even for an unprotected clip."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req if isinstance(req, str) else req.full_url
        return _FakeResponse(status=200, body=_OK_BODY)

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.254",
            "A:\\DCIM\\Movie\\RO\\2026_0513_193930_024528PR.MP4",
            "2026_0513_193930_024528PR.MP4",
        )
    assert ok is True
    assert captured["url"] == (
        "http://192.168.1.254/?custom=1&cmd=4003"
        "&str=A%3A%5CDCIM%5CMovie%5CRO%5C2026_0513_193930_024528PR.MP4"
    )


def test_delete_falls_back_to_the_posix_path_on_refusal() -> None:
    """Firmware that wants the other form still works — one retry, and
    only when the two forms actually differ."""
    urls = []

    def fake_urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        urls.append(url)
        body = _REFUSED_BODY if "%5C" in url else _OK_BODY
        return _FakeResponse(status=200, body=body)

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.254", "/DCIM/Movie", "X.MP4",
        )
    assert ok is True
    assert len(urls) == 2
    assert urls[0].endswith("str=A%3A%5CDCIM%5CMovie%5CX.MP4")
    assert urls[1].endswith("str=/DCIM/Movie/X.MP4")


def test_delete_gives_up_when_both_forms_are_refused() -> None:
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(status=200, body=_REFUSED_BODY)

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.254", "/DCIM/Movie", "X.MP4",
        )
    assert ok is False


def test_delete_does_not_retry_a_transport_failure() -> None:
    """A dropped connection says nothing about the path — retrying the
    other form would just double the timeout."""
    import urllib.error
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.254", "/DCIM/Movie", "X.MP4",
        )
    assert ok is False
    assert calls["n"] == 1


_OK_BODY = (
    b"<?xml version=\"1.0\" encoding=\"UTF-8\" ?>"
    b"<Function><Cmd>4003</Cmd><Status>0</Status></Function>"
)
_REFUSED_BODY = (
    b"<?xml version=\"1.0\" encoding=\"UTF-8\" ?>"
    b"<Function><Cmd>4003</Cmd><Status>-14</Status></Function>"
)


def _urlopen_returning(body: bytes, status: int = 200):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(status=status, body=body)
    return fake_urlopen


def test_delete_returns_true_on_zero_status_body() -> None:
    with patch("urllib.request.urlopen", _urlopen_returning(_OK_BODY)):
        ok = delete_dashcam_file(
            "http://192.168.1.230", "/DCIM/Movie/RO", "X.MP4",
        )
    assert ok is True


def test_delete_returns_false_when_camera_refuses() -> None:
    """HTTP 200 with a non-zero <Status> is a refusal, not a delete —
    reporting it as success would mark the clip gone while it is still
    on the card."""
    with patch("urllib.request.urlopen", _urlopen_returning(_REFUSED_BODY)):
        ok = delete_dashcam_file(
            "http://192.168.1.230", "/DCIM/Movie/RO", "X.MP4",
        )
    assert ok is False


def test_delete_returns_false_on_http_error() -> None:
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://x", 500, "boom", {}, None,
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.230",
            "/DCIM/Movie",
            "2026_0508_104020_001234F.MP4",
        )
    assert ok is False


def test_delete_returns_false_on_url_error() -> None:
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.230",
            "/DCIM/Movie",
            "2026_0508_104020_001234F.MP4",
        )
    assert ok is False


def test_delete_returns_false_on_socket_timeout() -> None:
    import socket

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timeout")

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.230",
            "/DCIM/Movie",
            "2026_0508_104020_001234F.MP4",
        )
    assert ok is False
    # Sanity: socket.timeout is a subclass of OSError in modern Python.
    assert issubclass(socket.timeout, OSError)
