"""If a download streams more bytes than expected (the file was still growing),
download_file aborts early and raises DownloadDeferred instead of looping on
'Incomplete download'. No attempt is burned; the partial is removed."""
from __future__ import annotations

import io

import pytest

from viofosync_lib import DownloadDeferred, _protocol
from viofosync_lib._archive import Recording


class _FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_overage_aborts_and_defers(tmp_path, monkeypatch):
    # HEAD says 60 bytes; GET streams well past the 2 MB overage slack
    # (still recording) -- the guard triggers on bytes_done, not on a
    # tiny handful of bytes, so the stream must actually outgrow
    # expected_size + 2 MiB.
    monkeypatch.setattr(_protocol, "get_remote_size", lambda url, t: 60)

    urlopen_calls = {"n": 0}
    overage_payload = b"\x00" * (3 * 1024 * 1024)  # 3 MB > 60B + 2MB slack

    def fake_urlopen(*a, **k):
        urlopen_calls["n"] += 1
        return _FakeResp(overage_payload)

    monkeypatch.setattr(_protocol.urllib.request, "urlopen", fake_urlopen)

    rec = Recording("2026_0628_133416_0002F.MP4", "/DCIM", 60, None, None, None)
    # The still-growing file is a deferral, not a failure: download_file
    # raises DownloadDeferred so the caller requeues without burning an attempt.
    with pytest.raises(DownloadDeferred):
        _protocol.download_file(
            "http://cam", rec, str(tmp_path), "Group",
            max_attempts=3,
        )
    # No leftover .part file anywhere under the destination (the
    # download lands in destination/Group/).
    assert list(tmp_path.rglob("*.part")) == []
    # Proves early abort rather than retry-until-exhausted: if the
    # overage weren't caught, the current code would loop on
    # "Incomplete download" and call urlopen once per attempt (3x).
    assert urlopen_calls["n"] == 1
