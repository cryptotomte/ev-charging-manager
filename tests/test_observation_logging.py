"""Tests for PR-20 observation logging: PLUG_STATE, CABLE_LOCK, MODEL_STATUS,
ERR_STATE, TRX_STATE categories, snapshot suffix, and no-regression checks.

Test naming follows the plan: T-OBS-01 through T-OBS-16.
"""

from __future__ import annotations

import logging
import pathlib
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_CABLE_LOCK_ENTITY,
    CONF_ERROR_ENTITY,
    CONF_MODEL_STATUS_ENTITY,
    CONF_PLUG_ENTITY,
    DOMAIN,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_STARTED,
    SessionEngineState,
)
from custom_components.ev_charging_manager.session_engine import SessionEngine

from .conftest import (
    MOCK_CAR_STATUS_ENTITY,
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    MOCK_POWER_ENTITY,
    MOCK_TRX_ENTITY,
)

# ---------------------------------------------------------------------------
# Constants for observation entities (matching MOCK_CHARGER_DATA serial abc123)
# ---------------------------------------------------------------------------
MOCK_PLUG_ENTITY = "binary_sensor.goe_abc123_car_0"
MOCK_CABLE_LOCK_ENTITY = "sensor.goe_abc123_cus_value"
MOCK_MODEL_STATUS_ENTITY = "sensor.goe_abc123_modelstatus_value"
MOCK_ERROR_ENTITY = "sensor.goe_abc123_err_value"


def _make_observation_entry(
    plug: str | None = MOCK_PLUG_ENTITY,
    cable_lock: str | None = MOCK_CABLE_LOCK_ENTITY,
    model_status: str | None = MOCK_MODEL_STATUS_ENTITY,
    error: str | None = MOCK_ERROR_ENTITY,
    debug_logging: bool = True,
) -> MockConfigEntry:
    """Return a MockConfigEntry with observation slots populated in options."""
    options: dict = {"debug_logging": debug_logging}
    if plug is not None:
        options[CONF_PLUG_ENTITY] = plug
    if cable_lock is not None:
        options[CONF_CABLE_LOCK_ENTITY] = cable_lock
    if model_status is not None:
        options[CONF_MODEL_STATUS_ENTITY] = model_status
    if error is not None:
        options[CONF_ERROR_ENTITY] = error

    return MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options=options,
        title="My go-e Charger",
    )


async def _setup_observation_engine(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> tuple[SessionEngine, MagicMock]:
    """Set up the integration, return (engine, mock_log).

    Patches DebugLogger.log so tests can assert call args without file I/O.
    """
    # Pre-set charger entity states
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.000")
    hass.states.async_set(MOCK_POWER_ENTITY, "0")

    # Pre-set observation entities so they exist in HA states
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_MODEL_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_ERROR_ENTITY, "-none-")

    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    engine: SessionEngine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Mock the debug logger's log method
    mock_log = MagicMock()
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    debug_logger.log = mock_log

    # Also patch on the engine's reference to the same logger
    if engine._debug_logger is not None:
        engine._debug_logger.log = mock_log

    return engine, mock_log


# ===========================================================================
# T-OBS-01: Plug-state transition off → on
# ===========================================================================


async def test_obs_01_plug_state_off_to_on(hass: HomeAssistant) -> None:
    """T-OBS-01: plug off → on emits PLUG_STATE category with snapshot suffix."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Reset cache so the transition registers as new
    engine._last_plug = "off"

    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()

    # Assert PLUG_STATE was logged
    plug_calls = [call for call in mock_log.call_args_list if call.args[0] == "PLUG_STATE"]
    assert plug_calls, "Expected PLUG_STATE log call"
    category, message = plug_calls[-1].args
    assert category == "PLUG_STATE"
    assert "plug changed: off → on" in message
    assert "| wh=" in message
    assert "power=" in message


# ===========================================================================
# T-OBS-02: Cable-lock transition Locked → Unlocked
# ===========================================================================


async def test_obs_02_cable_lock_transition(hass: HomeAssistant) -> None:
    """T-OBS-02: cable-lock Locked → Unlocked emits CABLE_LOCK with verbatim values."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # First transition: Unlocked → Locked (the mock sets initial state to Unlocked in setup)
    mock_log.reset_mock()
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()

    # Now transition Locked → Unlocked (the transition we care about)
    mock_log.reset_mock()
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    await hass.async_block_till_done()

    cable_calls = [call for call in mock_log.call_args_list if call.args[0] == "CABLE_LOCK"]
    assert cable_calls, "Expected CABLE_LOCK log call"
    category, message = cable_calls[-1].args
    assert category == "CABLE_LOCK"
    assert "cus changed: Locked → Unlocked" in message
    assert "| wh=" in message


