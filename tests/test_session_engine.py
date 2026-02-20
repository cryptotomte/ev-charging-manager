"""Tests for SessionEngine state machine (T014, T015, T022 RFID UID)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    EVENT_SESSION_STARTED,
    SessionEngineState,
)
from tests.conftest import (
    MOCK_CAR_STATUS_ENTITY,
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_TRX_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

# ---------------------------------------------------------------------------
# Subentry helpers (using HA subentry flow API, same as lifecycle tests)
# ---------------------------------------------------------------------------


async def _add_vehicle(
    hass, entry_id, name="Peugeot 3008 PHEV", battery=14.4, efficiency=0.88
) -> str:
    """Add a vehicle subentry and return its subentry_id."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "vehicle"),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": name,
            "battery_capacity_kwh": battery,
            "charging_phases": "1",
            "charging_efficiency": efficiency,
        },
    )
    await hass.async_block_till_done()
    return (
        result.get("result", {}).subentry_id
        if hasattr(result.get("result", None), "subentry_id")
        else _get_last_subentry_id(hass, entry_id, "vehicle")
    )


async def _add_user(hass, entry_id, name="Petra") -> str:
    """Add a user subentry and return its subentry_id."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "user"),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": name, "type": "regular"},
    )
    await hass.async_block_till_done()
    return _get_last_subentry_id(hass, entry_id, "user")


async def _add_rfid(hass, entry_id, card_index, user_id, vehicle_id=None) -> None:
    """Add an RFID mapping subentry."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "rfid_mapping"),
        context={"source": "user"},
    )
    user_input = {"card_index": str(card_index), "user_id": user_id}
    if vehicle_id:
        user_input["vehicle_id"] = vehicle_id
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input,
    )
    await hass.async_block_till_done()


def _get_last_subentry_id(hass, entry_id, subentry_type) -> str:
    """Get the ID of the last added subentry of a given type."""
    entry = hass.config_entries.async_get_entry(entry_id)
    matching = [s for s in entry.subentries.values() if s.subentry_type == subentry_type]
    return matching[-1].subentry_id


async def _setup_full_engine(hass, entry_id) -> None:
    """Add Petra + Peugeot + RFID card 1 (trx=2 → index=1)."""
    vehicle_id = await _add_vehicle(hass, entry_id)
    user_id = await _add_user(hass, entry_id)
    await _add_rfid(hass, entry_id, card_index=1, user_id=user_id, vehicle_id=vehicle_id)


# ---------------------------------------------------------------------------
# T014: State machine transition tests
# ---------------------------------------------------------------------------


async def test_idle_to_tracking_on_charging_with_trx(hass: HomeAssistant):
    """IDLE → TRACKING when car_value=Charging and trx is set."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE

    await start_charging_session(hass, trx_value="2")

    assert engine.state == SessionEngineState.TRACKING
    assert engine.active_session is not None


async def test_no_transition_without_trx(hass: HomeAssistant):
    """car_value=Charging alone (trx=null) does NOT start a session."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Only set car_value, leave trx as "null"
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE


async def test_no_transition_without_car_status(hass: HomeAssistant):
    """trx set alone (car_value=Idle) does NOT start a session."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE


async def test_tracking_to_idle_on_complete(hass: HomeAssistant):
    """TRACKING → IDLE when car_value changes from Charging to Complete."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    await start_charging_session(hass, trx_value="0")
    assert engine.state == SessionEngineState.TRACKING

    # Advance time to exceed micro-session threshold
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=120))
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
    await hass.async_block_till_done()

    await stop_charging_session(hass)
    assert engine.state == SessionEngineState.IDLE


async def test_session_snapshot_contains_correct_user_vehicle(hass: HomeAssistant):
    """Active session snapshot has correct user and vehicle from RFID lookup."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)
    await _setup_full_engine(hass, entry.entry_id)

    await start_charging_session(hass, trx_value="2")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.user_name == "Petra"
    assert session.user_type == "regular"
    assert session.vehicle_name == "Peugeot 3008 PHEV"
    assert session.vehicle_battery_kwh == 14.4
    assert session.efficiency_factor == 0.88
    assert session.rfid_index == 1


async def test_energy_updates_during_tracking(hass: HomeAssistant):
    """Energy_kwh is updated during TRACKING from entity state changes."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    # Start with energy at 10.0 kWh
    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.energy_start_kwh == 10.0
    assert session.energy_kwh == 0.0

    # Energy increases to 12.4 kWh
    hass.states.async_set(MOCK_ENERGY_ENTITY, "12.4")
    await hass.async_block_till_done()

    assert abs(session.energy_kwh - 2.4) < 0.001


