"""The currency of the cost sensors (spec 028 § 7; the recorder cases are in
test_currency_statistics.py).

The seven cost and price entities take their unit from ``entry_currency``; an entry
without the ``currency`` key is an installation created before the setting and shows
``kr`` (D-60).
"""

from __future__ import annotations

import copy
import logging
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

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
    STATS_STORE_KEY,
)
from tests.conftest import MOCK_CHARGER_DATA, setup_session_engine
from tests.session_rig import TRX_GFIX, TRX_PAUL, Rig, scenario_a, sessions_on_disk
from tests.test_config_flow import _setup_to_pricing_step
from tests.test_interruption_fixed_price import (
    PAUL_4KWH,
    _assert_one_paul_session,
    save_options,
)
from tests.test_interruption_snapshot import COMPLETED_KEYS, RECORD_KEYS

STATIC = {CONF_PRICING_MODE: "static", CONF_STATIC_PRICE_KWH: 2.50}
RECONCILE = "custom_components.ev_charging_manager.async_reconcile_cost_statistics"

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
    remaining = COST_SENSORS - {"paul_total_cost", "gaest_total_cost"} | {
        "petra_total_cost",
        "pelle_total_cost",
    }
    assert_unit(hass, entry, "kr", remaining)
    assert CONF_CURRENCY not in entry.data

    result = await options_save(hass, entry, STATIC, {"min_session_duration_s": 120})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["min_session_duration_s"] == 120
    assert_unit(hass, entry, "kr", remaining)


async def test_listener_schedules_the_reconcile_only_for_an_added_or_renamed_user(
    hass: HomeAssistant,
) -> None:
    """D-74 (b), X-14: removals, vehicle and RFID changes and an options change without user
    changes schedule nothing from the listener; a user add and a rename once each. The options
    save through the form counts one, from the setup of its reload."""
    entry = await setup_legacy_entry(hass)
    subentries = {
        "v_car": (
            "vehicle",
            {
                "name": "Car",
                "battery_capacity_kwh": 50.0,
                "usable_battery_kwh": 50.0,
                "charging_phases": 3,
                "max_charging_power_kw": 11.0,
                "charging_efficiency": 0.9,
            },
        ),
        "r0": (
            "rfid_mapping",
            {"card_index": 0, "card_uid": None, "user_id": "u_paul", "vehicle_id": None},
        ),
    }

    with patch(RECONCILE, new_callable=AsyncMock) as reconcile:

        async def scheduled() -> int:
            await hass.async_block_till_done(wait_background_tasks=True)
            return reconcile.call_count

        for sid, (subentry_type, data) in subentries.items():
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType({**data, "active": True}),
                    subentry_id=sid,
                    subentry_type=subentry_type,
                    title=sid,
                    unique_id=None,
                ),
            )
            assert await scheduled() == 0, sid
        for sid in (*subentries, "u_gaest"):
            hass.config_entries.async_remove_subentry(entry, sid)
            assert await scheduled() == 0, sid
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "min_session_duration_s": 120}
        )
        assert await scheduled() == 0
        await options_save(hass, entry, STATIC, {"min_session_duration_s": 90})
        assert await scheduled() == 1

        await _add_user(hass, entry, "Petra")
        assert await scheduled() == 2
        await _rename_user(hass, entry, "u_paul", "Pelle")
        assert await scheduled() == 3

    assert entry.state is ConfigEntryState.LOADED


async def options_save(
    hass: HomeAssistant, entry: MockConfigEntry, pricing: dict, init: dict | None = None
) -> dict:
    """Save the options form: step init, then step pricing (which reloads the entry)."""
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], init or {})
    result = await flow.async_configure(result["flow_id"], pricing)
    await hass.async_block_till_done()
    return result


async def test_options_save_without_touching_the_field_keeps_kr(hass: HomeAssistant) -> None:
    """T-3: the form shows kr; saved untouched, the entry gets the key with kr (D-63, F-1)."""
    entry = await setup_legacy_entry(hass)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {})
    key = schema_key(result, CONF_CURRENCY)
    assert next(iter(result["data_schema"].schema)) is key
    assert key.default() == "kr"

    result = await flow.async_configure(result["flow_id"], STATIC)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_CURRENCY] == "kr"
    assert_unit(hass, entry, "kr")