# ===========================================================================
# T-OBS-03: Model-status with multi-word value
# ===========================================================================


async def test_obs_03_model_status_verbatim(hass: HomeAssistant) -> None:
    """T-OBS-03: model-status emits MODEL_STATUS preserving multi-word values verbatim."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    engine._last_model_status = "Charging"

    hass.states.async_set(MOCK_MODEL_STATUS_ENTITY, "Charging complete")
    await hass.async_block_till_done()

    status_calls = [call for call in mock_log.call_args_list if call.args[0] == "MODEL_STATUS"]
    assert status_calls, "Expected MODEL_STATUS log call"
    category, message = status_calls[-1].args
    assert category == "MODEL_STATUS"
    assert "modelstatus changed: Charging → Charging complete" in message
    assert "| wh=" in message


# ===========================================================================
# T-OBS-04 / T-OBS-07 moved to US4 tests (T034/T035) — see below
# ===========================================================================


# ===========================================================================
# T-OBS-05: Error entity real transition
# ===========================================================================


async def test_obs_05_err_state_real_transition(hass: HomeAssistant) -> None:
    """T-OBS-05: err -none- → cable_overheat emits ERR_STATE."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    engine._last_err = "-none-"

    hass.states.async_set(MOCK_ERROR_ENTITY, "cable_overheat")
    await hass.async_block_till_done()

    err_calls = [call for call in mock_log.call_args_list if call.args[0] == "ERR_STATE"]
    assert err_calls, "Expected ERR_STATE log call"
    category, message = err_calls[-1].args
    assert category == "ERR_STATE"
    assert "err changed: -none- → cable_overheat" in message
    assert "| wh=" in message


# ===========================================================================
# T-OBS-06: TRX transition via RFID entity
# ===========================================================================


async def test_obs_06_trx_state_transition(hass: HomeAssistant) -> None:
    """T-OBS-06: trx entity 0 → 2 emits TRX_STATE (uses existing CONF_RFID_ENTITY).

    PR-28 (FR-004): trx values are RFID tag values — the TRX_STATE message
    carries only the masked form (length <= 2 masks fully).
    """
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Set cache to "0" so the transition to "2" is new
    engine._last_trx = "0"

    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    await hass.async_block_till_done()

    trx_calls = [call for call in mock_log.call_args_list if call.args[0] == "TRX_STATE"]
    assert trx_calls, "Expected TRX_STATE log call"
    category, message = trx_calls[-1].args
    assert category == "TRX_STATE"
    assert "trx changed: *** → ***" in message
    assert "| wh=" in message