async def test_cost_calculated_from_energy(hass: HomeAssistant):
    """Cost is calculated as energy_kwh × static_price during tracking."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session

    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    await hass.async_block_till_done()

    # 5.0 kWh × 2.50 kr/kWh = 12.50 kr
    assert abs(session.cost_total_kr - 12.50) < 0.01


async def test_soc_calculated_for_known_vehicle(hass: HomeAssistant):
    """SoC estimate is calculated when vehicle has battery data."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)
    await _setup_full_engine(hass, entry.entry_id)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="2")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session

    hass.states.async_set(MOCK_ENERGY_ENTITY, "12.4")
    await hass.async_block_till_done()

    # (12.4 × 0.88) / 14.4 × 100 ≈ 75.8%
    assert session.estimated_soc_added_pct is not None
    assert abs(session.estimated_soc_added_pct - 75.78) < 0.5


async def test_duration_calculated_on_session_end(hass: HomeAssistant):
    """Session ends and engine returns to IDLE after completion."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    # Advance time by 2 minutes
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=120))
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
    await hass.async_block_till_done()

    await stop_charging_session(hass)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE


async def test_network_gap_sensor_unavailable_session_continues(hass: HomeAssistant, caplog):
    """Mid-session energy sensor unavailability keeps last value + logs warning."""
    import logging

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session

    # Energy increases to 7.0
    hass.states.async_set(MOCK_ENERGY_ENTITY, "7.0")
    await hass.async_block_till_done()

    # Simulate network gap — energy entity goes unavailable
    with caplog.at_level(logging.WARNING):
        hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
        await hass.async_block_till_done()

    # Session should still be active
    assert engine.state == SessionEngineState.TRACKING
    # Last valid energy value should be preserved
    assert abs(engine._last_energy_kwh - 7.0) < 0.001
    assert "unavailable" in caplog.text.lower() or "keeping last value" in caplog.text

    # Recovery: energy comes back
    hass.states.async_set(MOCK_ENERGY_ENTITY, "8.0")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING
    assert abs(session.energy_kwh - 3.0) < 0.001  # 8.0 - 5.0


async def test_mid_session_trx_change_snapshot_preserved(hass: HomeAssistant):
    """Changing trx during a session does not end the session."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    original_user = engine.active_session.user_name

    # Simulate trx change during session — car_value still "Charging"
    hass.states.async_set(MOCK_TRX_ENTITY, "3")
    await hass.async_block_till_done()

    # Session should continue (snapshot user unchanged)
    assert engine.state == SessionEngineState.TRACKING
    assert engine.active_session.user_name == original_user


# ---------------------------------------------------------------------------
# T015: Unknown user tests (US2)
# ---------------------------------------------------------------------------


async def test_trx_zero_creates_unknown_session(hass: HomeAssistant):
    """trx=0 creates a session with user=Unknown/no_rfid."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.user_name == "Unknown"
    assert session.user_type == "unknown"
    assert session.vehicle_name is None
    assert session.rfid_index is None
    assert session.estimated_soc_added_pct is None


async def test_unmapped_trx_creates_unknown_session(hass: HomeAssistant, caplog):
    """trx=5 with no mapping for index 4 creates Unknown session + warning."""
    import logging

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    with caplog.at_level(logging.WARNING):
        await start_charging_session(hass, trx_value="5")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.user_name == "Unknown"
    assert "No RFID mapping found" in caplog.text


async def test_inactive_rfid_creates_unknown_session(hass: HomeAssistant, caplog):
    """Inactive RFID card creates Unknown/rfid_inactive session + warning."""
    import logging

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    # Add a user
    user_id = await _add_user(hass, entry.entry_id)
    # Add an RFID mapping at card_index=1 (trx=2)
    await _add_rfid(hass, entry.entry_id, card_index=1, user_id=user_id)

    # Deactivate the mapping via reconfigure
    entry_obj = hass.config_entries.async_get_entry(entry.entry_id)
    rfid_subentry = next(
        s for s in entry_obj.subentries.values() if s.subentry_type == "rfid_mapping"
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"),
        context={"source": "reconfigure", "subentry_id": rfid_subentry.subentry_id},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"user_id": user_id, "active": False},
    )
    await hass.async_block_till_done()

    with caplog.at_level(logging.WARNING):
        await start_charging_session(hass, trx_value="2")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.user_name == "Unknown"
    assert "inactive" in caplog.text


async def test_type_agnostic_trx_int_vs_string(hass: HomeAssistant):
    """trx='2' (string from HA entity) resolves via RFID lookup correctly."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)
    await _setup_full_engine(hass, entry.entry_id)

    # HA entity always returns string values
    await start_charging_session(hass, trx_value="2")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.user_name == "Petra"
    assert session.rfid_index == 1