@pytest.mark.parametrize("data", [{}, {CONF_CURRENCY: "EUR"}], ids=["without-key", "with-key"])
async def test_entry_version_stays_1_1(hass: HomeAssistant, data: dict) -> None:
    """T-4: no version bump, so a version 1.1 entry loads without a migration (F-4)."""
    assert (EvChargingManagerConfigFlow.VERSION, EvChargingManagerConfigFlow.MINOR_VERSION) == (
        1,
        1,
    )
    entry = await setup_legacy_entry(hass, **data)

    assert entry.state is ConfigEntryState.LOADED


async def test_config_flow_creates_a_version_1_1_entry(hass: HomeAssistant) -> None:
    """T-4: the entry the flow creates is 1.1 (a mock entry is 1.1 whatever the flow says)."""
    entry = await create_entry(hass, "EUR")

    assert (entry.version, entry.minor_version) == (1, 1)
    assert entry.state is ConfigEntryState.LOADED


# ---------------------------------------------------------------------------
# New installations: the config flow (D-61, D-62)
# ---------------------------------------------------------------------------

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


def assert_currency_field_empty(result: dict) -> None:
    """First, optional, no default, no suggested value: the frontend shows it empty (D-73)."""
    key = schema_key(result, CONF_CURRENCY)
    assert next(iter(result["data_schema"].schema)) is key
    assert type(key) is vol.Optional
    assert key.default is vol.UNDEFINED
    assert key.description is None


@pytest.mark.parametrize("ha_currency", ["SEK", "EUR"])
async def test_config_flow_currency_must_be_chosen(hass: HomeAssistant, ha_currency: str) -> None:
    """T-5 (D-73): omitted = the form again with a field error and the other values kept;
    then a submit with a currency goes on (F-5)."""
    await hass.config.async_update(currency=ha_currency)
    result = await _setup_to_pricing_step(hass)
    assert_currency_field_empty(result)
    handler = hass.config_entries.flow._progress[result["flow_id"]]
    stored = copy.deepcopy(handler.data)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICING_MODE: "spot", CONF_STATIC_PRICE_KWH: 3.10}
    )

    assert handler.data == stored
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pricing"
    assert result["errors"] == {CONF_CURRENCY: "currency_required"}
    assert_currency_field_empty(result)
    assert schema_key(result, CONF_PRICING_MODE).default() == "spot"
    assert schema_key(result, CONF_STATIC_PRICE_KWH).default() == 3.10

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CURRENCY: "NOK", **STATIC}
    )
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].data[CONF_CURRENCY] == "NOK"


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
    """T-11, config flow (F-8): the flow stays on the step and the next valid choice is the one
    the entry gets."""
    result = await _setup_to_pricing_step(hass)

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CURRENCY: value, **STATIC}
        )
    assert hass.config_entries.flow.async_get(result["flow_id"])["step_id"] == "pricing"
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CURRENCY: "SEK", **STATIC}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["result"].data[CONF_CURRENCY] == "SEK"


async def test_options_flow_currency_options(hass: HomeAssistant) -> None:
    """T-10, options flow."""
    entry = await setup_legacy_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert_currency_options(result)


