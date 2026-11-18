"""The cost sensors' long-term statistics against a real recorder (spec 028 D-74, D-77;
T-3, T-12…T-16 and the reconcile cases).

Statistics periods are compiled by hand (``do_adhoc_statistics``) on a frozen clock, as
in research 028 B. The reconcile runs as a background task of the entry, so every step
that sets up the entry or changes a user is followed by ``_settle``.
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as rstats
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.core_config import DATA_CUSTOMIZE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_values import EntityValues
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.ev_charging_manager.const import (
    CONF_CURRENCY,
    DOMAIN,
    EVENT_SESSION_COMPLETED,
)
from custom_components.ev_charging_manager.cost_statistics import (
    async_reconcile_cost_statistics,
)
from tests.conftest import MOCK_CHARGER_DATA, setup_session_engine
from tests.test_currency import (
    STATIC,
    USERS,
    _add_user,
    _rename_user,
    assert_unit,
    options_save,
    setup_legacy_entry,
)

METADATA = "homeassistant.components.recorder.statistics.async_update_statistics_metadata"
GET_METADATA = "homeassistant.components.recorder.statistics.get_metadata"
RECONCILE = "custom_components.ev_charging_manager.async_reconcile_cost_statistics"
RECONCILE_LOGGER = "custom_components.ev_charging_manager.cost_statistics"
TOTAL_COST = ("paul_total_cost", "gaest_total_cost", "unknown_total_cost", "guest_total_cost")
P0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations) -> None:
    """Replace the autouse fixture of tests/conftest.py: the recorder must come before hass."""


def _entity_id(hass: HomeAssistant, entry, suffix: str) -> str:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{suffix}")


def _total_cost_ids(hass: HomeAssistant, entry) -> list[str]:
    return sorted(_entity_id(hass, entry, suffix) for suffix in TOTAL_COST)


def _calls(metadata) -> list[tuple]:
    return sorted(
        (c.args[1], c.kwargs["new_unit_class"], c.kwargs["new_unit_of_measurement"])
        for c in metadata.call_args_list
    )


def _info(caplog) -> list[str]:
    return sorted(
        r.getMessage()
        for r in caplog.records
        if r.name == RECONCILE_LOGGER and r.levelno == logging.INFO
    )


def _relabelled(ids, old: str, new: str) -> list[str]:
    return sorted(
        f"Changing the unit of the long-term statistics of {eid} from {old} to {new}" for eid in ids
    )


async def _settle(hass: HomeAssistant) -> None:
    """Let the reconcile task finish and the recorder apply what it queued."""
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def _units(hass: HomeAssistant, ids) -> dict[str, str | None]:
    """statistic_id -> unit in the statistics metadata (None: no metadata)."""
    meta = await get_instance(hass).async_add_executor_job(
        lambda: rstats.get_metadata(hass, statistic_ids=set(ids))
    )
    return {eid: meta[eid][1]["unit_of_measurement"] if eid in meta else None for eid in ids}


def _period(n: int) -> datetime:
    return P0 + timedelta(minutes=5 * n)


async def _compile(hass: HomeAssistant, freezer, n: int) -> None:
    """Close period ``n`` and compile its statistics."""
    freezer.move_to(_period(n + 1) + timedelta(seconds=1))
    await async_wait_recording_done(hass)
    do_adhoc_statistics(hass, start=_period(n))
    await async_wait_recording_done(hass)


async def _completed(hass: HomeAssistant, user: str, user_type: str, cost: float) -> None:
    hass.bus.async_fire(
        EVENT_SESSION_COMPLETED,
        {
            "user_name": user,
            "user_type": user_type,
            "energy_kwh": 1.0,
            "cost_kr": cost,
            "started_at": "2026-11-18T09:00:00+00:00",
            "ended_at": "2026-11-18T09:30:00+00:00",
        },
    )
    await hass.async_block_till_done()


async def _entry_with_statistics(hass: HomeAssistant, freezer, **data) -> MockConfigEntry:
    """Period 0: the entry, a session for Paul and the guest, compiled; the clock in period 1."""
    freezer.move_to(_period(0) + timedelta(seconds=10))
    entry = await setup_legacy_entry(hass, **data)
    await _settle(hass)
    await _completed(hass, "Paul", "regular", 10.0)
    await _completed(hass, "Gaest", "guest", 4.0)
    await _compile(hass, freezer, 0)
    freezer.move_to(_period(1) + timedelta(seconds=10))
    return entry


async def _report(hass: HomeAssistant, ids) -> dict[str, dict]:
    """Per statistic: metadata unit, the 5-minute periods with rows, the rows, repair issue."""
    instance = get_instance(hass)
    await instance.async_add_executor_job(rstats.update_statistics_issues, hass)
    await hass.async_block_till_done()
    units = await _units(hass, ids)
    rows = await instance.async_add_executor_job(
        lambda: rstats.statistics_during_period(
            hass, P0, None, set(ids), "5minute", None, {"state", "sum"}
        )
    )
    issues = {i.issue_id for i in ir.async_get(hass).issues.values() if i.domain == "sensor"}
    return {
        eid: {
            "unit": units[eid],
            "periods": [int((r["start"] - P0.timestamp()) // 300) for r in rows.get(eid, [])],
            "rows": rows.get(eid, []),
            "issue": f"units_changed_{eid}" in issues,
        }
        for eid in ids
    }


async def _sessions_and_compile(hass: HomeAssistant, freezer, user: str, periods) -> None:
    for n in periods:
        freezer.move_to(_period(n) + timedelta(seconds=20))
        await _completed(hass, user, "regular", 1.0)
        await _compile(hass, freezer, n)


def _assert_recording(stat: dict, unit: str, last_period: int) -> None:
    """The statistic is in ``unit``, has rows again up to ``last_period``, no repair issue."""
    assert stat["unit"] == unit
    assert stat["issue"] is False
    assert stat["periods"][-1] == last_period


# ---------------------------------------------------------------------------
# A change in the options (T-3, T-12…T-14)
# ---------------------------------------------------------------------------


async def test_change_relabels_each_total_cost_statistic(
    hass: HomeAssistant, freezer, caplog
) -> None:
    """T-12: kr -> EUR relabels the four total-cost statistics of this entry, with one info
    line each; another integration's monetary total keeps its unit (F-6)."""
    caplog.set_level(logging.INFO)
    other = (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            "other_integration",
            "money",
            original_device_class="monetary",
            capabilities={"state_class": "total"},
        )
        .entity_id
    )
    freezer.move_to(_period(0) + timedelta(seconds=5))
    hass.states.async_set(
        other,
        "3.0",
        {"device_class": "monetary", "state_class": "total", "unit_of_measurement": "kr"},
    )
    entry = await _entry_with_statistics(hass, freezer)
    ids = _total_cost_ids(hass, entry)
    assert await _units(hass, [*ids, other]) == dict.fromkeys([*ids, other], "kr")
    caplog.clear()

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        result = await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})
        await _settle(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert_unit(hass, entry, "EUR")
    assert await _units(hass, [*ids, other]) == {**dict.fromkeys(ids, "EUR"), other: "kr"}
    assert _calls(metadata) == [(eid, None, "EUR") for eid in ids]
    assert _info(caplog) == _relabelled(ids, "kr", "EUR")


async def test_relabel_uses_the_current_entity_id(hass: HomeAssistant, freezer) -> None:
    """T-13: a total-cost entity whose entity id the user has renamed."""
    freezer.move_to(_period(0) + timedelta(seconds=5))
    entry = await setup_legacy_entry(hass)
    er.async_get(hass).async_update_entity(
        _entity_id(hass, entry, "paul_total_cost"), new_entity_id="sensor.pauls_money"
    )
    await _settle(hass)
    await _completed(hass, "Paul", "regular", 10.0)
    await _compile(hass, freezer, 0)
    freezer.move_to(_period(1) + timedelta(seconds=10))

    await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})
    await _settle(hass)

    assert await _units(hass, ["sensor.pauls_money"]) == {"sensor.pauls_money": "EUR"}