async def test_obs_06b_trx_state_long_tag_redacted(hass: HomeAssistant) -> None:
    """PR-28 (FR-004): a long tag value appears as ***{last2} in TRX_STATE,
    never in full; the cached 'before' value is also masked on display."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    engine._last_trx = "0"
    hass.states.async_set(MOCK_TRX_ENTITY, "abc123f4")
    await hass.async_block_till_done()

    trx_calls = [call for call in mock_log.call_args_list if call.args[0] == "TRX_STATE"]
    assert trx_calls, "Expected TRX_STATE log call"
    message = trx_calls[-1].args[1]
    assert "trx changed: *** → ***f4" in message
    assert "abc123f4" not in message

    # The raw value is still cached for transition detection (not the mask)
    assert engine._last_trx == "abc123f4"


# ===========================================================================
# T-OBS-09 / T-OBS-10 / T-OBS-11: _format_signal_snapshot()
# ===========================================================================


async def test_obs_09_snapshot_live_values(hass: HomeAssistant) -> None:
    """T-OBS-09: _format_signal_snapshot returns live values when both entities available."""
    entry = _make_observation_entry()
    engine, _ = await _setup_observation_engine(hass, entry)

    # Set live entity states
    hass.states.async_set(MOCK_ENERGY_ENTITY, "5.123")
    hass.states.async_set(MOCK_POWER_ENTITY, "3520")
    await hass.async_block_till_done()

    result = engine._format_signal_snapshot()
    assert result == " | wh=5.123 power=3520"


async def test_obs_10_snapshot_cached_fallback(hass: HomeAssistant) -> None:
    """T-OBS-10: _format_signal_snapshot falls back to cached values when live unavailable."""
    entry = _make_observation_entry()
    engine, _ = await _setup_observation_engine(hass, entry)

    from homeassistant.const import STATE_UNAVAILABLE

    # Make live entities unavailable
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_POWER_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    # Set non-zero cached values
    engine._last_energy_kwh = 2.450
    engine._last_power_w = 1760.0

    result = engine._format_signal_snapshot()
    assert result == " | wh=2.450 power=1760"


async def test_obs_11_snapshot_unknown_placeholder(hass: HomeAssistant) -> None:
    """T-OBS-11: _format_signal_snapshot returns wh=? power=? when cache is None (never set)."""
    entry = _make_observation_entry()
    engine, _ = await _setup_observation_engine(hass, entry)

    from homeassistant.const import STATE_UNAVAILABLE

    # Make live entities unavailable
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_POWER_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    # Ensure cached values are None (never set — not the same as zero)
    engine._last_energy_kwh = None
    engine._last_power_w = None

    result = engine._format_signal_snapshot()
    assert result == " | wh=? power=?"


async def test_obs_11b_snapshot_zero_renders_as_zero(hass: HomeAssistant) -> None:
    """T-OBS-11b: _format_signal_snapshot renders cached 0.0 as wh=0.000, not '?'."""
    entry = _make_observation_entry()
    engine, _ = await _setup_observation_engine(hass, entry)

    from homeassistant.const import STATE_UNAVAILABLE

    # Make live entities unavailable
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_POWER_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    # 0.0 is a legitimate reading (e.g. session just started)
    engine._last_energy_kwh = 0.0
    engine._last_power_w = 0.0

    result = engine._format_signal_snapshot()
    assert result == " | wh=0.000 power=0"


# ===========================================================================
# T-OBS-14: Existing CAR_STATE log line now includes snapshot suffix (FR-007)
# ===========================================================================


async def test_obs_14_car_state_has_snapshot_suffix(hass: HomeAssistant) -> None:
    """T-OBS-14: CAR_STATE log lines now include the energy+power snapshot suffix."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Fire a car_value state change (Idle → Charging)
    engine._last_car_status = "Idle"
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    car_calls = [call for call in mock_log.call_args_list if call.args[0] == "CAR_STATE"]
    assert car_calls, "Expected CAR_STATE log call"
    category, message = car_calls[-1].args
    assert category == "CAR_STATE"
    assert "| wh=" in message, f"CAR_STATE message missing snapshot suffix: {message!r}"
    assert "power=" in message


# ===========================================================================
# T-OBS-12: No session lifecycle change from observation events (IDLE state)
# ===========================================================================


async def test_obs_12_idle_no_session_lifecycle_change(hass: HomeAssistant) -> None:
    """T-OBS-12: Observation events in IDLE state do not trigger session start."""
    from pytest_homeassistant_custom_component.common import async_capture_events

    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Ensure IDLE
    assert engine.state == SessionEngineState.IDLE
    assert engine.active_session is None
    gate_before = engine._awaiting_reset

    session_events = async_capture_events(hass, EVENT_SESSION_STARTED)
    session_events += async_capture_events(hass, EVENT_SESSION_COMPLETED)

    # Pre-set caches so all transitions are new
    engine._last_plug = "off"
    engine._last_cable_lock = "Unlocked"
    engine._last_model_status = "Idle"
    engine._last_err = "-none-"
    engine._last_trx = "null"

    # Fire all five observation-category transitions
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_MODEL_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_ERROR_ENTITY, "cable_overheat")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    await hass.async_block_till_done()

    # Session-engine state must remain IDLE
    assert engine.state == SessionEngineState.IDLE
    assert engine.active_session is None
    assert engine._awaiting_reset == gate_before
    assert len(session_events) == 0


# ===========================================================================
# T-OBS-12 variant: TRACKING state — observation events do not stop session
# ===========================================================================


