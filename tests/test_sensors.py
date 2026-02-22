"""Tests for sensor and binary sensor entities (T020)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import DOMAIN, SessionEngineState
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

_ENGINE_MODULE = "custom_components.ev_charging_manager.session_engine"


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


# ---------------------------------------------------------------------------
# Helper: assert entity state
# ---------------------------------------------------------------------------


def _state(hass, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is None:
        return "MISSING"
    return state.state


# ---------------------------------------------------------------------------
# Tests: idle state
# ---------------------------------------------------------------------------


async def test_all_session_sensors_unavailable_when_idle(hass: HomeAssistant):
    """All session sensors are unavailable when no session is active."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    prefix = "my_go_e_charger"
    assert _state(hass, f"sensor.{prefix}_session_energy") == STATE_UNAVAILABLE
    assert _state(hass, f"sensor.{prefix}_session_cost") == STATE_UNAVAILABLE
    assert _state(hass, f"sensor.{prefix}_session_power") == STATE_UNAVAILABLE
    assert _state(hass, f"sensor.{prefix}_session_soc_added") == STATE_UNAVAILABLE
    assert _state(hass, f"sensor.{prefix}_current_vehicle") == STATE_UNAVAILABLE
    assert _state(hass, "sensor.my_go_e_charger_current_user") == STATE_UNAVAILABLE


async def test_binary_sensor_off_when_idle(hass: HomeAssistant):
    """Charging binary sensor is off when idle."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    assert _state(hass, "binary_sensor.my_go_e_charger_charging") == "off"


async def test_status_sensor_shows_idle_when_no_session(hass: HomeAssistant):
    """Status sensor always shows current state (never unavailable)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    assert _state(hass, "sensor.my_go_e_charger_status") == SessionEngineState.IDLE


# ---------------------------------------------------------------------------
# Tests: active session
# ---------------------------------------------------------------------------


async def test_sensors_populated_during_session(hass: HomeAssistant):
    """Sensors show correct values during an active session."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    vehicle_id = await _add_vehicle(hass, entry.entry_id)
    user_id = await _add_user(hass, entry.entry_id)
    await _add_rfid(hass, entry.entry_id, card_index=1, user_id=user_id, vehicle_id=vehicle_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "3650.0")
    await start_charging_session(hass, trx_value="2")

    # Simulate 5 kWh charged
    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "3650.0")
    await hass.async_block_till_done()

    prefix = "my_go_e_charger"
    # User sensor
    assert _state(hass, "sensor.my_go_e_charger_current_user") == "Petra"
    # Vehicle sensor
    assert _state(hass, f"sensor.{prefix}_current_vehicle") == "Peugeot 3008 PHEV"
    # Energy: 5.0 kWh
    assert float(_state(hass, f"sensor.{prefix}_session_energy")) == pytest.approx(5.0, abs=0.01)
    # Cost: 5.0 × 2.50 = 12.50 kr
    assert float(_state(hass, f"sensor.{prefix}_session_cost")) == pytest.approx(12.50, abs=0.01)
    # Power
    assert float(_state(hass, f"sensor.{prefix}_session_power")) == pytest.approx(3650.0, abs=1.0)
    # Status sensor shows tracking
    assert _state(hass, f"sensor.{prefix}_status") == SessionEngineState.TRACKING
    # Binary sensor is on
    assert _state(hass, f"binary_sensor.{prefix}_charging") == "on"


async def test_soc_sensor_populated_for_known_vehicle(hass: HomeAssistant):
    """SoC sensor shows calculated value when vehicle has battery data."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    vehicle_id = await _add_vehicle(hass, entry.entry_id, battery=14.4)
    user_id = await _add_user(hass, entry.entry_id)
    await _add_rfid(hass, entry.entry_id, card_index=1, user_id=user_id, vehicle_id=vehicle_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="2")

    # 5 kWh at 88% efficiency into 14.4 kWh battery: (5 × 0.88) / 14.4 × 100 ≈ 30.6%
    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    await hass.async_block_till_done()

    soc_state = _state(hass, "sensor.my_go_e_charger_session_soc_added")
    assert soc_state != STATE_UNAVAILABLE
    assert float(soc_state) == pytest.approx(30.6, abs=0.5)


