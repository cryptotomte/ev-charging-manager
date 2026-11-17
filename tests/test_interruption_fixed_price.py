"""Spec 027 RT-01…RT-10: a fixed-price or guest session survives reload, restart and crash.

Acceptance rule (§ 6.3): every interrupted run ends with the same completed event and stored
record as the uninterrupted run of the same scenario. Each test is parametrized with "none"
(the uninterrupted run) and asserts both against the same expected values, so a passing
"none" case is the reference the interrupted cases are held to. Allowed differences are
stated where a test has them.
"""

from __future__ import annotations

import pytest

from tests.session_rig import (
    ETO,
    INTERRUPTIONS,
    LOCK,
    PLUG,
    SPOT_DATA,
    TRX_GFIX,
    TRX_GMARK,
    TRX_PAUL,
    Interruption,
    Rig,
    restart,
)

PLUG_LAST = ["energy", "power", "trx", "lock", "car", "eto", "plug"]

# Scenario A, Paul (vehicle 50 kWh, efficiency 0.9) at 2.50 kr/kWh.
PAUL_4KWH = {
    "user": "Paul",
    "energy": 4.0,
    "cost": 10.0,
    "charge_price": None,
    "charge_method": None,
    "record_energy": 4.0,
    "record_cost": 10.0,
    "record_charge_price": None,
    "details_kwh": 0,
    "details": [],
}


async def scenario_a(rig: Rig, trx: str, at: dict[str, Interruption]) -> None:
    """One continuous charge 10:06–10:26, unplug 10:40 (research 027 scenario A)."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(trx)
    await rig.goto("10:08:00")
    await rig.set(energy=0.5, eto=1000.5)
    await rig.goto("10:08:30")
    if "young" in at:
        await at["young"](rig)
    await rig.goto("10:12:00")
    await rig.set(energy=1.5, eto=1001.5)
    await rig.goto("10:17:00")
    await rig.set(energy=2.5, eto=1002.5)
    await rig.goto("10:17:30")
    if "mid" in at:
        await at["mid"](rig)
    await rig.goto("10:22:00")
    if "mid2" in at:
        await at["mid2"](rig)
    await rig.goto("10:25:00")
    await rig.set(energy=4.0, eto=1004.0)
    await rig.goto("10:26:00")
    await rig.set(power=0)
    await rig.goto("10:40:00")
    await rig.unplug()
    await rig.goto("10:41:00")


def _assert_one_paul_session(rig: Rig) -> None:
    assert rig.money() == [PAUL_4KWH]
    assert len(rig.started) == 1, "an interruption must not start a second session"
    (record,) = rig.records()
    assert record["charger_total_before_kwh"] == 1000.0
    assert record["charger_total_after_kwh"] == 1004.0
    assert rig.completed[0].data["estimated_soc_added_pct"] == 7.2
    assert rig.stats.user_stats["Paul"].total_cost_kr == pytest.approx(10.0)
    assert rig.snapshot() is None


INTERRUPTION_CASES = {
    **INTERRUPTIONS,
    "restart-late-pluglast": restart("late", order=PLUG_LAST),
}


@pytest.mark.parametrize("intr", list(INTERRUPTION_CASES))
async def test_rt01_interruption_eleven_minutes_in(hass, hass_storage, freezer, intr) -> None:
    """RT-01: energy, cost and the charger totals as uninterrupted."""
    fn = INTERRUPTION_CASES[intr]
    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, TRX_PAUL, {"mid": fn} if fn else {})
    _assert_one_paul_session(rig)


@pytest.mark.parametrize("intr", list(INTERRUPTION_CASES))
async def test_rt02_interruption_younger_than_the_periodic_save(
    hass, hass_storage, freezer, intr
) -> None:
    """RT-02: 2.5 minutes in, before the first periodic save; one session, full energy."""
    fn = INTERRUPTION_CASES[intr]
    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, TRX_PAUL, {"young": fn} if fn else {})
    _assert_one_paul_session(rig)


async def test_rt05_orderly_stop_writes_the_engine_state(hass, hass_storage, freezer) -> None:
    """RT-05: after ``async_stop`` the snapshot on disk holds the engine's energy.

    The last periodic save (10:15) holds 1.5 kWh; the engine holds 2.5 kWh at the stop.
    """
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=1.5)
    await rig.goto("10:17:00")
    await rig.set(energy=2.5)
    await rig.goto("10:17:30")
    engine = rig.engine

    await hass.async_stop()

    snapshot = rig.snapshot()
    assert snapshot is not None
    assert snapshot["energy_kwh"] == engine.active_session.energy_kwh == 2.5
    assert snapshot["cost_total_kr"] == 6.25
    assert snapshot["resume_state"]["pricing"] == {"mode": "static", "static_price_kwh": 2.5}


async def _finish_charge(rig: Rig, *, fire: bool = True) -> None:
    """The car charges to 4.0 kWh and stops; with fire=False this happens while down."""
    await rig.goto("10:25:00", fire=fire)
    await rig.set(energy=4.0, eto=1004.0)
    await rig.goto("10:26:00", fire=fire)
    await rig.set(power=0)
    await rig.goto("10:35:00", fire=fire)


async def scenario_f(rig: Rig, trx: str, interruption: Interruption | None) -> None:
    """The charge finishes during the outage; no energy update follows start-up (research F)."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(trx)
    await rig.goto("10:12:00")
    await rig.set(energy=1.5, eto=1001.5)
    await rig.goto("10:17:00")
    await rig.set(energy=2.5, eto=1002.5)
    await rig.goto("10:17:30")
    if interruption is None:
        await _finish_charge(rig)
    else:
        await interruption(rig)
    await rig.goto("10:40:00")
    await rig.unplug()
    await rig.goto("10:41:00")


