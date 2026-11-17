"""Regression pins against main (spec 027 § 13.3).

The first version of spec 027 made these cases worse than main (§ 13.1). Each expected
value is what main records for the same sequence; the file imports only the rig, so it
runs unchanged against a checkout of main.
"""

from __future__ import annotations

from tests.session_rig import TRX_PAUL, Rig, reload, restart


def _summary(rig: Rig) -> list[tuple]:
    return [
        (m["user"], m["energy"], m["cost"], m["charge_price"], m["record_energy"])
        for m in rig.money()
    ]


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
