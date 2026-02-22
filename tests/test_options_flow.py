"""Tests for the OptionsFlowHandler (T026)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_MAX_STORED_SESSIONS,
    CONF_MIN_SESSION_DURATION_S,
    CONF_MIN_SESSION_ENERGY_WH,
    CONF_PERSISTENCE_INTERVAL_S,
    CONF_PRICING_MODE,
    CONF_SPOT_ADDITIONAL_COST_KWH,
    CONF_SPOT_FALLBACK_PRICE_KWH,
    CONF_SPOT_PRICE_ENTITY,
    CONF_SPOT_VAT_MULTIPLIER,
    CONF_STATIC_PRICE_KWH,
    DEFAULT_MAX_STORED_SESSIONS,
    DEFAULT_MIN_SESSION_DURATION_S,
    DEFAULT_MIN_SESSION_ENERGY_WH,
    DEFAULT_PERSISTENCE_INTERVAL_S,
    DEFAULT_SPOT_ADDITIONAL_COST_KWH,
    DEFAULT_SPOT_FALLBACK_PRICE_KWH,
    DEFAULT_SPOT_VAT_MULTIPLIER,
    DEFAULT_STATIC_PRICE_KWH,
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
    """Submitting valid options (init + pricing) creates config entry with those options."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    # Step 1: init (session thresholds)
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
    assert result["type"] == "form"
    assert result["step_id"] == "pricing"

    # Step 2: pricing (keep static mode)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PRICING_MODE: "static",
            CONF_STATIC_PRICE_KWH: DEFAULT_STATIC_PRICE_KWH,
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

    # Schema keys carry default values (skip Optional fields without an explicit default)
    schema = result["data_schema"].schema
    defaults = {
        str(k): k.default() for k in schema if hasattr(k, "default") and callable(k.default)
    }
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
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MIN_SESSION_DURATION_S: 0,
            CONF_MIN_SESSION_ENERGY_WH: 0,
            CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
            CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
        },
    )
    # Submit pricing step (keep static)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PRICING_MODE: "static",
            CONF_STATIC_PRICE_KWH: DEFAULT_STATIC_PRICE_KWH,
        },
    )
    await hass.async_block_till_done()

    # Immediately start + stop session — with 0 thresholds it should complete
    hass.states.async_set(MOCK_ENERGY_ENTITY, "0.1")
    await start_charging_session(hass, trx_value="0")
    await stop_charging_session(hass)

    # The session_completed event should fire since both thresholds are 0
    assert len(completed_events) == 1


# ---------------------------------------------------------------------------
# T017 — Options flow: spot pricing switch (US3)
# ---------------------------------------------------------------------------


async def _submit_init_step(hass, flow_id: str) -> dict:
    """Submit options init step with default threshold values."""
    return await hass.config_entries.options.async_configure(
        flow_id,
        user_input={
            CONF_MIN_SESSION_DURATION_S: DEFAULT_MIN_SESSION_DURATION_S,
            CONF_MIN_SESSION_ENERGY_WH: DEFAULT_MIN_SESSION_ENERGY_WH,
            CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
            CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
        },
    )


async def test_options_flow_switch_to_spot_updates_entry_data(hass: HomeAssistant) -> None:
    """Switching to spot pricing updates entry.data with spot fields."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)
    hass.states.async_set("sensor.spot_price", "0.89")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _submit_init_step(hass, result["flow_id"])
    assert result["step_id"] == "pricing"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PRICING_MODE: "spot",
            CONF_STATIC_PRICE_KWH: DEFAULT_STATIC_PRICE_KWH,
            CONF_SPOT_PRICE_ENTITY: "sensor.spot_price",
            CONF_SPOT_ADDITIONAL_COST_KWH: DEFAULT_SPOT_ADDITIONAL_COST_KWH,
            CONF_SPOT_VAT_MULTIPLIER: DEFAULT_SPOT_VAT_MULTIPLIER,
            CONF_SPOT_FALLBACK_PRICE_KWH: DEFAULT_SPOT_FALLBACK_PRICE_KWH,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    refreshed = hass.config_entries.async_get_entry(entry.entry_id)
    assert refreshed.data[CONF_PRICING_MODE] == "spot"
    assert refreshed.data[CONF_SPOT_PRICE_ENTITY] == "sensor.spot_price"
    assert refreshed.data[CONF_SPOT_ADDITIONAL_COST_KWH] == DEFAULT_SPOT_ADDITIONAL_COST_KWH
    assert refreshed.data[CONF_SPOT_VAT_MULTIPLIER] == DEFAULT_SPOT_VAT_MULTIPLIER
    assert refreshed.data[CONF_SPOT_FALLBACK_PRICE_KWH] == DEFAULT_SPOT_FALLBACK_PRICE_KWH


async def test_options_flow_switch_to_static_removes_spot_fields(hass: HomeAssistant) -> None:
    """Switching from spot to static removes spot keys from entry.data."""
    spot_data = {
        **MOCK_CHARGER_DATA,
        "pricing_mode": "spot",
        CONF_SPOT_PRICE_ENTITY: "sensor.spot_price",
        CONF_SPOT_ADDITIONAL_COST_KWH: DEFAULT_SPOT_ADDITIONAL_COST_KWH,
        CONF_SPOT_VAT_MULTIPLIER: DEFAULT_SPOT_VAT_MULTIPLIER,
        CONF_SPOT_FALLBACK_PRICE_KWH: DEFAULT_SPOT_FALLBACK_PRICE_KWH,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=spot_data, title="Test Charger")
    hass.states.async_set("sensor.spot_price", "0.89")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _submit_init_step(hass, result["flow_id"])
    assert result["step_id"] == "pricing"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PRICING_MODE: "static",
            CONF_STATIC_PRICE_KWH: 1.99,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    refreshed = hass.config_entries.async_get_entry(entry.entry_id)
    assert refreshed.data[CONF_PRICING_MODE] == "static"
    # spot_price_entity (no schema default) is removed; secondary spot fields stay
    # in data but are ignored while mode=static
    assert CONF_SPOT_PRICE_ENTITY not in refreshed.data


async def test_options_flow_spot_entity_not_found_shows_error(hass: HomeAssistant) -> None:
    """Options pricing step: non-existent spot entity shows entity_not_found error."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _submit_init_step(hass, result["flow_id"])

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PRICING_MODE: "spot",
            CONF_STATIC_PRICE_KWH: DEFAULT_STATIC_PRICE_KWH,
            CONF_SPOT_PRICE_ENTITY: "sensor.does_not_exist",
        },
    )

    assert result["type"] == "form"
    assert result["step_id"] == "pricing"
    assert result["errors"][CONF_SPOT_PRICE_ENTITY] == "entity_not_found"


async def test_options_flow_spot_non_numeric_entity_shows_error(hass: HomeAssistant) -> None:
    """Options pricing step: non-numeric spot entity shows entity_invalid error."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)
    hass.states.async_set("sensor.text_price", "not_a_number")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _submit_init_step(hass, result["flow_id"])

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PRICING_MODE: "spot",
            CONF_STATIC_PRICE_KWH: DEFAULT_STATIC_PRICE_KWH,
            CONF_SPOT_PRICE_ENTITY: "sensor.text_price",
        },
    )

    assert result["type"] == "form"
    assert result["step_id"] == "pricing"
    assert result["errors"][CONF_SPOT_PRICE_ENTITY] == "entity_invalid"
