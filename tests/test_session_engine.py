"""Tests for SessionEngine state machine (T014, T015, T022 RFID UID, T010 spot)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    EVENT_SESSION_COMPLETED,
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


# ---------------------------------------------------------------------------
# T010: Spot pricing session tests (US1)
# ---------------------------------------------------------------------------

MOCK_SPOT_PRICE_ENTITY = "sensor.nordpool_kwh"

# Spot config entry data — mirrors quickstart.md scenario setup
MOCK_SPOT_CHARGER_DATA = {
    **MOCK_CHARGER_DATA,
    "pricing_mode": "spot",
    "spot_price_entity": MOCK_SPOT_PRICE_ENTITY,
    "spot_additional_cost_kwh": 0.85,
    "spot_vat_multiplier": 1.25,
    "spot_fallback_price_kwh": 2.50,
}

# Options that bypass micro-session filter so all sessions are persisted
_NO_MICRO = {"min_session_duration_s": 0, "min_session_energy_wh": 0}


async def test_static_session_unchanged_by_spot_feature(hass: HomeAssistant):
    """Static sessions work identically after spot feature is added."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session

    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    await hass.async_block_till_done()

    assert session.cost_method == "static"
    assert session.price_details is None
    assert abs(session.cost_total_kr - 12.50) < 0.01  # 5 × 2.50


async def test_spot_session_initializes_with_empty_price_details(hass: HomeAssistant):
    """Starting a spot session sets cost_method='spot' and price_details=[]."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, title="Test")
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    await setup_session_engine(hass, entry)

    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.cost_method == "spot"
    assert session.price_details == []


async def test_spot_session_running_cost_includes_partial_hour(hass: HomeAssistant):
    """During tracking, cost_total_kr includes the partial hour estimate."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, title="Test")
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session

    # Consume 1.2 kWh (partial hour, no boundary crossed yet)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "11.2")
    await hass.async_block_till_done()

    # Running cost: 1.2 × (0.89 + 0.85) × 1.25 = 2.61 kr
    assert abs(session.cost_total_kr - 2.61) < 0.01
    assert session.price_details == []  # no completed hours yet


async def test_spot_session_single_hour_price_details(hass: HomeAssistant):
    """Single-hour session: price_details has 1 entry with correct values."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, options=_NO_MICRO, title="Test"
    )
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    # Consume 1.2 kWh, no hour boundary crossed
    hass.states.async_set(MOCK_ENERGY_ENTITY, "11.2")
    await hass.async_block_till_done()

    await stop_charging_session(hass)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE

    # Session was persisted — verify via completed event
    assert len(completed_events) == 1
    event = completed_events[0]
    assert event.data["cost_method"] == "spot"
    assert abs(event.data["cost_kr"] - 2.61) < 0.01


async def test_spot_session_multi_hour_price_details(hass: HomeAssistant):
    """Multi-hour session: 3 entries in price_details, total cost matches formula."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, options=_NO_MICRO, title="Test"
    )
    # Spot price for hour 14: 0.89 kr/kWh
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    await setup_session_engine(hass, entry)

    # Session starts at 14:23 — energy_start = 10.0 kWh
    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None

    # --- Hour 14:23 – 15:00 ---
    # By 15:00 boundary: total energy = 11.2 (consumed 1.2 kWh this hour)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "11.2")
    await hass.async_block_till_done()

    # Fire hour boundary at 15:00 UTC
    boundary_15 = datetime(2026, 3, 15, 15, 0, 0, tzinfo=timezone.utc)
    async_fire_time_changed(hass, boundary_15)
    await hass.async_block_till_done()

    # 1 completed hour captured
    assert len(session.price_details) == 1
    h14 = session.price_details[0]
    assert abs(h14["kwh"] - 1.2) < 0.001
    assert h14["spot_price_kr_kwh"] == 0.89
    assert h14["fallback"] is False
    assert abs(h14["cost_kr"] - 2.61) < 0.01

    # --- Hour 15:00 – 16:00 ---
    # Change spot price to 1.23 for hour 15
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "1.23")
    # By 16:00 boundary: total energy = 14.8 (consumed 3.6 kWh in hour 15)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "14.8")
    await hass.async_block_till_done()

    boundary_16 = datetime(2026, 3, 15, 16, 0, 0, tzinfo=timezone.utc)
    async_fire_time_changed(hass, boundary_16)
    await hass.async_block_till_done()

    assert len(session.price_details) == 2
    h15 = session.price_details[1]
    assert abs(h15["kwh"] - 3.6) < 0.001
    assert h15["spot_price_kr_kwh"] == 1.23
    # (1.23 + 0.85) × 1.25 = 2.60 kr/kWh; 3.6 × 2.60 = 9.36 kr
    assert abs(h15["cost_kr"] - 9.36) < 0.01

    # --- Hour 16:00 – 16:45 (partial) ---
    # Change spot price to 0.95 for hour 16
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.95")
    # By session end: total energy = 18.4 (consumed 3.6 kWh in partial hour 16)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "18.4")
    await hass.async_block_till_done()

    await stop_charging_session(hass)

    # Session ends: final partial hour captured
    assert engine.state == SessionEngineState.IDLE
    assert len(session.price_details) == 3
    h16 = session.price_details[2]
    assert abs(h16["kwh"] - 3.6) < 0.001
    assert h16["spot_price_kr_kwh"] == 0.95
    # (0.95 + 0.85) × 1.25 = 2.25 kr/kWh; 3.6 × 2.25 = 8.10 kr
    assert abs(h16["cost_kr"] - 8.10) < 0.01

    # Total: 2.61 + 9.36 + 8.10 = 20.07 kr
    assert abs(session.cost_total_kr - 20.07) < 0.01