async def _finish_while_down(rig: Rig) -> None:
    await _finish_charge(rig, fire=False)


@pytest.mark.parametrize(
    "intr",
    [
        "none",
        "restart-present",
        "restart-late",
    ],
)
async def test_rt03_charge_finishes_during_the_outage(hass, hass_storage, freezer, intr) -> None:
    """RT-03: cost equals energy × price and the SoC estimate is present."""
    interruption = {
        "none": None,
        "restart-present": restart(during=_finish_while_down),
        "restart-late": restart("late", during=_finish_while_down),
    }[intr]
    rig = Rig(hass, freezer, hass_storage)
    await scenario_f(rig, TRX_PAUL, interruption)
    _assert_one_paul_session(rig)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("reload", "reload"),
        ("restart-present", "restart-present"),
        ("reload", "restart-late"),
    ],
)
async def test_rt04_two_interruptions_in_one_session(
    hass, hass_storage, freezer, first, second
) -> None:
    """RT-04: two interruptions in one session end as uninterrupted (RT-01's reference)."""
    rig = Rig(hass, freezer, hass_storage)
    at = {"mid": INTERRUPTIONS[first], "mid2": INTERRUPTIONS[second]}
    await scenario_a(rig, TRX_PAUL, at)
    _assert_one_paul_session(rig)


def save_options(pricing: dict) -> Interruption:
    """Save the options form (observation step, then pricing step); it reloads the entry."""

    async def _save(rig: Rig) -> None:
        flow = rig.hass.config_entries.options
        result = await flow.async_init(rig.entry.entry_id)
        result = await flow.async_configure(
            result["flow_id"],
            {"plug_entity": PLUG, "cable_lock_entity": LOCK, "eto_entity": ETO},
        )
        result = await flow.async_configure(result["flow_id"], pricing)
        await rig.hass.async_block_till_done()
        assert result["type"] == "create_entry"

    return _save


async def _second_session(rig: Rig) -> None:
    """A second session after scenario A: 1.0 kWh, 10:45–10:58."""
    await rig.goto("10:45:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:55:00")
    await rig.set(energy=5.0)
    await rig.set(power=0)
    await rig.goto("10:58:00")
    await rig.unplug()


