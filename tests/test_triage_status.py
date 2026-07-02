"""Triage as a first-class sync status: hub substate + MQTT exposure."""
from __future__ import annotations

import types

from web.services.hub import Hub


async def test_hub_stores_and_clears_triage_substate():
    hub = Hub()
    assert hub.last_state["triage"] == {"active": False}

    await hub.broadcast({
        "type": "triage_progress", "active": True,
        "triaged": 40, "total": 876, "eta_s": 720,
    })
    assert hub.last_state["triage"] == {
        "active": True, "triaged": 40, "total": 876, "eta_s": 720,
    }

    await hub.broadcast({"type": "triage_progress", "active": False})
    assert hub.last_state["triage"] == {"active": False}


def _triage_hub():
    return types.SimpleNamespace(last_state={
        "sync_state": {"running": True, "paused": False},
        "dashcam_online": True,
        "triage": {"active": True, "triaged": 40, "total": 876, "eta_s": 720},
    })


def _snap():
    return types.SimpleNamespace(
        address="192.168.1.50", recordings="/recordings", disk_critical_pct=95,
    )


def test_mqtt_state_sync_status_is_triaging():
    from web.services import mqtt_state
    assert mqtt_state.state_sync_status(_triage_hub(), None, _snap()) == "triaging"


def test_mqtt_attrs_carry_triage_progress():
    from web.services import mqtt_state
    attrs = mqtt_state.attrs_sync_status(_triage_hub(), None, _snap())
    assert attrs["triage_active"] is True
    assert attrs["triaged"] == 40
    assert attrs["triage_total"] == 876
    assert attrs["triage_eta_s"] == 720


def test_mqtt_attrs_triage_inactive_by_default():
    from web.services import mqtt_state
    hub = types.SimpleNamespace(last_state={
        "sync_state": {"running": True, "paused": False},
        "dashcam_online": True, "current_item": None,
        "triage": {"active": False},
    })
    attrs = mqtt_state.attrs_sync_status(hub, None, _snap())
    assert attrs["triage_active"] is False


def test_sync_status_entity_republishes_on_triage_progress():
    from web.services.mqtt_topology import TOPOLOGY
    ent = next(e for e in TOPOLOGY if e.object_id == "sync_status")
    assert "triage_progress" in ent.affected_by_hub_events