@pytest.mark.parametrize("stored", ["ADP", "EUR"])
async def test_options_flow_offers_the_stored_unit(hass: HomeAssistant, stored: str) -> None:
    """D-75: the stored unit is offered once, also when Home Assistant's list lacks it (ADP is
    a retired code); the form sent back with it saves and keeps it."""
    assert (stored in ACTIVE_CURRENCIES) is (stored == "EUR")
    entry = await setup_legacy_entry(hass, **{CONF_CURRENCY: stored})
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {})

    config = result["data_schema"].schema[schema_key(result, CONF_CURRENCY)].config
    assert config["options"] == ["kr", *sorted(set(ACTIVE_CURRENCIES) | {stored})]
    result = await flow.async_configure(
        result["flow_id"], {CONF_CURRENCY: stored, **STATIC, CONF_STATIC_PRICE_KWH: 3.0}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (entry.data[CONF_CURRENCY], entry.data[CONF_STATIC_PRICE_KWH]) == (stored, 3.0)
    assert_unit(hass, entry, stored)


@pytest.mark.parametrize("value", ["euro", "sek", ""])
async def test_options_flow_rejects_a_value_outside_the_list(
    hass: HomeAssistant, value: str
) -> None:
    """T-11, options flow: nothing is written (F-8)."""
    entry = await setup_legacy_entry(hass)
    data, options = dict(entry.data), dict(entry.options)

    with pytest.raises(InvalidData):
        await options_save(hass, entry, {CONF_CURRENCY: value, **STATIC})

    assert (dict(entry.data), dict(entry.options)) == (data, options)
    assert_unit(hass, entry, "kr")


# ---------------------------------------------------------------------------
# Changing it (D-63…D-67); with a real recorder in test_currency_statistics.py
# ---------------------------------------------------------------------------


async def test_change_without_recorder_saves_quietly(hass: HomeAssistant, caplog) -> None:
    """T-17: no recorder, so nothing to reconcile: the save succeeds without a warning (D-74)."""
    entry = await setup_legacy_entry(hass)
    assert "recorder" not in hass.config.components
    caplog.clear()

    result = await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_CURRENCY] == "EUR"
    assert_unit(hass, entry, "EUR")
    assert [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name.startswith("custom_components.ev_charging_manager")
    ] == []


# The key paths on main at afcadef (read there 2026-11-18), for the T-19 run; F-9 keeps the
# stores, the session and the engines' data unchanged.
STATS_PATHS = {
    "guest_last", "guest_retired", "guest_retired.cost_kr", "guest_retired.energy_kwh",
    "unknown_session_times", "user_stats", "user_stats.*", "user_stats.*.current_month",
    "user_stats.*.current_month.cost_kr", "user_stats.*.current_month.energy_kwh",
    "user_stats.*.current_month.month", "user_stats.*.current_month.sessions",
    "user_stats.*.last_session_at", "user_stats.*.previous_month",
    "user_stats.*.previous_month.cost_kr", "user_stats.*.previous_month.energy_kwh",
    "user_stats.*.previous_month.month", "user_stats.*.previous_month.sessions",
    "user_stats.*.session_count", "user_stats.*.total_cost_kr", "user_stats.*.total_energy_kwh",
    "user_stats.*.user_name", "user_stats.*.user_type",
}  # fmt: skip
SNAPSHOT_PATHS = RECORD_KEYS | {
    "resume_state", "resume_state.guest_pricing", "resume_state.pricing",
    "resume_state.pricing.mode", "resume_state.pricing.static_price_kwh", "resume_state.spot_hour",
}  # fmt: skip


def key_paths(node: dict, prefix: str = "") -> set[str]:
    """Dotted key paths of nested dicts; the user names under user_stats become ``*``."""
    out = set()
    for key, value in node.items():
        path = "user_stats.*" if prefix == "user_stats" else f"{prefix}.{key}".lstrip(".")
        out.add(path)
        if isinstance(value, dict):
            out |= key_paths(value, path)
    return out


@pytest.mark.parametrize("change", ["none", "EUR"])
async def test_change_leaves_every_number(hass, hass_storage, freezer, change: str) -> None:
    """T-19: stats store, stored sessions and the completed payload as without a change (F-2, F-3).

    "none" is the reference run: the second session's money is the same in both. After the
    session that follows the change, the stores, the record, the active snapshot and the
    payload have main's keys.
    """
    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, TRX_PAUL, {})
    stats = copy.deepcopy(hass_storage[STATS_STORE_KEY]["data"])
    sessions = copy.deepcopy(sessions_on_disk(hass_storage))

    if change != "none":
        await save_options({CONF_CURRENCY: change, **STATIC})(rig)
        assert rig.entry.data[CONF_CURRENCY] == change
    assert hass_storage[STATS_STORE_KEY]["data"] == stats
    assert sessions_on_disk(hass_storage) == sessions

    await rig.goto("10:45:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:55:00")
    await rig.set(energy=5.0)
    assert key_paths(rig.snapshot()) == SNAPSHOT_PATHS
    await rig.set(power=0)
    await rig.goto("10:58:00")
    await rig.unplug()
    first, second = rig.money()
    assert first == PAUL_4KWH
    assert (second["energy"], second["cost"], second["record_cost"]) == (1.0, 2.5, 2.5)
    assert key_paths(hass_storage[STATS_STORE_KEY]["data"]) == STATS_PATHS
    assert [set(record) for record in rig.records()] == [RECORD_KEYS] * 2
    assert [set(event.data) for event in rig.completed] == [COMPLETED_KEYS] * 2