async def test_change_back_to_kr(hass: HomeAssistant, freezer) -> None:
    """T-14: EUR -> kr, the undo."""
    entry = await _entry_with_statistics(hass, freezer, **{CONF_CURRENCY: "EUR"})
    ids = _total_cost_ids(hass, entry)
    assert await _units(hass, ids) == dict.fromkeys(ids, "EUR")

    await options_save(hass, entry, {CONF_CURRENCY: "kr", **STATIC})
    await _settle(hass)

    assert_unit(hass, entry, "kr")
    assert await _units(hass, ids) == dict.fromkeys(ids, "kr")


@pytest.mark.parametrize("data", [{}, {CONF_CURRENCY: "EUR"}], ids=["without-key", "EUR"])
async def test_save_without_touching_the_field_keeps_unit_and_statistics(
    hass: HomeAssistant, freezer, caplog, data: dict
) -> None:
    """T-3 (D-63, D-77): the form's own value is saved; nothing is relabelled (F-1, F-6)."""
    caplog.set_level(logging.INFO)
    unit = data.get(CONF_CURRENCY, "kr")
    entry = await _entry_with_statistics(hass, freezer, **data)
    ids = _total_cost_ids(hass, entry)
    caplog.clear()

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        result = await options_save(hass, entry, STATIC)
        await _settle(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_CURRENCY] == unit
    assert_unit(hass, entry, unit)
    assert await _units(hass, ids) == dict.fromkeys(ids, unit)
    metadata.assert_not_called()
    assert _info(caplog) == []