async def test_obs_12_tracking_no_session_lifecycle_change(hass: HomeAssistant) -> None:
    """T-OBS-12 (TRACKING): Observation events during TRACKING do not stop session."""
    from pytest_homeassistant_custom_component.common import async_capture_events

    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Start a session manually: set Charging + valid trx
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    assert engine.state == SessionEngineState.TRACKING
    assert engine.active_session is not None
    session_id_before = engine.active_session.id

    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    # Pre-set caches
    engine._last_plug = "off"
    engine._last_cable_lock = "Unlocked"
    engine._last_model_status = "Charging"
    engine._last_err = "-none-"

    # Fire observation-category transitions (not car_value)
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_MODEL_STATUS_ENTITY, "Charging complete")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_ERROR_ENTITY, "cable_overheat")
    await hass.async_block_till_done()

    # Session must still be tracking same session
    assert engine.state == SessionEngineState.TRACKING
    assert engine.active_session is not None
    assert engine.active_session.id == session_id_before
    assert len(completed_events) == 0


# ===========================================================================
# T-OBS-13: Listener unsubscribes registered via entry.async_on_unload
# ===========================================================================


async def test_obs_13_listeners_unsubscribed_on_unload(hass: HomeAssistant) -> None:
    """T-OBS-13: After entry unload, observation entities no longer emit log lines."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Pre-set caches to make transitions register as new
    engine._last_plug = "off"

    # Unload the entry — this calls all async_on_unload callbacks
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Fire state change AFTER unload — no new log lines expected
    mock_log.reset_mock()
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()

    plug_calls = [call for call in mock_log.call_args_list if call.args[0] == "PLUG_STATE"]
    assert len(plug_calls) == 0, "Expected no PLUG_STATE calls after entry unload"


# ===========================================================================
# T-OBS-15: Missing entity at setup — no exception raised
# ===========================================================================


async def test_obs_15_missing_entity_no_exception(hass: HomeAssistant) -> None:
    """T-OBS-15: Configured observation entity missing from HA states — setup does not raise."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={
            "debug_logging": True,
            CONF_PLUG_ENTITY: "binary_sensor.does_not_exist_car_0",
            CONF_CABLE_LOCK_ENTITY: "sensor.does_not_exist_cus_value",
            CONF_MODEL_STATUS_ENTITY: "sensor.does_not_exist_modelstatus_value",
            CONF_ERROR_ENTITY: "sensor.does_not_exist_err_value",
        },
        title="Missing Entities Charger",
    )

    # Standard entities for the core session engine
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
    # NOTE: observation entities intentionally NOT set in hass.states

    entry.add_to_hass(hass)
    # Should not raise
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert result is True
    engine: SessionEngine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    assert engine.state == SessionEngineState.IDLE


# ===========================================================================
# T-OBS-16: Reload survival — listeners re-registered after reload
# ===========================================================================


async def test_obs_16_reload_survival(hass: HomeAssistant) -> None:
    """T-OBS-16: After config-entry reload, observation listeners are re-registered."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Pre-set cache and fire a transition before reload
    engine._last_plug = "off"
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()

    pre_reload_calls = [call for call in mock_log.call_args_list if call.args[0] == "PLUG_STATE"]
    assert len(pre_reload_calls) >= 1, "Expected at least one PLUG_STATE call before reload"

    # Reload the entry
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Get fresh engine and mock after reload
    engine_post = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    debug_logger_post = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    mock_log_post = MagicMock()
    debug_logger_post.log = mock_log_post
    if engine_post._debug_logger is not None:
        engine_post._debug_logger.log = mock_log_post

    # Post-reload: set entity to current state so cache is populated
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()

    # Force a real transition (engine._last_plug starts at None after reload)
    # The reload resets the engine, so _last_plug is None → any non-None state
    # will emit a log line.
    engine_post._last_plug = "on"
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    await hass.async_block_till_done()

    post_reload_calls = [
        call for call in mock_log_post.call_args_list if call.args[0] == "PLUG_STATE"
    ]
    assert len(post_reload_calls) >= 1, (
        "Expected PLUG_STATE calls after reload (listeners re-registered)"
    )


# ===========================================================================
# T-OBS-04: -none- → -none- on error category is suppressed (US4, T034)
# ===========================================================================


async def test_obs_04_none_to_none_err_suppressed(hass: HomeAssistant) -> None:
    """T-OBS-04: err -none- → -none- is suppressed; no ERR_STATE line emitted."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Set cache to -none- (already the default, but explicit for clarity)
    engine._last_err = "-none-"

    # Fire same-value refresh
    hass.states.async_set(MOCK_ERROR_ENTITY, "-none-")
    await hass.async_block_till_done()

    err_calls = [call for call in mock_log.call_args_list if call.args[0] == "ERR_STATE"]
    assert len(err_calls) == 0, "Expected ERR_STATE to be suppressed for -none- → -none-"


