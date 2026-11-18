"""The unit of the cost sensors (spec 028, T-1…T-4).

The seven cost and price entities take their unit from ``entry_currency``; an entry
without the ``currency`` key is an installation created before the setting and shows
``kr`` (D-60).
"""

from __future__ import annotations

import copy

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charging_manager.config_flow import EvChargingManagerConfigFlow
from custom_components.ev_charging_manager.const import CONF_CURRENCY, DOMAIN
from tests.conftest import MOCK_CHARGER_DATA, setup_session_engine

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
