"""Tests for data gap handling during network outages (US2, FR-007, FR-008, FR-009).

PR-27 (023-recovery-hardening) adds a v2-engine section at the bottom:
mid-session meter-reset detection on the plug-anchored engine (FR-015).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ev_charging_manager.const import (
    CONF_CHARGING_IDLE_TIMEOUT_MIN,
    CONF_DISCONNECT_GRACE_MIN,
    DEBUG_CAT_DATA_GAP,
    DOMAIN,
    EVENT_SESSION_COMPLETED,
    SessionEngineState,
)
from tests.conftest import (
    MOCK_CAR_STATUS_ENTITY,
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    MOCK_TRX_ENTITY,
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


# ===========================================================================
# PR-27 (023-recovery-hardening) US4: mid-session meter reset on the
# plug-anchored engine (FR-015)
# ===========================================================================

MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"


class _CaptureLogger:
    """Minimal DebugLogger stand-in recording (category, message) pairs."""

    enabled = True

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def log(self, category: str, message: str) -> None:
        self.entries.append((category, message))

    @property
    def categories(self) -> list[str]:
        return [c for c, _ in self.entries]


async def _make_v2_session(hass: HomeAssistant, start_energy: str = "10.0"):
    """Set up the plug-anchored engine and start a session at `start_energy`.

    Returns (entry, engine).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_profile": "goe_gemini"},
        options={
            "plug_entity": MOCK_PLUG_ENTITY,
            "cable_lock_entity": MOCK_CABLE_LOCK_ENTITY,
            CONF_CHARGING_IDLE_TIMEOUT_MIN: 5,
            CONF_DISCONNECT_GRACE_MIN: 10,
        },
        title="Test go-e Charger (meter reset)",
    )
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, start_energy)
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Plug in + blip → session starts with energy_start = start_energy.
        hass.states.async_set(MOCK_TRX_ENTITY, "2")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_PLUG_ENTITY, "on")
        hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
        await hass.async_block_till_done()
        hass.states.async_set(MOCK_POWER_ENTITY, "7200.0")
        await hass.async_block_till_done()

    engine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.active_session is not None
    return entry, engine


async def test_v2_mid_session_counter_reset_preserves_energy(hass: HomeAssistant) -> None:
    """FR-015: energy 10.0 → 12.5 → 0.2 → 1.0 mid-session → 2.5 kWh preserved
    at the drop, 3.3 kWh after the next reading; data_gap flagged; DATA_GAP
    logged. Previously the session froze at the pre-reset total."""
    _entry, engine = await _make_v2_session(hass, start_energy="10.0")
    capture = _CaptureLogger()
    engine._debug_logger = capture
    session = engine.active_session
    assert session.energy_start_kwh == pytest.approx(10.0)

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        hass.states.async_set(MOCK_ENERGY_ENTITY, "12.5")
        await hass.async_block_till_done()
        assert session.energy_kwh == pytest.approx(2.5, abs=0.001)
        assert session.data_gap is False

        # Charger reboots — counter restarts near zero.
        hass.states.async_set(MOCK_ENERGY_ENTITY, "0.2")
        await hass.async_block_till_done()
        assert session.energy_kwh == pytest.approx(2.5, abs=0.001), (
            "FR-015: accumulated energy must be preserved across the counter reset"
        )
        assert session.data_gap is True, "FR-015: the reset must flag a data gap"
        assert DEBUG_CAT_DATA_GAP in capture.categories, (
            f"FR-015: the reset must log a DATA_GAP entry; saw {capture.categories}"
        )

        # Subsequent deltas accumulate on the rebased reference.
        hass.states.async_set(MOCK_ENERGY_ENTITY, "1.0")
        await hass.async_block_till_done()
        assert session.energy_kwh == pytest.approx(3.3, abs=0.001), (
            "FR-015: post-reset deltas must accumulate on top of preserved energy"
        )


async def test_v2_energy_jitter_below_start_no_rebase(hass: HomeAssistant) -> None:
    """FR-015: a reading epsilon-below the start value is jitter — no rebase,
    no data-gap flag."""
    _entry, engine = await _make_v2_session(hass, start_energy="10.0")
    capture = _CaptureLogger()
    engine._debug_logger = capture
    session = engine.active_session

    with patch("homeassistant.helpers.storage.Store.async_save", new_callable=AsyncMock):
        # 5 Wh below start — within ENERGY_RESET_EPSILON_KWH (10 Wh).
        hass.states.async_set(MOCK_ENERGY_ENTITY, "9.995")
        await hass.async_block_till_done()

    assert session.energy_start_kwh == pytest.approx(10.0), (
        "jitter must not rebase energy_start_kwh"
    )
    assert session.data_gap is False, "jitter must not flag a data gap"
    assert DEBUG_CAT_DATA_GAP not in capture.categories
