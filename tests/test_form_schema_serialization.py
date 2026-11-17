"""Every form the integration shows must survive HA's frontend serialization.

HA's flow HTTP views convert each form's data_schema to a field list for the
frontend and JSON-encode the response. A validator the converter cannot handle
(e.g. a plain function) makes the view raise and the UI shows "500 Internal
Server Error" — v0.4.0–v0.4.6 shipped exactly that in the options flow. These
tests drive every form through those views, so they follow whatever converter
the installed HA uses.
"""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

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

_FLOW = "/api/config/config_entries/flow"
_OPTIONS_FLOW = "/api/config/config_entries/options/flow"
_SUBENTRY_FLOW = "/api/config/config_entries/subentries/flow"


@pytest.fixture
async def client(hass: HomeAssistant, hass_client: ClientSessionGenerator):
    assert await async_setup_component(hass, "config", {})
    return await hass_client()


async def _post(client, path: str, payload: dict, step_id: str | None = None) -> dict:
    """POST to a flow view; expect the form ``step_id``, or a created entry if None."""
    resp = await client.post(path, json=payload)
    assert resp.status == HTTPStatus.OK, await resp.text()
    result = await resp.json()
    if step_id is None:
        assert result["type"] == FlowResultType.CREATE_ENTRY
    else:
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == step_id
    return result


def _subentry_id(hass: HomeAssistant, entry_id: str, subentry_type: str, name: str) -> str:
    entry = hass.config_entries.async_get_entry(entry_id)
    return next(
        s.subentry_id
        for s in entry.subentries.values()
        if s.subentry_type == subentry_type and s.data["name"] == name
    )


async def test_config_flow_forms_serializable(hass: HomeAssistant, client) -> None:
    """user, serial, charger_entities (go-e + generic), pricing, spot_config, confirm."""
    hass.states.async_set("sensor.goe_abc123_car_value", "Idle")
    hass.states.async_set("sensor.goe_abc123_wh", "0.0")
    hass.states.async_set("sensor.goe_abc123_nrg_11", "0")
    hass.states.async_set("sensor.spot_price", "0.89")

    result = await _post(client, _FLOW, {"handler": DOMAIN}, "user")
    step = f"{_FLOW}/{result['flow_id']}"

    await _post(client, step, {CONF_CHARGER_PROFILE: "goe_gemini"}, "serial")
    await _post(client, step, {CONF_CHARGER_SERIAL: "abc123"}, "charger_entities")
    await _post(
        client,
        step,
        {
            CONF_CAR_STATUS_ENTITY: "sensor.goe_abc123_car_value",
            CONF_CAR_STATUS_CHARGING_VALUE: "Charging",
            CONF_ENERGY_ENTITY: "sensor.goe_abc123_wh",
            CONF_ENERGY_UNIT: "kWh",
            CONF_POWER_ENTITY: "sensor.goe_abc123_nrg_11",
            CONF_CHARGER_NAME: "My go-e Charger",
            CONF_CHARGER_HOST: "192.168.1.100",
        },
        "pricing",
    )
    await _post(
        client, step, {CONF_PRICING_MODE: "spot", CONF_STATIC_PRICE_KWH: 2.50}, "spot_config"
    )
    await _post(
        client,
        step,
        {
            CONF_SPOT_PRICE_ENTITY: "sensor.spot_price",
            CONF_SPOT_ADDITIONAL_COST_KWH: DEFAULT_SPOT_ADDITIONAL_COST_KWH,
            CONF_SPOT_VAT_MULTIPLIER: DEFAULT_SPOT_VAT_MULTIPLIER,
            CONF_SPOT_FALLBACK_PRICE_KWH: DEFAULT_SPOT_FALLBACK_PRICE_KWH,
        },
        "confirm",
    )

    # The generic profile builds charger_entities without profile suggestions.
    result = await _post(client, _FLOW, {"handler": DOMAIN}, "user")
    await _post(
        client,
        f"{_FLOW}/{result['flow_id']}",
        {CONF_CHARGER_PROFILE: "generic"},
        "charger_entities",
    )