async def test_spot_session_completed_event_has_cost_method(hass: HomeAssistant):
    """SESSION_COMPLETED event includes cost_method='spot'."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, options=_NO_MICRO, title="Test"
    )
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "11.2")
    await hass.async_block_till_done()
    await stop_charging_session(hass)

    assert len(completed_events) == 1
    assert completed_events[0].data["cost_method"] == "spot"


async def test_spot_session_price_details_have_required_keys(hass: HomeAssistant):
    """price_details entries contain all required keys."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, options=_NO_MICRO, title="Test"
    )
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "11.2")
    await hass.async_block_till_done()

    # Fire hour boundary to capture a price_details entry
    boundary = datetime(2026, 3, 15, 15, 0, 0, tzinfo=timezone.utc)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert len(session.price_details) == 1

    detail = session.price_details[0]
    required_keys = {
        "hour",
        "kwh",
        "spot_price_kr_kwh",
        "total_price_kr_kwh",
        "cost_kr",
        "fallback",
    }
    assert required_keys == set(detail.keys())


# ---------------------------------------------------------------------------
# T019: Fallback pricing tests (US4)
# ---------------------------------------------------------------------------


async def test_spot_session_fallback_when_sensor_unavailable(hass: HomeAssistant):
    """When spot sensor is unavailable at hour boundary, fallback price is used."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, options=_NO_MICRO, title="Test"
    )
    # Start with valid spot price
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    # Consume 2.0 kWh then sensor goes unavailable before hour boundary
    hass.states.async_set(MOCK_ENERGY_ENTITY, "12.0")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, STATE_UNAVAILABLE)

    # Fire hour boundary — sensor unavailable → fallback used
    boundary = datetime(2026, 3, 15, 15, 0, 0, tzinfo=timezone.utc)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert len(session.price_details) == 1

    h14 = session.price_details[0]
    assert h14["fallback"] is True
    assert h14["spot_price_kr_kwh"] is None
    # fallback_price_kwh = 2.50, 2.0 kWh × 2.50 = 5.00 kr
    assert abs(h14["cost_kr"] - 5.00) < 0.01
    assert abs(h14["total_price_kr_kwh"] - 2.50) < 0.001


async def test_spot_session_fallback_at_session_end_when_sensor_unavailable(hass: HomeAssistant):
    """When spot sensor is unavailable at session end, final partial hour uses fallback."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, options=_NO_MICRO, title="Test"
    )
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    # Consume 1.5 kWh then sensor goes unavailable
    hass.states.async_set(MOCK_ENERGY_ENTITY, "11.5")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, STATE_UNAVAILABLE)

    await stop_charging_session(hass)

    assert len(completed_events) == 1
    event_data = completed_events[0].data
    assert event_data["cost_method"] == "spot"
    # Final partial hour: 1.5 kWh at fallback 2.50 kr/kWh = 3.75 kr
    assert abs(event_data["cost_kr"] - 3.75) < 0.01


async def test_spot_session_fallback_logs_warning(hass: HomeAssistant, caplog):
    """Unavailable spot sensor at hour boundary logs a WARNING."""
    import logging

    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, options=_NO_MICRO, title="Test"
    )
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    # Sensor goes unavailable
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, STATE_UNAVAILABLE)

    with caplog.at_level(logging.WARNING, logger="custom_components.ev_charging_manager"):
        boundary = datetime(2026, 3, 15, 15, 0, 0, tzinfo=timezone.utc)
        async_fire_time_changed(hass, boundary)
        await hass.async_block_till_done()

    assert any("fallback" in record.message.lower() for record in caplog.records)


