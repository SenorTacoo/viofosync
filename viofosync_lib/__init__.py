"""Public API for Viofo dashcam sync helpers.

Split into three private submodules by responsibility:

- :mod:`viofosync_lib._archive` — filename patterns, path helpers,
  filesystem walking
- :mod:`viofosync_lib._protocol` — HTTP API to the dashcam (XML
  listing, HTML scrape, byte downloader)
- :mod:`viofosync_lib._gpx` — MP4 atom parsing + GPX generation

plus one deliberately public submodule:

- :mod:`viofosync_lib.cameras` — the camera registry (which
  lenses exist and how a filename's trailing letter maps to one).
  Imported directly (``from viofosync_lib.cameras import …``) by
  both this package and the web layer.
"""
from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request

# Re-export the public API.
from ._archive import (
    Recording,
    downloaded_filename_re,
    get_downloaded_recordings,
    get_filepath,
    get_group_name,
)
from ._gpx import (
    IncompleteRecording,
    extract_gps_data,
    generate_gpx,
    has_final_moov,
    parse_moov,
)
from ._protocol import (
    DownloadCancelled,
    DownloadDeferred,
    TruncatedRead,
    download_file,
    extract_remote_gps_points,
    get_dashcam_filenames,
    get_dashcam_filenames_html,
    remote_moov_reachable,
)
from .progress import ProgressSink


def download_file_with(
    *args,
    max_attempts: int | None = None,
    socket_timeout: float | None = None,
    **kwargs,
):
    """Call :func:`download_file` with per-call ``max_attempts`` /
    ``socket_timeout`` overrides. Passes them straight through as
    parameters (download_file resolves None to the module defaults),
    so two concurrent downloads never clobber each other's settings."""
    from . import _protocol as _proto
    return _proto.download_file(
        *args,
        max_attempts=max_attempts,
        socket_timeout=socket_timeout,
        **kwargs,
    )


# Novatek/cardv replies carry the real outcome in the body, not the
# HTTP status: ``<Function><Cmd>4003</Cmd><Status>0</Status></Function>``
# where 0 means "done" and anything else is a refusal (the firmware
# answers 200 OK either way). Bodies without a <Status> tag are treated
# as success — some firmware returns an empty body on a good delete.
_STATUS_RE = re.compile(r"<Status>\s*(-?\d+)\s*</Status>")


def _delete_body_status(body: str) -> int | None:
    """Parsed ``<Status>`` from a cmd=4003 reply, or None when absent."""
    m = _STATUS_RE.search(body)
    return int(m.group(1)) if m else None


def delete_dashcam_file(
    base_url: str,
    source_dir: str,
    filename: str,
    *,
    timeout: float = 10.0,
) -> bool:
    """Ask the Viofo dashcam to delete ``<source_dir>/<filename>``.

    Confirmed protocol against the A229 Pro:

        GET <base_url>/?custom=1&cmd=4003&str=<absolute-path>

    Works for write-protected clips too — those live under
    ``/DCIM/Movie/RO`` and are deleted by the same command with the
    ``/RO`` path; whether the firmware honours it is the camera's
    call, reported back through the body ``<Status>``.

    Returns True only when the request succeeded *and* the camera did
    not report a non-zero ``<Status>``. False on any HTTP, URL, or
    timeout error, and on an explicit refusal. Never raises — failure
    is the caller's cue to log a warning and continue.
    """
    log = logging.getLogger("viofosync_lib.delete")
    # source_dir already includes the leading slash on the dashcam
    # (e.g. "/DCIM/Movie") and never has a trailing slash; build the
    # absolute path with a single join.
    path = f"{source_dir}/{filename}"
    url = f"{base_url}/?custom=1&cmd=4003&str={path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if not 200 <= getattr(resp, "status", 0) < 300:
                log.warning(
                    "dashcam delete %s: HTTP %s", filename, resp.status
                )
                return False
            try:
                body = resp.read().decode("utf-8", errors="replace")
            except OSError:  # pragma: no cover — body read is best-effort
                body = ""
        status = _delete_body_status(body)
        if status not in (None, 0):
            log.warning(
                "dashcam delete %s: camera refused (status %s)",
                filename, status,
            )
            return False
        return True
    except urllib.error.HTTPError as e:
        log.warning("dashcam delete %s: HTTP %s", filename, e.code)
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("dashcam delete %s: %s", filename, e)
        return False


__all__ = [
    "DownloadCancelled",
    "DownloadDeferred",
    "IncompleteRecording",
    "TruncatedRead",
    "Recording",
    "ProgressSink",
    "delete_dashcam_file",
    "download_file",
    "download_file_with",
    "downloaded_filename_re",
    "extract_gps_data",
    "extract_remote_gps_points",
    "generate_gpx",
    "get_dashcam_filenames",
    "get_dashcam_filenames_html",
    "get_downloaded_recordings",
    "get_filepath",
    "get_group_name",
    "has_final_moov",
    "parse_moov",
    "remote_moov_reachable",
]