# ===========================================================================
# T-OBS-07: Duplicate transition suppressed (any category) (US4, T035)
# ===========================================================================


async def test_obs_07_duplicate_transition_suppressed(hass: HomeAssistant) -> None:
    """T-OBS-07: Second event with same value does not emit a second log line."""
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Set cache so first transition is new
    engine._last_plug = "off"

    # First transition: off → on (should emit)
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()

    first_calls = [call for call in mock_log.call_args_list if call.args[0] == "PLUG_STATE"]
    assert len(first_calls) == 1, "Expected exactly one PLUG_STATE call on first transition"

    # Second event: on → on (HA refresh, should be suppressed)
    mock_log.reset_mock()
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()

    second_calls = [call for call in mock_log.call_args_list if call.args[0] == "PLUG_STATE"]
    assert len(second_calls) == 0, "Expected PLUG_STATE to be suppressed for on → on refresh"


# ===========================================================================
# T-OBS-08: Debug logging disabled — no observation lines emitted (US4, T036)
# ===========================================================================


async def test_obs_08_debug_off_no_observation_lines(hass: HomeAssistant) -> None:
    """T-OBS-08: When debug logging is disabled, no observation lines are written."""
    entry = _make_observation_entry(debug_logging=False)

    # Standard entity states
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_MODEL_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_ERROR_ENTITY, "-none-")

    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    engine: SessionEngine = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    mock_log = MagicMock()
    debug_logger.log = mock_log
    # Engine has no debug_logger (disabled path) — but patch to be safe
    if engine._debug_logger is not None:
        engine._debug_logger.log = mock_log

    # Set caches so transitions would emit if logging were enabled
    engine._last_plug = "off"
    engine._last_cable_lock = "Unlocked"
    engine._last_model_status = "Idle"
    engine._last_err = "-none-"
    engine._last_trx = "null"

    # Fire transitions for all five categories
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_MODEL_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_ERROR_ENTITY, "cable_overheat")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    await hass.async_block_till_done()

    # No observation calls expected (debug logging is off)
    observation_categories = {"PLUG_STATE", "CABLE_LOCK", "MODEL_STATUS", "ERR_STATE", "TRX_STATE"}
    obs_calls = [call for call in mock_log.call_args_list if call.args[0] in observation_categories]
    assert len(obs_calls) == 0, (
        f"Expected no observation calls with debug logging disabled, got: {obs_calls}"
    )


# ===========================================================================
# C3: Cache not polluted by transient unavailability
# ===========================================================================


