"""Tests for PR-20 observation logging: PLUG_STATE, CABLE_LOCK, MODEL_STATUS,
ERR_STATE, TRX_STATE categories, snapshot suffix, and no-regression checks.

Test naming follows the plan: T-OBS-01 through T-OBS-16.
"""

from __future__ import annotations

from unittest.mock import MagicMock

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
    """T-OBS-06: trx entity 0 → 2 emits TRX_STATE (uses existing CONF_RFID_ENTITY)."""
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
    assert "trx changed: 0 → 2" in message
    assert "| wh=" in message


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
    """T-OBS-11: _format_signal_snapshot returns wh=? power=? when no values available."""
    entry = _make_observation_entry()
    engine, _ = await _setup_observation_engine(hass, entry)

    from homeassistant.const import STATE_UNAVAILABLE

    # Make live entities unavailable
    hass.states.async_set(MOCK_ENERGY_ENTITY, STATE_UNAVAILABLE)
    hass.states.async_set(MOCK_POWER_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    # Ensure cached values are zero (default)
    engine._last_energy_kwh = 0.0
    engine._last_power_w = 0.0

    result = engine._format_signal_snapshot()
    assert result == " | wh=? power=?"


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