# ---------------------------------------------------------------------------
# The reconcile (D-74)
# ---------------------------------------------------------------------------


async def test_unit_and_statistics_already_equal(hass: HomeAssistant, freezer, caplog) -> None:
    """Reload, user added, user renamed: the statistics already carry the unit; no call, no log."""
    caplog.set_level(logging.INFO)
    entry = await _entry_with_statistics(hass, freezer, **{CONF_CURRENCY: "EUR"})
    ids = _total_cost_ids(hass, entry)
    caplog.clear()

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await _settle(hass)
        await _add_user(hass, entry, "Petra")
        await _settle(hass)
        await _rename_user(hass, entry, "u_paul", "Pelle")
        await _settle(hass)

    metadata.assert_not_called()
    assert _info(caplog) == []
    assert await _units(hass, ids) == dict.fromkeys(ids, "EUR")


async def test_statistic_without_metadata_is_left_alone(
    hass: HomeAssistant, freezer, caplog
) -> None:
    """Nothing compiled yet: no call; the statistic is then created in the new unit."""
    caplog.set_level(logging.INFO)
    freezer.move_to(_period(0) + timedelta(seconds=10))
    entry = await setup_legacy_entry(hass)
    await _settle(hass)
    paul = _entity_id(hass, entry, "paul_total_cost")
    assert await _units(hass, [paul]) == {paul: None}

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})
        await _settle(hass)
    await _sessions_and_compile(hass, freezer, "Paul", (1, 2))

    metadata.assert_not_called()
    assert _info(caplog) == []
    _assert_recording((await _report(hass, [paul]))[paul], "EUR", 2)


async def test_removed_user_added_again_after_a_change(hass: HomeAssistant, freezer) -> None:
    """R-2: Paul removed in kr, kr -> EUR, Paul added again under the same entity id."""
    entry = await _entry_with_statistics(hass, freezer)
    paul = _entity_id(hass, entry, "paul_total_cost")
    hass.config_entries.async_remove_subentry(entry, "u_paul")
    await _settle(hass)
    await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})
    await _settle(hass)
    await _compile(hass, freezer, 1)
    assert await _units(hass, [paul]) == {paul: "kr"}

    freezer.move_to(_period(2) + timedelta(seconds=10))
    await _add_user(hass, entry, "Paul")
    await _settle(hass)
    assert _entity_id(hass, entry, "paul_total_cost") == paul
    await _sessions_and_compile(hass, freezer, "Paul", (2, 3))

    _assert_recording((await _report(hass, [paul]))[paul], "EUR", 3)


