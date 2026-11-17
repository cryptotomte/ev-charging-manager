"""Spec 027 RT-08, RT-11…RT-16: a spot-priced session bills every kWh exactly once.

Same acceptance rule as ``test_interruption_fixed_price``: each test holds the interrupted
runs and the uninterrupted run ("none") to the same expected values; allowed differences are
stated. Spot 1.0 / 2.0 / 3.0 kr/kWh in hours 10 / 11 / 12, no additional cost, VAT 1.0.
"""

from __future__ import annotations

import pytest

from tests.session_rig import (
    B_TRUTH,
    ETO,
    INTERRUPTIONS,
    LOCK,
    PLUG,
    SPOT,
    TRX_GMARK,
    TRX_PAUL,
    Interruption,
    Rig,
    finish_while_down,
    reload,
    restart,
    scenario_b,
    scenario_f,
)


async def _down_across_one_boundary(rig: Rig) -> None:
    """Down 10:50–11:10; the car charges to 4.0 kWh and the price flips at 10:59."""
    await rig.goto("10:55:00", fire=False)
    await rig.set(energy=4.0)
    await rig.goto("10:59:00", fire=False)
    await rig.set(spot=2.0)
    await rig.goto("11:10:00", fire=False)


def _assert_billed_once(money: dict) -> None:
    hours = [d[0] for d in money["details"]]
    assert len(hours) == len(set(hours)), "an hour appears twice"
    assert money["details_kwh"] == money["energy"] == money["record_energy"]


@pytest.mark.parametrize("intr", list(INTERRUPTIONS))
@pytest.mark.parametrize("point", ["h1", "h2", "final"])
async def test_rt11_interruption_in_the_first_a_later_and_the_last_hour(
    hass, hass_storage, freezer, point, intr
) -> None:
    """RT-11: cost and price_details as uninterrupted."""
    fn = INTERRUPTIONS[intr]
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_PAUL, {point: fn} if fn else {})
    assert rig.money() == [B_TRUTH]


@pytest.mark.parametrize("intr", ["none", "restart-present", "restart-late"])
async def test_rt12_down_across_one_hour_boundary(hass, hass_storage, freezer, intr) -> None:
    """RT-12: no energy was delivered in hour 11 before the restart, so as uninterrupted."""
    interruption = {
        "none": None,
        "restart-present": restart(during=_down_across_one_boundary),
        "restart-late": restart("late", during=_down_across_one_boundary),
    }[intr]
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_PAUL, {"span": interruption} if interruption else {})
    assert rig.money() == [B_TRUTH]


async def _down_across_two_boundaries(rig: Rig) -> None:
    """Down 10:50–12:10 while the car charges to 7.0 kWh through hours 10 and 11."""
    await rig.goto("10:55:00", fire=False)
    await rig.set(energy=4.0)
    await rig.goto("10:59:00", fire=False)
    await rig.set(spot=2.0)
    await rig.goto("11:30:00", fire=False)
    await rig.set(energy=6.0)
    await rig.goto("11:55:00", fire=False)
    await rig.set(energy=7.0)
    await rig.goto("11:59:00", fire=False)
    await rig.set(spot=3.0)
    await rig.goto("12:10:00", fire=False)


@pytest.mark.parametrize("intr", ["restart-present", "restart-late"])
async def test_rt12_down_across_two_hour_boundaries(hass, hass_storage, freezer, intr) -> None:
    """RT-12: the 7.0 kWh not yet billed at the restart are one entry for hour 10 at its
    stored price; hour 12 as measured live.

    Allowed difference from the uninterrupted 16.0 kr: the 3.0 kWh of hour 11, delivered
    while Home Assistant was down, cannot be split by hour and are billed at hour 10's
    price (D-39, Ö-4), so the cost is 7.0 + 6.0 = 13.0 kr and hour 11 has no entry.
    """
    interruption = {
        "restart-present": restart(during=_down_across_two_boundaries),
        "restart-late": restart("late", during=_down_across_two_boundaries),
    }[intr]
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:30:00")
    await rig.set(energy=3.0)
    await rig.goto("10:50:00")
    await interruption(rig)
    await rig.goto("12:20:00")
    await rig.set(energy=9.0)
    await rig.goto("12:21:00")
    await rig.set(power=0)
    await rig.goto("12:30:00")
    await rig.unplug()

    (money,) = rig.money()
    assert money["details"] == [("10:00", 7.0, 1.0, False), ("12:00", 2.0, 3.0, False)]
    assert (money["energy"], money["cost"], money["record_cost"]) == (9.0, 13.0, 13.0)
    _assert_billed_once(money)


