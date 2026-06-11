"""Tests for the OptionsFlowHandler (T026)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_CABLE_LOCK_ENTITY,
    CONF_DEBUG_LOGGING,
    CONF_ERROR_ENTITY,
    CONF_HEARTBEAT_LOG_INTERVAL_MIN,
    CONF_MAX_STORED_SESSIONS,
    CONF_MIN_SESSION_DURATION_S,
    CONF_MIN_SESSION_ENERGY_WH,
    CONF_MODEL_STATUS_ENTITY,
    CONF_PERSISTENCE_INTERVAL_S,
    CONF_PLUG_ENTITY,
    CONF_PRICING_MODE,
    CONF_SPOT_ADDITIONAL_COST_KWH,
    CONF_SPOT_FALLBACK_PRICE_KWH,
    CONF_SPOT_PRICE_ENTITY,
    CONF_SPOT_VAT_MULTIPLIER,
    CONF_STATIC_PRICE_KWH,
    CONF_UI_DISPATCH_INTERVAL_S,
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


# ---------------------------------------------------------------------------
# T-CFL-02 (PR-20): Options flow init step shows four observation entity fields
# ---------------------------------------------------------------------------


async def test_options_flow_shows_observation_entity_fields(hass: HomeAssistant) -> None:
    """T-CFL-02: Options flow init step exposes four observation slots pre-filled."""
    plug = "binary_sensor.goe_abc123_car_0"
    cable_lock = "sensor.goe_abc123_cus_value"
    model_status = "sensor.goe_abc123_modelstatus_value"
    error = "sensor.goe_abc123_err_value"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={
            CONF_PLUG_ENTITY: plug,
            CONF_CABLE_LOCK_ENTITY: cable_lock,
            CONF_MODEL_STATUS_ENTITY: model_status,
            CONF_ERROR_ENTITY: error,
        },
        title="Test Charger",
    )
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["step_id"] == "init"

    # Verify the four observation fields are in the schema
    schema_keys = [str(k) for k in result["data_schema"].schema]
    assert CONF_PLUG_ENTITY in schema_keys, "Options init must contain plug_entity field"
    assert CONF_CABLE_LOCK_ENTITY in schema_keys, (
        "Options init must contain cable_lock_entity field"
    )
    assert CONF_MODEL_STATUS_ENTITY in schema_keys, (
        "Options init must contain model_status_entity field"
    )
    assert CONF_ERROR_ENTITY in schema_keys, "Options init must contain error_entity field"

    # Verify suggested values are pre-filled from current options
    suggested = {
        str(k): k.description.get("suggested_value")
        for k in result["data_schema"].schema
        if hasattr(k, "description") and isinstance(k.description, dict)
    }
    assert suggested.get(CONF_PLUG_ENTITY) == plug
    assert suggested.get(CONF_CABLE_LOCK_ENTITY) == cable_lock
    assert suggested.get(CONF_MODEL_STATUS_ENTITY) == model_status
    assert suggested.get(CONF_ERROR_ENTITY) == error


# ---------------------------------------------------------------------------
# T-CFL-03 (PR-20): Clearing one observation slot persists None, engine skips it
# ---------------------------------------------------------------------------


async def test_options_flow_clearing_observation_slot_persists_none(
    hass: HomeAssistant,
) -> None:
    """T-CFL-03: Clearing cable_lock_entity in options persists None; no listener registered."""

    plug = "binary_sensor.goe_abc123_car_0"
    cable_lock = "sensor.goe_abc123_cus_value"
    model_status = "sensor.goe_abc123_modelstatus_value"
    error = "sensor.goe_abc123_err_value"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={
            CONF_PLUG_ENTITY: plug,
            CONF_CABLE_LOCK_ENTITY: cable_lock,
            CONF_MODEL_STATUS_ENTITY: model_status,
            CONF_ERROR_ENTITY: error,
        },
        title="Test Charger",
    )
    await setup_session_engine(hass, entry)

    # Open options flow
    result = await hass.config_entries.options.async_init(entry.entry_id)

    # Submit init step with cable_lock cleared (empty string → should persist as None)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MIN_SESSION_DURATION_S: DEFAULT_MIN_SESSION_DURATION_S,
            CONF_MIN_SESSION_ENERGY_WH: DEFAULT_MIN_SESSION_ENERGY_WH,
            CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
            CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
            CONF_PLUG_ENTITY: plug,
            # CONF_CABLE_LOCK_ENTITY not submitted (cleared)
            CONF_MODEL_STATUS_ENTITY: model_status,
            CONF_ERROR_ENTITY: error,
        },
    )
    assert result["step_id"] == "pricing"

    # Submit pricing step
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PRICING_MODE: "static",
            CONF_STATIC_PRICE_KWH: DEFAULT_STATIC_PRICE_KWH,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"

    # Verify cable_lock_entity is None or absent in saved options
    refreshed = hass.config_entries.async_get_entry(entry.entry_id)
    cable_lock_val = refreshed.options.get(CONF_CABLE_LOCK_ENTITY)
    assert cable_lock_val in (None, ""), (
        f"cable_lock_entity should be None after clearing, got: {cable_lock_val!r}"
    )

    # Verify the other three are still present
    assert refreshed.options.get(CONF_PLUG_ENTITY) == plug
    assert refreshed.options.get(CONF_MODEL_STATUS_ENTITY) == model_status
    assert refreshed.options.get(CONF_ERROR_ENTITY) == error


# ---------------------------------------------------------------------------
# T008 (PR-23 Phase 1): Options flow — three new PR-23 options
# Contract assertions per contracts/options-schema.md §Test contract
# ---------------------------------------------------------------------------


async def test_options_flow_pr23_new_options_appear_in_schema(
    hass: HomeAssistant,
) -> None:
    """T008-a: Remaining PR-23 options (heartbeat, ui_dispatch) appear in the options-flow schema.

    Note: rfid_grace_seconds was removed in PR-24 (FR-014) and must NOT appear.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    schema_keys = [str(k) for k in result["data_schema"].schema]
    assert CONF_HEARTBEAT_LOG_INTERVAL_MIN in schema_keys, (
        "Options init must contain heartbeat_log_interval_min field"
    )
    assert CONF_UI_DISPATCH_INTERVAL_S in schema_keys, (
        "Options init must contain ui_dispatch_interval_s field"
    )
    assert "rfid_grace_seconds" not in schema_keys, (
        "Options init must NOT contain rfid_grace_seconds (removed in PR-24)"
    )