def _regular(sid: str, name: str) -> dict:
    data = {**USERS[0]["data"], "name": name}
    return {**USERS[0], "subentry_id": sid, "title": name, "data": data}


async def test_rename_onto_an_old_name(hass: HomeAssistant, freezer) -> None:
    """R-2: Pelle removed in kr, kr -> EUR, then a rename to Pelle that cannot move the
    registry entries (the old slug is shared, spec 026 D-28) takes Pelle's old entity id."""
    users = [USERS[0], _regular("u_paul_lc", "paul"), _regular("u_pelle", "Pelle")]
    freezer.move_to(_period(0) + timedelta(seconds=10))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(MOCK_CHARGER_DATA),
        title="My go-e Charger",
        subentries_data=copy.deepcopy(users),
    )
    await setup_session_engine(hass, entry)
    await _settle(hass)
    pelle = _entity_id(hass, entry, "pelle_total_cost")
    await _completed(hass, "Pelle", "regular", 3.0)
    await _compile(hass, freezer, 0)
    freezer.move_to(_period(1) + timedelta(seconds=10))
    hass.config_entries.async_remove_subentry(entry, "u_pelle")
    await _settle(hass)
    await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})
    await _settle(hass)
    await _compile(hass, freezer, 1)
    assert await _units(hass, [pelle]) == {pelle: "kr"}

    freezer.move_to(_period(2) + timedelta(seconds=10))
    await _rename_user(hass, entry, "u_paul", "Pelle")
    await _settle(hass)
    assert _entity_id(hass, entry, "pelle_total_cost") == pelle
    await _sessions_and_compile(hass, freezer, "Pelle", (2, 3))

    _assert_recording((await _report(hass, [pelle]))[pelle], "EUR", 3)


async def test_entry_created_again_with_another_unit(hass: HomeAssistant, freezer) -> None:
    """R-2: the entry removed in kr and created again in EUR; the same entity ids come back."""
    entry = await _entry_with_statistics(hass, freezer)
    paul = _entity_id(hass, entry, "paul_total_cost")
    assert await hass.config_entries.async_remove(entry.entry_id)
    await _settle(hass)

    again = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, CONF_CURRENCY: "EUR"},
        title="My go-e Charger",
        subentries_data=copy.deepcopy(USERS),
    )
    await setup_session_engine(hass, again)
    await _settle(hass)
    assert _entity_id(hass, again, "paul_total_cost") == paul
    await _sessions_and_compile(hass, freezer, "Paul", (1, 2))

    _assert_recording((await _report(hass, [paul]))[paul], "EUR", 2)


@pytest.mark.parametrize("outcome", ["ready", "never-ready", "unloaded"])
async def test_reconcile_waits_for_the_recorder(hass: HomeAssistant, freezer, outcome: str) -> None:
    """The reload completes while the recorder is not ready; the statistics follow once it
    is, not at all if its database never becomes ready, and not after an unload (F-7)."""
    entry = await _entry_with_statistics(hass, freezer)
    ids = _total_cost_ids(hass, entry)
    instance = get_instance(hass)
    db_ready, instance.async_db_ready = instance.async_db_ready, hass.loop.create_future()
    try:
        await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})
        await async_wait_recording_done(hass)
        assert entry.state is ConfigEntryState.LOADED
        assert await _units(hass, ids) == dict.fromkeys(ids, "kr")
        if outcome == "unloaded":
            assert await hass.config_entries.async_unload(entry.entry_id)

        instance.async_db_ready.set_result(outcome != "never-ready")
        await _settle(hass)
    finally:
        instance.async_db_ready = db_ready

    assert await _units(hass, ids) == dict.fromkeys(ids, "EUR" if outcome == "ready" else "kr")


