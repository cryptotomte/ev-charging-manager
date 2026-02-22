"""Tests for data gap handling during network outages (US2, FR-007, FR-008, FR-009)."""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ev_charging_manager.const import (
    DOMAIN,
    EVENT_SESSION_COMPLETED,
    SessionEngineState,
)
from tests.conftest import (
    MOCK_CAR_STATUS_ENTITY,
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)

# Options that disable micro-session filtering so test sessions are not discarded
NO_FILTER_OPTIONS = {"min_session_duration_s": 0, "min_session_energy_wh": 0}


# ---------------------------------------------------------------------------
# FR-007: Retain last known values when sensor becomes unavailable
# ---------------------------------------------------------------------------


async def test_energy_unavailable_keeps_last_value(hass: HomeAssistant) -> None:
    """FR-007: When energy sensor becomes unavailable, last known value is kept."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="go-e")
    await setup_session_engine(hass, entry)

    # Start charging
    await start_charging_session(hass, trx_value="2")
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.TRACKING

    # Set energy to a known value
    hass.states.async_set(MOCK_ENERGY_ENTITY, "3.0")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()
    # energy_kwh should be 3.0 - 0.0 (start) = 3.0 approximately
    assert engine._last_energy_kwh == 3.0

    # Now energy sensor goes unavailable
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    # Last known value must be retained
    assert engine._last_energy_kwh == 3.0, "Should retain last known energy when unavailable"
    assert engine.state == SessionEngineState.TRACKING, "Session should still be tracking"


# ---------------------------------------------------------------------------
# FR-008: Flag sessions with data_gap=True
# ---------------------------------------------------------------------------


async def test_data_gap_flagged_when_sensor_unavailable(hass: HomeAssistant) -> None:
    """FR-008: Sessions with sensor unavailability are flagged with data_gap=True."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    # Start charging
    await start_charging_session(hass, trx_value="2")
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Set valid energy
    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.0")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    # Sensor goes unavailable — data_gap flag should be set
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine._data_gap is True, "data_gap flag must be set when energy sensor unavailable"

    # Complete the session
    await stop_charging_session(hass)

    assert len(events) == 1
    event_data = events[0].data
    assert event_data["data_gap"] is True, "Completed event must carry data_gap=True"


async def test_data_gap_not_flagged_for_clean_session(hass: HomeAssistant) -> None:
    """FR-008: Sessions without sensor issues should NOT be flagged with data_gap."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    # Start charging
    hass.states.async_set(MOCK_ENERGY_ENTITY, "10.0")
    await start_charging_session(hass, trx_value="2")

    # Sensor always available
    hass.states.async_set(MOCK_ENERGY_ENTITY, "12.0")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    await stop_charging_session(hass)

    assert len(events) == 1
    event_data = events[0].data
    assert event_data["data_gap"] is False, "Clean session must have data_gap=False"


# ---------------------------------------------------------------------------
# FR-008: Multiple brief outages — flag set once, not accumulated
# ---------------------------------------------------------------------------


async def test_multiple_outages_data_gap_set_once(hass: HomeAssistant) -> None:
    """FR-008: Multiple outages still produce data_gap=True (idempotent, not counter)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CHARGER_DATA, options=NO_FILTER_OPTIONS, title="go-e"
    )
    await setup_session_engine(hass, entry)
    events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="2")
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # First outage
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()
    assert engine._data_gap is True

    # Sensor recovers
    hass.states.async_set(MOCK_ENERGY_ENTITY, "2.0")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()
    assert engine._data_gap is True, "data_gap should stay True after recovery"

    # Second outage
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()
    assert engine._data_gap is True

    # Complete session
    await stop_charging_session(hass)
    assert len(events) == 1
    assert events[0].data["data_gap"] is True


# ---------------------------------------------------------------------------
# data_gap reset after session completion
# ---------------------------------------------------------------------------


async def test_data_gap_reset_after_session(hass: HomeAssistant) -> None:
    """data_gap is reset to False when a new session starts after a data-gap session."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="go-e")
    await setup_session_engine(hass, entry)

    # First session with data gap
    await start_charging_session(hass, trx_value="2")
    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()
    assert engine._data_gap is True

    # End session
    await stop_charging_session(hass)
    assert engine.state == SessionEngineState.IDLE
    assert engine._data_gap is False, "data_gap must reset to False after session ends"

    # Start new session — should start with data_gap=False
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    await start_charging_session(hass, trx_value="2")
    assert engine._data_gap is False, "New session must start with data_gap=False"