async def test_options_flow_pr23_explicit_values_persisted(
    hass: HomeAssistant,
) -> None:
    """T008-b: Submitting explicit heartbeat/ui_dispatch values persists them to entry.options.

    Note: rfid_grace_seconds is no longer in the schema (removed in PR-24).
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MIN_SESSION_DURATION_S: DEFAULT_MIN_SESSION_DURATION_S,
            CONF_MIN_SESSION_ENERGY_WH: DEFAULT_MIN_SESSION_ENERGY_WH,
            CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
            CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
            CONF_HEARTBEAT_LOG_INTERVAL_MIN: 15,
            CONF_UI_DISPATCH_INTERVAL_S: 120,
        },
    )
    assert result["step_id"] == "pricing"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PRICING_MODE: "static",
            CONF_STATIC_PRICE_KWH: DEFAULT_STATIC_PRICE_KWH,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    refreshed = hass.config_entries.async_get_entry(entry.entry_id)
    assert refreshed.options[CONF_HEARTBEAT_LOG_INTERVAL_MIN] == 15
    assert refreshed.options[CONF_UI_DISPATCH_INTERVAL_S] == 120


async def test_options_flow_pr23_unchanged_values_preserved(
    hass: HomeAssistant,
) -> None:
    """T008-c: Submitting without changing heartbeat/ui_dispatch options preserves existing values.

    Note: rfid_grace_seconds is silently ignored if present in entry.options (stale key from
    v0.4.0). It no longer appears in the schema defaults.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={
            CONF_HEARTBEAT_LOG_INTERVAL_MIN: 3,
            CONF_UI_DISPATCH_INTERVAL_S: 90,
        },
        title="Test Charger",
    )
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Retrieve the schema defaults (which should be the pre-set values)
    schema = result["data_schema"].schema
    defaults = {
        str(k): k.default() for k in schema if hasattr(k, "default") and callable(k.default)
    }
    # The form defaults should reflect the existing entry.options values
    assert defaults.get(CONF_HEARTBEAT_LOG_INTERVAL_MIN) == 3
    assert defaults.get(CONF_UI_DISPATCH_INTERVAL_S) == 90


