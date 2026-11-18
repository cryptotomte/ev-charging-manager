"""Spec 027 RT-08, RT-11…RT-16: a spot-priced session bills every kWh exactly once.

Same acceptance rule as ``test_interruption_fixed_price``: each test holds the interrupted
runs and the uninterrupted run ("none") to the same expected values; allowed differences are
stated. Spot 1.0 / 2.0 / 3.0 kr/kWh in hours 10 / 11 / 12, no additional cost, VAT 1.0.
"""

from __future__ import annotations

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from tests.session_rig import (
    B_TRUTH,
    DAY,
    ETO,
    FIELD_ORDER,
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


@pytest.mark.parametrize("charger", ["present", "late"])
async def test_rt14_price_entity_not_loaded_at_resume_hour_passed(
    hass, hass_storage, freezer, charger
) -> None:
    """RT-14 / D-54: hour passed → the stored hour keeps its price unflagged; the new
    hour is captured while the price entity is not loaded yet, so it takes the entity's
    first valid state within the hour (2.0 kr/kWh at 11:10) instead of the fallback, as
    uninterrupted. (Before D-54: hour 11 at the fallback 9.0 kr/kWh, flagged.)"""

    async def down_then_spot_late(rig: Rig) -> None:
        await restart(charger, during=_down_across_one_boundary, spot_late=True)(rig)

    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_PAUL, {"span": down_then_spot_late})
    assert rig.money() == [B_TRUTH]


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


OTHER_SPOT = "sensor.other_spot_price"


def _save_spot_options(additional_cost: float, price_entity: str) -> Interruption:
    async def _save(rig: Rig) -> None:
        rig.hass.states.async_set(OTHER_SPOT, "7.0")
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
                "spot_price_entity": price_entity,
                "spot_additional_cost_kwh": additional_cost,
                "spot_vat_multiplier": 1.0,
                "spot_fallback_price_kwh": 9.0,
            },
        )
        await rig.hass.async_block_till_done()
        assert result["type"] == "create_entry"

    return _save


CHANGES = {
    # options saved in hour 10, price of the next session's hour 12
    "none": (None, 3.0),
    "additional-cost": (_save_spot_options(1.0, SPOT), 4.0),
    "price-entity": (_save_spot_options(0.0, OTHER_SPOT), 7.0),
}


@pytest.mark.parametrize("change", list(CHANGES))
async def test_rt08_spot_parameters_changed_during_a_spot_session(
    hass, hass_storage, freezer, change
) -> None:
    """RT-08: the running session keeps its own spot parameters, its price entity
    included; the next session uses the new ones."""
    save, next_price = CHANGES[change]
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await scenario_b(rig, TRX_PAUL, {"h1": save} if save else {})
    await rig.goto("12:40:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("12:50:00")
    await rig.set(energy=10.0)
    await rig.set(power=0)
    await rig.goto("12:55:00")
    await rig.unplug()

    first, second = rig.money()
    assert first == B_TRUTH
    assert second["details"] == [("12:00", 1.0, next_price, False)]


async def _fire_now(rig: Rig) -> None:
    """What the event loop does with a timer whose time has already come: fire it now."""
    async_fire_time_changed(rig.hass, dt_util.utcnow())
    await rig.hass.async_block_till_done()


async def _resume_in_the_first_second(rig: Rig) -> None:
    rig.freezer.move_to(f"{DAY} 11:00:00.400+00:00")


@pytest.mark.parametrize("start", ["live", "resume"])
async def test_d55_tick_in_the_first_second_of_the_hour_in_progress(
    hass, hass_storage, freezer, start
) -> None:
    """D-55: a session started, or resumed after hour 10 passed, within the first second
    of hour 11 arms the hour timer in that second; its tick finds hour 11 in progress
    and does nothing, so hour 11 appears once."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    if start == "live":
        await rig.goto("10:59:00")
        await rig.set(spot=2.0)
        freezer.move_to(f"{DAY} 11:00:00.300+00:00")
        await rig.plug_in(TRX_PAUL)
    else:
        await rig.goto("10:06:00")
        await rig.plug_in(TRX_PAUL)
        await rig.goto("10:30:00")
        await rig.set(energy=3.0)
        await rig.goto("10:59:00")
        await rig.set(spot=2.0)
        await rig.goto("10:59:30")
        await restart(during=_resume_in_the_first_second)(rig)
    await _fire_now(rig)
    await rig.goto("11:20:00")
    await rig.set(energy=5.0)
    await rig.set(power=0)
    await rig.goto("11:30:00")
    await rig.unplug()

    (money,) = rig.money()
    if start == "live":
        assert money["details"] == [("11:00", 5.0, 2.0, False)]
    else:
        assert money["details"] == [("10:00", 3.0, 1.0, False), ("11:00", 2.0, 2.0, False)]
    _assert_billed_once(money)


async def _clock_back_to_10_58(rig: Rig) -> None:
    rig.freezer.move_to(f"{DAY} 10:58:00+00:00")


async def test_d55_stored_hour_later_than_the_clock_continues(
    hass, hass_storage, freezer, caplog
) -> None:
    """D-55: the host's clock reads 10:58 at boot, earlier than the stored hour 11 (no
    RTC). The stored hour is not closed but continues, a warning says so, and the tick
    at 11:00 finds it in progress: every hour once, every kWh at its hour's price."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:30:00")
    await rig.set(energy=3.0)
    await rig.goto("10:59:00")
    await rig.set(spot=2.0)
    await rig.goto("11:20:00")
    await rig.set(energy=5.0)
    await rig.goto("11:30:00")  # the periodic save at 11:25 holds 5.0 kWh in hour 11
    await restart(during=_clock_back_to_10_58)(rig)
    assert "is later than the clock" in caplog.text
    assert [d["hour"][11:16] for d in rig.engine.active_session.price_details] == ["10:00"]
    await rig.goto("11:10:00")
    await rig.set(energy=6.0)
    await rig.set(power=0)
    await rig.goto("11:20:00")
    await rig.unplug()

    (money,) = rig.money()
    assert money["details"] == [("10:00", 3.0, 1.0, False), ("11:00", 3.0, 2.0, False)]
    assert money["cost"] == 9.0
    _assert_billed_once(money)