async def test_spot_quickstart_scenario_4_fallback_with_recovery(hass: HomeAssistant):
    """Quickstart Scenario 4: hour 14 OK, hour 15 fallback, hour 16 OK again.

    Exact sequence: sensor available at 15:00 boundary (captures h14 at real price),
    then unavailable for hour 15, then recovers at 16:00.

    Expected totals from quickstart:
    - hour 14 (0.89 kr/kWh): 1.2 kWh → 2.61 kr
    - hour 15 (fallback 2.50): 3.6 kWh → 9.00 kr
    - hour 16 (0.95 kr/kWh): 2.0 kWh → 4.50 kr
    - total: 16.11 kr
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_SPOT_CHARGER_DATA, options=_NO_MICRO, title="Test"
    )
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.89")
    await setup_session_engine(hass, entry)

    # Session starts in hour 14; energy_start = 10.0 kWh
    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session

    # --- Hour 14:23–15:00: consume 1.2 kWh ---
    hass.states.async_set(MOCK_ENERGY_ENTITY, "11.2")
    await hass.async_block_till_done()

    # Fire 15:00 boundary with sensor still at 0.89 → h14 captured at real price
    boundary_15 = datetime(2026, 3, 15, 15, 0, 0, tzinfo=timezone.utc)
    async_fire_time_changed(hass, boundary_15)
    await hass.async_block_till_done()

    assert len(session.price_details) == 1
    h14 = session.price_details[0]
    assert h14["fallback"] is False
    assert h14["spot_price_kr_kwh"] == 0.89
    assert abs(h14["kwh"] - 1.2) < 0.001
    assert abs(h14["cost_kr"] - 2.61) < 0.01  # 1.2 × (0.89+0.85) × 1.25

    # Sensor goes unavailable after 15:00 boundary
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, STATE_UNAVAILABLE)

    # --- Hour 15:00–16:00: consume 3.6 kWh, sensor unavailable ---
    hass.states.async_set(MOCK_ENERGY_ENTITY, "14.8")
    await hass.async_block_till_done()

    # Fire 16:00 boundary — sensor still unavailable → h15 fallback
    boundary_16 = datetime(2026, 3, 15, 16, 0, 0, tzinfo=timezone.utc)
    async_fire_time_changed(hass, boundary_16)
    await hass.async_block_till_done()

    assert len(session.price_details) == 2
    h15 = session.price_details[1]
    assert h15["fallback"] is True
    assert h15["spot_price_kr_kwh"] is None
    assert abs(h15["kwh"] - 3.6) < 0.001
    assert abs(h15["cost_kr"] - 9.00) < 0.01  # 3.6 × 2.50 fallback

    # Sensor recovers after 16:00 boundary
    hass.states.async_set(MOCK_SPOT_PRICE_ENTITY, "0.95")

    # --- Hour 16:00–16:30: consume 2.0 kWh at 0.95 ---
    hass.states.async_set(MOCK_ENERGY_ENTITY, "16.8")
    await hass.async_block_till_done()

    await stop_charging_session(hass)

    assert len(session.price_details) == 3
    h16 = session.price_details[2]
    assert h16["fallback"] is False
    assert h16["spot_price_kr_kwh"] == 0.95
    assert abs(h16["kwh"] - 2.0) < 0.001
    # (0.95 + 0.85) × 1.25 = 2.25; 2.0 × 2.25 = 4.50
    assert abs(h16["cost_kr"] - 4.50) < 0.01

    # Total exactly matches quickstart Scenario 4: 2.61 + 9.00 + 4.50 = 16.11
    assert abs(session.cost_total_kr - 16.11) < 0.01


# ---------------------------------------------------------------------------
# T024: SessionEngine debug logging — all 6 log() categories called
# ---------------------------------------------------------------------------


async def test_session_engine_debug_logging_all_categories(hass: HomeAssistant, tmp_path):
    """All 6 debug log categories are emitted during a full charge cycle.

    Injects a real DebugLogger and verifies log() is called for:
    CAR_STATE, RFID_READ, SESSION_START, ENGINE_DECISION (x2), SESSION_STOP
    """
    # Use a real DebugLogger backed by tmp_path
    hass.config.config_dir = str(tmp_path)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="My go-e Charger",
    )
    await setup_session_engine(hass, entry)
    await _setup_full_engine(hass, entry.entry_id)

    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert debug_logger is not None
    assert debug_logger.enabled

    # Wrap log() with a spy
    logged_calls: list[tuple[str, str]] = []
    original_log = debug_logger.log

    def spy_log(category: str, message: str) -> None:
        logged_calls.append((category, message))
        original_log(category, message)

    debug_logger.log = spy_log  # type: ignore[method-assign]

    # Simulate plug-in → charge → unplug
    await start_charging_session(hass, trx_value="2")  # trx=2 → card_index=1 → Petra

    # Update energy to exceed micro-session threshold
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.2")
    await hass.async_block_till_done()

    await stop_charging_session(hass)

    categories = [c for c, _ in logged_calls]

    assert "CAR_STATE" in categories, f"CAR_STATE not found in {categories}"
    assert "RFID_READ" in categories, f"RFID_READ not found in {categories}"
    assert "SESSION_START" in categories, f"SESSION_START not found in {categories}"
    assert categories.count("ENGINE_DECISION") >= 2, (
        f"Expected at least 2 ENGINE_DECISION calls, got {categories}"
    )
    assert "SESSION_STOP" in categories, f"SESSION_STOP not found in {categories}"

    # Verify log file actually has content
    content = open(debug_logger.file_path, encoding="utf-8").read()
    assert "CAR_STATE" in content
    assert "SESSION_START" in content
    assert "SESSION_STOP" in content


# ---------------------------------------------------------------------------
# PR-013: Balancing cycle race fix — _awaiting_reset gate
# (Tests added below; PR-011 _prev_car_status tests removed)
# ---------------------------------------------------------------------------


async def _do_complete_real_session(hass, entry_id: str) -> None:
    """Drive the engine through one real persisted session (Charging → Complete → IDLE).

    Patches dt_util in session_engine so the session's start and end times are
    120 seconds apart, passing the micro-session filter (>60s, >0.05 kWh).
    The _awaiting_reset gate is engaged when the session ends.
    """
    from unittest.mock import MagicMock, patch

    session_start = dt_util.utcnow()
    session_end = session_start + timedelta(seconds=120)

    mock_dt = MagicMock()
    mock_dt.utcnow.return_value = session_start

    with patch("custom_components.ev_charging_manager.session_engine.dt_util", mock_dt):
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
        # Start session with trx=2 (card index 1)
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
        await hass.async_block_till_done()

        # Advance mocked time and energy past micro-session filter (>60s, >0.05 kWh)
        mock_dt.utcnow.return_value = session_end
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
        await hass.async_block_till_done()

        # End the real session
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Complete")
        await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# T002: Race condition — trx event arrives before car_status event
# ---------------------------------------------------------------------------


async def test_balancing_cycle_blocked_when_trx_event_arrives_first(
    hass: HomeAssistant,
):
    """T002 (US1 AC-1/AC-2): trx event before car_status Complete→Charging must not start session.

    Reproduces the real go-e event delivery race: after a session ends in Complete,
    the charger fires trx state-change first, then car_status state-change to Charging.
    The _awaiting_reset gate must block the session regardless of event order.
    BALANCING_SKIP must be logged with the exact spec-required message.
    """
    hass.config.config_dir = str(
        (hass.config.config_dir if hasattr(hass.config, "config_dir") else "/tmp")
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="Test",
    )
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert debug_logger is not None

    # Complete one real session so the gate is engaged
    await _do_complete_real_session(hass, entry.entry_id)
    assert engine.state == SessionEngineState.IDLE
    assert engine._awaiting_reset is True

    # Spy on debug log calls
    logged_calls: list[tuple[str, str]] = []
    original_log = debug_logger.log

    def spy_log(category: str, message: str) -> None:
        logged_calls.append((category, message))
        original_log(category, message)

    debug_logger.log = spy_log  # type: ignore[method-assign]

    # Pre-condition: trx must not already be "2" for the race trigger to fire a real
    # state-change event (not just EVENT_STATE_REPORTED, which is ignored by listeners).
    # After _do_complete_real_session trx is "2", so toggle it to "0" first.
    # The gate is still engaged (car_status is "Complete"), so this does not start a session.
    hass.states.async_set(MOCK_TRX_ENTITY, "0")
    await hass.async_block_till_done()
    assert engine._awaiting_reset is True, "Gate must still be engaged after trx reset to 0"

    current_trx = hass.states.get(MOCK_TRX_ENTITY)
    assert current_trx is not None and current_trx.state != "2", (
        "Test setup error — trx must not already be '2' for the race trigger to fire"
    )

    # Simulate the race: set car_status in hass.states so the engine reads "Charging"
    # when the trx event handler calls _handle_idle_state, then fire trx event first.
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")

    # Fire the trx entity state-change event FIRST (simulating go-e race condition).
    # "0" → "2" is a genuine value change so EVENT_STATE_CHANGED fires for the listener.
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    await hass.async_block_till_done()

    # Now fire the car_status state-change event (Complete → Charging)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    # No new session must be created
    assert engine.state == SessionEngineState.IDLE, (
        "Engine must remain IDLE — balancing cycle after session end"
    )
    assert engine.active_session is None

    # BALANCING_SKIP must have been logged with the exact spec-required wording
    skip_messages = [msg for cat, msg in logged_calls if cat == "BALANCING_SKIP"]
    assert skip_messages, "BALANCING_SKIP must be logged when gate blocks session start"
    assert any(
        "session start blocked — Complete → Charging (balancing cycle)" in msg
        for msg in skip_messages
    ), f"Exact BALANCING_SKIP message not found; got: {skip_messages}"


# ---------------------------------------------------------------------------
# T003: 20 consecutive balancing cycles — exactly one session persisted
# ---------------------------------------------------------------------------


async def test_synthetic_20_balancing_cycles_persist_one_session(
    hass: HomeAssistant,
):
    """T003 (US1 AC-3): 20 consecutive balancing cycles after one real session → one session total.

    Simulates the 2026-04-24 production incident (23 spurious sessions for one plug-in event).
    After the real session ends, all Complete→Charging cycles must be blocked.
    The session store must contain exactly one session (the original real one).
    """
    hass.config.config_dir = str(
        (hass.config.config_dir if hasattr(hass.config, "config_dir") else "/tmp")
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="Test",
    )
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert debug_logger is not None

    # Complete one real session
    await _do_complete_real_session(hass, entry.entry_id)
    assert engine.state == SessionEngineState.IDLE
    assert engine._awaiting_reset is True

    # Count BALANCING_SKIP log calls
    skip_count = 0
    original_log = debug_logger.log

    def spy_log(category: str, message: str) -> None:
        nonlocal skip_count
        if category == "BALANCING_SKIP":
            skip_count += 1
        original_log(category, message)

    debug_logger.log = spy_log  # type: ignore[method-assign]

    # Drive 20 consecutive Complete → Charging → Complete balancing cycles
    for _ in range(20):
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Complete")
        await hass.async_block_till_done()

    # Verify exactly one session in the session store (in-memory list)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_sessions = session_store.sessions
    assert len(completed_sessions) == 1, (
        f"Expected 1 session (the real one), got {len(completed_sessions)}"
    )

    # At least 20 BALANCING_SKIP entries must have been logged
    assert skip_count >= 20, f"Expected at least 20 BALANCING_SKIP log entries, got {skip_count}"

    # Engine remains IDLE
    assert engine.state == SessionEngineState.IDLE


# ---------------------------------------------------------------------------
# T004: Micro-session completion also engages the gate (spec FR-007)
# ---------------------------------------------------------------------------


async def test_micro_session_completion_engages_gate(hass: HomeAssistant):
    """T004 (US1 / FR-007): Gate is set even when a micro-session is discarded below thresholds.

    A session shorter than min_duration (60s) and/or below min_energy (0.05 kWh)
    is discarded — but the gate must still engage so balancing cycles that follow
    do not create spurious sessions.
    """
    hass.config.config_dir = str(
        (hass.config.config_dir if hasattr(hass.config, "config_dir") else "/tmp")
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="Test",
    )
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert debug_logger is not None

    # Start a micro-session (very short, very little energy — will be discarded)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING

    # End the micro-session immediately (no time advance, no energy — below all thresholds)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Complete")
    await hass.async_block_till_done()

    # Micro-session must be discarded (nothing persisted) but gate must still be engaged
    assert engine.state == SessionEngineState.IDLE
    assert engine._awaiting_reset is True, "Gate must be engaged even after micro-session discard"

    # Verify no session was persisted (in-memory list)
    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_sessions = session_store.sessions
    assert len(completed_sessions) == 0, "Micro-session must not be persisted"

    # Now simulate a balancing cycle — must be blocked
    logged_calls: list[tuple[str, str]] = []
    original_log = debug_logger.log

    def spy_log(category: str, message: str) -> None:
        logged_calls.append((category, message))
        original_log(category, message)

    debug_logger.log = spy_log  # type: ignore[method-assign]

    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE, "Balancing after micro-session must be blocked"
    skip_messages = [msg for cat, msg in logged_calls if cat == "BALANCING_SKIP"]
    assert skip_messages, "BALANCING_SKIP must be logged after micro-session gate"

    # Confirm still no sessions (in-memory list)
    completed2 = session_store.sessions
    assert len(completed2) == 0, "No session must be persisted from balancing after micro-session"


# ---------------------------------------------------------------------------
# T006: Gate cleared by Idle → new session starts normally (US2 AC-1)
# ---------------------------------------------------------------------------


async def test_idle_transition_clears_gate_and_allows_new_session(hass: HomeAssistant):
    """T006 (US2 AC-1): Complete→Idle clears gate; subsequent Charging starts new session.

    Full flow: real session ends in Complete → Idle → Wait for car → Charging.
    The gate is cleared by the Idle transition and the new session starts normally.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Complete a real session
    await _do_complete_real_session(hass, entry.entry_id)
    assert engine._awaiting_reset is True

    # Car unplugs — gate must clear on Idle
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    await hass.async_block_till_done()

    assert engine._awaiting_reset is False, "Gate must be cleared by Idle transition"

    # Car re-plugs → Wait for car
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Wait for car")
    await hass.async_block_till_done()

    # New legitimate charge starts
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING, "New session must start after gate clears"
    assert engine.active_session is not None

    # Complete the new session — mock time so duration passes micro-session filter
    from unittest.mock import MagicMock, patch

    session2_start = dt_util.utcnow()
    session2_end = session2_start + timedelta(seconds=120)
    mock_dt2 = MagicMock()
    mock_dt2.utcnow.return_value = session2_start

    with patch("custom_components.ev_charging_manager.session_engine.dt_util", mock_dt2):
        mock_dt2.utcnow.return_value = session2_end
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.3")
        await hass.async_block_till_done()

        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Complete")
        await hass.async_block_till_done()

    session_store = hass.data[DOMAIN][entry.entry_id]["session_store"]
    completed_sessions = session_store.sessions
    assert len(completed_sessions) == 2, (
        f"Expected 2 sessions (original + new), got {len(completed_sessions)}"
    )


