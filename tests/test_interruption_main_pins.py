"""Regression pins against main (spec 027 § 13.3).

The first version of spec 027 made these cases worse than main (§ 13.1). Each expected
value is what main records for the same sequence; the file imports only the rig, so it
runs unchanged against a checkout of main.
"""

from __future__ import annotations

import pytest

from tests.session_rig import TRX_GFIX, TRX_PAUL, Rig, reload, restart


def _summary(rig: Rig) -> list[tuple]:
    return [
        (m["user"], m["energy"], m["cost"], m["charge_price"], m["record_energy"])
        for m in rig.money()
    ]


async def test_pin_main_card_after_power_then_reload(hass, hass_storage, freezer) -> None:
    """Finding A: power beat the card at the plug-in (an Unknown session), the card
    shows a moment later, and an options save reloads the entry. The first version
    split the session and counted the connection twice (8.0 kWh); main records one
    session with 5.0 kWh."""
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.set(plug="on", lock="Locked")
    await rig.set(power=7000)
    await rig.set(trx=TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:22:00")
    await reload(rig)
    await rig.goto("10:55:00")
    await rig.set(energy=5.0)
    await rig.goto("10:56:00")
    await rig.set(power=0)
    await rig.goto("11:05:00")
    await rig.unplug()
    await rig.goto("11:06:00")

    assert _summary(rig) == [("Unknown", 5.0, 12.5, None, 5.0)]
    assert len(rig.started) == 1


async def test_pin_main_rfid_entity_invalid_while_another_user_charges(
    hass, hass_storage, freezer
) -> None:
    """Finding B: the RFID entity stays missing after a restart. Paul's snapshot resumes
    after the short wait (D-49), Paul leaves, and the next car's charge is its own
    session, as on main (it was held for an hour and billed to Paul)."""
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:22:00")
    await restart("late", absent=["trx"], order=[])(rig)
    await rig.goto("10:30:00")
    await rig.set(energy=3.5)
    await rig.set(power=0)
    await rig.unplug()
    await rig.goto("10:35:00")
    await rig.set(energy=0.0)
    await rig.set(plug="on", lock="Locked")
    await rig.set(power=7000)
    await rig.goto("11:15:00")
    await rig.set(energy=5.0)
    await rig.set(power=0)
    await rig.goto("11:40:00")
    await rig.unplug()
    await rig.goto("11:41:00")

    assert _summary(rig) == [("Paul", 3.5, 8.75, None, 3.5), ("Unknown", 5.0, 12.5, None, 5.0)]


async def test_pin_main_reload_after_the_rfid_entity_was_missing(
    hass, hass_storage, freezer
) -> None:
    """Reviewer case X7: the RFID entity missing after a restart, then a reload. The
    first version force-completed the snapshot at the 60-minute timeout and the reload
    counted it again; as on main, Paul has one session with 4.0 kWh."""
    rig = Rig(hass, freezer, hass_storage)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:12:00")
    await rig.set(energy=1.0)
    await rig.goto("10:18:00")
    await rig.set(energy=3.0)
    await rig.goto("10:22:30")
    await restart("late", absent=["trx"], order=[])(rig)
    await rig.goto("10:40:00")
    await rig.set(energy=4.0)
    await rig.set(power=0)
    await rig.goto("11:25:00")
    await reload(rig)
    await rig.set(trx=TRX_PAUL)
    await rig.goto("11:30:00")
    await rig.unplug()
    await rig.goto("11:31:00")

    assert _summary(rig) == [("Paul", 4.0, 10.0, None, 4.0)]
    assert len(rig.started) == 1


async def _paul_leaves_with_the_counter_at_10(rig: Rig) -> None:
    """Paul charges 10.0 kWh and unplugs; the counter keeps 10.0 until the next plug-in."""
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:20:00")
    await rig.set(energy=10.0, eto=1010.0)
    await rig.goto("10:30:00")
    await rig.set(power=0)
    await rig.goto("10:40:00")
    await rig.unplug()
    await rig.set(trx="null")
    await rig.goto("10:45:00")


async def _guest_charges_3_from_a_stale_counter(rig: Rig) -> None:
    """The guest's plug-on arrives while the counter still shows Paul's 10.0; it
    restarts at zero a moment later and the guest charges 3.0 kWh."""
    await rig.set(trx=TRX_GFIX)
    await rig.set(plug="on", lock="Locked")
    await rig.set(energy=0.0)
    await rig.set(power=7000)
    await rig.goto("11:00:00")
    await rig.set(energy=3.0)
    await rig.set(power=0)
    await rig.goto("11:10:00")
    await rig.unplug()
    await rig.goto("11:11:00")


@pytest.mark.parametrize("before", ["unavailable", "unknown", "off"])
async def test_pin_main_plug_on_from_no_valid_state_with_a_stale_counter(
    hass, hass_storage, freezer, before
) -> None:
    """Verification V-1: the plug entity has no valid state (or, as control, reads off)
    when the guest plugs in, and the counter still shows the previous connection. The
    zero baseline of the fix round billed the guest 13.0 kWh; main records 3.0."""
    rig = Rig(hass, freezer, hass_storage)
    await _paul_leaves_with_the_counter_at_10(rig)
    if before != "off":
        await rig.set(plug=before)
    await _guest_charges_3_from_a_stale_counter(rig)

    assert _summary(rig) == [("Paul", 10.0, 25.0, None, 10.0), ("Gaest", 3.0, 7.5, 15.0, 3.0)]


async def test_pin_main_charger_offline_then_back_plug_first(hass, hass_storage, freezer) -> None:
    """Verification V-1: every charger entity goes unavailable, the guest plugs in
    meanwhile, and on return the plug reads on together with the new counter."""
    rig = Rig(hass, freezer, hass_storage)
    await _paul_leaves_with_the_counter_at_10(rig)
    for name in ("plug", "lock", "trx", "energy", "power", "car", "eto"):
        await rig.set(**{name: "unavailable"})
    await rig.goto("10:50:00")
    await rig.set(plug="on", lock="Locked", trx=TRX_GFIX, power=7000, energy=0.5)
    await rig.goto("11:00:00")
    await rig.set(energy=3.0, power=0)
    await rig.goto("11:10:00")
    await rig.unplug()
    await rig.goto("11:11:00")

    assert _summary(rig) == [("Paul", 10.0, 25.0, None, 10.0), ("Gaest", 3.0, 7.5, 15.0, 3.0)]