async def test_x9_tick_of_an_earlier_hour_does_not_close_the_stored_hour(
    hass, hass_storage, freezer
) -> None:
    """§ 12 X-9: the stored hour is 12 and the clock reads 10:58 at boot. The tick at
    11:00 is for an hour earlier than the one in progress: it must not close hour 12 and
    open hour 11, or the tick at 12:00 would bill hour 12 a second time."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:30:00")
    await rig.set(energy=3.0)
    await rig.goto("10:59:00")
    await rig.set(spot=2.0)
    await rig.goto("11:30:00")
    await rig.set(energy=5.0)
    await rig.goto("11:59:00")
    await rig.set(spot=3.0)
    await rig.goto("12:20:00")
    await rig.set(energy=6.0)
    await rig.goto("12:30:00")  # the periodic save at 12:25 holds hour 12 open
    await restart(during=_clock_back_to_10_58)(rig)
    await rig.goto("11:01:00")
    engine = rig.engine
    assert [d["hour"][11:16] for d in engine.active_session.price_details] == ["10:00", "11:00"]
    assert engine._hour_start_time == f"{DAY}T12:00+00:00"  # noqa: SLF001
    await rig.set(energy=7.0)
    await rig.set(power=0)
    await rig.goto("11:10:00")
    await rig.unplug()

    (money,) = rig.money()
    assert money["details"] == [
        ("10:00", 3.0, 1.0, False),
        ("11:00", 2.0, 2.0, False),
        ("12:00", 2.0, 3.0, False),
    ]
    _assert_billed_once(money)


async def _card_and_cable_while_down(rig: Rig) -> None:
    """Paul shows his card and plugs in at 10:10 while down; 2.0 kWh by 10:20."""
    await rig.goto("10:10:00", fire=False)
    await rig.set(trx=TRX_PAUL)
    await rig.set(plug="on", lock="Locked")
    await rig.set(power=7000)
    await rig.goto("10:20:00", fire=False)
    await rig.set(energy=2.0)


async def test_d54_cable_in_session_with_the_price_entity_late(hass, hass_storage, freezer):
    """D-54 (D-51 path): the session of a cable already in starts at setup while the
    price entity is not loaded yet; the hour takes its first valid state (1.0), not the
    fallback for the whole counter (reviewer F: 45.0 kr for 5.0)."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await restart("late", during=_card_and_cable_while_down, spot_late=True)(rig)
    await rig.goto("10:40:00")
    await rig.set(energy=5.0)
    await rig.set(power=0)
    await rig.goto("10:50:00")
    await rig.unplug()

    (money,) = rig.money()
    assert money["details"] == [("10:00", 5.0, 1.0, False)]
    assert money["cost"] == 5.0