# ---------------------------------------------------------------------------
# T007: Gate cleared by Wait for car (re-swipe, no unplug) (US2 AC-2)
# ---------------------------------------------------------------------------


async def test_wait_for_car_transition_clears_gate_without_idle(hass: HomeAssistant):
    """T007 (US2 AC-2 / spec FR-003): Complete→Wait for car clears gate; Charging starts session.

    Re-swipe scenario: cable stays connected, RFID re-swiped.
    car_status goes Complete→Wait for car (no Idle step).
    Gate must clear and the new session must start normally.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Complete a real session
    await _do_complete_real_session(hass, entry.entry_id)
    assert engine._awaiting_reset is True

    # RFID re-swipe: Complete → Wait for car (skipping Idle)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Wait for car")
    await hass.async_block_till_done()

    assert engine._awaiting_reset is False, "Gate must be cleared by Wait for car transition"
    assert engine.state == SessionEngineState.IDLE

    # New charge starts
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING, "New session must start after re-swipe"
    assert engine.active_session is not None


# ---------------------------------------------------------------------------
# T008: unknown/unavailable do NOT clear the gate (spec FR-008)
# ---------------------------------------------------------------------------


async def test_unknown_and_unavailable_do_not_clear_gate(hass: HomeAssistant):
    """T008 (US2 / spec FR-008): Transient unknown/unavailable transitions do not clear gate.

    After a real session ends in Complete, car_status goes through unknown and
    unavailable states. The gate must remain engaged. A subsequent Charging event
    (balancing cycle) must be blocked.
    """
    hass.config.config_dir = str(
        (hass.config.config_dir if hasattr(hass.config, "config_dir") else "/tmp")
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="Test",
    )
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert debug_logger is not None

    # Complete a real session (gate engages)
    await _do_complete_real_session(hass, entry.entry_id)
    assert engine._awaiting_reset is True

    # Drive through unknown, unavailable, and back to Complete — gate must stay
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()
    assert engine._awaiting_reset is True, "Gate must not clear on unknown"

    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert engine._awaiting_reset is True, "Gate must not clear on unavailable"

    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Complete")
    await hass.async_block_till_done()
    assert engine._awaiting_reset is True, "Gate must not clear on re-entry to Complete"

    # Now simulate a balancing cycle — must be blocked
    logged_calls: list[tuple[str, str]] = []
    original_log = debug_logger.log

    def spy_log(category: str, message: str) -> None:
        logged_calls.append((category, message))
        original_log(category, message)

    debug_logger.log = spy_log  # type: ignore[method-assign]

    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE, (
        "Balancing cycle after unknown/unavailable must still be blocked"
    )
    skip_messages = [msg for cat, msg in logged_calls if cat == "BALANCING_SKIP"]
    assert skip_messages, "BALANCING_SKIP must be logged — gate was not cleared by transient states"


# ---------------------------------------------------------------------------
# PR-012: Sensor unavailability grace during active sessions
# ---------------------------------------------------------------------------


async def test_session_survives_car_status_unknown(hass: HomeAssistant):
    """T002: Session stays TRACKING when car_status transitions to STATE_UNKNOWN then back.

    A transient unknown state must not end an active charging session.
    Energy accumulation must be continuous across the glitch.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING

    # Sensor goes unknown — session must survive
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING
    assert engine.active_session is not None

    # Sensor recovers to Charging
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "6.5")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING
    assert abs(engine.active_session.energy_kwh - 1.5) < 0.001


