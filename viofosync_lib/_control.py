"""Camera control layer for the Viofo Novatek *netapp* HTTP interface.

Reads the camera's current settings (``cmd=3014``) and applies *validated*
setting changes, decoding everything against a bundled per-model command map
(``data/command_map.json`` — command ids, keys and option enumerations derived
from the official VIOFO app; see ``scripts/build_command_map.py``). This is the
semantic map the bare HTTP protocol lacks.

SAFETY MODEL — read this before touching the write path
-------------------------------------------------------
On this protocol a *bare* ``?custom=1&cmd=NNNN`` call is **not** a guaranteed
harmless read: low command ids are a mix of getters, setters and destructive
*actions*. Two ids (``3010`` format-SD, ``3013``) are known to wedge the
single-threaded camera daemon on a bare call. Therefore:

* Reads use only the explicit, known-safe GET commands defined here.
* A hard denylist of destructive/disruptive ids (:data:`DESTRUCTIVE_CMDS`) is
  refused before any request is built — they are never sent, under any path.
* Writes are allowlisted: a setting is writable only if the vendor DB gives it
  an enumerated option table for this model; the requested value is validated
  against those options, sent as ``&par=<index>``, then **read back and
  verified** via ``cmd=3014``.
* Transport is deliberately gentle: one request at a time, 10s timeout, no
  sweeps — so we never overrun the daemon.
"""
from __future__ import annotations

import functools
import http.client
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("viofosync_lib.control")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_DEFAULT_MAP = os.path.join(os.path.dirname(__file__), "data",
                            "command_map.json")
MAP_PATH = os.environ.get("CAMERA_CMD_MAP", _DEFAULT_MAP)
DEFAULT_MODEL = os.environ.get("CAMERA_MODEL", "A329S")
HTTP_TIMEOUT = float(os.environ.get("CAMERA_HTTP_TIMEOUT", "10.0"))
UA = "viofosync-control/1.0"

# Write read-back: the camera can lag a freshly-written value in cmd=3014, so
# verification polls a few times before giving up (avoids false negatives).
VERIFY_ATTEMPTS = int(os.environ.get("CAMERA_VERIFY_ATTEMPTS", "3"))
VERIFY_SETTLE = float(os.environ.get("CAMERA_VERIFY_SETTLE", "0.4"))

# Some settings are rejected while the camera is actively recording. For these
# we (only if a direct write fails) briefly stop recording, re-apply, and
# restart recording — restoring the prior record state. CMD_RECORD = 2001,
# par 1=start/0=stop. Confirmed gated on the A329S: loop length, bitrate,
# resolution. (Resolution may still reject specific modes for other reasons.)
RECORD_CMD = 2001
RECORD_GATED_CMDS = {
    int(x) for x in os.environ.get(
        "CAMERA_RECORD_GATED", "2003,8200,8222").split(",") if x.strip()
}
RECORD_SETTLE = float(os.environ.get("CAMERA_RECORD_SETTLE", "1.0"))

# Settings the camera won't accept over the Wi-Fi (station) HTTP interface, or
# that this control layer can't encode yet. The UI shows these read-only with
# the note, rather than offering a control that silently fails.
UNSUPPORTED_CMDS: dict[int, str] = {
    8222: "Resolution can't be changed over Wi-Fi",
    8220: "Exposure isn't adjustable through this interface",
}
# Settings that need a specific lens physically attached — the camera rejects
# them until it is. Value is the CAMERA_TAG letter that lens contributes.
LENS_DEPENDENT_CMDS: dict[int, str] = {
    9319: "R",   # HDR rear
    9322: "I",   # interior-camera enable
    9333: "I",   # HDR interior
    9339: "I",   # interior fisheye / dewarp
}
MULTI_LENS_CMDS = {9342}  # video merge — needs >=2 lenses


