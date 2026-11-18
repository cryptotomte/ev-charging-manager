"""The currency of the cost sensors (spec 028 § 7; the recorder cases are in
test_currency_statistics.py).

The seven cost and price entities take their unit from ``entry_currency``; an entry
without the ``currency`` key is an installation created before the setting and shows
``kr`` (D-60).
"""

from __future__ import annotations

import copy
from types import MappingProxyType

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.generated.currencies import ACTIVE_CURRENCIES
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.config_flow import EvChargingManagerConfigFlow
from custom_components.ev_charging_manager.const import (
    CONF_CURRENCY,
    CONF_PRICING_MODE,
    CONF_SPOT_ADDITIONAL_COST_KWH,
    CONF_SPOT_FALLBACK_PRICE_KWH,
    CONF_SPOT_PRICE_ENTITY,
    CONF_SPOT_VAT_MULTIPLIER,
    CONF_STATIC_PRICE_KWH,
    DOMAIN,
)
from tests.conftest import MOCK_CHARGER_DATA, setup_session_engine
from tests.test_config_flow import _setup_to_pricing_step

USERS = [
    {
        "subentry_id": "u_paul",
        "subentry_type": "user",
        "title": "Paul",
        "unique_id": None,
        "data": {"name": "Paul", "type": "regular", "active": True},
    },
    {
        "subentry_id": "u_gaest",
        "subentry_type": "user",
        "title": "Gaest",
        "unique_id": None,
        "data": {
            "name": "Gaest",
            "type": "guest",
            "active": True,
            "guest_pricing": {"method": "fixed", "price_per_kwh": 5.0},
        },
    },
]

# unique_id suffixes of the seven cost and price entities with USERS (spec 028 § 2.1).
COST_SENSORS = {
    "session_cost",
    "session_charge_price",
    "paul_total_cost",
    "gaest_total_cost",
    "unknown_total_cost",
    "guest_last_charge_price",
    "guest_total_cost",
}


def cost_units(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, tuple]:
    """unique_id suffix -> (state unit, registry unit) of every monetary entity of the entry."""
    registry = er.async_get(hass)
    out = {}
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.original_device_class != "monetary":
            continue
        state = hass.states.get(reg.entity_id)
        out[reg.unique_id.removeprefix(f"{entry.entry_id}_")] = (
            state.attributes.get("unit_of_measurement"),
            reg.unit_of_measurement,
        )
    return out


def assert_unit(hass: HomeAssistant, entry: MockConfigEntry, unit: str, names=COST_SENSORS):
    assert cost_units(hass, entry) == dict.fromkeys(names, (unit, unit))


async def setup_legacy_entry(hass: HomeAssistant, **data) -> MockConfigEntry:
    """An entry as 0.4.7 left it: no ``currency`` key unless given, a regular and a guest user."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, **data},
        title="My go-e Charger",
        subentries_data=copy.deepcopy(USERS),
    )
    await setup_session_engine(hass, entry)
    return entry


async def test_entry_without_key_shows_kr(hass: HomeAssistant) -> None:
    """T-1: the pin on today's contract, state and registry, users and Unknown present."""
    entry = await setup_legacy_entry(hass)

    assert CONF_CURRENCY not in entry.data
    assert_unit(hass, entry, "kr")


async def _add_user(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> None:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"), context={"source": "user"}
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": name, "type": "regular"}
    )
    await hass.async_block_till_done()


async def _rename_user(hass: HomeAssistant, entry: MockConfigEntry, sid: str, new: str) -> None:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "user"), context={"source": "reconfigure", "subentry_id": sid}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": new, "active": True}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()