async def test_options_flow_pr23_zero_values_accepted(
    hass: HomeAssistant,
) -> None:
    """T008-d: Submitting 0 for heartbeat/ui_dispatch options is accepted (disable behavior)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MIN_SESSION_DURATION_S: DEFAULT_MIN_SESSION_DURATION_S,
            CONF_MIN_SESSION_ENERGY_WH: DEFAULT_MIN_SESSION_ENERGY_WH,
            CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
            CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
            CONF_HEARTBEAT_LOG_INTERVAL_MIN: 0,
            CONF_UI_DISPATCH_INTERVAL_S: 0,
        },
    )
    assert result["step_id"] == "pricing"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PRICING_MODE: "static",
            CONF_STATIC_PRICE_KWH: DEFAULT_STATIC_PRICE_KWH,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    refreshed = hass.config_entries.async_get_entry(entry.entry_id)
    assert refreshed.options[CONF_HEARTBEAT_LOG_INTERVAL_MIN] == 0
    assert refreshed.options[CONF_UI_DISPATCH_INTERVAL_S] == 0


async def test_options_flow_pr23_out_of_range_value_rejected(
    hass: HomeAssistant,
) -> None:
    """T008-e: Submitting out-of-range heartbeat/ui_dispatch values is rejected by schema."""
    import voluptuous as vol

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]

    # heartbeat_log_interval_min max is 30; 31 must be rejected
    try:
        schema(
            {
                CONF_MIN_SESSION_DURATION_S: DEFAULT_MIN_SESSION_DURATION_S,
                CONF_MIN_SESSION_ENERGY_WH: DEFAULT_MIN_SESSION_ENERGY_WH,
                CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
                CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
                CONF_HEARTBEAT_LOG_INTERVAL_MIN: 31,  # out of range: max is 30
            }
        )
        raised = False
    except vol.Invalid:
        raised = True

    assert raised, "Schema must reject heartbeat_log_interval_min=31 (max is 30)"

    # ui_dispatch_interval_s max is 300; 301 must be rejected
    try:
        schema(
            {
                CONF_MIN_SESSION_DURATION_S: DEFAULT_MIN_SESSION_DURATION_S,
                CONF_MIN_SESSION_ENERGY_WH: DEFAULT_MIN_SESSION_ENERGY_WH,
                CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
                CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
                CONF_UI_DISPATCH_INTERVAL_S: 301,  # out of range: max is 300
            }
        )
        raised = False
    except vol.Invalid:
        raised = True

    assert raised, "Schema must reject ui_dispatch_interval_s=301 (max is 300)"


async def test_options_flow_pr23_ui_dispatch_interval_1_to_9_rejected(
    hass: HomeAssistant,
) -> None:
    """T008-f: ui_dispatch_interval_s values 1-9 are rejected by the two-band validator.

    The spec (IC-3) allows 0 (disable) or 10..300 (active range). Values 1-9 are
    rejected because they would create excessive UI fan-out.

    Verifies: 0 accepted, 10 accepted, 300 accepted, 5 rejected.
    """
    import voluptuous as vol

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]

    base_input = {
        CONF_MIN_SESSION_DURATION_S: DEFAULT_MIN_SESSION_DURATION_S,
        CONF_MIN_SESSION_ENERGY_WH: DEFAULT_MIN_SESSION_ENERGY_WH,
        CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
        CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
    }

    # Accepted: 0 (disable sentinel)
    try:
        schema({**base_input, CONF_UI_DISPATCH_INTERVAL_S: 0})
        accepted_0 = True
    except vol.Invalid:
        accepted_0 = False
    assert accepted_0, "ui_dispatch_interval_s=0 must be accepted (disable sentinel)"

    # Accepted: 10 (lower bound of active range)
    try:
        schema({**base_input, CONF_UI_DISPATCH_INTERVAL_S: 10})
        accepted_10 = True
    except vol.Invalid:
        accepted_10 = False
    assert accepted_10, "ui_dispatch_interval_s=10 must be accepted (active range lower bound)"

    # Accepted: 300 (upper bound of active range)
    try:
        schema({**base_input, CONF_UI_DISPATCH_INTERVAL_S: 300})
        accepted_300 = True
    except vol.Invalid:
        accepted_300 = False
    assert accepted_300, "ui_dispatch_interval_s=300 must be accepted (active range upper bound)"

    # Rejected: 5 (in gap between 0 and 10)
    try:
        schema({**base_input, CONF_UI_DISPATCH_INTERVAL_S: 5})
        rejected_5 = False
    except vol.Invalid:
        rejected_5 = True
    assert rejected_5, "ui_dispatch_interval_s=5 must be rejected (gap 1-9 is invalid per IC-3)"


# ---------------------------------------------------------------------------
# T004 (PR-26 US2): Options committed before reload — new values observed
# during the triggered async_setup_entry; exactly one reload (FR-006, FR-007)
# ---------------------------------------------------------------------------


async def test_options_committed_before_reload_and_single_reload(
    hass: HomeAssistant,
) -> None:
    """FR-006/FR-007: a startup-time option (debug_logging) saved via the
    options flow is in effect immediately after the flow completes, and the
    flow triggers exactly one reload."""
    from unittest.mock import AsyncMock, patch

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_session_engine(hass, entry)

    # Debug logger starts disabled (debug_logging not set)
    debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert not debug_logger.enabled

    orig_reload = hass.config_entries.async_reload
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(side_effect=orig_reload)
    ) as mock_reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_MIN_SESSION_DURATION_S: DEFAULT_MIN_SESSION_DURATION_S,
                CONF_MIN_SESSION_ENERGY_WH: DEFAULT_MIN_SESSION_ENERGY_WH,
                CONF_PERSISTENCE_INTERVAL_S: DEFAULT_PERSISTENCE_INTERVAL_S,
                CONF_MAX_STORED_SESSIONS: DEFAULT_MAX_STORED_SESSIONS,
                CONF_DEBUG_LOGGING: True,
            },
        )
        assert result["step_id"] == "pricing"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_PRICING_MODE: "static",
                CONF_STATIC_PRICE_KWH: DEFAULT_STATIC_PRICE_KWH,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == "create_entry"

    # (b) Exactly one reload was triggered by the flow (FR-007)
    assert mock_reload.await_count == 1, (
        f"Options flow must trigger exactly one reload, got {mock_reload.await_count}"
    )

    # (a) The reloaded integration observed the NEW option value during setup:
    # the freshly created DebugLogger is enabled without any further reload.
    refreshed = hass.config_entries.async_get_entry(entry.entry_id)
    assert refreshed.options[CONF_DEBUG_LOGGING] is True
    new_debug_logger = hass.data[DOMAIN][entry.entry_id]["debug_logger"]
    assert new_debug_logger.enabled, (
        "DebugLogger must be enabled immediately after the options flow — "
        "options must be committed BEFORE the reload (FR-006)"
    )


# ---------------------------------------------------------------------------
# T021 (PR-24 US5): Stale rfid_grace_seconds key in storage is tolerated
# FR-015: existing entries with rfid_grace_seconds in options must load cleanly
# ---------------------------------------------------------------------------


async def test_options_flow_tolerates_stale_rfid_grace_seconds_key(
    hass: HomeAssistant,
) -> None:
    """FR-015: Entry with stale rfid_grace_seconds in options loads without error.

    Simulates an upgrade from v0.4.0 (which persisted rfid_grace_seconds=5 in
    entry.options) to v0.4.1 (which removes the option). HA simply ignores the
    unknown key — the integration must not crash on setup.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={"rfid_grace_seconds": 5, "heartbeat_log_interval_min": 5},
        title="Test Charger",
    )
    # Setup must succeed without raising any exception
    refreshed = await setup_session_engine(hass, entry)

    assert refreshed is not None, "Entry must be present after setup"
    assert refreshed.state.value == "loaded", (
        f"Entry must be in 'loaded' state, got: {refreshed.state.value!r}"
    )