def _support(cmd: int, camera_tag: str | None) -> tuple[bool, str | None]:
    """Whether a setting is adjustable right now: (supported, reason).

    Reasons cover the Wi-Fi/encoding limits above and lenses that aren't
    attached (so e.g. rear/interior settings light up once the lens is
    connected, rather than being permanently hidden)."""
    if cmd in UNSUPPORTED_CMDS:
        return False, UNSUPPORTED_CMDS[cmd]
    tag = (camera_tag or "").upper()
    need = LENS_DEPENDENT_CMDS.get(cmd)
    if need and need not in tag:
        lens = {"R": "rear", "I": "interior"}.get(need, need)
        return False, f"Needs the {lens} camera"
    if cmd in MULTI_LENS_CMDS and sum(ch.isalpha() for ch in tag) < 2:
        return False, "Needs a second camera"
    return True, None

# Explicit read-only GET commands (safe to call bare on this protocol).
CMD_SYSTEM_INFO = 3012        # firmware version <String>
CMD_QUERY_CUR_STATUS = 3014   # all setting states: repeated <Cmd>/<Status>
CMD_DISK_FREE_SPACE = 3017    # <Value> bytes free
CMD_GET_CARD_STATUS = 3024    # <Value> 0=removed 1=inserted 2=locked
CMD_GET_SENSOR_NUMBER = 8260  # <FrontSensor>/<InteriorSensor>/<RearSensor>/<Total>

# Destructive / disruptive command ids — REFUSED before a request is built.
# Names from the vendor DB (DEVICE_MODEL='A329S'); 3013 is undocumented there
# but empirically wedges the daemon, so it stays blocked.
DESTRUCTIVE_CMDS: dict[int, str] = {
    3010: "CMD_FORMAT_SD_CARD — formats the SD card",
    3011: "CMD_RESET_FACTORY — factory-resets settings",
    3013: "undocumented; wedges the camera daemon",
    3018: "CMD_RESTART_WIFI — drops the Wi-Fi connection",
    3023: "CMD_REMOVE_USER — kicks the connected client",
    4003: "CMD_DELETE_FILE — deletes a recording",
    8230: "CMD_REBOOT_DEVICE — reboots the camera",
    9316: "CMD_DELETE_SSD_FILE — deletes SSD files",
    9317: "CMD_FORMAT_SSD — formats the external SSD",
}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class ControlError(Exception):
    """Base class for control-layer errors."""


class CameraUnreachable(ControlError):
    """No usable HTTP response from the camera (reset / timeout / refused)."""


class DestructiveCommandError(ControlError):
    """A destructive/disruptive command id was requested; refused."""


class ValidationError(ControlError):
    """A setting key/value failed validation against the command map."""


# --------------------------------------------------------------------------- #
# DB access (read-only)
# --------------------------------------------------------------------------- #