async def test_session_survives_car_status_unavailable(hass: HomeAssistant):
    """T003: Session stays TRACKING when car_status transitions to STATE_UNAVAILABLE then back.

    Mirrors T002 but for STATE_UNAVAILABLE (hardware disconnect / HA entity removed).
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "3.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING

    # Sensor goes unavailable — session must survive
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING
    assert engine.active_session is not None

    # Sensor recovers
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "4.2")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING
    assert abs(engine.active_session.energy_kwh - 1.2) < 0.001


async def test_session_ends_normally_on_complete_no_glitch(hass: HomeAssistant):
    """T004: Session ends normally when car_status goes directly to Complete (no regression).

    Validates that the grace mechanism does not interfere with the normal end path.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING

    # Advance time and energy to pass micro-session filter
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=120))
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
    await hass.async_block_till_done()

    # Normal end: car goes to Complete
    await stop_charging_session(hass)

    assert engine.state == SessionEngineState.IDLE


async def test_session_ends_after_unknown_then_complete(hass: HomeAssistant):
    """T005: Session ends when car_status goes STATE_UNKNOWN then "Complete".

    Grace activates on unknown, then session terminates on the valid Complete state.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING

    # Advance time and energy to pass micro-session filter
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=120))
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
    await hass.async_block_till_done()

    # Transient glitch — session must survive
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING

    # Valid Complete state — session must end
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Complete")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE


async def test_session_ends_after_unknown_then_idle(hass: HomeAssistant):
    """T006: Session ends when car_status goes STATE_UNKNOWN then "Idle".

    Grace activates on unknown, then session terminates on the valid Idle state
    (non-Complete valid end — e.g. cable disconnected without reaching Complete).
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING

    # Advance time and energy to pass micro-session filter
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=120))
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
    await hass.async_block_till_done()

    # Transient glitch — session must survive
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING

    # Valid Idle state — session must end
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE


async def test_energy_accumulates_through_car_status_glitch(hass: HomeAssistant):
    """T008: Energy continues accumulating correctly through a sensor glitch.

    The last valid energy value is preserved during the glitch, and new
    readings after recovery are correctly attributed to the session.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    session = engine.active_session
    assert session is not None
    assert session.energy_start_kwh == 10.0

    # Energy increases before glitch
    hass.states.async_set(MOCK_ENERGY_ENTITY, "11.5")
    await hass.async_block_till_done()
    assert abs(session.energy_kwh - 1.5) < 0.001

    # Sensor glitch — car_status becomes unknown, energy entity still has last value
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()

    # Session alive, energy preserved at last known value
    assert engine.state == SessionEngineState.TRACKING
    assert abs(session.energy_kwh - 1.5) < 0.001

    # Recovery: car_status and energy both come back
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "13.0")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING
    # energy_kwh = 13.0 - 10.0 = 3.0
    assert abs(session.energy_kwh - 3.0) < 0.001


async def test_data_gap_set_after_car_status_glitch(hass: HomeAssistant):
    """T010: data_gap is True after a car_status sensor glitch during TRACKING."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Verify no data gap before glitch
    assert engine._data_gap is False

    # Trigger glitch
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()

    # data_gap must be flagged
    assert engine._data_gap is True


async def test_data_gap_false_when_no_glitch(hass: HomeAssistant):
    """T011: data_gap stays False for a clean session with no sensor glitches."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Normal energy updates — no glitches
    hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
    await hass.async_block_till_done()

    hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
    await hass.async_block_till_done()

    # No glitch → data_gap must remain False
    assert engine._data_gap is False


async def test_data_gap_set_on_first_glitch_stays_true(hass: HomeAssistant):
    """T012: data_gap is set on the first glitch and stays True through multiple glitches."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    assert engine._data_gap is False

    # First glitch — must set data_gap
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()
    assert engine._data_gap is True

    # Recovery
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    # Second glitch — data_gap stays True (not reset between glitches)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert engine._data_gap is True

    # Third glitch
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()
    assert engine._data_gap is True


async def test_debug_log_car_state_unavail_when_logger_enabled(hass: HomeAssistant, tmp_path):
    """T015: Debug log records CAR_STATE_UNAVAIL when logger is enabled and glitch occurs."""
    hass.config.config_dir = str(tmp_path)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="Test",
    )
    await setup_session_engine(hass, entry)

    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert debug_logger is not None

    # Spy on log calls
    logged_calls: list[tuple[str, str]] = []
    original_log = debug_logger.log

    def spy_log(category: str, message: str) -> None:
        logged_calls.append((category, message))
        original_log(category, message)

    debug_logger.log = spy_log  # type: ignore[method-assign]

    # Start a session then trigger a glitch
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()

    categories = [c for c, _ in logged_calls]
    assert "CAR_STATE_UNAVAIL" in categories, f"CAR_STATE_UNAVAIL not found in {categories}"

    messages = [msg for cat, msg in logged_calls if cat == "CAR_STATE_UNAVAIL"]
    assert any("keeping session alive" in msg for msg in messages)