async def test_soc_sensor_unavailable_for_unknown_user(hass: HomeAssistant):
    """SoC sensor is unavailable when user has no vehicle."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    # trx=0 → unknown user → no vehicle → SoC unavailable
    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    await start_charging_session(hass, trx_value="0")

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await hass.async_block_till_done()

    assert _state(hass, "sensor.my_go_e_charger_session_soc_added") == STATE_UNAVAILABLE


async def test_charge_price_always_unavailable(hass: HomeAssistant):
    """Charge price sensor is always unavailable in this PR (implemented in PR-06)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)
    await start_charging_session(hass, trx_value="0")

    assert _state(hass, "sensor.my_go_e_charger_session_charge_price") == STATE_UNAVAILABLE


async def test_sensors_return_to_unavailable_after_session_ends(hass: HomeAssistant):
    """After session ends, all session sensors return to unavailable."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    session_start = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
    with patch(f"{_ENGINE_MODULE}.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = session_start
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await start_charging_session(hass, trx_value="0")

        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
        await hass.async_block_till_done()

        # Advance time > 60s to avoid micro-session filter
        mock_dt.utcnow.return_value = session_start + timedelta(seconds=120)
        await stop_charging_session(hass)

    prefix = "my_go_e_charger"
    assert _state(hass, f"sensor.{prefix}_session_energy") == STATE_UNAVAILABLE
    assert _state(hass, f"sensor.{prefix}_session_cost") == STATE_UNAVAILABLE
    assert _state(hass, f"binary_sensor.{prefix}_charging") == "off"
    assert _state(hass, f"sensor.{prefix}_status") == SessionEngineState.IDLE


async def test_duration_sensor_format(hass: HomeAssistant):
    """Duration sensor returns HH:MM:SS format string."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)
    await start_charging_session(hass, trx_value="0")

    duration = _state(hass, "sensor.my_go_e_charger_session_duration")
    assert duration != STATE_UNAVAILABLE
    # Should match HH:MM:SS format
    parts = duration.split(":")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


async def test_energy_sensor_device_class(hass: HomeAssistant):
    """Energy sensor has correct device class (ENERGY)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)
    await start_charging_session(hass, trx_value="0")

    state = hass.states.get("sensor.my_go_e_charger_session_energy")
    assert state is not None
    assert state.attributes.get("device_class") == "energy"
    assert state.attributes.get("unit_of_measurement") == "kWh"


async def test_cost_sensor_device_class(hass: HomeAssistant):
    """Cost sensor has MONETARY device class."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)
    await start_charging_session(hass, trx_value="0")

    state = hass.states.get("sensor.my_go_e_charger_session_cost")
    assert state is not None
    assert state.attributes.get("device_class") == "monetary"


async def test_status_sensor_last_session_attributes_after_completed_session(
    hass: HomeAssistant,
):
    """StatusSensor exposes last_session_user and last_session_rfid_index after session."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")

    session_start = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    with patch(f"{_ENGINE_MODULE}.dt_util") as mock_dt:
        mock_dt.utcnow.return_value = session_start

        await setup_session_engine(hass, entry)
        vehicle_id = await _add_vehicle(hass, entry.entry_id)
        user_id = await _add_user(hass, entry.entry_id)
        await _add_rfid(hass, entry.entry_id, card_index=1, user_id=user_id, vehicle_id=vehicle_id)

        # Before any session, attributes should be None
        status = hass.states.get("sensor.my_go_e_charger_status")
        assert status.attributes.get("last_session_user") is None
        assert status.attributes.get("last_session_rfid_index") is None

        # Start and complete a session
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        await start_charging_session(hass, trx_value="2")

        mock_dt.utcnow.return_value = session_start + timedelta(seconds=120)
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        await hass.async_block_till_done()

        await stop_charging_session(hass)

    # After session, status sensor should have last session info
    status = hass.states.get("sensor.my_go_e_charger_status")
    assert status.attributes.get("last_session_user") == "Petra"
    assert status.attributes.get("last_session_rfid_index") == 1