@functools.lru_cache(maxsize=1)
def _load_map() -> dict:
    """Load and cache the derived command map (data/command_map.json)."""
    if not os.path.exists(MAP_PATH):
        raise ControlError(f"command map not found at {MAP_PATH}")
    with open(MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


def _model_commands(model: str) -> dict:
    """``{cmd_str: {key, description, options[]}}`` for a model (or empty)."""
    m = _load_map().get("models", {}).get(model)
    return m["commands"] if m else {}


def detect_model(version_string: str | None) -> str:
    """Pick the map's device model whose name appears in a firmware version
    string (e.g. 'VIOFO_A329S_V2.0_260313' -> 'A329S'). Longest match wins so
    'A329S' beats 'A329'. Falls back to :data:`DEFAULT_MODEL`."""
    if not version_string:
        return DEFAULT_MODEL
    vs = version_string.upper()
    models = list(_load_map().get("models", {}).keys())
    hits = sorted((m for m in models if m and m.upper() in vs),
                  key=len, reverse=True)
    return hits[0] if hits else DEFAULT_MODEL


def _cmd_key(model: str, cmd: int) -> str | None:
    entry = _model_commands(model).get(str(cmd))
    return entry["key"] if entry else None


def _resolve_cmd(model: str, key_or_cmd) -> tuple[int, str]:
    """Resolve a setting identifier (numeric cmd or CMD_KEY) to (cmd, key)."""
    s = str(key_or_cmd).strip()
    cmds = _model_commands(model)
    if s.isdigit():
        entry = cmds.get(s)
        if entry is None:
            raise ValidationError(f"cmd {s} not in map for model {model}")
        return int(s), entry["key"]
    for c, entry in cmds.items():
        if entry["key"] == s:
            return int(c), entry["key"]
    raise ValidationError(f"unknown setting '{s}' for model {model}")


def _options(model: str, cmd: int, camera_tag: str | None = None
             ) -> list[dict]:
    """Enumerated options for a setting: ``[{index, value, camera_tag}]``.

    Options can be replicated per camera configuration (camera_tag); when a
    tag is given we prefer its label, but the *index set* (what gets sent as
    ``par``) is the union across tags — different configs share indices and
    only differ in label."""
    entry = _model_commands(model).get(str(cmd))
    if not entry:
        return []
    by_index: dict[str, dict] = {}
    for o in entry["options"]:
        idx = str(o["index"])
        # First wins, unless a later row matches the requested camera tag.
        if idx not in by_index or (camera_tag and o.get("camera_tag") == camera_tag):
            by_index[idx] = {"index": idx, "value": o["value"],
                             "camera_tag": o.get("camera_tag")}
    return sorted(by_index.values(), key=lambda o: int(o["index"]))


# --------------------------------------------------------------------------- #
# Transport (single gentle request)
# --------------------------------------------------------------------------- #

def base_url(address: str) -> str:
    """Normalise an ``ADDRESS`` (host or URL) to an ``http://host`` base."""
    address = address.strip().rstrip("/")
    if address.startswith(("http://", "https://")):
        return address
    return f"http://{address}"


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, socket.timeout, http.client.HTTPException,
            ConnectionError, OSError) as e:
        raise CameraUnreachable(f"{type(e).__name__}: {e}") from e


_TAG_RE = re.compile(r"<(\w+)>([^<]*)</\1>")
_PAIR_RE = re.compile(r"<Cmd>(\d+)</Cmd>\s*<Status>(-?\d+)</Status>")


def _flat_tags(raw: str) -> dict[str, str]:
    """First value of each leaf tag in a (possibly multi-root/malformed)
    netapp body. Tolerant of the nested <Function> quirk cardv emits."""
    out: dict[str, str] = {}
    for tag, val in _TAG_RE.findall(raw):
        out.setdefault(tag, val.strip())
    return out


def _send(address: str, cmd: int, par=None, s=None) -> dict[str, str]:
    """Issue one ``?custom=1&cmd=`` request. Refuses destructive ids."""
    if int(cmd) in DESTRUCTIVE_CMDS:
        raise DestructiveCommandError(
            f"cmd {cmd} is destructive ({DESTRUCTIVE_CMDS[int(cmd)]}); refused")
    params = {"custom": "1", "cmd": str(cmd)}
    if par is not None:
        params["par"] = str(par)
    if s is not None:
        params["str"] = str(s)
    url = f"{base_url(address)}/?{urllib.parse.urlencode(params)}"
    return _flat_tags(_http_get(url))


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def read_info(address: str) -> dict:
    """Quick device facts via explicit GET commands (all read-only)."""
    info: dict = {}
    try:
        info["firmware"] = _send(address, CMD_SYSTEM_INFO).get("String")
    except CameraUnreachable:
        raise
    for key, cmd, tag in (
        ("free_space_bytes", CMD_DISK_FREE_SPACE, "Value"),
        ("card_status", CMD_GET_CARD_STATUS, "Value"),
    ):
        try:
            v = _send(address, cmd).get(tag)
            info[key] = int(v) if v and v.lstrip("-").isdigit() else v
        except CameraUnreachable:
            info[key] = None
    info["card_status_label"] = {0: "removed", 1: "inserted",
                                 2: "locked"}.get(info.get("card_status"))
    try:
        sensors = _send(address, CMD_GET_SENSOR_NUMBER)
        info["sensors"] = {
            "front": sensors.get("FrontSensor"),
            "interior": sensors.get("InteriorSensor"),
            "rear": sensors.get("RearSensor"),
            "total": sensors.get("Total"),
        }
        info["camera_tag"] = _camera_tag_from_sensors(sensors)
    except CameraUnreachable:
        info["sensors"] = None
        info["camera_tag"] = None
    return info


