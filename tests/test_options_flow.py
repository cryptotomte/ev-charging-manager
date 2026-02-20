"""Tests for the OptionsFlowHandler (T026)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_MAX_STORED_SESSIONS,
    CONF_MIN_SESSION_DURATION_S,
    CONF_MIN_SESSION_ENERGY_WH,
    CONF_PERSISTENCE_INTERVAL_S,
    DEFAULT_MAX_STORED_SESSIONS,
    DEFAULT_MIN_SESSION_DURATION_S,
    DEFAULT_MIN_SESSION_ENERGY_WH,
    DEFAULT_PERSISTENCE_INTERVAL_S,
    DOMAIN,
    EVENT_SESSION_COMPLETED,
)
from tests.conftest import (
    MOCK_CHARGER_DATA,
    MOCK_ENERGY_ENTITY,
    setup_session_engine,
    start_charging_session,
    stop_charging_session,
)


async def test_options_flow_shows_form_with_defaults(hass: HomeAssistant) -> None:
    """Options flow init step shows form pre-filled with default values."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    schema = result["data_schema"].schema
    keys = [str(k) for k in schema]
    assert CONF_MIN_SESSION_DURATION_S in keys
    assert CONF_MIN_SESSION_ENERGY_WH in keys
    assert CONF_PERSISTENCE_INTERVAL_S in keys
    assert CONF_MAX_STORED_SESSIONS in keys


async def test_options_flow_submit_creates_entry(hass: HomeAssistant) -> None:
    """Submitting valid options creates config entry with those options."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MIN_SESSION_DURATION_S: 120,
            CONF_MIN_SESSION_ENERGY_WH: 100,
            CONF_PERSISTENCE_INTERVAL_S: 600,
            CONF_MAX_STORED_SESSIONS: 500,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.options[CONF_MIN_SESSION_DURATION_S] == 120
    assert entry.options[CONF_MIN_SESSION_ENERGY_WH] == 100
    assert entry.options[CONF_PERSISTENCE_INTERVAL_S] == 600
    assert entry.options[CONF_MAX_STORED_SESSIONS] == 500


async def test_options_flow_defaults_used_when_no_options_set(hass: HomeAssistant) -> None:
    """Options flow form defaults match the const DEFAULT_* values."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    # Schema keys carry default values
    schema = result["data_schema"].schema
    defaults = {str(k): k.default() for k in schema if hasattr(k, "default")}
    assert defaults.get(CONF_MIN_SESSION_DURATION_S) == DEFAULT_MIN_SESSION_DURATION_S
    assert defaults.get(CONF_MIN_SESSION_ENERGY_WH) == DEFAULT_MIN_SESSION_ENERGY_WH
    assert defaults.get(CONF_PERSISTENCE_INTERVAL_S) == DEFAULT_PERSISTENCE_INTERVAL_S
    assert defaults.get(CONF_MAX_STORED_SESSIONS) == DEFAULT_MAX_STORED_SESSIONS


async def test_options_min_duration_applied_to_next_session(hass: HomeAssistant) -> None:
    """Custom min_duration_s option is used by session engine for micro-session filter."""
    from pytest_homeassistant_custom_component.common import async_capture_events

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    completed_events = async_capture_events(hass, EVENT_SESSION_COMPLETED)

    await setup_session_engine(hass, entry)

    # Set min_duration to 0 via options flow → even instant sessions are not micro
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MIN_SESSION_DURATION_S: 0,
            CONF_MIN_SESSION_ENERGY_WH: 0,
            CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
            CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
        },
    )
    await hass.async_block_till_done()

    # Immediately start + stop session — with 0 thresholds it should complete
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
    await start_charging_session(hass, trx_value="0")
    await stop_charging_session(hass)

    # The session_completed event should fire since both thresholds are 0
    assert len(completed_events) == 1