async def test_obs_c3_unavailable_does_not_pollute_cache(hass: HomeAssistant) -> None:
    """C3: plug on → unavailable → on: cache stays 'on'; second log shows on → on suppressed."""
    from homeassistant.const import STATE_UNAVAILABLE

    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Prime cache
    engine._last_plug = "on"

    # Transition to unavailable — should log but NOT update cache
    hass.states.async_set(MOCK_PLUG_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    unavail_calls = [c for c in mock_log.call_args_list if c.args[0] == "PLUG_STATE"]
    assert len(unavail_calls) == 1, "Expected one PLUG_STATE log for on → unavailable"
    assert STATE_UNAVAILABLE in unavail_calls[0].args[1]

    # Cache should still be "on" (not polluted)
    assert engine._last_plug == "on", "Cache must not be updated to unavailable"

    mock_log.reset_mock()

    # Transition back to "on" — same as cache so must be SUPPRESSED
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()

    on_again_calls = [c for c in mock_log.call_args_list if c.args[0] == "PLUG_STATE"]
    assert len(on_again_calls) == 0, (
        "Expected on → on to be suppressed because cache was not polluted to 'unavailable'"
    )


# ===========================================================================
# I2: Signal caches primed at setup
# ===========================================================================


async def test_obs_i2_cache_primed_at_setup(hass: HomeAssistant) -> None:
    """I2: After async_setup, _last_plug is primed from current HA state."""
    # Pre-set plug to "on" before setup so it's in HA states when async_setup runs
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    hass.states.async_set(MOCK_MODEL_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_ERROR_ENTITY, "-none-")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.000")
    hass.states.async_set(MOCK_POWER_ENTITY, "0")

    entry = _make_observation_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    engine: SessionEngine = hass.data[DOMAIN][entry.entry_id]["session_engine"]

    # Cache should be primed from the HA state at setup time
    assert engine._last_plug == "on", "Expected _last_plug primed to 'on'"
    assert engine._last_cable_lock == "Locked", "Expected _last_cable_lock primed"

    # Fire a transition from the primed value → should log actual before/after
    mock_log = MagicMock()
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    debug_logger.log = mock_log
    if engine._debug_logger is not None:
        engine._debug_logger.log = mock_log

    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    await hass.async_block_till_done()

    plug_calls = [c for c in mock_log.call_args_list if c.args[0] == "PLUG_STATE"]
    assert plug_calls, "Expected PLUG_STATE log call"
    # Must show "on → off", not "None → off"
    assert "on → off" in plug_calls[-1].args[1], (
        f"Expected 'on → off' but got: {plug_calls[-1].args[1]!r}"
    )


# ===========================================================================
# I3: Warning emitted when configured observation entity is not registered
# ===========================================================================


async def test_obs_i3_warning_on_missing_entity(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """I3: A configured observation entity that doesn't exist in HA emits a warning."""
    nonexistent = "binary_sensor.this_does_not_exist"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={
            "debug_logging": True,
            CONF_PLUG_ENTITY: nonexistent,
        },
        title="Missing Entity Test",
    )

    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
    # nonexistent intentionally NOT set

    with caplog.at_level(logging.WARNING, logger="custom_components.ev_charging_manager"):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert any(
        nonexistent in record.message and "not registered" in record.message
        for record in caplog.records
    ), "Expected warning about unregistered observation entity"


async def test_obs_i3_no_warning_when_entity_exists(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """I3: No warning when the configured entity exists in HA states."""
    entry = _make_observation_entry()
    # Standard setup pre-sets all observation entities
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_TRX_ENTITY, "null")
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.0")
    hass.states.async_set(MOCK_POWER_ENTITY, "0.0")
    hass.states.async_set(MOCK_PLUG_ENTITY, "off")
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    hass.states.async_set(MOCK_MODEL_STATUS_ENTITY, "Idle")
    hass.states.async_set(MOCK_ERROR_ENTITY, "-none-")

    with caplog.at_level(logging.WARNING, logger="custom_components.ev_charging_manager"):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    not_registered_warnings = [r for r in caplog.records if "not registered" in r.message]
    assert len(not_registered_warnings) == 0, (
        f"Expected no 'not registered' warnings but got: {not_registered_warnings}"
    )


# ===========================================================================
# T1 (T-OBS-17): trx transition during gate-engaged state — no promotion
# ===========================================================================


async def test_obs_17_trx_transition_during_gate_engaged_no_promotion(
    hass: HomeAssistant,
) -> None:
    """T1: trx transitions while _awaiting_reset=True and car_status=Charging
    must not trigger gate promotion or session start.
    """
    from pytest_homeassistant_custom_component.common import async_capture_events

    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Set up the engine in a gate-engaged state
    engine._awaiting_reset = True
    engine._gate_engaged_energy_kwh = 5.0
    engine._gate_charging_started_at = None  # H1 not yet started
    engine._gate_skipped_count = 0

    # Ensure engine is IDLE
    assert engine.state == SessionEngineState.IDLE

    session_events = async_capture_events(hass, EVENT_SESSION_STARTED)

    # Simulate car_status = Charging (but gate is engaged, so session blocked)
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()

    # No session should have started (gate blocks it)
    assert engine.state == SessionEngineState.IDLE or len(session_events) == 0

    # Fire trx transition from "1" to "2"
    engine._last_trx = "1"
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    await hass.async_block_till_done()

    # Gate must still be engaged
    assert engine._awaiting_reset is True, "Gate must remain engaged after trx transition"

    # No GATE_PROMOTE log
    promote_calls = [c for c in mock_log.call_args_list if c.args[0] == "GATE_PROMOTE"]
    assert len(promote_calls) == 0, "Expected no GATE_PROMOTE on trx transition alone"

    # TRX_STATE log should have been emitted (observation)
    trx_calls = [c for c in mock_log.call_args_list if c.args[0] == "TRX_STATE"]
    assert trx_calls, "Expected TRX_STATE observation log"


# ===========================================================================
# T2: T-OBS-16 strengthened — verify binding integrity after reload
# ===========================================================================


async def test_obs_16_reload_binding_integrity(hass: HomeAssistant) -> None:
    """T-OBS-16 (strengthened): After reload, entry.options retains the plug entity,
    and state changes on a different entity do NOT emit plug log lines.
    """
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    original_plug = entry.options[CONF_PLUG_ENTITY]

    # Reload the entry
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Verify binding survived reload
    reloaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert reloaded_entry.options.get(CONF_PLUG_ENTITY) == original_plug, (
        "CONF_PLUG_ENTITY must survive reload unchanged"
    )

    # Set up mock on post-reload engine
    engine_post = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    debug_logger_post = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    mock_log_post = MagicMock()
    debug_logger_post.log = mock_log_post
    if engine_post._debug_logger is not None:
        engine_post._debug_logger.log = mock_log_post

    # Fire a state change on the CABLE_LOCK entity (NOT the plug) — must NOT produce PLUG_STATE
    engine_post._last_cable_lock = "Unlocked"
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()

    plug_calls = [c for c in mock_log_post.call_args_list if c.args[0] == "PLUG_STATE"]
    assert len(plug_calls) == 0, "No PLUG_STATE log expected when only CABLE_LOCK entity changed"

    cable_calls = [c for c in mock_log_post.call_args_list if c.args[0] == "CABLE_LOCK"]
    assert cable_calls, "Expected CABLE_LOCK log from cable-lock state change"


# ===========================================================================
# SC-001: Single session, all 5 observation categories fire
# ===========================================================================


async def test_obs_sc001_multi_category_single_session(hass: HomeAssistant) -> None:
    """SC-001: Within one logical charging session, all 5 observation categories emit
    at least one log line each.
    """
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Start a session
    hass.states.async_set(MOCK_TRX_ENTITY, "2")
    hass.states.async_set(MOCK_CAR_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()
    assert engine.state == SessionEngineState.TRACKING

    # Set caches so all transitions are new
    engine._last_plug = "off"
    engine._last_cable_lock = "Unlocked"
    engine._last_model_status = "Idle"
    engine._last_err = "-none-"
    engine._last_trx = "2"

    # Fire all five observation categories
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_MODEL_STATUS_ENTITY, "Charging")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_ERROR_ENTITY, "cable_warm")
    await hass.async_block_till_done()
    hass.states.async_set(MOCK_TRX_ENTITY, "3")
    await hass.async_block_till_done()

    for category in ("PLUG_STATE", "CABLE_LOCK", "MODEL_STATUS", "ERR_STATE", "TRX_STATE"):
        calls = [c for c in mock_log.call_args_list if c.args[0] == category]
        assert calls, f"Expected at least one log line for category {category}"