def _camera_tag_from_sensors(sensors: dict) -> str:
    """Build the CAMERA_TAG (e.g. 'F', 'F+R', 'F+R+I') from a 8260 reply."""
    parts = []
    if (sensors.get("FrontSensor") or "0") != "0":
        parts.append("F")
    if (sensors.get("RearSensor") or "0") != "0":
        parts.append("R")
    if (sensors.get("InteriorSensor") or "0") != "0":
        parts.append("I")
    return "+".join(parts) or "F"


def read_status_pairs(address: str) -> list[tuple[int, int]]:
    """Raw (cmd, status_value) pairs from ``cmd=3014``."""
    raw = _http_get(f"{base_url(address)}/?custom=1&cmd={CMD_QUERY_CUR_STATUS}")
    return [(int(c), int(s)) for c, s in _PAIR_RE.findall(raw)]


def read_settings(address: str, model: str | None = None) -> dict:
    """Decode the live ``cmd=3014`` dump into labelled settings.

    Returns ``{"model", "camera_tag", "settings": [ {cmd, key, value,
    label, writable, description}, ... ]}``. ``value`` is the raw status
    integer; ``label`` is the option text for that value when known."""
    info = read_info(address)
    if model is None:
        model = detect_model(info.get("firmware"))
    camera_tag = info.get("camera_tag")
    pairs = read_status_pairs(address)
    settings = []
    commands = _model_commands(model)
    for cmd, value in pairs:
        entry = commands.get(str(cmd))
        if entry is None:
            # Present in the live dump but not in this model's documented
            # map — surface it raw rather than hide it.
            settings.append({"cmd": cmd, "key": None, "value": value,
                             "label": None, "writable": False,
                             "destructive": cmd in DESTRUCTIVE_CMDS,
                             "description": None})
            continue
        opts = _options(model, cmd, camera_tag)
        label = next((o["value"] for o in opts
                      if int(o["index"]) == value), None)
        settings.append({
            "cmd": cmd, "key": entry["key"], "value": value, "label": label,
            "writable": bool(opts) and cmd not in DESTRUCTIVE_CMDS,
            "destructive": cmd in DESTRUCTIVE_CMDS,
            "description": entry.get("description"),
        })
    recording = next((s["value"] for s in settings if s["cmd"] == RECORD_CMD),
                     None)
    return {"model": model, "camera_tag": camera_tag, "recording": recording,
            "settings": settings}


def writable_catalog(address: str, model: str | None = None) -> dict:
    """All safely-writable settings for the UI: each with its option list and
    current value. Only enumerated, non-destructive settings are included."""
    snap = read_settings(address, model)
    cur = {s["cmd"]: s for s in snap["settings"]}
    model = snap["model"]
    tag = snap["camera_tag"]
    items = []
    for cmd_str, entry in sorted(_model_commands(model).items(),
                                 key=lambda kv: int(kv[0])):
        cmd = int(cmd_str)
        if cmd in DESTRUCTIVE_CMDS:
            continue
        opts = _options(model, cmd, tag)
        if not opts:
            continue  # not an enumerated setter — not in the safe UI set
        c = cur.get(cmd)
        supported, reason = _support(cmd, tag)
        items.append({
            "cmd": cmd, "key": entry["key"], "description": entry.get("description"),
            "options": [{"index": int(o["index"]), "value": o["value"]}
                        for o in opts],
            "current": (c["value"] if c else None),
            "current_label": (c["label"] if c else None),
            "supported": supported,
            "unsupported_reason": reason,
        })
    return {"model": model, "camera_tag": tag, "recording": snap.get("recording"),
            "settings": items}


