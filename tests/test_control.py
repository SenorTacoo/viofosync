"""Unit tests for the camera-control layer (viofosync_lib._control).

All hardware I/O is monkeypatched, so these run without a camera. They cover
the parts that protect the device: the destructive denylist, value validation,
the support classification (lens / Wi-Fi limits), the read-back verification,
and the record-gated retry.
"""
from __future__ import annotations

import pytest

from viofosync_lib import _control as control

# --------------------------------------------------------------------------- #
# Pure data layer (uses the shipped command_map.json; no camera)
# --------------------------------------------------------------------------- #

def test_detect_model_longest_match():
    assert control.detect_model("VIOFO_A329S_V2.0_260313") == "A329S"
    # 'A329S' must beat the shorter 'A329'
    assert control.detect_model("A329") == "A329"
    assert control.detect_model(None) == control.DEFAULT_MODEL


def test_resolve_cmd_by_key_and_number():
    assert control._resolve_cmd("A329S", "CMD_GPS_SWITCH")[0] == 8208
    assert control._resolve_cmd("A329S", 8208)[1] == "CMD_GPS_SWITCH"


def test_resolve_cmd_unknown_raises():
    with pytest.raises(control.ValidationError):
        control._resolve_cmd("A329S", "CMD_NOPE")
    with pytest.raises(control.ValidationError):
        control._resolve_cmd("A329S", 999999)


def test_options_have_unique_sorted_indices():
    opts = control._options("A329S", 8222, "F")
    idx = [int(o["index"]) for o in opts]
    assert idx == sorted(idx)
    assert len(idx) == len(set(idx))


def test_support_station_locked_and_lenses():
    # Resolution is not changeable over Wi-Fi.
    ok, reason = control._support(8222, "F")
    assert not ok and "Wi-Fi" in reason
    # Rear-HDR needs the rear lens: blocked on 'F', allowed on 'F+R'.
    assert control._support(9319, "F") == (False, "Needs the rear camera")
    assert control._support(9319, "F+R") == (True, None)
    # A plain setting is supported.
    assert control._support(8208, "F") == (True, None)


# --------------------------------------------------------------------------- #
# Transport helpers
# --------------------------------------------------------------------------- #

def test_base_url_normalisation():
    assert control.base_url("192.168.1.254") == "http://192.168.1.254"
    assert control.base_url("http://cam/") == "http://cam"


def test_parser_reads_pairs_and_leaf_tags():
    xml = ('<?xml version="1.0"?><Function><Cmd>3012</Cmd><Status>0</Status>'
           '<String>VIOFO_A329S_V2.0</String></Function>')
    assert control._flat_tags(xml)["String"] == "VIOFO_A329S_V2.0"
    dump = "<Cmd>2001</Cmd><Status>1</Status><Cmd>8208</Cmd><Status>0</Status>"
    pairs = control._PAIR_RE.findall(dump)
    assert [(int(c), int(s)) for c, s in pairs] == [(2001, 1), (8208, 0)]


# --------------------------------------------------------------------------- #
# Write path (monkeypatched transport)
# --------------------------------------------------------------------------- #

def test_set_setting_refuses_destructive():
    with pytest.raises(control.DestructiveCommandError):
        control.set_setting("cam", 3010, 0, model="A329S")  # format SD


def test_set_setting_validates_value():
    with pytest.raises(control.ValidationError):
        control.set_setting("cam", "CMD_GPS_SWITCH", "banana", model="A329S")


def test_set_setting_happy_path(monkeypatch):
    sent = {}

    def fake_send(address, cmd, par=None, s=None):
        sent["cmd"], sent["par"] = cmd, par
        return {"Cmd": str(cmd), "Status": "0"}

    # Read-back reports the value we just wrote -> verified.
    monkeypatch.setattr(control, "_send", fake_send)
    monkeypatch.setattr(control, "read_status_pairs",
                        lambda addr: [(8208, 1)])
    monkeypatch.setattr(control, "VERIFY_SETTLE", 0)

    r = control.set_setting("cam", "CMD_GPS_SWITCH", "1", model="A329S")
    assert sent == {"cmd": 8208, "par": 1}
    assert r["ok"] and r["verified"] is True and r["applied_index"] == 1
    assert r["record_cycled"] is False


def test_record_gated_retry_cycles_recording(monkeypatch):
    calls = []

    def fake_send(address, cmd, par=None, s=None):
        calls.append((cmd, par))
        return {"Status": "0"}

    # First verify fails, second (after stopping recording) succeeds.
    results = iter([(True, False, 3, "0"), (True, True, 2, "0")])
    monkeypatch.setattr(control, "_send", fake_send)
    monkeypatch.setattr(control, "_apply_and_verify",
                        lambda a, c, i: next(results))
    monkeypatch.setattr(control, "_record_state", lambda a: 1)
    monkeypatch.setattr(control, "RECORD_SETTLE", 0)

    r = control.set_setting("cam", 8222, 2, model="A329S")  # resolution (gated)
    assert r["record_cycled"] is True and r["verified"] is True
    # Recording was stopped (par=0) and restarted (par=1).
    assert (control.RECORD_CMD, 0) in calls and (control.RECORD_CMD, 1) in calls
