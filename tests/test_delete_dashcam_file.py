"""Tests for the dashcam delete HTTP helper.

We mock urllib.request.urlopen so the tests don't hit a real
dashcam. The helper builds the URL — the assertions check we
hit /?custom=1&cmd=4003&str=<path> and propagate success/failure
correctly.
"""
from __future__ import annotations

from unittest.mock import patch

from viofosync_lib import delete_dashcam_file


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
        "&str=/DCIM/Movie/2026_0508_104020_001234F.MP4"
    )
    assert captured["timeout"] == 5.0


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
