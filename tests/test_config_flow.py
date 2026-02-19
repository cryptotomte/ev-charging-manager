"""Tests for EV Charging Manager config flow."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ev_charging_manager.charger_profiles import CHARGER_PROFILES
from custom_components.ev_charging_manager.const import (
    CONF_CAR_STATUS_CHARGING_VALUE,
    CONF_CAR_STATUS_ENTITY,
    CONF_CHARGER_HOST,
    CONF_CHARGER_NAME,
    CONF_CHARGER_PROFILE,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_UNIT,
    CONF_POWER_ENTITY,
    CONF_PRICING_MODE,
    CONF_RFID_ENTITY,
    CONF_RFID_UID_ENTITY,
    CONF_STATIC_PRICE_KWH,
    CONF_TOTAL_ENERGY_ENTITY,
    DOMAIN,
)

# ---------------------------------------------------------------------------
# User Story 1 — Known charger (go-e) happy path
# ---------------------------------------------------------------------------


async def test_config_flow_goe_happy_path(hass: HomeAssistant) -> None:
    """Full 4-step config flow with go-e Gemini profile."""
    # Register 5 go-e sensor entities with valid states
    hass.states.async_set("sensor.goe_abc123_car_0", "2")
    hass.states.async_set("sensor.goe_abc123_wh", "1000")
    hass.states.async_set("sensor.goe_abc123_nrg_total_power", "7400")
    hass.states.async_set("sensor.goe_abc123_trx", "none")
    hass.states.async_set("sensor.goe_abc123_lri", "DEADBEEF")

    # Step 0 — init flow, should show charger_type form
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_type"

    # Step 0 — submit profile selection
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CHARGER_PROFILE: "goe_gemini"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_entities"

    # Step 1 — submit go-e entity mapping
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CAR_STATUS_ENTITY: "sensor.goe_abc123_car_0",
            CONF_CAR_STATUS_CHARGING_VALUE: 2,
            CONF_ENERGY_ENTITY: "sensor.goe_abc123_wh",
            CONF_ENERGY_UNIT: "Wh",
            CONF_POWER_ENTITY: "sensor.goe_abc123_nrg_total_power",
            CONF_RFID_ENTITY: "sensor.goe_abc123_trx",
            CONF_RFID_UID_ENTITY: "sensor.goe_abc123_lri",
            CONF_CHARGER_NAME: "My go-e Charger",
            CONF_CHARGER_HOST: "192.168.1.100",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "pricing"

    # Step 2 — submit pricing
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PRICING_MODE: "static", CONF_STATIC_PRICE_KWH: 2.50},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm"

    # Step 3 — confirm
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY

    data = result["data"]
    assert data[CONF_CHARGER_PROFILE] == "goe_gemini"
    assert data[CONF_CAR_STATUS_ENTITY] == "sensor.goe_abc123_car_0"
    assert data[CONF_CAR_STATUS_CHARGING_VALUE] == 2
    assert data[CONF_ENERGY_ENTITY] == "sensor.goe_abc123_wh"
    assert data[CONF_ENERGY_UNIT] == "Wh"
    assert data[CONF_POWER_ENTITY] == "sensor.goe_abc123_nrg_total_power"
    assert data[CONF_RFID_ENTITY] == "sensor.goe_abc123_trx"
    assert data[CONF_RFID_UID_ENTITY] == "sensor.goe_abc123_lri"
    assert data[CONF_CHARGER_HOST] == "192.168.1.100"
    assert data[CONF_CHARGER_NAME] == "My go-e Charger"
    assert data[CONF_PRICING_MODE] == "static"
    assert data[CONF_STATIC_PRICE_KWH] == 2.50
    assert data[CONF_TOTAL_ENERGY_ENTITY] is None


# ---------------------------------------------------------------------------
# User Story 2 — Manual / generic profile
# ---------------------------------------------------------------------------


async def test_config_flow_generic_profile(hass: HomeAssistant) -> None:
    """Config flow with generic profile — all fields manual, optionals empty."""
    hass.states.async_set("sensor.my_car_status", "charging")
    hass.states.async_set("sensor.my_energy", "500")
    hass.states.async_set("sensor.my_power", "3700")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CHARGER_PROFILE: "generic"},
    )
    assert result["step_id"] == "charger_entities"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CAR_STATUS_ENTITY: "sensor.my_car_status",
            CONF_CAR_STATUS_CHARGING_VALUE: "charging",
            CONF_ENERGY_ENTITY: "sensor.my_energy",
            CONF_ENERGY_UNIT: "kWh",
            CONF_POWER_ENTITY: "sensor.my_power",
            CONF_CHARGER_NAME: "My Generic Charger",
        },
    )
    assert result["step_id"] == "pricing"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PRICING_MODE: "static", CONF_STATIC_PRICE_KWH: 1.99},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_CHARGER_PROFILE] == "generic"
    assert data[CONF_CAR_STATUS_CHARGING_VALUE] == "charging"
    assert data[CONF_RFID_ENTITY] is None
    assert data[CONF_RFID_UID_ENTITY] is None
    assert data[CONF_CHARGER_HOST] is None


async def test_config_flow_zaptec_maps_to_generic(hass: HomeAssistant) -> None:
    """Zaptec profile has no pre-fills but stores distinct 'zaptec' key."""
    hass.states.async_set("sensor.zaptec_status", "connected")
    hass.states.async_set("sensor.zaptec_energy", "0")
    hass.states.async_set("sensor.zaptec_power", "0")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CHARGER_PROFILE: "zaptec"},
    )
    assert result["step_id"] == "charger_entities"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CAR_STATUS_ENTITY: "sensor.zaptec_status",
            CONF_CAR_STATUS_CHARGING_VALUE: "charging",
            CONF_ENERGY_ENTITY: "sensor.zaptec_energy",
            CONF_ENERGY_UNIT: "kWh",
            CONF_POWER_ENTITY: "sensor.zaptec_power",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PRICING_MODE: "static", CONF_STATIC_PRICE_KWH: 2.00},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # Profile key must be "zaptec", not "generic"
    assert result["data"][CONF_CHARGER_PROFILE] == "zaptec"


# ---------------------------------------------------------------------------
# User Story 3 — Sensor validation
# ---------------------------------------------------------------------------


async def test_config_flow_entity_not_found(hass: HomeAssistant) -> None:
    """Entering a non-existent entity shows entity_not_found error."""
    hass.states.async_set("sensor.good_energy", "100")
    hass.states.async_set("sensor.good_power", "3000")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGER_PROFILE: "generic"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CAR_STATUS_ENTITY: "sensor.does_not_exist",
            CONF_CAR_STATUS_CHARGING_VALUE: "charging",
            CONF_ENERGY_ENTITY: "sensor.good_energy",
            CONF_ENERGY_UNIT: "kWh",
            CONF_POWER_ENTITY: "sensor.good_power",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_entities"
    assert result["errors"][CONF_CAR_STATUS_ENTITY] == "entity_not_found"


async def test_config_flow_entity_unavailable(hass: HomeAssistant) -> None:
    """Entity with 'unavailable' state shows entity_unavailable error."""
    hass.states.async_set("sensor.unavailable_car", "unavailable")
    hass.states.async_set("sensor.good_energy", "100")
    hass.states.async_set("sensor.good_power", "3000")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGER_PROFILE: "generic"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CAR_STATUS_ENTITY: "sensor.unavailable_car",
            CONF_CAR_STATUS_CHARGING_VALUE: "charging",
            CONF_ENERGY_ENTITY: "sensor.good_energy",
            CONF_ENERGY_UNIT: "kWh",
            CONF_POWER_ENTITY: "sensor.good_power",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_CAR_STATUS_ENTITY] == "entity_unavailable"


async def test_config_flow_invalid_optional_valid_mandatory(
    hass: HomeAssistant,
) -> None:
    """Invalid optional entity shows error only on that field; mandatory pass."""
    hass.states.async_set("sensor.good_car", "2")
    hass.states.async_set("sensor.good_energy", "100")
    hass.states.async_set("sensor.good_power", "3000")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGER_PROFILE: "generic"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CAR_STATUS_ENTITY: "sensor.good_car",
            CONF_CAR_STATUS_CHARGING_VALUE: "2",
            CONF_ENERGY_ENTITY: "sensor.good_energy",
            CONF_ENERGY_UNIT: "Wh",
            CONF_POWER_ENTITY: "sensor.good_power",
            CONF_RFID_ENTITY: "sensor.nonexistent_rfid",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert CONF_CAR_STATUS_ENTITY not in result["errors"]
    assert CONF_ENERGY_ENTITY not in result["errors"]
    assert CONF_POWER_ENTITY not in result["errors"]
    assert result["errors"][CONF_RFID_ENTITY] == "entity_not_found"


# ---------------------------------------------------------------------------
# Phase 6 — Profile structure validation
# ---------------------------------------------------------------------------


def test_charger_profiles_structure() -> None:
    """CHARGER_PROFILES contains all required profiles with correct attributes."""
    assert "goe_gemini" in CHARGER_PROFILES
    assert "easee_home" in CHARGER_PROFILES
    assert "zaptec" in CHARGER_PROFILES
    assert "generic" in CHARGER_PROFILES

    goe = CHARGER_PROFILES["goe_gemini"]
    assert goe["car_status_charging_value"] == 2
    assert goe["requires_charger_host"] is True
    assert "{serial}" in goe["car_status_sensor"]

    generic = CHARGER_PROFILES["generic"]
    assert generic["requires_charger_host"] is False
    assert generic["car_status_sensor"] is None
