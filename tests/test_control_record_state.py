"""record_state: resilient camera record-flag read for the active-recording
guard. 1=recording, 0=stopped, None=unknown (unsupported command, unreachable,
absent pair, or unparseable) — callers hold the newest capture on None."""
from __future__ import annotations

from viofosync_lib import _control as control


def test_record_state_returns_recording(monkeypatch):
    monkeypatch.setattr(control, "read_status_pairs",
                        lambda addr: [(2001, 1), (2002, 15)])
    assert control.record_state("1.2.3.4") == 1


def test_record_state_returns_stopped(monkeypatch):
    monkeypatch.setattr(control, "read_status_pairs",
                        lambda addr: [(2001, 0)])
    assert control.record_state("1.2.3.4") == 0


def test_record_state_none_when_pair_absent(monkeypatch):
    # A model whose cmd=3014 dump has no 2001 record pair.
    monkeypatch.setattr(control, "read_status_pairs",
                        lambda addr: [(2002, 15), (2003, 1)])
    assert control.record_state("1.2.3.4") is None


def test_record_state_none_when_unreachable(monkeypatch):
    def _boom(addr):
        raise control.CameraUnreachable("gone")
    monkeypatch.setattr(control, "read_status_pairs", _boom)
    assert control.record_state("1.2.3.4") is None


def test_record_state_none_on_unexpected_error(monkeypatch):
    def _boom(addr):
        raise ValueError("garbage xml")
    monkeypatch.setattr(control, "read_status_pairs", _boom)
    assert control.record_state("1.2.3.4") is None
