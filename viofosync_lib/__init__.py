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
import urllib.parse
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


def is_ro_path(path: str | None) -> bool:
    """True when a dashcam path lies in the write-protected ``RO``
    folder (the camera's locked/event clips).

    Separator- and case-agnostic, and true for both a directory and a
    full file path, because the listing hands us either shape: the XML
    ``FPATH`` is native (``A:\\DCIM\\Movie\\RO\\X.MP4``), the HTML
    scrape's href is URL-style (``/DCIM/Movie/RO/X.MP4``). Every RO
    decision in the app funnels through here so one path shape can't
    read as locked in one place and as ordinary footage in another.
    """
    norm = (path or "").replace("\\", "/").upper().rstrip("/")
    return "/RO/" in norm or norm.endswith("/RO")


def _dashcam_posix_path(source_dir: str, filename: str) -> str:
    """URL-style absolute path — ``/DCIM/Movie/RO/X.MP4``.

    This is the form ``download_file`` puts in a GET URL, and the form
    ``cmd=4003`` does NOT accept (see :func:`_dashcam_native_path`). Kept
    as the fallback for any firmware that wants it.

    ``source_dir`` is whatever the listing recorded for the clip: in
    practice the *full file path* (the XML's ``FPATH``, the HTML
    scrape's href), for older rows a plain directory. The filename is
    appended only when the path doesn't already end with it.
    """
    cleaned = re.sub(r"^[A-Za-z]:", "", source_dir or "")
    cleaned = cleaned.replace("\\", "/").rstrip("/")
    if cleaned.rsplit("/", 1)[-1].lower() != filename.lower():
        cleaned = f"{cleaned}/{filename}"
    return cleaned if cleaned.startswith("/") else f"/{cleaned}"


def _dashcam_native_path(source_dir: str, filename: str) -> str:
    """The camera's own path form — ``A:\\DCIM\\Movie\\RO\\X.MP4``.

    Verified against an A229 on firmware answering ``cmd=4003``: the
    POSIX form is rejected with ``Status -5`` ("no such file") even for
    an ordinary, unprotected clip in ``/DCIM/Movie``, while the same
    delete with the drive letter and backslashes succeeds. The drive
    letter is preserved when the listing supplied one (``B:`` is the
    SSD on models that have it) and defaults to ``A:`` (the SD card)
    otherwise.

    Locked clips keep living in the ``RO`` subdirectory, which the
    listing already encodes in the path — there is nothing extra to do
    for them here beyond not mangling it.
    """
    raw = (source_dir or "").replace("/", "\\").rstrip("\\")
    m = re.match(r"^([A-Za-z]:)(.*)$", raw)
    drive, rest = (m.group(1), m.group(2)) if m else ("A:", raw)
    if rest.rsplit("\\", 1)[-1].lower() != filename.lower():
        rest = f"{rest}\\{filename}"
    if not rest.startswith("\\"):
        rest = f"\\{rest}"
    return f"{drive}{rest}"


def delete_dashcam_file(
    base_url: str,
    source_dir: str,
    filename: str,
    *,
    timeout: float = 10.0,
) -> bool:
    """Ask the Viofo dashcam to delete a clip.

    Verified against an A229:

        GET <base_url>/?custom=1&cmd=4003&str=A%3A%5CDCIM%5CMovie%5CX.MP4

    The ``str`` argument is a *camera filesystem* path, not a URL path:
    drive letter, backslashes, percent-encoded. The URL-style
    ``/DCIM/Movie/X.MP4`` is rejected with ``Status -5`` even for an
    ordinary unprotected clip — which is what made this look like a
    write-protection problem. Write-protected clips are no different;
    the listing simply puts them in the ``RO`` subdirectory, and that
    path deletes like any other.

    ``source_dir`` is whatever the listing recorded for the clip — the
    full ``A:\\DCIM\\Movie\\…\\X.MP4`` file path in practice, a plain
    directory for older rows; both resolve correctly. On a refusal the
    URL-style path is retried once, so firmware that wants the other
    form still works.

    Returns True only when a request succeeded *and* the camera did not
    report a non-zero ``<Status>``. False on any HTTP, URL, or timeout
    error, and on a refusal of both path forms. Never raises — failure
    is the caller's cue to log a warning and continue.
    """
    log = logging.getLogger("viofosync_lib.delete")
    native = _dashcam_native_path(source_dir, filename)
    posix = _dashcam_posix_path(source_dir, filename)
    # Native first (the confirmed form); the URL-style path is only
    # worth a second request when it actually differs. safe="" on the
    # native form percent-encodes ':' and '\' as the camera expects;
    # the fallback keeps its '/' separators literal.
    attempts = [(native, "")]
    if posix != native:
        attempts.append((posix, "/"))
    for path, safe in attempts:
        quoted = urllib.parse.quote(path, safe=safe)
        outcome = _try_delete(
            f"{base_url}/?custom=1&cmd=4003&str={quoted}",
            filename, path, timeout=timeout, log=log,
        )
        if outcome is not None:
            return outcome
    return False


def _try_delete(url, filename, path, *, timeout, log) -> bool | None:
    """One cmd=4003 attempt. True = deleted, False = transport/HTTP
    failure (no point trying another path form), None = the camera
    answered but refused this path, so a different form may still
    work."""
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
    except urllib.error.HTTPError as e:
        log.warning("dashcam delete %s: HTTP %s", filename, e.code)
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("dashcam delete %s: %s", filename, e)
        return False
    status = _delete_body_status(body)
    if status not in (None, 0):
        # Log the path we asked for: a refusal and a mistyped path look
        # identical from the status code alone (this firmware answers
        # -5 for "no such file"), and the path is the part we control.
        log.warning(
            "dashcam delete %s: camera refused (status %s) for %s",
            filename, status, path,
        )
        return None
    return True


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
    "is_ro_path",
    "parse_moov",
    "remote_moov_reachable",
]
