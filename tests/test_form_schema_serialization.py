"""Every form the integration shows must survive HA's frontend serialization.

HA's flow HTTP views run each form's data_schema through
voluptuous_serialize.convert(..., custom_serializer=cv.custom_serializer). A
validator it cannot convert (e.g. a plain function) makes the view raise and the
UI shows "500 Internal Server Error" — v0.4.0–v0.4.6 shipped exactly that in
the options flow. These tests drive each flow to each of its forms and run the
same conversion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import voluptuous_serialize
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.const import (
    CONF_CAR_STATUS_CHARGING_VALUE,
    CONF_CAR_STATUS_ENTITY,
    CONF_CHARGER_HOST,
    CONF_CHARGER_NAME,
    CONF_CHARGER_PROFILE,
    CONF_CHARGER_SERIAL,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_UNIT,
    CONF_PLUG_ENTITY,
    CONF_POWER_ENTITY,
    CONF_PRICING_MODE,
    CONF_SPOT_ADDITIONAL_COST_KWH,
    CONF_SPOT_FALLBACK_PRICE_KWH,
    CONF_SPOT_PRICE_ENTITY,
    CONF_SPOT_VAT_MULTIPLIER,
    CONF_STATIC_PRICE_KWH,
    CONF_UI_DISPATCH_INTERVAL_S,
    DEFAULT_SPOT_ADDITIONAL_COST_KWH,
    DEFAULT_SPOT_FALLBACK_PRICE_KWH,
    DEFAULT_SPOT_VAT_MULTIPLIER,
    DOMAIN,
)
from custom_components.ev_charging_manager.rfid_discovery import DiscoveredCard

from .conftest import MOCK_CHARGER_DATA, setup_entry_with_subentries, setup_session_engine


def _assert_serializable(result: dict, step_id: str) -> None:
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == step_id
    voluptuous_serialize.convert(result["data_schema"], custom_serializer=cv.custom_serializer)


def _subentry_id(hass: HomeAssistant, entry_id: str, subentry_type: str, name: str) -> str:
    entry = hass.config_entries.async_get_entry(entry_id)
    return next(
        s.subentry_id
        for s in entry.subentries.values()
        if s.subentry_type == subentry_type and s.data["name"] == name
    )


async def test_config_flow_forms_serializable(hass: HomeAssistant) -> None:
    """user, serial, charger_entities (go-e + generic), pricing, spot_config, confirm."""
    hass.states.async_set("sensor.goe_abc123_car_value", "Idle")
    hass.states.async_set("sensor.goe_abc123_wh", "0.0")
    hass.states.async_set("sensor.goe_abc123_nrg_11", "0")
    hass.states.async_set("sensor.spot_price", "0.89")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    _assert_serializable(result, "user")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGER_PROFILE: "goe_gemini"}
    )
    _assert_serializable(result, "serial")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGER_SERIAL: "abc123"}
    )
    _assert_serializable(result, "charger_entities")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CAR_STATUS_ENTITY: "sensor.goe_abc123_car_value",
            CONF_CAR_STATUS_CHARGING_VALUE: "Charging",
            CONF_ENERGY_ENTITY: "sensor.goe_abc123_wh",
            CONF_ENERGY_UNIT: "kWh",
            CONF_POWER_ENTITY: "sensor.goe_abc123_nrg_11",
            CONF_CHARGER_NAME: "My go-e Charger",
            CONF_CHARGER_HOST: "192.168.1.100",
        },
    )
    _assert_serializable(result, "pricing")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICING_MODE: "spot", CONF_STATIC_PRICE_KWH: 2.50}
    )
    _assert_serializable(result, "spot_config")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SPOT_PRICE_ENTITY: "sensor.spot_price",
            CONF_SPOT_ADDITIONAL_COST_KWH: DEFAULT_SPOT_ADDITIONAL_COST_KWH,
            CONF_SPOT_VAT_MULTIPLIER: DEFAULT_SPOT_VAT_MULTIPLIER,
            CONF_SPOT_FALLBACK_PRICE_KWH: DEFAULT_SPOT_FALLBACK_PRICE_KWH,
        },
    )
    _assert_serializable(result, "confirm")

    # The generic profile builds charger_entities without profile suggestions.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGER_PROFILE: "generic"}
    )
    _assert_serializable(result, "charger_entities")


async def test_options_flow_forms_serializable_static(hass: HomeAssistant) -> None:
    """init (stored options + re-shown with error) and pricing for a static-priced entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={CONF_PLUG_ENTITY: "binary_sensor.goe_abc123_car_0"},
        title="Test Charger",
    )
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    _assert_serializable(result, "init")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UI_DISPATCH_INTERVAL_S: 5}
    )
    _assert_serializable(result, "init")
    assert result["errors"] == {CONF_UI_DISPATCH_INTERVAL_S: "ui_dispatch_interval_invalid"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UI_DISPATCH_INTERVAL_S: 60}
    )
    _assert_serializable(result, "pricing")


