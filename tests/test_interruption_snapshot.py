"""Spec 027 RT-27…RT-33: the active snapshot, the unchanged contract and unchanged behaviour."""

from __future__ import annotations

from tests.session_rig import (
    SPOT,
    TRX_GMARK,
    TRX_PAUL,
    Rig,
)

SPOT_PRICING = {
    "mode": "spot",
    "spot_price_entity": SPOT,
    "spot_additional_cost_kwh": 0.0,
    "spot_vat_multiplier": 1.0,
    "spot_fallback_price_kwh": 9.0,
}
MARKUP = {"method": "markup", "markup_factor": 1.5}


def _resume_state(hour: str, price: float) -> dict:
    return {
        "pricing": SPOT_PRICING,
        "guest_pricing": MARKUP,
        "spot_hour": {"start": f"2026-11-17T{hour}+00:00", "price": price, "fallback": False},
    }


async def test_rt27_snapshot_written_at_each_session_event(hass, hass_storage, freezer) -> None:
    """RT-27: start, window open, window close, hour tick, plug off and stop each write.

    The periodic save is pushed out to 3600 s (first at 11:05) so every change seen on disk
    comes from the event under test; the energy set just before each event is the marker.
    """
    rig = Rig(hass, freezer, hass_storage, pricing="spot", persistence_interval_s=3600)
    await rig.setup()
    await rig.goto("10:06:00")
    await rig.set(trx=TRX_GMARK)
    await rig.set(plug="on", lock="Locked")
    start = rig.snapshot()
    assert start is not None and start["energy_kwh"] == 0.0
    assert start["charger_total_before_kwh"] == 1000.0
    assert start["resume_state"] == _resume_state("10:00", 1.0)

    await rig.set(energy=0.2)
    await rig.set(power=7000)
    window_open = rig.snapshot()
    assert window_open["energy_kwh"] == 0.2
    assert window_open["charging_started_at"] is not None

    await rig.goto("10:30:00")
    await rig.set(energy=1.0)
    await rig.set(power=0)
    assert rig.snapshot()["energy_kwh"] == 0.2, "an energy update alone writes nothing"
    await rig.goto("10:36:00")  # idle timer (5 min) closes the window at 10:35
    window_close = rig.snapshot()
    assert window_close["energy_kwh"] == 1.0
    assert window_close["charging_ended_at"] is not None

    await rig.goto("10:50:00")
    await rig.set(energy=1.2, spot=2.0)
    await rig.goto("11:00:00")
    tick = rig.snapshot()
    assert tick["energy_kwh"] == 1.2
    assert [(d["hour"], d["kwh"]) for d in tick["price_details"]] == [
        ("2026-11-17T10:00+00:00", 1.2)
    ]
    assert tick["resume_state"] == _resume_state("11:00", 2.0)

    await rig.goto("11:10:00")
    await rig.set(energy=1.5)
    await rig.set(plug="off")  # cable lock still Locked: transient, grace starts
    assert rig.snapshot()["energy_kwh"] == 1.5

    await rig.set(energy=1.6)
    live = rig.engine.active_session
    built = rig.engine.get_active_session_dict()
    assert built["price_details"] == live.price_details
    assert built["price_details"] is not live.price_details
    assert all(a is not b for a, b in zip(built["price_details"], live.price_details, strict=True))

    await hass.async_stop()
    stop = rig.snapshot()
    assert stop["energy_kwh"] == 1.6
    assert stop["resume_state"] == _resume_state("11:00", 2.0)


async def test_rt28_write_for_a_session_no_longer_held_does_nothing(
    hass, hass_storage, freezer
) -> None:
    """RT-28: a write requested for a discarded or completed session id is skipped."""
    rig = Rig(hass, freezer, hass_storage, persistence_interval_s=3600)
    await rig.setup()
    await rig.goto("10:06:00")

    # Micro session: in and out within the 60 s minimum; the discard clears the snapshot.
    await rig.set(trx=TRX_PAUL)
    await rig.set(plug="on", lock="Locked")
    micro_id = rig.engine.active_session.id
    await rig.unplug()
    assert rig.engine.active_session is None and rig.snapshot() is None
    await rig.engine.async_write_snapshot(micro_id)
    assert rig.snapshot() is None, "a discarded session must not be resurrected"

    # Session A completes, session B runs; a late write for A must not touch the disk.
    await rig.goto("10:10:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:20:00")
    await rig.set(energy=1.0)
    first_id = rig.engine.active_session.id
    await rig.unplug()
    assert [r["id"] for r in rig.records()] == [first_id]
    await rig.goto("10:30:00")
    await rig.plug_in(TRX_PAUL)
    await rig.goto("10:40:00")
    await rig.set(energy=1.4)  # B has 0.4 kWh in memory, 0.0 on disk
    assert rig.snapshot()["energy_kwh"] == 0.0

    await rig.engine.async_write_snapshot(first_id)

    assert rig.snapshot()["energy_kwh"] == 0.0
    assert [r["id"] for r in rig.records()] == [first_id]