async def test_options_flow_forms_serializable_static(hass: HomeAssistant, client) -> None:
    """init (stored options + re-shown with error) and pricing for a static-priced entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CHARGER_DATA,
        options={CONF_PLUG_ENTITY: "binary_sensor.goe_abc123_car_0"},
        title="Test Charger",
    )
    await setup_session_engine(hass, entry)

    result = await _post(client, _OPTIONS_FLOW, {"handler": entry.entry_id}, "init")
    step = f"{_OPTIONS_FLOW}/{result['flow_id']}"

    result = await _post(client, step, {CONF_UI_DISPATCH_INTERVAL_S: 5}, "init")
    assert result["errors"] == {CONF_UI_DISPATCH_INTERVAL_S: "ui_dispatch_interval_invalid"}

    await _post(client, step, {CONF_UI_DISPATCH_INTERVAL_S: 60}, "pricing")


async def test_options_flow_pricing_form_serializable_spot(hass: HomeAssistant, client) -> None:
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

    result = await _post(client, _OPTIONS_FLOW, {"handler": entry.entry_id}, "init")
    await _post(client, f"{_OPTIONS_FLOW}/{result['flow_id']}", {}, "pricing")


async def test_vehicle_subentry_forms_serializable(hass: HomeAssistant, client) -> None:
    """vehicle: user (add) and reconfigure."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_entry_with_subentries(hass, entry)

    result = await _post(client, _SUBENTRY_FLOW, {"handler": [entry.entry_id, "vehicle"]}, "user")
    await _post(
        client,
        f"{_SUBENTRY_FLOW}/{result['flow_id']}",
        {"name": "Car", "battery_capacity_kwh": 60.0, "charging_phases": "3"},
    )
    vehicle_id = _subentry_id(hass, entry.entry_id, "vehicle", "Car")

    await _post(
        client,
        _SUBENTRY_FLOW,
        {"handler": [entry.entry_id, "vehicle"], "subentry_id": vehicle_id},
        "reconfigure",
    )


async def test_user_subentry_forms_serializable(hass: HomeAssistant, client) -> None:
    """user: user, guest_pricing (add); reconfigure, reconfigure_guest_pricing (edit)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CHARGER_DATA, title="Test Charger")
    await setup_entry_with_subentries(hass, entry)

    result = await _post(client, _SUBENTRY_FLOW, {"handler": [entry.entry_id, "user"]}, "user")
    step = f"{_SUBENTRY_FLOW}/{result['flow_id']}"
    await _post(client, step, {"name": "Visitor", "type": "guest"}, "guest_pricing")
    await _post(client, step, {"guest_pricing_method": "fixed", "price_per_kwh": 4.5})
    guest_id = _subentry_id(hass, entry.entry_id, "user", "Visitor")

    result = await _post(
        client,
        _SUBENTRY_FLOW,
        {"handler": [entry.entry_id, "user"], "subentry_id": guest_id},
        "reconfigure",
    )
    await _post(
        client,
        f"{_SUBENTRY_FLOW}/{result['flow_id']}",
        {"name": "Visitor", "active": True},
        "reconfigure_guest_pricing",
    )


async def test_rfid_mapping_subentry_forms_serializable(hass: HomeAssistant, client) -> None:
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
        result = await _post(
            client, _SUBENTRY_FLOW, {"handler": [entry.entry_id, "rfid_mapping"]}, "select_card"
        )
    step = f"{_SUBENTRY_FLOW}/{result['flow_id']}"
    await _post(client, step, {"card_index": "0"}, "map_card")
    await _post(client, step, {"user_id": user_id})
    mapping_id = next(
        s.subentry_id
        for s in hass.config_entries.async_get_entry(entry.entry_id).subentries.values()
        if s.subentry_type == "rfid_mapping"
    )

    await _post(
        client,
        _SUBENTRY_FLOW,
        {"handler": [entry.entry_id, "rfid_mapping"], "subentry_id": mapping_id},
        "reconfigure",
    )

    # Discovery unavailable -> the manual form.
    with patch(
        "custom_components.ev_charging_manager.config_flow.get_discovery_provider",
        return_value=None,
    ):
        await _post(client, _SUBENTRY_FLOW, {"handler": [entry.entry_id, "rfid_mapping"]}, "manual")
