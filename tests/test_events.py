"""Tests for session lifecycle events (T017)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_STARTED,
)
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)


async def _add_vehicle(hass, entry_id, name="Peugeot 3008 PHEV", battery=14.4) -> str:
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "vehicle"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": name,
            "battery_capacity_kwh": battery,
            "charging_phases": "1",
            "charging_efficiency": 0.88,
        },
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry_id)
    return [s for s in entry.subentries.values() if s.subentry_type == "vehicle"][-1].subentry_id


async def _add_user(hass, entry_id, name="Petra") -> str:
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": name, "type": "regular"},
    )
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry_id)
    return [s for s in entry.subentries.values() if s.subentry_type == "user"][-1].subentry_id


async def _add_rfid(hass, entry_id, card_index, user_id, vehicle_id=None) -> None:
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    user_input = {"card_index": str(card_index), "user_id": user_id}
    if vehicle_id:
        user_input["vehicle_id"] = vehicle_id
    await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()


async def test_session_started_event_fields(hass: HomeAssistant):
    """session_started event contains all required fields with correct values."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    started_events = async_capture_events(hass, EVENT_SESSION_STARTED)

    await setup_session_engine(hass, entry)
    vehicle_id = await _add_vehicle(hass, entry.entry_id)
    user_id = await _add_user(hass, entry.entry_id)
    await _add_rfid(hass, entry.entry_id, card_index=1, user_id=user_id, vehicle_id=vehicle_id)

    await start_charging_session(hass, trx_value="2")

    assert len(started_events) == 1
    data = started_events[0].data

    assert "session_id" in data
    assert len(data["session_id"]) == 36  # UUID format
    assert data["user_name"] == "Petra"
    assert data["user_type"] == "regular"
    assert data["vehicle_name"] == "Peugeot 3008 PHEV"
    assert data["rfid_index"] == 1
    assert data["rfid_uid"] is None  # no lri entity configured
    assert "started_at" in data
    assert data["charger"] == "My go-e Charger"


async def test_session_completed_event_fields(hass: HomeAssistant):
    """session_completed event contains all required fields with correct values."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    session_start = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    with patch("custom_components.ev_charging_manager.session_engine.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = session_start

        await setup_session_engine(hass, entry)
        vehicle_id = await _add_vehicle(hass, entry.entry_id)
        user_id = await _add_user(hass, entry.entry_id)
        await _add_rfid(hass, entry.entry_id, card_index=1, user_id=user_id, vehicle_id=vehicle_id)

        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await start_charging_session(hass, trx_value="2")

        # Advance time > 60s and add energy > 50 Wh
        mock_dt.utcnow.return_value = session_start + timedelta(seconds=120)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        await hass.async_block_till_done()

        await stop_charging_session(hass)

    assert len(completed_events) == 1
    data = completed_events[0].data

    assert "session_id" in data
    assert data["user_name"] == "Petra"
    assert data["user_type"] == "regular"
    assert data["vehicle_name"] == "Peugeot 3008 PHEV"
    assert data["energy_kwh"] > 0
    assert data["cost_kr"] > 0
    assert data["charge_price_kr"] is None  # Always null in this PR
    assert data["duration_minutes"] >= 0
    assert data["avg_power_w"] >= 0
    assert "estimated_soc_added_pct" in data
    assert "started_at" in data
    assert "ended_at" in data
    assert data["rfid_index"] == 1  # card_index=1 → rfid_index=1


async def test_no_completed_event_for_micro_session(hass: HomeAssistant):
    """Micro-sessions do NOT fire session_completed event."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    await setup_session_engine(hass, entry)
    await start_charging_session(hass, trx_value="0")
    await stop_charging_session(hass)

    assert len(completed_events) == 0


async def test_started_event_still_fired_for_micro_sessions(hass: HomeAssistant):
    """session_started fires even for sessions that become micro-sessions."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    started_events = async_capture_events(hass, EVENT_SESSION_STARTED)

    await setup_session_engine(hass, entry)
    await start_charging_session(hass, trx_value="0")
    await stop_charging_session(hass)

    assert len(started_events) == 1