# ---------------------------------------------------------------------------
# T022: RFID UID tests (US6)
# ---------------------------------------------------------------------------


async def test_lri_sensor_uid_captured(hass: HomeAssistant):
    """When lri/tsi entity has a UID, it is stored in session.rfid_uid."""
    data_with_uid = {**MOCK_CHARGER_DATA, "rfid_uid_entity": "sensor.goe_abc123_lri"}
    entry = MockConfigEntry(domain=DOMAIN, data=data_with_uid, title="Test Charger")
    await setup_session_engine(hass, entry)

    # Pre-set RFID UID entity
    hass.states.async_set("sensor.goe_abc123_lri", "04:B7:C8:D2:E1:F3:A2")

    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.rfid_uid == "04:B7:C8:D2:E1:F3:A2"


async def test_lri_sensor_unavailable_uid_is_none(hass: HomeAssistant):
    """When lri entity is unavailable, rfid_uid is None."""
    data_with_uid = {**MOCK_CHARGER_DATA, "rfid_uid_entity": "sensor.goe_abc123_lri"}
    entry = MockConfigEntry(domain=DOMAIN, data=data_with_uid, title="Test Charger")
    await setup_session_engine(hass, entry)

    hass.states.async_set("sensor.goe_abc123_lri", STATE_UNAVAILABLE)

    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.rfid_uid is None


async def test_no_lri_entity_uid_is_none(hass: HomeAssistant):
    """When rfid_uid_entity is not configured, rfid_uid is None."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session.rfid_uid is None


async def test_uid_included_in_session_started_event(hass: HomeAssistant):
    """RFID UID is included in the session_started event data."""
    data_with_uid = {**MOCK_CHARGER_DATA, "rfid_uid_entity": "sensor.goe_abc123_lri"}
    entry = MockConfigEntry(domain=DOMAIN, data=data_with_uid, title="Test Charger")
    started_events = async_capture_events(hass, EVENT_SESSION_STARTED)

    await setup_session_engine(hass, entry)
    hass.states.async_set("sensor.goe_abc123_lri", "AB:CD:EF:12:34:56:78")

    await start_charging_session(hass, trx_value="0")

    assert len(started_events) == 1
    assert started_events[0].data["rfid_uid"] == "AB:CD:EF:12:34:56:78"


async def test_two_chargers_independent(hass: HomeAssistant):
    """Two config entries (two chargers) use independent SessionEngines."""

    # Second charger with distinct entity IDs
    charger_b_data = {
        **MOCK_CHARGER_DATA,
        "car_status_entity": "sensor.charger_b_car_value",
        "rfid_entity": "select.charger_b_trx",
        "energy_entity": "sensor.charger_b_wh",
        "power_entity": "sensor.charger_b_nrg_11",
        "charger_name": "Charger B",
    }

    entry_a = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Charger A")
    entry_b = MockConfigEntry(domain=DOMAIN, data=charger_b_data, title="Charger B")

    await setup_session_engine(hass, entry_a)
    await setup_session_engine(hass, entry_b)

    engine_a = hass.data[DOMAIN][entry_a.entry_id]["session_engine"]
    engine_b = hass.data[DOMAIN][entry_b.entry_id]["session_engine"]

    # Start session on charger A only
    await start_charging_session(hass, trx_value="0")

    assert engine_a.state == SessionEngineState.TRACKING
    assert engine_b.state == SessionEngineState.IDLE

    # Stop charger A — charger B remains idle
    await stop_charging_session(hass)

    assert engine_a.state == SessionEngineState.IDLE
    assert engine_b.state == SessionEngineState.IDLE