async def test_second_entry_is_not_touched(hass: HomeAssistant, freezer) -> None:
    """F-6: a change on one entry leaves the other entry's statistics in its unit."""
    freezer.move_to(_period(0) + timedelta(seconds=10))
    first = await setup_legacy_entry(hass)
    second = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CHARGER_DATA, "charger_serial": "zzz", "charger_name": "Second"},
        title="Second",
        subentries_data=copy.deepcopy(USERS),
    )
    await setup_session_engine(hass, second)
    await _settle(hass)
    await _completed(hass, "Paul", "regular", 10.0)
    await _completed(hass, "Gaest", "guest", 4.0)
    await _compile(hass, freezer, 0)
    freezer.move_to(_period(1) + timedelta(seconds=10))
    first_ids, second_ids = _total_cost_ids(hass, first), _total_cost_ids(hass, second)
    assert not set(first_ids) & set(second_ids)
    assert await _units(hass, second_ids) == dict.fromkeys(second_ids, "kr")

    await options_save(hass, first, {CONF_CURRENCY: "EUR", **STATIC})
    await _settle(hass)

    assert await _units(hass, first_ids) == dict.fromkeys(first_ids, "EUR")
    assert await _units(hass, second_ids) == dict.fromkeys(second_ids, "kr")
    assert_unit(hass, second, "kr")


async def test_failing_metadata_read_does_not_block_anything(
    hass: HomeAssistant, freezer, caplog
) -> None:
    """D-74, F-7 (replaces T-18): the read raises in the reload after a save: one warning
    naming Developer tools → Statistics; saved, loaded, the sensors work."""
    entry = await _entry_with_statistics(hass, freezer)
    caplog.clear()

    with patch(GET_METADATA, side_effect=RuntimeError("boom")):
        result = await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})
        await _settle(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_CURRENCY] == "EUR"
    assert entry.state is ConfigEntryState.LOADED
    assert_unit(hass, entry, "EUR")
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name.startswith("custom_components.ev_charging_manager")
    ]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert "Developer tools → Statistics" in warnings[0].getMessage()
    paul = _entity_id(hass, entry, "paul_total_cost")
    await _completed(hass, "Paul", "regular", 2.5)
    assert hass.states.get(paul).state == "12.5"


# ---------------------------------------------------------------------------
# A unit set with customize (the D-74 amendment)
# ---------------------------------------------------------------------------

CUSTOMIZED = "sensor.my_go_e_charger_paul_total_cost"


async def _customized_on_047(hass: HomeAssistant, freezer) -> MockConfigEntry:
    """0.4.7 with ``customize: unit_of_measurement: EUR`` on Paul's total: entry without the
    key, state and statistics in EUR after period 0; the clock in period 1."""
    hass.data[DATA_CUSTOMIZE] = EntityValues({CUSTOMIZED: {"unit_of_measurement": "EUR"}})
    freezer.move_to(_period(0) + timedelta(seconds=10))
    with patch(RECONCILE):  # 0.4.7 has no reconcile
        entry = await setup_legacy_entry(hass)
        await _settle(hass)
        assert _entity_id(hass, entry, "paul_total_cost") == CUSTOMIZED
        await _completed(hass, "Paul", "regular", 10.0)
        await _compile(hass, freezer, 0)
    assert hass.states.get(CUSTOMIZED).attributes["unit_of_measurement"] == "EUR"
    assert await _units(hass, [CUSTOMIZED]) == {CUSTOMIZED: "EUR"}
    freezer.move_to(_period(1) + timedelta(seconds=10))
    return entry