# --------------------------------------------------------------------------- #
# Writes (allowlisted, validated, verified)
# --------------------------------------------------------------------------- #

def set_setting(address: str, key_or_cmd, value, model: str | None = None
                ) -> dict:
    """Validate and apply one enumerated setting, then read back to verify.

    ``value`` may be the option index (int/str) or its exact label text.
    Raises :class:`ValidationError` for unknown settings/values,
    :class:`DestructiveCommandError` for blocked ids. Returns a result dict
    with ``ok``/``verified`` and the applied value."""
    if model is None:
        model = detect_model(read_info(address).get("firmware"))
    cmd, key = _resolve_cmd(model, key_or_cmd)
    if cmd in DESTRUCTIVE_CMDS:
        raise DestructiveCommandError(
            f"{key} (cmd {cmd}) is destructive; refused")
    opts = _options(model, cmd)
    if not opts:
        raise ValidationError(
            f"{key} (cmd {cmd}) is not an enumerated setting; not writable "
            f"via this safe interface")
    # Resolve value -> index.
    want = str(value).strip()
    index = None
    for o in opts:
        if want == str(o["index"]) or want.lower() == str(o["value"]).lower():
            index = int(o["index"])
            break
    if index is None:
        allowed = ", ".join(f"{o['index']}={o['value']}" for o in opts)
        raise ValidationError(
            f"value '{value}' invalid for {key}; allowed: {allowed}")

    sent_ok, verified, applied, status = _apply_and_verify(address, cmd, index)

    # Some settings the camera silently refuses while recording. If a gated
    # setting didn't take and recording is active, briefly stop recording,
    # re-apply, and restore the prior record state.
    record_cycled = False
    if (not verified and cmd in RECORD_GATED_CMDS and cmd != RECORD_CMD):
        if _record_state(address) == 1:
            record_cycled = True
            try:
                _send(address, RECORD_CMD, par=0)   # stop recording
                time.sleep(RECORD_SETTLE)
                sent_ok, verified, applied, status = _apply_and_verify(
                    address, cmd, index)
            finally:
                # Always resume recording, even if the re-apply raised.
                try:
                    _send(address, RECORD_CMD, par=1)
                except CameraUnreachable:
                    logger.warning("failed to resume recording after a gated "
                                   "write to cmd %s", cmd)

    return {
        "ok": bool(sent_ok),
        "cmd": cmd, "key": key,
        "requested_index": index, "requested_value": value,
        "applied_index": applied,
        "verified": verified,
        "raw_status": status,
        "record_cycled": record_cycled,
    }


def _record_state(address: str) -> int | None:
    """Current recording state (1=recording, 0=stopped) from cmd=3014."""
    try:
        for cmd, v in read_status_pairs(address):
            if cmd == RECORD_CMD:
                return v
    except CameraUnreachable:
        return None
    return None


def _apply_and_verify(address: str, cmd: int, index: int):
    """Send ``cmd&par=index`` then poll cmd=3014 to confirm it applied.

    Returns ``(sent_ok, verified, applied_index, raw_status)``. The poll
    tolerates the camera lagging a freshly-written value in its status dump
    (otherwise a successful write reads back as a false negative)."""
    reply = _send(address, cmd, par=index)
    status = reply.get("Status")
    sent_ok = (status is not None and status.lstrip("-").isdigit()
               and int(status) == 0)
    verified = None
    applied = None
    for _ in range(VERIFY_ATTEMPTS):
        time.sleep(VERIFY_SETTLE)
        try:
            pairs = read_status_pairs(address)
        except CameraUnreachable:
            break
        for c, v in pairs:
            if c == cmd:
                applied = v
                verified = (v == index)
                break
        if verified:
            break
    return sent_ok, verified, applied, status