# ===========================================================================
# FR-006: Real DebugLogger writes the correct line format
# ===========================================================================


async def test_obs_fr006_real_debug_logger_format(
    hass: HomeAssistant, tmp_path: pathlib.Path
) -> None:
    """FR-006: A real DebugLogger writes the correct contract format for observation lines."""
    import re

    from custom_components.ev_charging_manager.debug_logger import DebugLogger

    log_dir = str(tmp_path)
    logger = DebugLogger(hass, log_dir)
    await logger.async_enable()

    # Write one observation-style log line
    logger.log("PLUG_STATE", "plug changed: off → on | wh=1.234 power=1100")
    await logger.async_disable()  # flushes the buffer to disk

    # Read the file (PR-28: at the config root, no longer under www/)
    log_file = tmp_path / "ev_charging_manager_debug.log"
    assert log_file.exists(), f"Log file not created at {log_file}"
    lines = log_file.read_text().splitlines()

    # Find the PLUG_STATE line (skip DEBUG_ON/DEBUG_OFF markers)
    obs_lines = [ln for ln in lines if "PLUG_STATE" in ln]
    assert obs_lines, f"No PLUG_STATE line found in log file. Lines: {lines}"

    line = obs_lines[0]
    # Contract: <ISO-ms timestamp> | PLUG_STATE | <message>
    # ISO timestamp must include milliseconds (at least 3 decimal places)
    assert re.match(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+",
        line,
    ), f"Timestamp missing milliseconds in line: {line!r}"
    assert "PLUG_STATE" in line, f"Category not found in line: {line!r}"
    assert "plug changed: off → on" in line, f"Message not found in line: {line!r}"
    assert "wh=1.234" in line
    assert "power=1100" in line
    # Verify the pipe-separated format contract: timestamp | category | message
    parts = line.split(" | ", maxsplit=2)
    assert len(parts) == 3, f"Expected 3 pipe-separated parts, got: {parts}"