async def test_customized_unit_is_left_alone_after_the_upgrade(
    hass: HomeAssistant, freezer, caplog
) -> None:
    """(a) The upgrade start and a later reload: the statistic stays in EUR and records."""
    caplog.set_level(logging.INFO)
    entry = await _customized_on_047(hass, freezer)
    caplog.clear()

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await _settle(hass)
        await _sessions_and_compile(hass, freezer, "Paul", (1,))
        assert await hass.config_entries.async_reload(entry.entry_id)
        await _settle(hass)
        await _sessions_and_compile(hass, freezer, "Paul", (2, 3))

    metadata.assert_not_called()
    assert _info(caplog) == []
    stat = (await _report(hass, [CUSTOMIZED]))[CUSTOMIZED]
    _assert_recording(stat, "EUR", 3)
    assert stat["periods"] == [0, 1, 2, 3]


async def test_customized_unit_repaired_by_the_user_stays(
    hass: HomeAssistant, freezer, caplog
) -> None:
    """(b) Statistics relabelled to kr against the state (the build before the amendment),
    the user's repair back to EUR, then the entry set up again: still EUR, recording."""
    caplog.set_level(logging.INFO)
    entry = await _customized_on_047(hass, freezer)
    rstats.async_update_statistics_metadata(
        hass, CUSTOMIZED, new_unit_class=None, new_unit_of_measurement="kr"
    )
    await _sessions_and_compile(hass, freezer, "Paul", (1,))
    assert (await _report(hass, [CUSTOMIZED]))[CUSTOMIZED]["issue"] is True
    rstats.async_update_statistics_metadata(
        hass, CUSTOMIZED, new_unit_class=None, new_unit_of_measurement="EUR"
    )
    await async_wait_recording_done(hass)
    freezer.move_to(_period(2) + timedelta(seconds=10))
    caplog.clear()

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await _settle(hass)
        await _sessions_and_compile(hass, freezer, "Paul", (2, 3))

    metadata.assert_not_called()
    assert _info(caplog) == []
    _assert_recording((await _report(hass, [CUSTOMIZED]))[CUSTOMIZED], "EUR", 3)


async def test_customized_user_then_chooses_the_same_currency(
    hass: HomeAssistant, freezer, caplog
) -> None:
    """(c) The customized user picks EUR in the options: Paul's total stays EUR and records;
    the other totals, in kr, follow to EUR."""
    caplog.set_level(logging.INFO)
    entry = await _customized_on_047(hass, freezer)
    others = [eid for eid in _total_cost_ids(hass, entry) if eid != CUSTOMIZED]
    caplog.clear()

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})
        await _settle(hass)
        await _sessions_and_compile(hass, freezer, "Paul", (1, 2))

    assert entry.data[CONF_CURRENCY] == "EUR"
    assert hass.states.get(CUSTOMIZED).attributes["unit_of_measurement"] == "EUR"
    assert CUSTOMIZED not in [c.args[1] for c in metadata.call_args_list]
    assert not any(CUSTOMIZED in line for line in _info(caplog))
    assert {unit for unit in (await _units(hass, others)).values() if unit} == {"EUR"}
    stat = (await _report(hass, [CUSTOMIZED]))[CUSTOMIZED]
    _assert_recording(stat, "EUR", 2)
    assert stat["periods"] == [0, 1, 2]