def _session_cost(hass: HomeAssistant, entry) -> tuple[str, str]:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_session_cost"
    )
    state = hass.states.get(entity_id)
    return state.state, state.attributes.get("unit_of_measurement")


@pytest.mark.parametrize("change", ["none", "EUR"])
async def test_session_across_a_change_completes_once(
    hass, hass_storage, freezer, change: str
) -> None:
    """T-20: goe_gemini, kr -> EUR at 2.5 kWh: one record, the cost of the run without it."""
    seen = {}

    async def _change(rig: Rig) -> None:
        seen["before"] = _session_cost(hass, rig.entry)
        await save_options({CONF_CURRENCY: change, **STATIC})(rig)
        seen["after"] = _session_cost(hass, rig.entry)

    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, TRX_PAUL, {} if change == "none" else {"mid": _change})

    _assert_one_paul_session(rig)
    if change != "none":
        assert seen == {"before": ("6.25", "kr"), "after": ("6.25", "EUR")}


def _cost_states(hass: HomeAssistant, entry) -> dict[str, tuple[str, str]]:
    """unique_id suffix -> (state, unit) of every monetary entity of the entry."""
    out = {}
    for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if reg.original_device_class == "monetary":
            state = hass.states.get(reg.entity_id)
            out[reg.unique_id.removeprefix(f"{entry.entry_id}_")] = (
                state.state,
                state.attributes.get("unit_of_measurement"),
            )
    return out


async def _short_session(rig: Rig, trx: str, start: str, end: str, energy: float) -> None:
    await rig.goto(start)
    await rig.plug_in(trx)
    await rig.goto(end)
    await rig.set(energy=energy)
    await rig.set(power=0)


async def test_every_cost_sensor_keeps_its_value_across_a_change(
    hass, hass_storage, freezer
) -> None:
    """D-77: the seven cost and price sensors read by value before and after kr -> EUR, with
    Paul, an unmapped card and the guest done and a guest session running (F-2)."""
    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, TRX_PAUL, {})
    for trx, start, end, energy in (
        ("9", "10:45", "10:50", 5.0),
        (TRX_GFIX, "11:00", "11:05", 6.0),
    ):
        await _short_session(rig, trx, f"{start}:00", f"{end}:00", energy)
        await rig.unplug()
    await rig.goto("11:15:00")
    await rig.plug_in(TRX_GFIX)
    await rig.goto("11:20:00")
    await rig.set(energy=7.0)

    before = _cost_states(hass, rig.entry)
    await save_options({CONF_CURRENCY: "EUR", **STATIC})(rig)
    after = _cost_states(hass, rig.entry)

    assert COST_SENSORS <= set(before)
    for name in COST_SENSORS:
        float(before[name][0])
    assert {name: value for name, (value, _) in after.items()} == {
        name: value for name, (value, _) in before.items()
    }
    assert {unit for _, unit in before.values()} == {"kr"}
    assert {unit for _, unit in after.values()} == {"EUR"}


async def test_user_added_after_a_change_gets_the_new_unit(hass: HomeAssistant) -> None:
    """T-21."""
    entry = await setup_legacy_entry(hass)
    await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})

    await _add_user(hass, entry, "Petra")

    assert_unit(hass, entry, "EUR", COST_SENSORS | {"petra_total_cost"})
