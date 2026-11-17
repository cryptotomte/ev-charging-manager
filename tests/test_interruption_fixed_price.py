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
    INTERRUPTIONS,
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