async def test_sensor_showing_a_third_unit_or_no_state_is_left_alone(
    hass: HomeAssistant, freezer, caplog
) -> None:
    """Entry in EUR, statistics in kr: only the total whose state says EUR is relabelled; one
    that shows USD and one without a state keep kr, without a warning."""
    entry = await _entry_with_statistics(hass, freezer)
    guest, paul, unknown = (
        _entity_id(hass, entry, f"{name}_total_cost") for name in ("guest", "paul", "unknown")
    )
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CURRENCY: "EUR"})
    await _settle(hass)
    for eid, unit in ((guest, "EUR"), (paul, "USD")):
        state = hass.states.get(eid)
        hass.states.async_set(eid, state.state, {**state.attributes, "unit_of_measurement": unit})
    hass.states.async_remove(unknown)
    caplog.clear()

    await async_reconcile_cost_statistics(hass, entry)
    await _settle(hass)

    assert await _units(hass, [guest, paul, unknown]) == {guest: "EUR", paul: "kr", unknown: "kr"}
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_only_monetary_totals_are_selected(hass: HomeAssistant, freezer) -> None:
    """F-6: rows of this entry that are total but not monetary, or monetary but not total,
    keep statistics in another unit; a monetary total in the same situation is relabelled."""
    freezer.move_to(_period(0) + timedelta(seconds=10))
    entry = await setup_legacy_entry(hass)
    await _settle(hass)
    rows = {
        name: er.async_get(hass)
        .async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_x_{name}",
            config_entry=entry,
            original_device_class=device_class,
            capabilities={"state_class": state_class},
        )
        .entity_id
        for name, device_class, state_class in (
            ("total_not_monetary", "energy", "total"),
            ("monetary_not_total", "monetary", "measurement"),
            ("monetary_total", "monetary", "total"),
        )
    }

    def show(unit: str) -> None:
        for eid in rows.values():
            hass.states.async_set(eid, "1.0", {"state_class": "total", "unit_of_measurement": unit})

    show("EUR")
    await _compile(hass, freezer, 0)
    assert await _units(hass, rows.values()) == dict.fromkeys(rows.values(), "EUR")
    show("kr")

    await async_reconcile_cost_statistics(hass, entry)
    await _settle(hass)

    assert await _units(hass, rows.values()) == {
        rows["total_not_monetary"]: "EUR",
        rows["monetary_not_total"]: "EUR",
        rows["monetary_total"]: "kr",
    }


# ---------------------------------------------------------------------------
# T-15, T-16: the statistics rows themselves
# ---------------------------------------------------------------------------


async def _change_to_sek_between_sessions(hass: HomeAssistant, freezer):
    """Period 0 in kr; kr -> SEK in period 1; sessions in periods 1-3; all four compiled."""
    entry = await _entry_with_statistics(hass, freezer)
    await options_save(hass, entry, {CONF_CURRENCY: "SEK", **STATIC})
    await _settle(hass)
    for n, cost in ((1, 5.0), (2, 2.0), (3, 1.0)):
        freezer.move_to(_period(n) + timedelta(seconds=20))
        await _completed(hass, "Paul", "regular", cost)
        await _completed(hass, "Gaest", "guest", cost)
        await _compile(hass, freezer, n)
    return await _report(hass, _total_cost_ids(hass, entry))


async def test_statistics_follow_the_change(hass: HomeAssistant, freezer) -> None:
    """T-15: rows from period 2 on, metadata SEK, a continuous sum, no repair issue."""
    result = await _change_to_sek_between_sessions(hass, freezer)

    for eid, stat in result.items():
        assert stat["unit"] == "SEK", eid
        assert stat["issue"] is False, eid
        assert stat["periods"][0] == 0, eid
        assert {2, 3} <= set(stat["periods"]), eid
        first, last = stat["rows"][0], stat["rows"][-1]
        assert last["sum"] - first["sum"] == pytest.approx(last["state"] - first["state"]), eid
    paul = next(stat for eid, stat in result.items() if eid.endswith("paul_total_cost"))
    assert paul["rows"][-1]["state"] == pytest.approx(18.0)


async def test_statistics_stop_without_the_reconcile(hass: HomeAssistant, freezer) -> None:
    """T-16, the control: the same run with D-74 switched off stops every statistic.

    Shows that T-15's rows come from the reconcile, not from Home Assistant.
    """
    with patch(RECONCILE):
        result = await _change_to_sek_between_sessions(hass, freezer)

    for eid, stat in result.items():
        assert stat["unit"] == "kr", eid
        assert stat["issue"] is True, eid
        assert stat["periods"] == [0], eid
    assert len(result) == len(TOTAL_COST)