async def test_options_flow_pricing_form_serializable_spot(hass: HomeAssistant) -> None:
    """pricing for a spot-priced entry (suggested spot entity added to the schema)."""
    spot_data = {
        **MOCK_CHARGER_DATA,
        CONF_PRICING_MODE: "spot",
        CONF_SPOT_PRICE_ENTITY: "sensor.spot_price",
        CONF_SPOT_ADDITIONAL_COST_KWH: DEFAULT_SPOT_ADDITIONAL_COST_KWH,
        CONF_SPOT_VAT_MULTIPLIER: DEFAULT_SPOT_VAT_MULTIPLIER,
        CONF_SPOT_FALLBACK_PRICE_KWH: DEFAULT_SPOT_FALLBACK_PRICE_KWH,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=spot_data, title="Test Charger")
    hass.states.async_set("sensor.spot_price", "0.89")
    await setup_session_engine(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    _assert_serializable(result, "pricing")


async def test_vehicle_subentry_forms_serializable(hass: HomeAssistant) -> None:
    """vehicle: user (add) and reconfigure."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_entry_with_subentries(hass, entry)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "vehicle"), context={"source": "user"}
    )
    _assert_serializable(result, "user")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Car", "battery_capacity_kwh": 60.0, "charging_phases": "3"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    vehicle_id = _subentry_id(hass, entry.entry_id, "vehicle", "Car")

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "vehicle"),
        context={"source": "reconfigure", "subentry_id": vehicle_id},
    )
    _assert_serializable(result, "reconfigure")


async def test_user_subentry_forms_serializable(hass: HomeAssistant) -> None:
    """user: user, guest_pricing (add); reconfigure, reconfigure_guest_pricing (edit)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_entry_with_subentries(hass, entry)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"), context={"source": "user"}
    )
    _assert_serializable(result, "user")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Guest", "type": "guest"}
    )
    _assert_serializable(result, "guest_pricing")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"guest_pricing_method": "fixed", "price_per_kwh": 4.5}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    guest_id = _subentry_id(hass, entry.entry_id, "user", "Guest")

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"),
        context={"source": "reconfigure", "subentry_id": guest_id},
    )
    _assert_serializable(result, "reconfigure")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Guest", "active": True}
    )
    _assert_serializable(result, "reconfigure_guest_pricing")


async def test_rfid_mapping_subentry_forms_serializable(hass: HomeAssistant) -> None:
    """rfid_mapping: manual, select_card, map_card (add); reconfigure (edit).

    A vehicle exists so the optional vehicle_id field is part of every schema.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, CONF_CHARGER_PROFILE: "goe_gemini"},
        title="Test Charger",
    )
    await setup_entry_with_subentries(hass, entry)
    for flow_type, user_input in (
        ("user", {"name": "Paul", "type": "regular"}),
        ("vehicle", {"name": "Car", "battery_capacity_kwh": 60.0, "charging_phases": "3"}),
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, flow_type), context={"source": "user"}
        )
        result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
        assert result["type"] == FlowResultType.CREATE_ENTRY
    user_id = _subentry_id(hass, entry.entry_id, "user", "Paul")

    provider = MagicMock()
    provider.get_programmed_cards = AsyncMock(
        return_value=[DiscoveredCard(index=0, name="Paul", energy_kwh=1.5, is_programmed=True)]
    )
    provider.get_last_rfid_uid = AsyncMock(return_value="04:B7:C8")
    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=provider,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )
    _assert_serializable(result, "select_card")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"card_index": "0"}
    )
    _assert_serializable(result, "map_card")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"user_id": user_id}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mapping_id = next(
        s.subentry_id
        for s in hass.config_entries.async_get_entry(entry.entry_id).subentries.values()
        if s.subentry_type == "rfid_mapping"
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rfid_mapping"),
        context={"source": "reconfigure", "subentry_id": mapping_id},
    )
    _assert_serializable(result, "reconfigure")

    # Discovery unavailable -> the manual form.
    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=None,
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "rfid_mapping"), context={"source": "user"}
        )
    _assert_serializable(result, "manual")
