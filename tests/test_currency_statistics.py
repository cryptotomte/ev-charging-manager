"""A currency change against a real recorder (spec 028 D-65, D-66; T-12…T-16, T-18).

Statistics periods are compiled by hand (``do_adhoc_statistics``) on a frozen clock, as
in research 028 B.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as rstats
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.ev_charging_manager.const import (
    CONF_CURRENCY,
    DOMAIN,
    EVENT_SESSION_COMPLETED,
)
from tests.test_currency import (
    STATIC,
    assert_unit,
    options_save,
    setup_legacy_entry,
)

METADATA = "homeassistant.components.recorder.statistics.async_update_statistics_metadata"
RELABEL = "custom_components.ev_charging_manager.config_flow._relabel_cost_statistics"
TOTAL_COST = ("paul_total_cost", "gaest_total_cost", "unknown_total_cost", "guest_total_cost")
P0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations) -> None:
    """Replace the autouse fixture of tests/conftest.py: the recorder must come before hass."""


def _entity_id(hass: HomeAssistant, entry, suffix: str) -> str:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{suffix}")


def _calls(metadata) -> list[tuple]:
    return sorted(
        (c.args[1], c.kwargs["new_unit_class"], c.kwargs["new_unit_of_measurement"])
        for c in metadata.call_args_list
    )


async def test_change_relabels_each_total_cost_statistic(hass: HomeAssistant) -> None:
    """T-12: one call per total-cost entity of this entry, none for any other (F-6)."""
    er.async_get(hass).async_get_or_create(
        "sensor",
        "other_integration",
        "money",
        original_device_class="monetary",
        capabilities={"state_class": "total"},
    )
    entry = await setup_legacy_entry(hass)

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        result = await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert_unit(hass, entry, "EUR")
    assert _calls(metadata) == sorted(
        (_entity_id(hass, entry, suffix), None, "EUR") for suffix in TOTAL_COST
    )


async def test_relabel_uses_the_current_entity_id(hass: HomeAssistant) -> None:
    """T-13: a total-cost entity whose entity id the user has renamed."""
    entry = await setup_legacy_entry(hass)
    er.async_get(hass).async_update_entity(
        _entity_id(hass, entry, "paul_total_cost"), new_entity_id="sensor.pauls_money"
    )
    await hass.async_block_till_done()

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})

    assert ("sensor.pauls_money", None, "EUR") in _calls(metadata)
    assert len(metadata.call_args_list) == len(TOTAL_COST)


async def test_change_back_to_kr(hass: HomeAssistant) -> None:
    """T-14: EUR -> kr, the undo."""
    entry = await setup_legacy_entry(hass, **{CONF_CURRENCY: "EUR"})
    assert_unit(hass, entry, "EUR")

    with patch(METADATA, wraps=rstats.async_update_statistics_metadata) as metadata:
        await options_save(hass, entry, {CONF_CURRENCY: "kr", **STATIC})

    assert_unit(hass, entry, "kr")
    assert {c[2] for c in _calls(metadata)} == {"kr"}
    assert len(metadata.call_args_list) == len(TOTAL_COST)


async def test_failing_relabel_does_not_block_the_save(hass: HomeAssistant, caplog) -> None:
    """T-18: one warning naming Developer tools → Statistics; saved and reloaded (D-66, F-7)."""
    entry = await setup_legacy_entry(hass)
    caplog.clear()

    with patch(METADATA, side_effect=RuntimeError("boom")):
        result = await options_save(hass, entry, {CONF_CURRENCY: "EUR", **STATIC})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_CURRENCY] == "EUR"
    assert_unit(hass, entry, "EUR")
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name.startswith("custom_components.ev_charging_manager")
    ]
    assert len(warnings) == 1
    assert "Developer tools → Statistics" in warnings[0]


# ---------------------------------------------------------------------------
# T-15, T-16: the statistics rows themselves
# ---------------------------------------------------------------------------


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


async def _change_to_sek_between_sessions(hass: HomeAssistant, freezer):
    """Period 0 in kr; kr -> SEK in period 1; sessions in periods 1-3; all four compiled."""
    freezer.move_to(_period(0) + timedelta(seconds=10))
    entry = await setup_legacy_entry(hass)
    await _completed(hass, "Paul", "regular", 10.0)
    await _completed(hass, "Gaest", "guest", 4.0)
    await _compile(hass, freezer, 0)

    freezer.move_to(_period(1) + timedelta(seconds=10))
    await options_save(hass, entry, {CONF_CURRENCY: "SEK", **STATIC})
    for n, cost in ((1, 5.0), (2, 2.0), (3, 1.0)):
        freezer.move_to(_period(n) + timedelta(seconds=20))
        await _completed(hass, "Paul", "regular", cost)
        await _completed(hass, "Gaest", "guest", cost)
        await _compile(hass, freezer, n)

    instance = get_instance(hass)
    await instance.async_add_executor_job(rstats.update_statistics_issues, hass)
    await hass.async_block_till_done()
    ids = {_entity_id(hass, entry, suffix) for suffix in TOTAL_COST}
    meta = await instance.async_add_executor_job(
        lambda: rstats.get_metadata(hass, statistic_ids=ids)
    )
    rows = await instance.async_add_executor_job(
        lambda: rstats.statistics_during_period(
            hass, P0, None, ids, "5minute", None, {"state", "sum"}
        )
    )
    issues = {i.issue_id for i in ir.async_get(hass).issues.values() if i.domain == "sensor"}
    return {
        eid: {
            "unit": meta[eid][1]["unit_of_measurement"],
            "periods": [int((r["start"] - P0.timestamp()) // 300) for r in rows.get(eid, [])],
            "rows": rows.get(eid, []),
            "issue": f"units_changed_{eid}" in issues,
        }
        for eid in ids
    }


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


async def test_statistics_stop_without_the_relabel(hass: HomeAssistant, freezer) -> None:
    """T-16, the control: the same run with D-65 switched off stops every statistic.

    Shows that T-15's rows come from the relabel, not from Home Assistant.
    """
    with patch(RELABEL):
        result = await _change_to_sek_between_sessions(hass, freezer)

    for eid, stat in result.items():
        assert stat["unit"] == "kr", eid
        assert stat["issue"] is True, eid
        assert stat["periods"] == [0], eid
    assert len(result) == len(TOTAL_COST)