async def test_no_debug_log_when_logger_is_none(hass: HomeAssistant):
    """T016: No crash and no debug log entry when debug logger is disabled (None)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Verify logger is disabled
    assert engine._debug_logger is None

    # Start a session and trigger a glitch — must not raise
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()

    # Session survived the glitch, no exception raised
    assert engine.state == SessionEngineState.TRACKING


async def test_simultaneous_car_status_and_energy_unavailable(hass: HomeAssistant):
    """T017: Session survives when both car_status and energy go unavailable simultaneously.

    Double-glitch scenario: both sensors report unknown/unavailable at the same time.
    The session must stay TRACKING, energy stays at last known value, and data_gap is True.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Start a session with a known energy reading
    hass.states.async_set(MOCK_ENERGY_ENTITY, "1.5")
    await start_charging_session(hass, trx_value="0")

    assert engine.state == SessionEngineState.TRACKING
    assert engine._data_gap is False

    last_energy = engine._last_energy_kwh

    # Both sensors go unavailable simultaneously
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    # Session must survive
    assert engine.state == SessionEngineState.TRACKING
    assert engine.active_session is not None

    # Energy stays at last known value
    assert engine._last_energy_kwh == last_energy

    # Data gap must be flagged
    assert engine._data_gap is True


async def test_unknown_car_status_in_idle_does_not_start_session(hass: HomeAssistant):
    """T018: STATE_UNKNOWN car_status while IDLE with a valid trx does not start a session.

    The engine must stay IDLE — an unknown/unavailable sensor reading is not a charging event.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Set a valid trx (session would start if car_status were "Charging")
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE

    # car_status goes unknown — must NOT start a session
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE
    assert engine.active_session is None


async def test_debug_log_car_state_transition_to_invalid(hass: HomeAssistant, tmp_path):
    """T019: Debug log records CAR_STATE transition when car_status goes to STATE_UNKNOWN.

    With debug logger enabled: after a session starts (car_status = "Charging"), setting
    car_status to STATE_UNKNOWN must produce a CAR_STATE log entry showing the transition.
    """
    hass.config.config_dir = str(tmp_path)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"debug_logging": True},
        title="Test",
    )
    await setup_session_engine(hass, entry)

    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert debug_logger is not None

    logged_calls: list[tuple[str, str]] = []
    original_log = debug_logger.log

    def spy_log(category: str, message: str) -> None:
        logged_calls.append((category, message))
        original_log(category, message)

    debug_logger.log = spy_log  # type: ignore[method-assign]

    # Start a session so car_status is "Charging"
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="0")

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING

    # Trigger transition to invalid state
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    await hass.async_block_till_done()

    # CAR_STATE log must record the transition from "Charging" to STATE_UNKNOWN
    car_state_msgs = [msg for cat, msg in logged_calls if cat == "CAR_STATE"]
    assert any("Charging" in msg for msg in car_state_msgs), (
        f"Expected 'Charging' in CAR_STATE messages, got: {car_state_msgs}"
    )


# ---------------------------------------------------------------------------
# PR-013: Recovery path tests (T009–T012, US3)
# ---------------------------------------------------------------------------


def _make_recovery_snapshot(
    session_id: str = "recovery-snap-001",
    rfid_index: int | None = 1,
    energy_start_kwh: float = 10.0,
    energy_kwh: float = 3.0,
    started_at: str = "2026-02-22T08:00:00+00:00",
) -> dict:
    """Return a minimal active-session snapshot dict for recovery tests."""
    return {
        "id": session_id,
        "user_name": "Petra",
        "user_type": "regular",
        "vehicle_name": None,
        "vehicle_battery_kwh": None,
        "efficiency_factor": None,
        "rfid_index": rfid_index,
        "rfid_uid": None,
        "started_at": started_at,
        "ended_at": None,
        "duration_seconds": 0,
        "energy_kwh": energy_kwh,
        "energy_start_kwh": energy_start_kwh,
        "avg_power_w": 0.0,
        "max_power_w": 0.0,
        "phases_used": None,
        "max_current_a": None,
        "cost_total_kr": 0.0,
        "cost_method": "static",
        "price_details": None,
        "charge_price_total_kr": None,
        "charge_price_method": None,
        "estimated_soc_added_pct": None,
        "charger_name": "My go-e Charger",
        "charger_total_before_kwh": None,
        "charger_total_after_kwh": None,
        "data_gap": False,
        "reconstructed": False,
    }


def _store_load_side_effect_for_recovery(snapshot: dict):
    """Return an async_load side_effect that injects snapshot into SessionStore slot."""
    call_count = 0

    async def side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # ConfigStore
        if call_count == 2:
            return [snapshot]  # SessionStore — active snapshot
        return None  # StatsStore

    return side_effect


async def test_recovery_resume_branch_leaves_gate_disengaged(hass: HomeAssistant):
    """T009 (US3 AC-1): Resume path — engine resumes TRACKING, gate stays False.

    When HA restarts with an active session and the charger is still charging with
    the same card, the engine resumes in TRACKING. The _awaiting_reset gate must
    remain False so the resumed session continues normally.
    """
    from unittest.mock import AsyncMock, patch

    snapshot = _make_recovery_snapshot(rfid_index=1, energy_start_kwh=10.0, energy_kwh=3.0)

    # charger is still charging with same card (trx=2 → rfid_index=1)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "13.5")

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_store_load_side_effect_for_recovery(snapshot),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Resume path: engine is TRACKING, gate disengaged
    assert engine.state == SessionEngineState.TRACKING, "Resume path must restore TRACKING state"
    assert engine.active_session is not None
    assert engine._awaiting_reset is False, "Gate must stay False on resume path"

    # Session can continue and complete normally
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Complete")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE


async def test_recovery_complete_snapshot_with_complete_state_engages_gate(hass: HomeAssistant):
    """T010 (US3 AC-3): Complete-snapshot + car_status=Complete at recovery → gate engages.

    When HA restarts, completes the old snapshot, and the charger is already in
    Complete state, the gate must engage (balancing cycles begun during downtime
    must not create new sessions).
    """
    from unittest.mock import AsyncMock, patch

    snapshot = _make_recovery_snapshot(rfid_index=1, energy_start_kwh=5.0, energy_kwh=4.0)

    # Charger is in Complete at recovery time (charging ended during downtime)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Complete")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "9.5")

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_store_load_side_effect_for_recovery(snapshot),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Complete-snapshot branch ran: engine is IDLE, old session persisted
    assert engine.state == SessionEngineState.IDLE
    assert engine._awaiting_reset is True, (
        "Gate must engage when recovery completes snapshot and car_status is Complete"
    )

    # Balancing cycle during downtime must be blocked
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE, (
        "Balancing cycle after recovery + Complete state must be blocked"
    )
    assert engine.active_session is None


async def test_recovery_complete_snapshot_with_unknown_car_status_engages_gate_defensively(
    hass: HomeAssistant,
):
    """T010b (spec FR-010): Complete-snapshot + car_status unknown at recovery → gate engages.

    When the charger integration is still initializing at HA restart, car_status may
    be unavailable/unknown. Treating None as equivalent to "Complete" prevents spurious
    sessions if a balancing cycle starts before the sensor loads.
    """
    from unittest.mock import AsyncMock, patch

    snapshot = _make_recovery_snapshot(rfid_index=1, energy_start_kwh=5.0, energy_kwh=4.0)

    # Charger sensor is not yet loaded at recovery time → STATE_UNKNOWN
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, STATE_UNKNOWN)
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "9.5")

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_store_load_side_effect_for_recovery(snapshot),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    assert engine.state == SessionEngineState.IDLE
    assert engine._awaiting_reset is True, (
        "Gate must engage defensively when car_status is unknown at recovery time"
    )

    # Balancing cycle must be blocked until car transitions to Idle or Wait for car
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.IDLE, (
        "Session start must be blocked when gate engaged via unknown car_status at recovery"
    )
    assert engine.active_session is None

    # Gate clears normally when sensor reports Idle
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    await hass.async_block_till_done()

    assert engine._awaiting_reset is False, "Gate must clear on Idle after defensive engagement"


@pytest.mark.parametrize(
    ("car_status_at_recovery", "trx_at_recovery", "expect_new_session"),
    [
        ("Idle", "null", False),
        ("Wait for car", "null", False),
        # Charging with a different card than the snapshot's rfid_index=1 (trx=2).
        # Recovery completes the old snapshot, then the state machine starts a new
        # session for the new card (rfid_index=2 / trx=3, resolves to unknown user).
        ("Charging", "3", True),
    ],
)
async def test_recovery_complete_snapshot_with_non_complete_state_leaves_gate_disengaged(
    hass: HomeAssistant,
    car_status_at_recovery: str,
    trx_at_recovery: str,
    expect_new_session: bool,
):
    """T011 (US3 AC-2): Complete-snapshot + car_status != Complete → gate stays False.

    When the old snapshot is completed but the charger is now in Idle, Wait for car,
    or Charging with a different card, the gate must be disengaged so a legitimate
    new session can start.
    """
    from unittest.mock import AsyncMock, patch

    # Snapshot used rfid_index=1 (trx=2). The "Charging" case uses trx=3 (rfid_index=2)
    # to ensure a different card triggers a new session start.
    snapshot = _make_recovery_snapshot(rfid_index=1, energy_start_kwh=5.0, energy_kwh=4.0)

    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, car_status_at_recovery)
    hass.states.async_set(MOCK_TRX_ENTITY, trx_at_recovery)
    hass.states.async_set(MOCK_ENERGY_ENTITY, "9.0")

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=_store_load_side_effect_for_recovery(snapshot),
        ),
        patch(
            "homeassistant.helpers.storage.Store.async_save",
            new_callable=AsyncMock,
        ),
    ):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    assert engine._awaiting_reset is False, (
        f"Gate must stay False for car_status={car_status_at_recovery!r} at recovery"
    )

    if expect_new_session:
        # "Charging" + different card: recovery completes old snapshot and immediately
        # starts a new session for the new card via _handle_idle_state().
        assert engine.state == SessionEngineState.TRACKING, (
            "New session must start when recovery finds Charging with a different card"
        )
        assert engine.active_session is not None
        # The new session uses the current trx (rfid_index=2, unknown user — no mapping)
        assert engine.active_session.rfid_index is None or engine.active_session.rfid_index != 1, (
            "New session must not reuse the old snapshot's rfid_index"
        )
    else:
        assert engine.state == SessionEngineState.IDLE
        # A subsequent legitimate Charging must start a new session
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
        await hass.async_block_till_done()

        assert engine.state == SessionEngineState.TRACKING, (
            f"New session must start after recovery with car_status={car_status_at_recovery!r}"
        )
        assert engine.active_session is not None


async def test_recovery_cold_start_leaves_gate_disengaged(hass: HomeAssistant):
    """T012 (US3 AC-4): Cold start (no snapshot) — gate defaults to False.

    When the engine initializes with no session snapshot, _awaiting_reset must be False.
    The first observed Charging event must start a session normally (spec FR-004).
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test")
    await setup_session_engine(hass, entry)

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Cold start: gate must default to False
    assert engine._awaiting_reset is False, "_awaiting_reset must default to False on cold start"

    # First Charging event must start a session
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING, (
        "Cold start must allow first Charging event to start a session"
    )
    assert engine.active_session is not None