async def test_entry_without_key_keeps_kr_through_reload_and_subentry_changes(
    hass: HomeAssistant,
) -> None:
    """T-2: reload, user add, rename and remove leave the unit at kr."""
    entry = await setup_legacy_entry(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert_unit(hass, entry, "kr")

    await _add_user(hass, entry, "Petra")
    assert_unit(hass, entry, "kr", COST_SENSORS | {"petra_total_cost"})

    await _rename_user(hass, entry, "u_paul", "Pelle")
    assert_unit(
        hass,
        entry,
        "kr",
        COST_SENSORS - {"paul_total_cost"} | {"petra_total_cost", "pelle_total_cost"},
    )

    hass.config_entries.async_remove_subentry(entry, "u_gaest")
    await hass.async_block_till_done()
    assert_unit(
        hass,
        entry,
        "kr",
        COST_SENSORS - {"paul_total_cost", "gaest_total_cost"}
        | {"petra_total_cost", "pelle_total_cost"},
    )
    assert CONF_CURRENCY not in entry.data


@pytest.mark.parametrize("data", [{}, {CONF_CURRENCY: "EUR"}], ids=["without-key", "with-key"])
async def test_entry_version_stays_1_1(hass: HomeAssistant, data: dict) -> None:
    """T-4: no version bump, so a version 1.1 entry loads without a migration (F-4)."""
    assert (EvChargingManagerConfigFlow.VERSION, EvChargingManagerConfigFlow.MINOR_VERSION) == (
        1,
        1,
    )
    entry = await setup_legacy_entry(hass, **data)

    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (1, 1)


# ---------------------------------------------------------------------------
# New installations: the config flow (D-61, D-62)
# ---------------------------------------------------------------------------

STATIC = {CONF_PRICING_MODE: "static", CONF_STATIC_PRICE_KWH: 2.50}
SPOT = {
    CONF_SPOT_PRICE_ENTITY: "sensor.spot_price",
    CONF_SPOT_ADDITIONAL_COST_KWH: 0.85,
    CONF_SPOT_VAT_MULTIPLIER: 1.25,
    CONF_SPOT_FALLBACK_PRICE_KWH: 2.50,
}


def schema_key(result: dict, key: str):
    return next(k for k in result["data_schema"].schema if k == key)


async def create_entry(hass: HomeAssistant, currency: str, pricing: str = "static"):
    """Run the config flow (generic profile) to the end; return the set-up entry."""
    hass.states.async_set("sensor.spot_price", "0.89")
    result = await _setup_to_pricing_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CURRENCY: currency, **STATIC, CONF_PRICING_MODE: pricing}
    )
    if pricing == "spot":
        result = await hass.config_entries.flow.async_configure(result["flow_id"], SPOT)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


def add_users(hass: HomeAssistant, entry) -> None:
    """Give a flow-created entry the regular and the guest user of USERS."""
    for user in USERS:
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(user["data"]),
                subentry_id=user["subentry_id"],
                subentry_type="user",
                title=user["title"],
                unique_id=None,
            ),
        )


@pytest.mark.parametrize("ha_currency", ["SEK", "EUR"])
async def test_config_flow_currency_is_required_without_default(
    hass: HomeAssistant, ha_currency: str
) -> None:
    """T-5: first, required, no default, no suggested value; omitted = rejected (F-5)."""
    await hass.config.async_update(currency=ha_currency)
    result = await _setup_to_pricing_step(hass)

    key = schema_key(result, CONF_CURRENCY)
    assert next(iter(result["data_schema"].schema)) is key
    assert isinstance(key, vol.Required)
    assert key.default is vol.UNDEFINED
    assert key.description is None
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], STATIC)
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize("currency", ["EUR", "kr"])
async def test_config_flow_choice_reaches_entry_and_sensors(
    hass: HomeAssistant, currency: str
) -> None:
    """T-6 (EUR), T-7 (kr): the chosen value is in entry.data and on all seven sensors."""
    entry = await create_entry(hass, currency)
    add_users(hass, entry)
    await hass.async_block_till_done()

    assert entry.data[CONF_CURRENCY] == currency
    assert_unit(hass, entry, currency)


async def test_home_assistant_currency_does_not_move_the_unit(hass: HomeAssistant) -> None:
    """T-8: Home Assistant's own currency changes and the entry reloads: still EUR (F-5)."""
    entry = await create_entry(hass, "EUR")
    add_users(hass, entry)
    await hass.async_block_till_done()

    await hass.config.async_update(currency="NOK")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data[CONF_CURRENCY] == "EUR"
    assert_unit(hass, entry, "EUR")


@pytest.mark.parametrize("pricing", ["static", "spot"])
async def test_config_flow_both_pricing_paths_carry_the_currency(
    hass: HomeAssistant, pricing: str
) -> None:
    """T-9."""
    entry = await create_entry(hass, "SEK", pricing)

    assert entry.data[CONF_PRICING_MODE] == pricing
    assert entry.data[CONF_CURRENCY] == "SEK"
    assert_unit(hass, entry, "SEK", COST_SENSORS - {"paul_total_cost", "gaest_total_cost"})


def assert_currency_options(result: dict) -> None:
    config = result["data_schema"].schema[schema_key(result, CONF_CURRENCY)].config
    assert config["options"] == ["kr", *sorted(ACTIVE_CURRENCIES)]
    assert {"SEK", "NOK", "DKK", "EUR", "USD"} <= set(config["options"])
    assert config["custom_value"] is False
    assert config["mode"] == "dropdown"


async def test_config_flow_currency_options(hass: HomeAssistant) -> None:
    """T-10, config flow."""
    assert_currency_options(await _setup_to_pricing_step(hass))


@pytest.mark.parametrize("value", ["euro", "sek", ""])
async def test_config_flow_rejects_a_value_outside_the_list(
    hass: HomeAssistant, value: str
) -> None:
    """T-11, config flow (F-8)."""
    result = await _setup_to_pricing_step(hass)

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CURRENCY: value, **STATIC}
        )
    assert hass.config_entries.async_entries(DOMAIN) == []