# ===========================================================================
# FR-013: Runtime entity removal — no exception, no log line
# ===========================================================================


async def test_obs_fr013_runtime_entity_removal_no_exception(hass: HomeAssistant) -> None:
    """FR-013: Removing a configured observation entity mid-session raises no exception
    and emits no further log lines for it.
    """
    entry = _make_observation_entry()
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Prime the cache and verify one transition works
    engine._last_plug = "off"
    hass.states.async_set(MOCK_PLUG_ENTITY, "on")
    await hass.async_block_till_done()

    pre_remove_calls = [c for c in mock_log.call_args_list if c.args[0] == "PLUG_STATE"]
    assert pre_remove_calls, "Expected PLUG_STATE log before entity removal"

    # Remove the entity from HA states (simulates firmware rename / entity deletion)
    hass.states.async_remove(MOCK_PLUG_ENTITY)
    await hass.async_block_till_done()

    # No exception should have been raised. The state removal itself fires a
    # state-changed event with new_state=None.  The handler checks new_val is not None.
    mock_log.reset_mock()

    # No new PLUG_STATE lines should appear after removal
    post_remove_calls = [c for c in mock_log.call_args_list if c.args[0] == "PLUG_STATE"]
    assert len(post_remove_calls) == 0, "Expected no PLUG_STATE log after entity removal"


# ===========================================================================
# T-CFL-03 (strengthened): After clearing cable_lock in options, no CABLE_LOCK log
# ===========================================================================


async def test_obs_cfl03_cleared_cable_lock_no_log(hass: HomeAssistant) -> None:
    """T-CFL-03 (strengthen): After user clears cable_lock_entity in options flow and
    entry is reloaded, cable-lock state changes emit no CABLE_LOCK lines.
    """
    # Start with cable_lock configured
    entry = _make_observation_entry(cable_lock=MOCK_CABLE_LOCK_ENTITY)
    engine, mock_log = await _setup_observation_engine(hass, entry)

    # Verify it logs before clearing
    engine._last_cable_lock = "Unlocked"
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Locked")
    await hass.async_block_till_done()
    pre_calls = [c for c in mock_log.call_args_list if c.args[0] == "CABLE_LOCK"]
    assert pre_calls, "Expected CABLE_LOCK log before slot cleared"

    # Simulate user clearing the slot: write None into entry.options
    new_options = dict(entry.options)
    new_options[CONF_CABLE_LOCK_ENTITY] = None
    hass.config_entries.async_update_entry(entry, options=new_options)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Get fresh engine
    engine_post = hass.data[DOMAIN][entry.entry_id]["session_engine"]
    debug_logger_post = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    mock_log_post = MagicMock()
    debug_logger_post.log = mock_log_post
    if engine_post._debug_logger is not None:
        engine_post._debug_logger.log = mock_log_post

    # Fire cable-lock transition — no CABLE_LOCK log expected
    hass.states.async_set(MOCK_CABLE_LOCK_ENTITY, "Unlocked")
    await hass.async_block_till_done()

    post_calls = [c for c in mock_log_post.call_args_list if c.args[0] == "CABLE_LOCK"]
    assert len(post_calls) == 0, "Expected no CABLE_LOCK log after slot was cleared in options"