@pytest.mark.parametrize("intr", ["none", "restart-present", "restart-late"])
async def test_rt13_spot_charge_finishes_during_the_outage(hass, hass_storage, freezer, intr):
    """RT-13: the cost is the energy at the stored hour's price (4.0 kWh @1.0)."""
    interruption = {
        "none": None,
        "restart-present": restart(during=finish_while_down),
        "restart-late": restart("late", during=finish_while_down),
    }[intr]
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_f(rig, TRX_PAUL, interruption)
    (money,) = rig.money()
    assert money["details"] == [("10:00", 4.0, 1.0, False)]
    assert (money["energy"], money["cost"], money["record_cost"]) == (4.0, 4.0, 4.0)


SPOT_LATE = restart("late", absent=["spot"], order=["spot"])


async def test_rt14_price_entity_invalid_at_resume_same_hour(hass, hass_storage, freezer):
    """RT-14: same hour → the stored price is used, no fallback flag (as uninterrupted)."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_PAUL, {"h2": SPOT_LATE})
    assert rig.money() == [B_TRUTH]


async def test_rt14_price_entity_invalid_at_resume_hour_passed(hass, hass_storage, freezer):
    """RT-14: hour passed → the stored hour keeps its price unflagged; the new hour is
    captured while the price entity is missing, so it uses the fallback and is flagged.

    Allowed difference from the uninterrupted run: hour 11 at the fallback 9.0 kr/kWh.
    """

    async def down_then_spot_late(rig: Rig) -> None:
        await restart("late", during=_down_across_one_boundary, absent=["spot"], order=["spot"])(
            rig
        )

    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_PAUL, {"span": down_then_spot_late})
    (money,) = rig.money()
    assert money["details"] == [
        ("10:00", 4.0, 1.0, False),
        ("11:00", 3.0, 9.0, True),
        ("12:00", 2.0, 3.0, False),
    ]
    assert money["cost"] == 4.0 + 27.0 + 6.0


async def _restart_twice(rig: Rig) -> None:
    await restart()(rig)
    await restart()(rig)


async def _span_then_restart(rig: Rig) -> None:
    await restart(during=_down_across_one_boundary)(rig)
    await restart()(rig)


async def _span_late_then_restart(rig: Rig) -> None:
    """The second restart comes before any energy reading closed the passed hour."""
    await restart("late", during=_down_across_one_boundary, order=["plug", "lock", "trx", "power"])(
        rig
    )
    await restart()(rig)


@pytest.mark.parametrize(
    "at",
    [
        {"h1": reload, "h2": reload},
        {"h2": _restart_twice},
        {"span": _span_then_restart},
        {"span": _span_late_then_restart},
    ],
    ids=[
        "reload-h1-and-h2",
        "restart-after-restart",
        "span-then-restart",
        "span-late-then-restart",
    ],
)
async def test_rt15_repeated_interruptions_bill_no_hour_twice(hass, hass_storage, freezer, at):
    """RT-15: two reloads in different hours, or a restart right after a restart."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_PAUL, at)
    assert rig.money() == [B_TRUTH]


@pytest.mark.parametrize("intr", list(INTERRUPTIONS))
async def test_rt16_guest_markup_on_spot(hass, hass_storage, freezer, intr) -> None:
    """RT-16: the charge price is the markup on the correct cost (1.5 × 16.0)."""
    fn = INTERRUPTIONS[intr]
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_GMARK, {"h2": fn} if fn else {})
    assert rig.money() == [
        B_TRUTH
        | {
            "user": "Markup",
            "charge_price": 24.0,
            "charge_method": "markup",
            "record_charge_price": 24.0,
        }
    ]


def _save_spot_options(additional_cost: float) -> Interruption:
    async def _save(rig: Rig) -> None:
        flow = rig.hass.config_entries.options
        result = await flow.async_init(rig.entry.entry_id)
        result = await flow.async_configure(
            result["flow_id"],
            {"plug_entity": PLUG, "cable_lock_entity": LOCK, "eto_entity": ETO},
        )
        result = await flow.async_configure(
            result["flow_id"],
            {
                "pricing_mode": "spot",
                "static_price_kwh": 2.5,
                "spot_price_entity": SPOT,
                "spot_additional_cost_kwh": additional_cost,
                "spot_vat_multiplier": 1.0,
                "spot_fallback_price_kwh": 9.0,
            },
        )
        await rig.hass.async_block_till_done()
        assert result["type"] == "create_entry"

    return _save


@pytest.mark.parametrize("change", ["none", "additional-cost"])
async def test_rt08_spot_parameters_changed_during_a_spot_session(
    hass, hass_storage, freezer, change
) -> None:
    """RT-08: the running session keeps its own spot parameters; the next uses the new."""
    at = {} if change == "none" else {"h1": _save_spot_options(1.0)}
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_PAUL, at)
    await rig.goto("12:40:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("12:50:00")
    await rig.set(energy=10.0)
    await rig.set(power=0)
    await rig.goto("12:55:00")
    await rig.unplug()

    first, second = rig.money()
    assert first == B_TRUTH
    expected_price = 3.0 + (1.0 if change == "additional-cost" else 0.0)
    assert second["details"] == [("12:00", 1.0, expected_price, False)]