async def test_d54_pending_price_updates_the_running_cost_and_the_snapshot(
    hass, hass_storage, freezer
) -> None:
    """D-54: when the price entity's first valid state prices the hour in progress, the
    session's running cost is recomputed and the snapshot stores the hour's price at once,
    not at the next counter reading or save."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await restart("late", during=_card_and_cable_while_down, spot_late=True, order=FIELD_ORDER)(rig)
    await rig.goto("10:22:00")
    session = rig.engine.active_session
    assert session.cost_total_kr == 18.0, "2.0 kWh at the fallback while the price is pending"
    assert rig.snapshot()["resume_state"]["spot_hour"]["price"] is None

    await rig.set(spot=1.0)

    assert session.cost_total_kr == 2.0
    snapshot = rig.snapshot()
    assert snapshot["resume_state"]["spot_hour"]["price"] == 1.0
    assert snapshot["cost_total_kr"] == 2.0


async def _paul_leaves_and_the_markup_guest_charges(rig: Rig) -> None:
    await rig.goto("11:10:00", fire=False)
    await rig.set(power=0)
    await rig.unplug()
    await rig.set(trx="null")
    await rig.goto("11:20:00", fire=False)
    await rig.set(energy=0.0)
    await rig.plug_in(TRX_GMARK)
    await rig.goto("11:50:00", fire=False)
    await rig.set(energy=4.0)


async def test_d54_split_session_with_the_price_entity_late(hass, hass_storage, freezer):
    """D-54 (D-50 path): the guest's session started by the split while the price
    entity is not loaded yet bills hour 11 at the entity's first valid state (2.0), not
    at the fallback (reviewer F: 36.0 kr for 8.0)."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:30:00")
    await rig.set(energy=3.0)
    await rig.goto("10:59:00")
    await rig.set(spot=2.0)
    await rig.goto("11:06:00")  # the periodic save at 11:05 holds hour 10 closed
    await restart("late", during=_paul_leaves_and_the_markup_guest_charges, spot_late=True)(rig)
    await rig.goto("12:00:00")
    await rig.set(power=0)
    await rig.goto("12:10:00")
    await rig.unplug()

    paul, guest = rig.money()
    assert (paul["user"], paul["energy"], paul["cost"]) == ("Paul", 3.0, 3.0)
    assert guest["user"] == "Markup"
    assert guest["details"][0] == ("11:00", 4.0, 2.0, False)
    assert (guest["energy"], guest["cost"], guest["charge_price"]) == (4.0, 8.0, 12.0)


async def test_d54_hour_ends_before_the_price_entity_loads(hass, hass_storage, freezer):
    """D-54: the price entity does not load within hour 10, so hour 10 takes the
    fallback as today; it loads at 11:05, and hour 11 (captured while it still had no
    state) takes that first valid state."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await restart("late", during=_card_and_cable_while_down, spot_late=True, order=FIELD_ORDER)(rig)
    await rig.goto("11:05:00")
    await rig.set(spot=2.0)
    await rig.goto("11:30:00")
    await rig.set(energy=3.0)
    await rig.set(power=0)
    await rig.goto("11:40:00")
    await rig.unplug()

    (money,) = rig.money()
    assert money["details"] == [("10:00", 2.0, 9.0, True), ("11:00", 1.0, 2.0, False)]
    assert money["cost"] == 20.0


async def test_d54_a_state_of_the_next_hour_does_not_price_the_hour_in_progress(
    hass, hass_storage, freezer
) -> None:
    """D-54: the price entity's first valid state arrives at 11:00:00.5, before the
    hour tick has run; it carries hour 11's price, so hour 10 keeps the fallback and
    hour 11 is captured from it at the tick (the boundary race of spec 025)."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await restart("late", during=_card_and_cable_while_down, spot_late=True, order=FIELD_ORDER)(rig)
    await rig.goto("10:59:00")
    freezer.move_to(f"{DAY} 11:00:00.500+00:00")
    await rig.set(spot=2.0)
    await _fire_now(rig)
    await rig.goto("11:30:00")
    await rig.set(energy=3.0)
    await rig.set(power=0)
    await rig.goto("11:40:00")
    await rig.unplug()

    (money,) = rig.money()
    assert money["details"] == [("10:00", 2.0, 9.0, True), ("11:00", 1.0, 2.0, False)]


async def test_d54_price_listener_is_gone_after_unload(hass, hass_storage, freezer) -> None:
    """D-54: the listener that waits for the price entity's first valid state ends with
    the entry; the unloaded engine no longer sees the entity."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await restart("late", during=_card_and_cable_while_down, spot_late=True, order=FIELD_ORDER)(rig)
    engine = rig.engine
    assert engine.active_session is not None and SPOT not in engine._spot_seen_valid  # noqa: SLF001
    assert await hass.config_entries.async_unload(rig.entry.entry_id)
    await hass.async_block_till_done()
    await rig.set(spot=1.0)
    assert SPOT not in engine._spot_seen_valid  # noqa: SLF001


async def test_d55_stored_hour_later_than_the_clock_awaits_no_close(
    hass, hass_storage, freezer
) -> None:
    """D-55: with the clock behind the stored hour and no counter reading at the resume,
    the stored hour is not marked for closing at the first reading; it is the hour in
    progress."""
    rig = Rig(hass, freezer, hass_storage, pricing="spot")
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:30:00")
    await rig.set(energy=3.0)
    await rig.goto("11:30:00")
    await restart("late", during=_clock_back_to_10_58, order=["plug", "lock", "trx", "power"])(rig)
    engine = rig.engine
    assert engine.active_session is not None
    assert engine._hour_start_time == f"{DAY}T11:00+00:00"  # noqa: SLF001
    assert engine._spot_close_pending is False  # noqa: SLF001