@pytest.mark.parametrize("change", ["none", "same", "price"])
async def test_rt06_fixed_price_changed_during_a_session(
    hass, hass_storage, freezer, change
) -> None:
    """RT-06: the running session keeps 2.50 kr/kWh; the next one is priced at 4.00."""
    new_price = {"none": 2.5, "same": 2.5, "price": 4.0}[change]
    at = (
        {}
        if change == "none"
        else {"mid": save_options({"pricing_mode": "static", "static_price_kwh": new_price})}
    )
    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, TRX_PAUL, at)
    await _second_session(rig)

    first, second = rig.money()
    assert first == PAUL_4KWH
    assert second["energy"] == 1.0
    assert second["cost"] == 1.0 * new_price


async def test_rt07_fixed_to_spot_during_a_session(hass, hass_storage, freezer) -> None:
    """RT-07: the running session completes at its fixed price; the next one is spot."""
    rig = Rig(hass, freezer, hass_storage)
    spot = {"pricing_mode": "spot", "static_price_kwh": 2.5, **SPOT_DATA}
    await scenario_a(rig, TRX_PAUL, {"mid": save_options(spot)})
    await _second_session(rig)

    first, second = rig.money()
    assert first == PAUL_4KWH
    assert rig.completed[0].data["cost_method"] == "static"
    assert rig.completed[1].data["cost_method"] == "spot"
    # Spot 1.0 kr/kWh, no additional cost, VAT multiplier 1.0.
    assert second["cost"] == 1.0
    assert second["details"] == [("10:00", 1.0, 1.0, False)]


GUESTS = {
    # trx, charge price for 4.0 kWh at 2.50 kr/kWh, method
    "fixed": (TRX_GFIX, "Gaest", 20.0),
    "markup": (TRX_GMARK, "Markup", 15.0),
}


@pytest.mark.parametrize("intr", list(INTERRUPTIONS))
@pytest.mark.parametrize("method", list(GUESTS))
async def test_rt09_guest_charge_price_survives(hass, hass_storage, freezer, method, intr) -> None:
    """RT-09: charge price and method as uninterrupted in event, record and guest_last."""
    trx, name, charge = GUESTS[method]
    fn = INTERRUPTIONS[intr]
    rig = Rig(hass, freezer, hass_storage)
    await scenario_a(rig, trx, {"mid": fn} if fn else {})

    assert rig.money() == [
        PAUL_4KWH
        | {
            "user": name,
            "charge_price": charge,
            "charge_method": method,
            "record_charge_price": charge,
        }
    ]
    assert rig.stats.guest_last.charge_price_kr == charge


async def _unplug_guest(rig: Rig, *, fire: bool = True) -> None:
    """The guest's car charges to 3.8 kWh and leaves at 10:30; with fire=False while down."""
    await rig.goto("10:30:00", fire=fire)
    await rig.set(energy=3.8)
    await rig.set(power=0)
    await rig.unplug()
    await rig.set(trx="null")
    await rig.goto("10:45:00", fire=fire)


async def _unplug_guest_while_down(rig: Rig) -> None:
    await _unplug_guest(rig, fire=False)


@pytest.mark.parametrize("intr", ["none", "restart-present", "restart-late-energy-first"])
async def test_rt10_guest_unplugged_during_the_outage(hass, hass_storage, freezer, intr) -> None:
    """RT-10: energy, cost and charge price from the final counter (3.8 kWh).

    Allowed difference: the end time is the start-up time, the unplug was not observed.
    """
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_GFIX)
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:22:00")
    if intr == "none":
        await _unplug_guest(rig)
    else:
        order = ["energy", "plug", "lock", "trx", "power", "car", "eto"]
        await {
            "restart-present": restart(during=_unplug_guest_while_down),
            "restart-late-energy-first": restart(
                "late", during=_unplug_guest_while_down, order=order
            ),
        }[intr](rig)
    await rig.goto("10:46:00")

    (money,) = rig.money()
    assert (money["energy"], money["cost"], money["charge_price"]) == (3.8, 9.5, 19.0)
    assert (money["record_energy"], money["record_cost"], money["record_charge_price"]) == (
        3.8,
        9.5,
        19.0,
    )
    assert money["charge_method"] == "fixed"
    assert rig.stats.guest_last.charge_price_kr == 19.0
