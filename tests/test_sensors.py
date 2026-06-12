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


# ---------------------------------------------------------------------------
# PR-29 (US2/FR-003): session-sensor availability keyed on active session
# ---------------------------------------------------------------------------

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"

# unique_id suffixes of all session sensors covered by the FR-003 base gate
_SESSION_SENSOR_SUFFIXES = [
    "current_user",
    "current_vehicle",
    "session_energy",
    "session_duration",
    "session_cost",
    "session_charge_price",
    "session_power",
    "session_soc_added",
    "charging_duration",
]


async def _setup_v2_engine(hass: HomeAssistant) -> "MockConfigEntry":
    """Set up a goe_gemini entry so the PlugAnchoredSessionEngine is active.

    The v2 engine is the only engine with a TRACKING-without-session state
    (waiting-for-RFID), which is the FR-003 scenario under test.
    """
    from unittest.mock import AsyncMock

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
        },
        title="Test go-e Charger",
    )

    from tests.conftest import MOCK_CAR_STATUS_ENTITY, MOCK_TRX_ENTITY

    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

    entry.add_to_hass(hass)
    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _state_by_uid(hass: HomeAssistant, entry, uid_suffix: str) -> str:
    """Return HA state for the sensor with unique_id {entry_id}_{uid_suffix}."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{uid_suffix}")
    if entity_id is None:
        return "MISSING"
    state = hass.states.get(entity_id)
    return state.state if state else "MISSING"


async def test_session_sensors_unavailable_during_waiting_for_rfid(hass: HomeAssistant):
    """FR-003: TRACKING without a session (waiting-for-RFID) → ALL session sensors unavailable.

    Before PR-29 the base gate was keyed on the engine's coarse TRACKING state,
    so the wait phase rendered every session sensor as 'unknown' (available with
    empty value) — and crashed the power sensor (round(None), US1).
    """
    from unittest.mock import AsyncMock

    from custom_components.ev_charging_manager.const import SessionSubState
    from tests.conftest import MOCK_TRX_ENTITY  # noqa: F401 — readability

    entry = await _setup_v2_engine(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in with trx=null → engine TRACKING, no session (RFID wait)
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING
    assert engine.active_session is None
    assert engine.get_status_sub_state() == SessionSubState.WAITING_FOR_RFID

    for suffix in _SESSION_SENSOR_SUFFIXES:
        assert _state_by_uid(hass, entry, suffix) == STATE_UNAVAILABLE, (
            f"FR-003: sensor '{suffix}' must be unavailable during waiting-for-RFID, "
            f"got {_state_by_uid(hass, entry, suffix)!r}"
        )


async def test_session_sensors_available_when_session_starts(hass: HomeAssistant):
    """FR-003: session start (RFID resolved) → session sensors become available with values."""
    from unittest.mock import AsyncMock

    from tests.conftest import MOCK_TRX_ENTITY

    entry = await _setup_v2_engine(hass)
    vehicle_id = await _add_vehicle(hass, entry.entry_id)
    user_id = await _add_user(hass, entry.entry_id)
    await _add_rfid(hass, entry.entry_id, card_index=1, user_id=user_id, vehicle_id=vehicle_id)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Plug in waiting for RFID, then blip → session starts
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()

        # Power + energy flow
        hass.states.async_set(MOCK_POWER_ENTITY, "3650.0")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
        await hass.async_block_till_done()

    assert engine.active_session is not None

    assert _state_by_uid(hass, entry, "current_user") == "Petra"
    assert _state_by_uid(hass, entry, "current_vehicle") == "Peugeot 3008 PHEV"
    assert float(_state_by_uid(hass, entry, "session_energy")) == pytest.approx(5.0, abs=0.01)
    assert float(_state_by_uid(hass, entry, "session_cost")) == pytest.approx(12.50, abs=0.01)
    assert float(_state_by_uid(hass, entry, "session_power")) == pytest.approx(3650.0, abs=1.0)
    assert _state_by_uid(hass, entry, "session_duration") != STATE_UNAVAILABLE


async def test_session_sensors_unavailable_after_unplug(hass: HomeAssistant):
    """FR-003: unplug (back to IDLE) → all session sensors unavailable again."""
    from unittest.mock import AsyncMock

    from tests.conftest import MOCK_TRX_ENTITY

    entry = await _setup_v2_engine(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Blip-first start (fast path)
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        assert engine.active_session is not None

        # Unplug: cable_lock → Unlocked confirms genuine unplug, then plug off
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "off")
        await hass.async_block_till_done()

    assert engine.active_session is None
    assert engine.state == SessionEngineState.IDLE

    for suffix in _SESSION_SENSOR_SUFFIXES:
        assert _state_by_uid(hass, entry, suffix) == STATE_UNAVAILABLE, (
            f"FR-003: sensor '{suffix}' must be unavailable after unplug"
        )


async def test_status_and_binary_sensor_unaffected_by_availability_rule(hass: HomeAssistant):
    """FR-004: status sensor and the charging binary sensor keep their always-available design."""
    from unittest.mock import AsyncMock

    entry = await _setup_v2_engine(hass)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # Waiting-for-RFID phase (no session)
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

    # Status sensor: available, showing the wait sub-state
    assert _state_by_uid(hass, entry, "status") == "waiting_for_rfid"

    # Charging binary sensor: available (off), never unavailable while engine exists
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    binary_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_charging"
    )
    assert binary_id is not None
    state = hass.states.get(binary_id)
    assert state is not None
    assert state.state == "off"


# ---------------------------------------------------------------------------
# PR-29 (US1/FR-001, FR-002): power sensor never crashes; public property
# ---------------------------------------------------------------------------


async def test_power_sensor_renders_unknown_without_reading_no_exception(
    hass: HomeAssistant, caplog
):
    """FR-002: active session but no power reading yet → state 'unknown', no exception/error.

    The no-reading-yet condition is reachable in production via restart
    recovery: PlugAnchoredSessionEngine.async_recover() sets
    _last_power_w = self._get_power(), which is None while the power entity
    is still unavailable after the restart. Before PR-29 this made
    SessionPowerSensor.native_value raise TypeError (round(None)) on every
    update.
    """
    import logging
    from unittest.mock import AsyncMock

    from homeassistant.const import STATE_UNKNOWN
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    from custom_components.ev_charging_manager.const import SIGNAL_SESSION_UPDATE
    from tests.conftest import MOCK_TRX_ENTITY

    entry = await _setup_v2_engine(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()

    assert engine.active_session is not None

    # Simulate the post-restart condition: no power reading processed yet
    # (async_recover with the power entity unavailable leaves this None).
    engine._last_power_w = None

    with caplog.at_level(logging.ERROR):
        caplog.clear()
        async_dispatcher_send(hass, SIGNAL_SESSION_UPDATE.format(entry.entry_id))
        await hass.async_block_till_done()

    # FR-001: the public surface yields "no reading yet" distinctly
    assert engine.last_power_w is None

    # Sensor available (session exists) but value unknown — and NO error log
    assert _state_by_uid(hass, entry, "session_power") == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        f"FR-002: no error log expected, got: {[r.message for r in caplog.records]}"
    )


async def test_power_sensor_shows_rounded_value_via_public_property(hass: HomeAssistant):
    """FR-001: with a reading present, the sensor shows the rounded value as today."""
    from unittest.mock import AsyncMock

    from tests.conftest import MOCK_TRX_ENTITY

    entry = await _setup_v2_engine(hass)
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_POWER_ENTITY, "3650.4")
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
        await hass.async_block_till_done()

    # FR-001: public read-only property on the engine
    assert engine.last_power_w == pytest.approx(3650.4)
    assert float(_state_by_uid(hass, entry, "session_power")) == pytest.approx(3650.0, abs=0.5)


async def test_last_power_w_property_exists_on_both_engines(hass: HomeAssistant):
    """FR-001: both engines expose last_power_w; the sensor uses no private reach-ins."""
    import inspect

    from custom_components.ev_charging_manager.sensor import SessionPowerSensor

    # Legacy engine (generic profile)
    legacy_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Legacy")
    await setup_session_engine(hass, legacy_entry)
    legacy_engine = hass.data[DOMAIN][legacy_entry.entry_id]["session_engine"]
    assert hasattr(type(legacy_engine), "last_power_w"), (
        "FR-001: legacy SessionEngine must expose the last_power_w property"
    )
    assert isinstance(type(legacy_engine).last_power_w, property), (
        "FR-001: last_power_w must be a read-only property"
    )
    assert legacy_engine.last_power_w is None or isinstance(legacy_engine.last_power_w, float)

    # v2 engine
    v2_entry = await _setup_v2_engine(hass)
    v2_engine = hass.data[DOMAIN][v2_entry.entry_id]["session_engine"]
    assert isinstance(type(v2_engine).last_power_w, property), (
        "FR-001: PlugAnchoredSessionEngine must expose the last_power_w property"
    )

    # The sensor consumes the public property — no private access remains
    source = inspect.getsource(SessionPowerSensor.native_value.fget)
    assert "_last_power_w" not in source, (
        "FR-001: SessionPowerSensor.native_value must not reach into _last_power_w"
    )


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
